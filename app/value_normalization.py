"""Small shared conversions with no application or model dependencies."""

from typing import Any


def normalize_currency(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    return "RUB" if text in {"RUR", "РУБ", "РУБ.", "₽"} else text


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def column_name(index: int) -> str:
    """Convert a zero-based column index to the common A/Z/AA notation."""
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(ord("A") + remainder) + name
        index -= 1
    return name
