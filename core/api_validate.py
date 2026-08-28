"""api_validate —— Page API 零依赖参数校验层（M0 工程加固）。

目标：字段拼写/类型错误即时给出可读 400，而不是 500 或静默出错。
成功路径行为与原手工取值完全一致；错误路径只收紧（原 500 → 400）。

约定：ApiValidationError 由 webapi._Bound 统一捕获转 error_response(400)，
端点代码只负责 pick/qi/json_body，不必各自 try/except。
"""

from __future__ import annotations

from astrbot.api.web import request


class ApiValidationError(Exception):
    """参数校验失败；message 面向调用方（含字段名与期望）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


async def json_body() -> dict:
    """读取 JSON 请求体并保证是 dict（list/标量 → ApiValidationError）。"""
    try:
        body = await request.json(default={})
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise ApiValidationError("请求体必须为 JSON 对象")
    return body


def pick(
    data: dict,
    key: str,
    *,
    cast=str,
    default=None,
    required=False,
    enum=None,
    empty_allowed=True,
    error_prefix="",
) -> object:
    """从 dict 中按类型取字段。

    - required：缺失/None 抛错
    - cast：str/int/float/bool/list；转换失败抛错（消息含字段名与期望类型）
    - enum：取值白名单（不在其中抛错，消息列出合法值）
    - empty_allowed=False：str/list 为空抛错
    - 默认值行为与手工 .get(key, default) 一致
    """
    prefix = f"{error_prefix}字段 '{key}'" if error_prefix else f"字段 '{key}'"

    if key not in data or data[key] is None:
        if required:
            raise ApiValidationError(f"{prefix} 无效: 缺少必填参数")
        return default

    value = data[key]
    if cast is list:
        if not isinstance(value, list):
            raise ApiValidationError(f"{prefix} 无效: 期望数组")
        converted = value
    elif cast is bool:
        if isinstance(value, bool):
            converted = value
        elif isinstance(value, str) and value.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            converted = True
        elif isinstance(value, str) and value.strip().lower() in (
            "0",
            "false",
            "no",
            "off",
            "",
        ):
            converted = False
        else:
            converted = bool(value)
    else:
        try:
            converted = cast(value)
        except (TypeError, ValueError):
            raise ApiValidationError(f"{prefix} 无效: 期望 {cast.__name__}")

    if not empty_allowed and isinstance(converted, (str, list)) and not converted:
        raise ApiValidationError(f"{prefix} 无效: 不能为空")
    if enum is not None and converted not in enum:
        raise ApiValidationError(
            f"{prefix} 无效: 取值必须为 {'|'.join(map(str, enum))}"
        )
    return converted


def qi(value, field: str = "id", default: int = 0) -> int:
    """查询参数安全转 int：空值/None 返回 default；非数字抛 ApiValidationError。"""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ApiValidationError(f"字段 '{field}' 无效: 期望整数")
