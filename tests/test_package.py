"""打包产物的契约测试：SkillHub 拿到的 zip 必须是「根目录直挂 SKILL.md + 可复现 + 不含隐私」。

这里断言的是**产物**，不是脚本内部实现：所以统一走 ``sys.executable scripts/build_skill_zip.py``
子进程，和用户/CI 实际执行的是同一条路径。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import shutil
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
    "COMMERCIAL-LICENSE.md",
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
        "COMMERCIAL-LICENSE.md",
        "README.md",
        "CHANGELOG.md",
        "SECURITY.md",
    }
)


def utf8_env() -> dict[str, str]:
    """子进程固定 UTF-8。

    Windows 默认代码页（中文机器上是 GBK）会把中文 stderr 写成非 UTF-8 字节，
    这里再按 UTF-8 读就是乱码。这是宿主编码问题，不是断言问题，
    所以修在 env 上——不把断言降级成 ASCII 来掩盖。
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_build(
    output: Path, cwd: Path = ROOT, repo: Path | None = None
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--output", str(output)]
    if repo is not None:
        argv += ["--repo", str(repo)]
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_env(),
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


def _make_line_ending_repo(base: Path, newline: bytes) -> Path:
    """造一个「内容相同、只有行尾不同」的最小仓库（用 write_bytes 精确控制字节）。"""
    for name in ("src/pkg", "scripts", "references", "agents"):
        (base / name).mkdir(parents=True)
    body = newline.join([b"# demo", b"", b"alpha", b"beta", b""])
    for name in (
        "SKILL.md",
        "LICENSE",
        "COMMERCIAL-LICENSE.md",
        "README.md",
        "references/doc.md",
    ):
        (base / name).write_bytes(body)
    (base / "src" / "pkg" / "__init__.py").write_bytes(body)
    (base / "scripts" / "cli.py").write_bytes(body)
    (base / "agents" / "openai.yaml").write_bytes(body)
    # 二进制里的 CRLF 是数据，不是换行——绝不能被归一
    (base / "agents" / "blob.bin").write_bytes(b"BIN\r\nDATA\x00\xff")
    return base


def test_line_endings_do_not_change_archive_hash(tmp_path: Path) -> None:
    """同一个 commit、同一台机器，只要检出被改写过行尾（CRLF/LF），不能变成两个包。

    这是真实踩过的坑：主工作区是 CRLF 检出，worktree 是 LF，内容一字未改，
    打出来的 zip 却是两个 SHA256——那是「检出差异」冒充「内容差异」。
    """
    lf_repo = _make_line_ending_repo(tmp_path / "lf", b"\n")
    crlf_repo = _make_line_ending_repo(tmp_path / "crlf", b"\r\n")
    lf_zip = tmp_path / "lf.zip"
    crlf_zip = tmp_path / "crlf.zip"

    for repo, out in ((lf_repo, lf_zip), (crlf_repo, crlf_zip)):
        proc = run_build(out, cwd=tmp_path, repo=repo)
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    assert crlf_repo.joinpath("README.md").read_bytes().count(b"\r\n") > 0, "夹具没造出 CRLF"
    digest_lf = hashlib.sha256(lf_zip.read_bytes()).hexdigest()
    digest_crlf = hashlib.sha256(crlf_zip.read_bytes()).hexdigest()
    assert digest_lf == digest_crlf, "行尾差异泄漏到产物：文本条目入包前必须归一到 LF"

    with zipfile.ZipFile(crlf_zip) as archive:
        assert archive.read("README.md").count(b"\r\n") == 0
        assert archive.read("agents/blob.bin") == b"BIN\r\nDATA\x00\xff", "二进制被误归一"


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
    (base / "COMMERCIAL-LICENSE.md").write_text("# Commercial licensing\n", encoding="utf-8")
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
    proc = run_build(out, cwd=tmp_path, repo=repo)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    names = set(_names(out))
    assert names == {
        "SKILL.md",
        "LICENSE",
        "COMMERCIAL-LICENSE.md",
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
    proc = run_build(out, cwd=tmp_path, repo=repo)
    assert proc.returncode != 0
    assert "凭据" in proc.stderr


#: 子进程必须看到的两个环境变量。缺任一个，Windows 中文机器上就会按 GBK 写 stderr。
UTF8_ENV_REQUIRED = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: 断言护栏要扫的文件：凡是测试里真的起子进程的都算上。
GUARDED_TEST_FILES = (
    "test_package.py",
    "test_quick_validate.py",
    "test_skill_metadata.py",
    "test_cli_encoding.py",
)

#: 允许「故意清空 UTF-8 变量」的 env 构造器。
#: 只有 Windows 编码回归测试需要这种反向场景：它要验证的正是「用户没设变量时」
#: CLI 的输出是否仍然可读。名字写死在这里，避免有人随手起个函数名就绕过护栏；
#: 下面的 test_utf8_guard_exemption_is_real 还会检查该函数真的删了两个变量。
CLEAN_ENV_HELPER = "_env_without_utf8"


def _env_keyword_problem(node: ast.Call) -> list[str]:
    """检查单个 subprocess 调用的 ``env=`` 是否**真的**固定了 UTF-8。

    只看「有没有 env 关键字」是不够的：``env={}`` 也会让子进程丢掉这两个变量，
    等于护栏形同虚设。所以要么是对 ``utf8_env()`` 的调用，要么是含两个键值的字面量 dict。
    """
    env_keywords = [kw for kw in node.keywords if kw.arg == "env"]
    if not env_keywords:
        return ["没有传 env="]
    value = env_keywords[0].value
    if isinstance(value, ast.Call):
        name = value.func.attr if isinstance(value.func, ast.Attribute) else getattr(
            value.func, "id", ""
        )
        if name in {"utf8_env", CLEAN_ENV_HELPER}:
            return []
        return [f"env= 调用的不是 utf8_env()，而是 {ast.unparse(value)}"]
    if isinstance(value, ast.Dict):
        pairs: dict[str, str] = {}
        for key, item in zip(value.keys, value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            pairs[key.value] = (
                item.value if isinstance(item, ast.Constant) else ast.unparse(item)
            )
        missing = [name for name in UTF8_ENV_REQUIRED if name not in pairs]
        if missing:
            return [f"env 字面量缺少 {missing}"]
        wrong = [
            f"{name}={pairs[name]}"
            for name, want in UTF8_ENV_REQUIRED.items()
            if pairs[name] != want
        ]
        if wrong:
            return [f"env 字面量取值不对：{wrong}"]
        return []
    return [f"env= 既不是 utf8_env() 调用也不是字面量 dict，而是 {ast.unparse(value)}"]


def _subprocess_calls(tree: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        if func.attr in {"run", "check_output", "check_call", "Popen"}:
            calls.append(node)
    return calls


def test_every_subprocess_call_pins_utf8() -> None:
    """所有测试里的 subprocess 调用都必须**真的**把子进程固定成 UTF-8。

    两重收紧（原有断言没删，只是在它上面加严）：

    1. 扫描范围从本文件扩到 ``test_quick_validate.py``——它也真的 ``subprocess.run``
       起打包脚本，漏掉它等于护栏只有一半。
    2. ``env=`` 存在还不够：``env={}`` 同样能过旧检查，但子进程会在中文 Windows 上
       按 GBK 写 stderr。必须是对 ``utf8_env()`` 的调用，或是含两个键值的字面量 dict。

    失败时给出文件名与行号，便于直接定位。
    """
    offenders: list[str] = []
    for name in GUARDED_TEST_FILES:
        path = Path(__file__).resolve().parent / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _subprocess_calls(tree):
            for reason in _env_keyword_problem(node):
                offenders.append(f"{name}:{node.lineno}: {reason}")
    assert not offenders, "这些子进程调用没有把子进程固定成 UTF-8：\n" + "\n".join(offenders)


def test_shipped_docs_have_no_placeholder_contact(packaged: tuple[Path, str]) -> None:
    """包里发出去的安全文档不能带占位联系方式。

    ``security@example.com`` 这类占位地址一旦随包发布，等于告诉用户「报漏洞发到黑洞」。
    """
    archive_path, _ = packaged
    # 只拦「联系方式类」占位：配置示例里的 api.example.com 是合法示例域名，不该误伤。
    placeholders = ("security@example.com", "your-email@", "<email>", "TO" + "DO:", "FIXME:")
    offenders: list[str] = []
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if not name.endswith((".md", ".yaml", ".yml")):
                continue
            text = zf.read(name).decode("utf-8", errors="replace")
            for token in placeholders:
                if token in text:
                    offenders.append(f"{name}: {token}")
    assert not offenders, f"包内文档仍有占位内容: {offenders}"


# --- 失败关闭：不产出、不覆盖 -------------------------------------------------
#
# 这里断言的都是「磁盘上到底剩了什么」。自检失败的包一个字节都不该落到最终路径：
# 一个带 ``sk-`` 凭据的 zip 留在 dist/ 里，下一步就会被人工上传出去。


def _plant_credential(repo: Path) -> None:
    (repo / "references" / "leak.md").write_text(
        "token: sk-abcdefghijklmnop1234\n", encoding="utf-8"
    )


def _listing(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def test_failed_self_check_leaves_no_output_file(tmp_path: Path) -> None:
    """自检失败时，最终 output 路径必须根本不存在（连空文件都不许有）。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    _plant_credential(repo)
    out_dir = tmp_path / "artifacts"
    out = out_dir / "leak.zip"

    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert not out.exists(), "自检失败却把带凭据的 zip 落到了最终路径"
    assert "校验失败" in proc.stderr, proc.stderr
    assert _listing(out_dir) == [], f"临时文件没清干净：{_listing(out_dir)}"


def test_failed_self_check_keeps_existing_output_bytes(tmp_path: Path) -> None:
    """已经有一份合法的旧 zip 时，失败的重建**不能**把它改掉。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    out = tmp_path / "skill.zip"
    first = run_build(out, cwd=tmp_path, repo=repo)
    assert first.returncode == 0, first.stderr
    before = hashlib.sha256(out.read_bytes()).hexdigest()

    _plant_credential(repo)
    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert hashlib.sha256(out.read_bytes()).hexdigest() == before, "失败的重建覆盖了既有合法产物"
    assert "校验失败" in proc.stderr, proc.stderr
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "临时文件没清干净"


def test_successful_build_still_reproducible_after_atomic_rewrite(tmp_path: Path) -> None:
    """原子化改造不能动产物字节：同源两次打包仍必须同哈希。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    first = tmp_path / "a.zip"
    second = tmp_path / "b.zip"
    assert run_build(first, cwd=tmp_path, repo=repo).returncode == 0
    assert run_build(second, cwd=tmp_path, repo=repo).returncode == 0
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


# --- 符号链接：命中即失败关闭 -------------------------------------------------


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool) -> None:
    """建符号链接。

    Windows 上建链接需要开发者模式或管理员权限，普通用户会拿到 WinError 1314；
    那种情况下 ``pytest.skip``（这是环境限制，不是被测代码的行为）。
    **只**吞权限类异常：其它 OSError 照常抛，避免把真实缺陷藏进 skip 里。
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError as exc:  # pragma: no cover - 平台不支持 symlink
        pytest.skip(f"当前平台不支持符号链接：{exc}")
    except PermissionError as exc:  # pragma: no cover - Windows 未开开发者模式
        pytest.skip(f"当前环境不允许创建符号链接：{exc}")
    except OSError as exc:  # pragma: no cover - Windows 的 ERROR_PRIVILEGE_NOT_HELD
        winerror = getattr(exc, "winerror", None)
        if winerror in {5, 1314}:
            pytest.skip(f"当前环境不允许创建符号链接：{exc}")
        raise


def test_symlinked_file_pointing_outside_repo_is_rejected(tmp_path: Path) -> None:
    """指向仓库外的符号链接文件绝不能被打进包（真实踩过的 ``linked-secret.txt``）。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    outside = tmp_path / "outside-secret.md"
    outside.write_text("# 仓库外机密\n", encoding="utf-8")
    _symlink_or_skip(
        repo / "references" / "linked-secret.md", outside, target_is_directory=False
    )

    out = tmp_path / "linked.zip"
    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert "linked-secret.md" in proc.stderr, proc.stderr
    assert not out.exists()


def test_symlinked_root_dir_is_rejected(tmp_path: Path) -> None:
    """``references`` 整个目录是指向外部的链接时，必须直接失败。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    outside = tmp_path / "outside-references"
    outside.mkdir()
    (outside / "doc.md").write_text("# 外部文档\n", encoding="utf-8")
    shutil.rmtree(repo / "references")
    _symlink_or_skip(repo / "references", outside, target_is_directory=True)

    out = tmp_path / "rootdir.zip"
    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert "references" in proc.stderr, proc.stderr
    assert not out.exists()


def test_symlinked_subdir_file_is_rejected(tmp_path: Path) -> None:
    """深层子目录里的链接同样要拦——``src/pkg/evil.py -> 仓库外``。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    outside = tmp_path / "outside-module.py"
    outside.write_text("SECRET = 'external'\n", encoding="utf-8")
    _symlink_or_skip(repo / "src" / "pkg" / "evil.py", outside, target_is_directory=False)

    out = tmp_path / "subdir.zip"
    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert "evil.py" in proc.stderr, proc.stderr
    assert not out.exists()


def test_symlink_disguised_as_markdown_is_rejected(tmp_path: Path) -> None:
    """名字伪装成普通 ``.md`` 不构成豁免：判断依据是文件系统类型，不是后缀。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    outside = tmp_path / "outside-notes.md"
    outside.write_text("# 外部笔记\n", encoding="utf-8")
    _symlink_or_skip(repo / "agents" / "notes.md", outside, target_is_directory=False)

    out = tmp_path / "disguised.zip"
    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode != 0
    assert "notes.md" in proc.stderr, proc.stderr
    assert not out.exists()


# --- 自检不可绕过 + 预期错误不打 traceback（Gate 5c） ------------------------


def test_build_has_no_verify_bypass() -> None:
    """``build()`` 不允许有跳过自检的参数。

    一个 ``verify=False`` 开关，迟早会被某个调用方用来「先跑通再说」，
    而它跳过的正是发布前唯一还会拦一次的隐私/凭据检查。
    """
    import inspect

    module = _load_build_script_module()
    parameters = inspect.signature(module.build).parameters
    assert "verify" not in parameters, f"build() 仍可关闭自检：{sorted(parameters)}"


def test_output_under_a_file_fails_cleanly(tmp_path: Path) -> None:
    """父路径是文件时：退出码 1、无 traceback、不留最终产物与临时文件。"""
    repo = _make_synthetic_repo(tmp_path / "repo")
    blocker = tmp_path / "blocker.md"
    blocker.write_text("not a directory\n", encoding="utf-8")
    out = blocker / "out.zip"

    proc = run_build(out, cwd=tmp_path, repo=repo)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, combined
    assert not out.exists()
    residue = sorted(
        item.name
        for item in tmp_path.iterdir()
        if item.suffix == ".zip" or item.name.endswith(".tmp")
    )
    assert residue == [], f"失败后留下产物/临时文件：{residue}"
    assert blocker.read_text(encoding="utf-8") == "not a directory\n"


def test_utf8_guard_exemption_is_real() -> None:
    """``_env_without_utf8`` 这个豁免必须名副其实：真的删掉两个变量。

    否则「允许清空」会变成「允许随便传 env」，护栏就白做了。
    """
    source = (Path(__file__).resolve().parent / "test_cli_encoding.py").read_text(encoding="utf-8")
    assert 'env.pop("PYTHONUTF8", None)' in source
    assert 'env.pop("PYTHONIOENCODING", None)' in source
