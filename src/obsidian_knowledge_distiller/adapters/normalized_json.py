"""标准化会话 JSON 适配器。

输入就是 ``NormalizedSession`` 协议本身（Agent 原生模式最常用），不需要翻译，
只需要校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import read_json_file
from ..errors import SchemaError
from ..models import NormalizedSession


def load_normalized_json(path: Path) -> NormalizedSession:
    """读取并校验一个标准化会话 JSON 文件。"""
    payload: Any = read_json_file(Path(path))
    if not isinstance(payload, dict):
        raise SchemaError(f"标准化会话必须是 JSON 对象: {path}")
    return NormalizedSession.from_dict(payload)
