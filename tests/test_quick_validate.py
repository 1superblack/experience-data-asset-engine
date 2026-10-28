"""正式 Skill 校验入口（``scripts/quick_validate.py``）的测试。

校验器本身是发布前的最后一道门：它能跑、且真的会失败，比「CI 里有个 warning」重要得多。
所以这里既测规则（每条都能挡住一种坏元数据），也测「CI 真的调它、缺了就红」。
"""

from __future__ import annotations

import os
import re
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


# --- 规则：displayName 首尾空格 ---------------------------------------------
#
# 这条规则曾经是死代码（先 strip 再比较 `!= strip()`），所以必须用「带引号 +
# 内部空格」这种 strip 之后也看得见的输入来锁，否则测试本身也会跟着假绿。


def test_display_name_with_padding_is_rejected(tmp_path: Path) -> None:
    text = replace_line(GOOD, "  displayName:", '  displayName: "  padded  "')
    directory = make_skill(tmp_path / "s", text)
    problems, summary = quick_validate.validate(directory)
    padding = [item for item in problems if "displayName" in item and "whitespace" in item]
    assert padding, problems
    # 摘要里给的是纯值，长度/展示都按去掉空格后的内容算
    assert summary["displayName"] == "padded"


def test_display_name_padding_does_not_break_length_rule(tmp_path: Path) -> None:
    """首尾空格单独报错即可，不能被当成超长（长度按纯值算）。"""
    text = replace_line(GOOD, "  displayName:", '  displayName: "  数据资产引擎  "')
    directory = make_skill(tmp_path / "s", text)
    problems, _ = quick_validate.validate(directory)
    assert any("whitespace" in item for item in problems), problems
    assert not any("longer than" in item for item in problems), problems


# --- YAML 子集：不支持的写法必须炸，而不是静默解析成错值 ---------------------


def frontmatter_text(frontmatter: str) -> str:
    return f"---\n{frontmatter}---\n\n# 正文\n"


BASELINE_FRONTMATTER = """name: experience-data-asset-engine
description: 把对话蒸馏成经验数据资产笔记，不写原文、不删笔记。
license: MIT
metadata:
  slug: experience-data-asset-engine
  displayName: 数据资产引擎｜经验资产化
  version: 0.1.0
"""


@pytest.mark.parametrize(
    ("case", "frontmatter", "field"),
    [
        (
            "indented-sequence-item-with-colon",
            BASELINE_FRONTMATTER.replace(
                "  displayName: 数据资产引擎｜经验资产化\n",
                "  - displayName: 数据资产引擎｜经验资产化\n",
            ),
            "displayName",
        ),
        (
            "top-level-sequence-item",
            BASELINE_FRONTMATTER.replace("license: MIT\n", "license: MIT\n- stray: item\n"),
            "stray",
        ),
        (
            "inline-sequence-value",
            BASELINE_FRONTMATTER.replace("license: MIT\n", "tags: - a\nlicense: MIT\n"),
            "tags",
        ),
    ],
)
def test_sequences_are_rejected(tmp_path: Path, case: str, frontmatter: str, field: str) -> None:
    directory = make_skill(tmp_path / case, frontmatter_text(frontmatter))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    message = str(excinfo.value)
    assert "line " in message, f"{case}: 错误信息必须带行号: {message}"
    assert "sequence" in message, f"{case}: 错误信息应说明序列不支持: {message}"


def test_duplicate_top_level_key_is_rejected(tmp_path: Path) -> None:
    frontmatter = BASELINE_FRONTMATTER + "name: other-skill\n"
    directory = make_skill(tmp_path / "s", frontmatter_text(frontmatter))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    message = str(excinfo.value)
    assert "duplicate key" in message and "name" in message, message
    assert "line 9" in message, f"重复键应指向真实行号: {message}"


def test_duplicate_nested_key_is_rejected(tmp_path: Path) -> None:
    frontmatter = BASELINE_FRONTMATTER + "  version: 0.1.0\n"
    directory = make_skill(tmp_path / "s", frontmatter_text(frontmatter))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    message = str(excinfo.value)
    assert "metadata.version" in message, message
    assert "line 9" in message, f"重复键应指向真实行号: {message}"


@pytest.mark.parametrize("indent", [4, 1, 3])
def test_inconsistent_indent_is_rejected(tmp_path: Path, indent: int) -> None:
    """本子集只认 2 空格缩进；4 空格（=2 层跳变）、1/3 空格都要报错。"""
    nested = BASELINE_FRONTMATTER.replace("  slug:", " " * indent + "slug:")
    directory = make_skill(tmp_path / f"s{indent}", frontmatter_text(nested))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    message = str(excinfo.value)
    assert "line " in message and "slug" in message, message
    assert "2 spaces per level" in message, message


def test_mixed_indent_in_same_level_is_rejected(tmp_path: Path) -> None:
    frontmatter = BASELINE_FRONTMATTER.replace("  version: 0.1.0\n", "    version: 0.1.0\n")
    directory = make_skill(tmp_path / "s", frontmatter_text(frontmatter))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    assert "2 spaces per level" in str(excinfo.value), str(excinfo.value)


def test_tab_indent_is_rejected(tmp_path: Path) -> None:
    frontmatter = BASELINE_FRONTMATTER.replace("  slug:", "\tslug:")
    directory = make_skill(tmp_path / "s", frontmatter_text(frontmatter))
    with pytest.raises(quick_validate.FrontmatterError) as excinfo:
        quick_validate.validate(directory)
    assert "tab indentation" in str(excinfo.value), str(excinfo.value)


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


# --- CLI 契约：输出必须纯 ASCII ---------------------------------------------
#
# docstring 承诺「输出恒为 ASCII」，那就必须对动态值（路径、displayName、
# 报错里回显的原文）真的转义，而不是靠运气——代码页一换就乱码的契约等于没有。


def test_cli_output_is_pure_ascii_on_repository() -> None:
    """仓库根路径本身含中文，正好用来验路径转义。"""
    proc = run_cli(ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.isascii(), proc.stdout
    assert proc.stderr == ""
    assert "\\u" in proc.stdout, "动态值应转义成 \\uXXXX，而不是被丢掉"


def test_cli_escapes_non_ascii_display_name(tmp_path: Path) -> None:
    directory = make_skill(tmp_path / "s")
    proc = run_cli(directory)
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.isascii(), proc.stdout
    line = next(item for item in proc.stdout.splitlines() if item.startswith("OK   displayName="))
    assert line == "OK   displayName=\\u6570\\u636e\\u8d44\\u4ea7\\u5f15\\u64ce\\uff5c\\u7ecf\\u9a8c\\u8d44\\u4ea7\\u5316", line


def test_cli_help_output_is_ascii() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_env(),
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.isascii(), proc.stdout
    for code in ("0", "1", "2"):
        assert code in proc.stdout


# --- CLI 契约：读不出来 = 2，内容非法 = 1，且都不许冒 traceback -------------


def test_exit_code_constants_are_public_contract() -> None:
    assert quick_validate.EXIT_OK == 0
    assert quick_validate.EXIT_INVALID == 1
    assert quick_validate.EXIT_UNREADABLE == 2


def test_docstring_states_exit_codes_and_ascii_policy() -> None:
    doc = quick_validate.__doc__ or ""
    assert "退出码契约" in doc, "docstring 必须写明退出码契约"
    for code in ("``0``", "``1``", "``2``"):
        assert code in doc, f"docstring 缺少退出码 {code}"
    assert "\\uXXXX" in doc, "docstring 必须写明动态值的 ASCII 转义形式"


def test_cli_exit_two_when_skill_md_is_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").mkdir()
    proc = run_cli(tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr


def test_cli_exit_two_when_skill_md_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_bytes(b"---\nname: x\n---\n\xff\xfe not utf8\n")
    proc = run_cli(tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert proc.stdout.startswith("ERROR: cannot read SKILL.md"), proc.stdout
    assert proc.stdout.isascii(), proc.stdout


def test_cli_exit_two_on_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = make_skill(tmp_path / "s")

    def deny(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", deny)
    assert quick_validate.main([str(directory)]) == 2


def test_cli_exit_one_on_broken_frontmatter(tmp_path: Path) -> None:
    """读得出来但结构非法 → 1，且不许冒 traceback。"""
    directory = make_skill(tmp_path / "s", "---\nname: x\n")
    proc = run_cli(directory)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL frontmatter:" in proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr


def test_cli_exit_one_on_yaml_subset_violation(tmp_path: Path) -> None:
    frontmatter = BASELINE_FRONTMATTER.replace("  slug:", "    slug:")
    directory = make_skill(tmp_path / "s", frontmatter_text(frontmatter))
    proc = run_cli(directory)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL frontmatter:" in proc.stdout
    assert proc.stdout.isascii(), proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr


# --- CI 必须真的调它，且不许 warning 后跳过 ---------------------------------


def test_ci_calls_validator_and_does_not_skip() -> None:
    text = CI_YAML.read_text(encoding="utf-8")
    assert re.search(r"python\s+scripts/quick_validate\.py\b", text), (
        "CI 必须直接调用正式校验入口 scripts/quick_validate.py"
    )
    assert "::warning::" not in text, "校验入口缺失时必须让 job 失败，不能 warning 后跳过"
    assert "scripts/validate_skill.py" not in text, "不要保留多入口探测分支"
