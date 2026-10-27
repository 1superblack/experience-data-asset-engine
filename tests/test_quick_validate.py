"""正式 Skill 校验入口（``scripts/quick_validate.py``）的测试。

校验器本身是发布前的最后一道门：它能跑、且真的会失败，比「CI 里有个 warning」重要得多。
所以这里既测规则（每条都能挡住一种坏元数据），也测「CI 真的调它、缺了就红」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quick_validate.py"
BUILD = ROOT / "scripts" / "build_skill_zip.py"
CI_YAML = ROOT / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(SCRIPT.parent))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("quick_validate", SCRIPT)
assert _spec is not None and _spec.loader is not None
quick_validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(quick_validate)


def utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


GOOD = """---
name: experience-data-asset-engine
description: 把对话蒸馏成经验数据资产笔记，不写原文、不删笔记。
license: MIT
metadata:
  slug: experience-data-asset-engine
  displayName: 数据资产引擎｜经验资产化
  version: 0.1.0
---

# 正文
"""


def make_skill(directory: Path, text: str = GOOD) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def replace_line(text: str, prefix: str, replacement: str) -> str:
    """把以 ``prefix`` 开头的那一行整行替换掉（保持其余内容不动）。"""
    lines = [replacement if line.startswith(prefix) else line for line in text.splitlines()]
    return "\n".join(lines) + "\n"


def run_cli(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_env(),
        check=False,
    )


# --- 正例：真的仓库、真的包 -------------------------------------------------


def test_repository_passes() -> None:
    problems, summary = quick_validate.validate(ROOT)
    assert problems == []
    assert summary["name"] == "experience-data-asset-engine"
    assert summary["version"] == "0.1.0"


def test_extracted_package_passes(tmp_path: Path) -> None:
    """解压后的分发包也要能过——打包不能丢元数据。"""
    archive = tmp_path / "skill.zip"
    build = subprocess.run(
        [sys.executable, str(BUILD), "--output", str(archive)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_env(),
        check=False,
    )
    assert build.returncode == 0, build.stderr

    unzipped = tmp_path / "unzipped"
    unzipped.mkdir()
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(unzipped)

    problems, summary = quick_validate.validate(unzipped)
    assert problems == [], problems
    assert summary["slug"] == "experience-data-asset-engine"


# --- 规则：每条都要真的报错 -------------------------------------------------


def test_missing_skill_md_is_a_hard_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        quick_validate.validate(tmp_path)


@pytest.mark.parametrize("bad_name", ["Bad_Skill", "BadSkill", "bad_skill", "bad--skill", "-bad", "bad-"])
def test_name_must_be_hyphen_case(tmp_path: Path, bad_name: str) -> None:
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "name:", f"name: {bad_name}"))
    problems, _ = quick_validate.validate(directory)
    assert any(item.startswith("name:") for item in problems), problems


def test_description_must_not_be_empty(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "description:", 'description: ""'))
    problems, _ = quick_validate.validate(directory)
    assert any(item.startswith("description:") for item in problems), problems


def test_description_length_is_capped(tmp_path: Path) -> None:
    long_text = "说" * 2000
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "description:", f"description: {long_text}"))
    problems, _ = quick_validate.validate(directory)
    assert any("platform limit" in item for item in problems), problems


def test_version_must_be_semver(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "  version:", "  version: 1.0"))
    problems, _ = quick_validate.validate(directory)
    assert any(item.startswith("metadata.version:") for item in problems), problems


def test_slug_must_match_name(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "  slug:", "  slug: other-skill"))
    problems, _ = quick_validate.validate(directory)
    assert any("does not match name" in item for item in problems), problems


def test_missing_metadata_block_is_reported(tmp_path: Path) -> None:
    text = """---
name: experience-data-asset-engine
description: 把对话蒸馏成经验笔记
license: MIT
---
"""
    directory = make_skill(tmp_path / "s", text)
    problems, _ = quick_validate.validate(directory)
    assert "metadata: required nested block is missing" in problems


def test_unclosed_frontmatter_is_reported(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", "---\nname: x\n")
    with pytest.raises(quick_validate.FrontmatterError):
        quick_validate.validate(directory)


def test_multiline_scalar_is_rejected_loudly(tmp_path: Path) -> None:
    """不支持就明确报错，不能静默解析成错值。"""
    text = GOOD.replace(
        "description: 把对话蒸馏成经验数据资产笔记，不写原文、不删笔记。",
        "description: |\n  第一行\n  第二行",
    )
    directory = make_skill(tmp_path / "s", text)
    with pytest.raises(quick_validate.FrontmatterError):
        quick_validate.validate(directory)


def test_bom_is_tolerated(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", "\ufeff" + GOOD)
    problems, _ = quick_validate.validate(directory)
    assert problems == []


# --- CLI 契约：退出码 -------------------------------------------------------


def test_cli_exit_zero_on_good(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s")
    proc = run_cli(directory)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_cli_exit_one_on_bad(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s", replace_line(GOOD, "name:", "name: Bad_Name"))
    proc = run_cli(directory)
    assert proc.returncode == 1
    assert "FAIL name:" in proc.stdout


def test_cli_exit_two_when_skill_md_missing(tmp_path: Path) -> None:
    proc = run_cli(tmp_path)
    assert proc.returncode == 2
    assert "SKILL.md not found" in proc.stdout


# --- CI 必须真的调它，且不许 warning 后跳过 ---------------------------------


def test_ci_calls_validator_and_does_not_skip() -> None:
    text = CI_YAML.read_text(encoding="utf-8")
    assert "scripts/quick_validate.py" in text, "CI 必须调用正式校验入口"
    assert "::warning::" not in text, "校验入口缺失时必须让 job 失败，不能 warning 后跳过"
    assert "scripts/validate_skill.py" not in text, "不要保留多入口探测分支"
