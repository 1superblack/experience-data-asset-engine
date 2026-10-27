"""Markdown 手动导入适配器。

把一个手写/导出的 Markdown 会话翻译成 ``NormalizedSession``：

```markdown
# 会话标题

## 用户
需求内容

## 助手
回答内容
```

规则（与 ``references/output-schema.md`` 的会话协议一致）：

- 一级标题是会话标题，必须出现且只出现一次，且位于第一条角色标题之前。
- 角色标题只认白名单别名，大小写与首尾空白可容忍；不在白名单里的二级标题
  一律失败关闭并报行号——**绝不把全文当成 assistant**。
- 代码块（``` 或 ~~~ 围栏）里的 ``## 用户`` 是内容，不是角色标题；
  关闭围栏要求「同字符 且 长度不少于开启长度」，因此四反引号块里的三反引号不会提前关闭。
- 空文件、无标题、无角色段落、空段落、标题前有正文、多个一级标题，全部失败关闭。
- BOM / CRLF / LF 都支持；输入不改写、不落盘，只读。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Final

from ..errors import InputError, SessionFormatError
from ..models import NormalizedSession, validate_source

#: 输入上限：Markdown 会话是手写/导出文本，超过 2MB 基本是误传了大文件。
MAX_MARKDOWN_BYTES: Final = 2 * 1024 * 1024

#: 角色别名（大小写不敏感，做 ``lower()`` 后比较）。
ROLE_ALIASES: Final = {
    "用户": "user",
    "user": "user",
    "human": "user",
    "助手": "assistant",
    "assistant": "assistant",
    "codex": "assistant",
    "claude": "assistant",
    "ai": "assistant",
}

_H1_RE: Final = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*$")
_H2_RE: Final = re.compile(r"^##[ \t]+(?P<name>.*?)[ \t]*$")
_FENCE_RE: Final = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")


def _read_text(path: Path) -> tuple[str, bytes]:
    if not path.exists():
        raise InputError(f"文件不存在: {path}")
    if not path.is_file():
        raise InputError(f"输入不是文件: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:  # pragma: no cover - 权限等极端情况
        raise InputError(f"无法读取文件大小: {path}（{exc}）") from exc
    if size > MAX_MARKDOWN_BYTES:
        raise SessionFormatError(
            f"Markdown 输入过大（{size} 字节 > 上限 {MAX_MARKDOWN_BYTES} 字节）: {path}"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"无法读取文件: {path}（{exc}）") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SessionFormatError(
            f"输入不是 UTF-8 编码（偏移 {exc.start} 字节）: {path}"
        ) from None
    return text, raw


def _timestamp(path: Path, now: datetime | None) -> str:
    if now is not None:
        stamp = now
    else:
        stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.replace(microsecond=0).isoformat()


def _parse(
    text: str, path: Path
) -> tuple[str, list[tuple[str, int, list[str]]]]:
    """返回 (标题, [(角色, 起始行号, 原始行)])。所有结构问题都带行号失败关闭。

    围栏状态机（不是正则猜嵌套）：

    - 打开时记住 ``fence_char``（````` 或 ``~``）与 ``fence_length``（实际个数）；
    - **只有**「字符相同 且 长度 >= 开启长度」的行才能关闭——所以四反引号块里出现
      三反引号不会被提前关掉；
    - 围栏内的任何内容（包括 ````## 用户`` / ``## 助手`` / 未知二级标题）都是正文；
    - 围栏开在第一条角色标题之前 → 视为「标题前正文」，失败关闭；
    - 到 EOF 仍未关闭 → 失败关闭，不把剩下内容当普通正文。
    """
    title: str | None = None
    sections: list[tuple[str, int, list[str]]] = []
    current: list[str] | None = None
    fence_char: str | None = None
    fence_length = 0
    fence_start = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        marker_match = _FENCE_RE.match(raw)

        if fence_char is not None:
            # 围栏内：只有「同字符 + 长度不少于开启长度」才算关闭
            if marker_match:
                marker = marker_match.group("fence")
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = None
                    fence_length = 0
                    current.append(raw)
                    continue
            current.append(raw)
            continue

        if marker_match:
            marker = marker_match.group("fence")
            if current is None:
                raise SessionFormatError(
                    f"{path} 第 {lineno} 行：第一条角色标题之前出现了代码块"
                    "（只允许一级标题与空行）"
                )
            fence_char = marker[0]
            fence_length = len(marker)
            fence_start = lineno
            current.append(raw)
            continue

        h1 = _H1_RE.match(raw)
        if h1:
            if title is not None:
                raise SessionFormatError(
                    f"{path} 第 {lineno} 行：出现第二个一级标题，只允许一个会话标题"
                )
            if sections:
                raise SessionFormatError(
                    f"{path} 第 {lineno} 行：一级标题必须出现在第一条角色标题之前"
                )
            title = h1.group("title").strip()
            if not title:
                raise SessionFormatError(f"{path} 第 {lineno} 行：一级标题为空")
            continue

        h2 = _H2_RE.match(raw)
        if h2:
            name = h2.group("name").strip()
            role = ROLE_ALIASES.get(name.lower())
            if role is None:
                allowed = " / ".join(sorted(ROLE_ALIASES))
                raise SessionFormatError(
                    f"{path} 第 {lineno} 行：无法识别的角色标题「## {name}」；"
                    f"允许的角色别名：{allowed}。已失败关闭，不会把正文当成 assistant"
                )
            current = []
            sections.append((role, lineno, current))
            continue

        if current is not None:
            current.append(raw)
        elif raw.strip():
            raise SessionFormatError(
                f"{path} 第 {lineno} 行：第一条角色标题之前出现了正文"
                "（只允许一级标题与空行）"
            )

    if fence_char is not None:
        raise SessionFormatError(
            f"{path}：第 {fence_start} 行开始的代码围栏没有闭合（到文件末尾仍未关闭）"
        )
    if title is None:
        raise SessionFormatError(f"{path}：缺少一级标题（# 会话标题）")
    if not sections:
        raise SessionFormatError(f"{path}：没有任何角色段落（## 用户 / ## 助手）")
    return title, sections


def load_markdown_session(
    path: Path, source: str = "manual", now: datetime | None = None
) -> NormalizedSession:
    """读取一份 Markdown 会话，返回标准化会话。只读，不修改输入文件。"""
    source_id = validate_source(source)
    text, raw = _read_text(Path(path))
    title, sections = _parse(text, Path(path))

    messages: list[dict[str, str]] = []
    for role, lineno, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            raise SessionFormatError(
                f"{path} 第 {lineno} 行：「## {role}」段落内容为空，已失败关闭"
            )
        messages.append({"role": role, "text": body, "kind": "message"})

    stamp = _timestamp(Path(path), now)
    session_id = f"md-{hashlib.sha256(raw).hexdigest()[:16]}"

    return NormalizedSession.from_dict(
        {
            "schema_version": 1,
            "source": source_id,
            "session_id": session_id,
            "title": title,
            "created_at": stamp,
            "updated_at": stamp,
            "model": None,
            "cwd": None,
            "messages": messages,
            "metadata": {
                "adapter": "markdown",
                "file": Path(path).name,
                "sections": len(sections),
            },
        }
    )
