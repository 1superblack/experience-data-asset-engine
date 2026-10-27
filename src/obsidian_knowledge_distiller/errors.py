"""本工具的异常体系。

约定：
- 所有异常都继承 ``DistillerError``，CLI 统一捕获并转成退出码 1。
- 需要参与 ``except ValueError`` 的异常同时继承 ``ValueError``，便于调用方按
  Python 习惯捕获协议校验失败。
"""

from __future__ import annotations


class DistillerError(Exception):
    """本工具所有预期内错误的基类。"""


class ConfigError(DistillerError, ValueError):
    """配置缺失、配置非法或路径越界。"""


class InputError(DistillerError, ValueError):
    """输入文件不存在、不可读或格式不可识别。"""


class SessionFormatError(InputError):
    """会话输入的结构不符合协议（截断 JSON、缺字段、无可提取消息等）。"""


class OptionalDependencyError(InputError):
    """缺少可选依赖。绝不降级为其它处理方式。"""


class SchemaError(DistillerError, ValueError):
    """JSON 或数据结构不符合协议。"""
