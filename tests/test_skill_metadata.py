"""Skill 元数据与包装文档的一致性校验。

不引入 PyYAML：frontmatter 只允许 ``key: value`` 与单层 ``metadata:`` 嵌套，
用文本解析足够，且能保证「文档里写了什么」就是「打包时读到什么」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
README_PATH = ROOT / "README.md"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
OPENAI_YAML_PATH = ROOT / "agents" / "openai.yaml"
REFERENCE_PATHS = sorted((ROOT / "references").glob("*.md"))

EXPECTED_SLUG = "experience-data-asset-engine"
EXPECTED_DISPLAY_NAME = "数据资产引擎｜经验资产化"
EXPECTED_VERSION = "0.1.0"

REQUIRED_METADATA_KEYS = (
    "name",
    "description",
    "license",
    "metadata.slug",
    "metadata.displayName",
    "metadata.version",
)


# --- 极简 frontmatter 解析 ---------------------------------------------------


def split_frontmatter(text: str) -> str:
    """返回第一条 ``---`` 与第二条 ``---`` 之间的原文。

    文件必须以 frontmatter 开头，否则直接失败：SKILL.md 没有 frontmatter 就无法被
    Skill 运行时装载。
    """
    lines = text.splitlines()
    assert lines, "文件为空"
    assert lines[0].strip() == "---", f"文件不以 frontmatter 开头，首行是 {lines[0]!r}"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    raise AssertionError("frontmatter 没有闭合的 ---")


def parse_frontmatter(text: str) -> dict[str, str]:
    """把 frontmatter 解成扁平字典，嵌套一层用 ``父键.子键`` 表示。"""
    result: dict[str, str] = {}
    parent: str | None = None
    for raw in split_frontmatter(text).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, sep, value = raw.strip().partition(":")
        assert sep, f"frontmatter 行无法解析: {raw!r}"
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not value:
            # 只有嵌套父键才允许不带值
            parent = key
            continue
        result[f"{parent}.{key}" if raw.startswith((" ", "\t")) and parent else key] = value
    return result


# --- SKILL.md ---------------------------------------------------------------


def test_skill_has_closed_frontmatter() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    body = split_frontmatter(text)
    assert body.strip(), "frontmatter 内容为空"


@pytest.mark.parametrize("key", REQUIRED_METADATA_KEYS)
def test_skill_frontmatter_has_required_key(key: str) -> None:
    data = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    assert key in data, f"SKILL.md frontmatter 缺少 {key}"
    assert data[key].strip(), f"SKILL.md frontmatter 的 {key} 为空"


def test_skill_metadata_values() -> None:
    data = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    assert data["name"] == EXPECTED_SLUG
    assert data["metadata.slug"] == EXPECTED_SLUG
    assert data["metadata.displayName"] == EXPECTED_DISPLAY_NAME
    assert data["metadata.version"] == EXPECTED_VERSION
    assert data["license"].upper() == "MIT"


def test_skill_description_mentions_trigger_and_guardrails() -> None:
    data = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    description = data["description"]
    assert "归档" in description
    assert "不写入对话原文" in description
    assert "不删除" in description


def test_skill_has_exactly_five_top_level_sections() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    sections = re.findall(r"^## (.+)$", body, flags=re.MULTILINE)
    assert sections == ["触发条件", "主流程", "安全边界", "CLI 入口", "Reference 路由"], sections


def test_skill_keeps_capability_boundary() -> None:
    """打包发布没实现这件事必须写在 SKILL.md 里，不能被精简掉。"""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "打包发布" in text
    assert "未实现" in text
    # 不能出现「已发布 / 已上架 / 生产可用」这类与实际不符的措辞
    for word in ("已发布到 SkillHub", "已上架", "可直接生产使用"):
        assert word not in text, f"SKILL.md 出现了未实现的宣称: {word}"


# --- agents/openai.yaml -----------------------------------------------------


def _openai_display_name() -> str:
    text = OPENAI_YAML_PATH.read_text(encoding="utf-8")
    match = re.search(r'^\s*display_name:\s*"?(?P<name>[^"\n]+)"?\s*$', text, flags=re.MULTILINE)
    assert match, "agents/openai.yaml 缺少 display_name"
    return match.group("name").strip()


def test_openai_yaml_display_name_matches_skill() -> None:
    data = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    assert _openai_display_name() == data["metadata.displayName"]


def test_openai_yaml_references_skill_slug() -> None:
    text = OPENAI_YAML_PATH.read_text(encoding="utf-8")
    assert f"${EXPECTED_SLUG}" in text, "default_prompt 里应引用 $<slug>"


# --- README -----------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "## 安装",
        "Python 3.11",
        "## 快速开始",
        ".jsonl.zst",
        ".jsonl",
        "Markdown 手动导入",
        "archive",
        "validate",
        "status",
        "recheck",
        "uninstall",
        "dry-run",
        "staging",
        "## 笔记状态语义",
        "有效",
        "待复核",
        "已过时",
        "已被取代",
        "过期只过滤，不删除",
        "## 隐私",
        "## 卸载",
    ],
)
def test_readme_covers_required_topic(needle: str) -> None:
    assert needle in README_PATH.read_text(encoding="utf-8"), f"README.md 缺少: {needle}"


def test_readme_distinguishes_dry_run_and_staging() -> None:
    text = README_PATH.read_text(encoding="utf-8")
    section = text.split("## dry-run 与 staging 的区别", 1)
    assert len(section) == 2, "README.md 缺少「dry-run 与 staging 的区别」小节"
    body = section[1].split("\n## ", 1)[0]
    assert "一个字节都不写" in body
    assert "退出码" in body


def test_readme_does_not_claim_unimplemented_write_path() -> None:
    """Gate 4 已实现写入，不能再留「没有写入能力 / 退出码 4」这类旧文案。"""
    text = README_PATH.read_text(encoding="utf-8")
    for stale in ("没有写入能力", "仍是退出码 4", "未实现 |"):
        assert stale not in text, f"README.md 残留过期陈述: {stale}"
    assert "SkillHub" in text
    assert "未实现" in text, "README.md 应说明打包发布仍未实现"


def test_readme_marks_fixtures_as_synthetic() -> None:
    text = README_PATH.read_text(encoding="utf-8")
    assert "合成" in text, "README.md 必须说明 tests/fixtures 是合成样本"
    assert "不是真实用户数据" in text


# --- 隐私：文档里不得出现本机痕迹 --------------------------------------------

FORBIDDEN_PATTERNS = {
    "windows 用户目录": r"[A-Za-z]:\\+Users",
    "本机 vault 绝对路径": r"[A-Za-z]:\\+obsidian",
    "codex 绝对路径": r"[A-Za-z]:\\+codex",
    "posix 用户目录": r"/Users/[A-Za-z0-9._-]+",
    "posix home": r"/home/[A-Za-z0-9._-]+",
    "本机用户名": r"84946",
    "真实 vault 名": r"可可乐",
}


def _documents() -> list[Path]:
    files = [SKILL_PATH, README_PATH, CHANGELOG_PATH, OPENAI_YAML_PATH]
    return files + REFERENCE_PATHS


@pytest.mark.parametrize("path", _documents(), ids=lambda item: item.name)
def test_documents_have_no_personal_paths(path: Path) -> None:
    assert path.exists(), f"文档不存在: {path}"
    text = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in FORBIDDEN_PATTERNS.items() if re.search(pattern, text)]
    assert not hits, f"{path.name} 出现本机痕迹: {hits}"


@pytest.mark.parametrize("path", _documents(), ids=lambda item: item.name)
def test_documents_end_with_newline(path: Path) -> None:
    assert path.read_text(encoding="utf-8").endswith("\n"), f"{path.name} 结尾缺少换行"


def test_references_are_reachable_from_skill() -> None:
    """SKILL.md 的 reference 路由必须覆盖 references/ 下的每个文件。"""
    text = SKILL_PATH.read_text(encoding="utf-8")
    missing = [item.name for item in REFERENCE_PATHS if f"references/{item.name}" not in text]
    assert not missing, f"SKILL.md 未路由这些 reference: {missing}"


# --- 不得承诺未实现的能力 ----------------------------------------------------
#
# 这些措辞在历史上都出现过，但 src/ 里没有对应实现（审计已确认）：
#   - `policy: {volatility: days}`：AppConfig 没有 policy 字段；
#   - 写入时由 compute_review_after() 算 review_after：该函数零生产调用点；
#   - 「检索笔记」：CLI 没有 search 子命令，只有 validate / status 的计数过滤；
#   - quick_validate.py：本仓库不存在该文件。
FORBIDDEN_CLAIMS = {
    "config 覆盖复核周期": r"policy:\s*\{",
    "自动重算 review_after": r"review_after`? 由\s*`?compute_review_after",
    "不存在的检索子命令": r"检索笔记",
    "不存在的校验脚本": r"quick_validate",
    "未发生的 CI 通过": r"CI\s*(已通过|校验通过)|GitHub Actions 已(通过|跑过)",
}


@pytest.mark.parametrize(
    "path",
    [item for item in _documents() if item != CHANGELOG_PATH],
    ids=lambda item: item.name,
)
def test_documents_make_no_unimplemented_promise(path: Path) -> None:
    # CHANGELOG 例外：它必须能点名「曾经写错的承诺」，否则无法记录修复本身。
    text = path.read_text(encoding="utf-8")
    hits = [name for name, pattern in FORBIDDEN_CLAIMS.items() if re.search(pattern, text)]
    assert not hits, f"{path.name} 承诺了未实现的能力: {hits}"


def test_readme_states_review_after_is_an_input() -> None:
    """写入端直接采用 DistilledNote.review_after，不会按周期重算。"""
    text = README_PATH.read_text(encoding="utf-8")
    assert "review_after` 是写入时的输入" in text
    assert "compute_review_after" in text, "README 应指出周期函数尚未接入归档主链路"


def test_expiry_policy_admits_missing_wiring() -> None:
    text = re.sub(r"\s+", "", (ROOT / "references" / "expiry-policy.md").read_text(encoding="utf-8"))
    assert "没有接入归档主链路" in text
    assert "没有config级覆盖开关" in text


def test_readme_documents_uninstalled_usage() -> None:
    """分发压缩包里没有 pyproject.toml，README 必须给出免安装入口。"""
    text = README_PATH.read_text(encoding="utf-8")
    assert "python scripts/cli.py" in text
    assert "分发压缩包" in text
    assert "不需要也不要执行 pip 安装" in text
