"""api_validate — dependency-free parameter validation for the Page API.

Goal: field typos / type errors produce a readable 400 immediately instead of
a 500 or silent misbehavior.

Contract: ApiValidationError is caught centrally by webapi._Bound and turned
into error_response(400); endpoint code only uses pick/qi/json_body and needs
no try/except of its own.
"""

from __future__ import annotations

from astrbot.api.web import request


class ApiValidationError(Exception):
    """Parameter validation failure; message is caller-facing (field name + expectation)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


async def json_body() -> dict:
    """Read the JSON request body and require a dict (list/scalar raises ApiValidationError)."""
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
    """Pick a typed field from a dict.

    - required: missing/None raises
    - cast: str/int/float/bool/list; conversion failure raises (message
      includes the field name and expected type)
    - enum: value whitelist (raises when absent; message lists valid values)
    - empty_allowed=False: empty str/list raises
    - default behavior matches manual .get(key, default)
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
    """Safe int conversion for query params: empty/None -> default; non-numeric raises."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ApiValidationError(f"字段 '{field}' 无效: 期望整数")
