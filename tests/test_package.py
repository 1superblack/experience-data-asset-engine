"""打包产物的契约测试：SkillHub 拿到的 zip 必须是「根目录直挂 SKILL.md + 可复现 + 不含隐私」。

这里断言的是**产物**，不是脚本内部实现：所以统一走 ``sys.executable scripts/build_skill_zip.py``
子进程，和用户/CI 实际执行的是同一条路径。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_skill_zip.py"

# 个人绝对路径与凭据的「形状」断言。账号名是本机真实值，故意只写在测试里，
# 不进打包脚本，否则脚本源码自己就会命中这条规则。
PERSONAL_PATH_PATTERNS = (
    re.compile(rb"[A-Za-z]:\\+Users"),
    re.compile(rb"[A-Za-z]:\\+obsidian", re.IGNORECASE),
    re.compile(rb"84946"),
)
CREDENTIAL_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{16,}"),
    re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY"),
)

EXPECTED_TOP_LEVEL = {
    "SKILL.md",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "agents",
    "references",
    "scripts",
    "src",
}

#: 包内根目录必须一字不差带上的文件。
#: 这里是**显式**清单（产物契约不跟着脚本实现走），另有
#: ``test_root_file_manifest_matches_build_script`` 把它和打包白名单钉在一起，
#: 任何一边增删文件而另一边没跟，测试就会失败——避免清单悄悄漂移。
REQUIRED_ROOT_FILES = frozenset(
    {
        "SKILL.md",
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        "SECURITY.md",
    }
)


def run_build(output: Path, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture(scope="module")
def packaged(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """打一次包，返回 (zip 路径, 构建输出)。"""
    out = tmp_path_factory.mktemp("pkg") / "skill.zip"
    proc = run_build(out)
    assert proc.returncode == 0, f"打包失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return out, proc.stdout


def _names(archive_path: Path) -> list[str]:
    with zipfile.ZipFile(archive_path) as archive:
        return archive.namelist()


def test_skill_md_is_at_zip_root(packaged: tuple[Path, str]) -> None:
    archive_path, _ = packaged
    assert "SKILL.md" in _names(archive_path)


def test_no_wrapping_subdirectory(packaged: tuple[Path, str]) -> None:
    """不能出现 ``<某个目录>/SKILL.md`` 这种套一层的结构。"""
    archive_path, _ = packaged
    names = _names(archive_path)
    nested = [name for name in names if name.endswith("/SKILL.md")]
    assert nested == []
    tops = {name.split("/", 1)[0] for name in names}
    assert tops <= EXPECTED_TOP_LEVEL, f"出现了计划外的顶层条目：{sorted(tops - EXPECTED_TOP_LEVEL)}"


def _load_build_script_module():
    """导入打包脚本本身，用于比对白名单（不影响上面「只断言产物」的原则）。"""
    spec = importlib.util.spec_from_file_location("build_skill_zip", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_file_manifest_matches_build_script() -> None:
    """测试里的根目录清单必须与打包脚本的 ``ROOT_FILES`` 完全一致（双向）。"""
    root_files = set(_load_build_script_module().ROOT_FILES)
    assert root_files == set(REQUIRED_ROOT_FILES), (
        "根目录清单漂移："
        f"脚本独有={sorted(root_files - REQUIRED_ROOT_FILES)}，"
        f"测试独有={sorted(set(REQUIRED_ROOT_FILES) - root_files)}"
    )


def test_required_members_present(packaged: tuple[Path, str]) -> None:
    archive_path, _ = packaged
    names = set(_names(archive_path))
    missing = REQUIRED_ROOT_FILES - names
    assert not missing, f"包内根目录缺少必需文件：{sorted(missing)}"
    assert "agents/openai.yaml" in names
    for prefix in ("scripts/", "src/", "references/"):
        assert any(name.startswith(prefix) for name in names), f"缺少 {prefix}"
    # README 里写到的参考文档必须真的在包里，否则用户点开就是 404。
    assert any(name.startswith("references/") and name.endswith(".md") for name in names)


def test_entries_sorted_and_count_reported(packaged: tuple[Path, str]) -> None:
    archive_path, stdout = packaged
    names = _names(archive_path)
    assert names == sorted(names)
    match = re.search(r"files: (\d+)", stdout)
    assert match is not None, stdout
    assert int(match.group(1)) == len(names)


def test_excludes_artifacts_and_tests(packaged: tuple[Path, str]) -> None:
    """仓库里此时一定存在 .pytest_cache / __pycache__，正好用来验证真的被排掉了。"""
    archive_path, _ = packaged
    names = _names(archive_path)
    forbidden_prefixes = (
        ".git/",
        ".github/",
        "tests/",
        "__pycache__/",
        ".pytest_cache/",
        ".experience-data-asset-engine/",
    )
    for name in names:
        assert not name.startswith(forbidden_prefixes), f"不该入包：{name}"
        assert not name.endswith(".pyc"), f"不该入包：{name}"
        assert "__pycache__/" not in name, f"不该入包：{name}"
        assert ".coverage" not in name, f"不该入包：{name}"
        assert not name.endswith(".pending.md"), f"不该入包：{name}"
        assert not name.endswith(".tmp"), f"不该入包：{name}"
        assert ".egg-info/" not in name, f"不该入包：{name}"
        assert ".dist-info/" not in name, f"不该入包：{name}"


def test_payload_has_no_personal_paths_or_credentials(packaged: tuple[Path, str]) -> None:
    archive_path, _ = packaged
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            for pattern in PERSONAL_PATH_PATTERNS:
                assert not pattern.search(data), f"{name} 含个人路径片段：{pattern.pattern!r}"
            for pattern in CREDENTIAL_PATTERNS:
                assert not pattern.search(data), f"{name} 含疑似凭据：{pattern.pattern!r}"


def test_build_is_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "a.zip"
    second = tmp_path / "b.zip"
    assert run_build(first).returncode == 0
    assert run_build(second).returncode == 0
    digest_first = hashlib.sha256(first.read_bytes()).hexdigest()
    digest_second = hashlib.sha256(second.read_bytes()).hexdigest()
    assert digest_first == digest_second, "两次打包哈希不同，说明 zip 里混进了时间戳或系统信息"


def test_cli_prints_path_sha256_and_size(packaged: tuple[Path, str]) -> None:
    archive_path, stdout = packaged
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert f"sha256: {expected}" in stdout
    assert "zip: " in stdout
    assert "files: " in stdout


def _make_synthetic_repo(base: Path) -> Path:
    """造一个带各种污染物的最小仓库，用来单独验证排除规则与自检。"""
    (base / "src" / "pkg").mkdir(parents=True)
    (base / "scripts").mkdir()
    (base / "references").mkdir()
    (base / "agents").mkdir()
    (base / "tests").mkdir()
    (base / ".git").mkdir()
    (base / "__pycache__").mkdir()
    (base / ".pytest_cache").mkdir()

    (base / "SKILL.md").write_text("---\nname: demo\n---\n# demo\n", encoding="utf-8")
    (base / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (base / "README.md").write_text("# demo\n", encoding="utf-8")
    (base / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (base / "scripts" / "cli.py").write_text("print('hi')\n", encoding="utf-8")
    (base / "references" / "doc.md").write_text("# doc\n", encoding="utf-8")
    (base / "agents" / "openai.yaml").write_text("name: demo\n", encoding="utf-8")

    # 污染项：全都不许进包
    (base / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (base / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (base / "__pycache__" / "pkg.cpython-311.pyc").write_bytes(b"\x00\x01")
    (base / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
    (base / "src" / "pkg" / "__pycache__").mkdir()
    (base / "src" / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\x00\x02")
    # pip install -e 的副产物：SOURCES.txt / PKG-INFO 里会写本机绝对路径
    (base / "src" / "pkg.egg-info").mkdir()
    (base / "src" / "pkg.egg-info" / "SOURCES.txt").write_text("SKILL.md\n", encoding="utf-8")
    (base / "src" / "pkg.egg-info" / "PKG-INFO").write_text("Metadata-Version: 2.1\n", encoding="utf-8")
    (base / "config.local.json").write_text("{}\n", encoding="utf-8")
    (base / "draft.pending.md").write_text("# 待确认\n", encoding="utf-8")
    (base / "scripts" / "scratch.tmp").write_text("tmp\n", encoding="utf-8")
    (base / ".coverage").write_text("data\n", encoding="utf-8")
    return base


def test_exclusion_rules_hold_on_synthetic_tree(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path / "repo")
    out = tmp_path / "synth.zip"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(out), "--repo", str(repo)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    names = set(_names(out))
    assert names == {
        "SKILL.md",
        "LICENSE",
        "README.md",
        "agents/openai.yaml",
        "references/doc.md",
        "scripts/cli.py",
        "src/pkg/__init__.py",
    }


def test_planted_credential_fails_closed(tmp_path: Path) -> None:
    """自检必须在发现疑似凭据时失败——这是「宁可打不出包」的那条线。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    (repo / "references" / "leak.md").write_text(
        "token: sk-abcdefghijklmnop1234\n", encoding="utf-8"
    )
    out = tmp_path / "leak.zip"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(out), "--repo", str(repo)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode != 0
    assert "凭据" in proc.stderr
