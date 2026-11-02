"""Temporary business policy for fields removed from the current Daily form.

The tender logic, prompts and regression tests stay in the repository.  To
restore a field, remove its API name from ``DISABLED_TENDER_FIELDS`` (and, for
payment delay, remove the corresponding reason from
``DISABLED_DECISION_REASONS``).  This is intentionally a policy switch rather
than commented-out or deleted extraction code.
"""

from __future__ import annotations

from collections.abc import Iterable


DISABLED_TENDER_FIELDS = frozenset(
    {
        "nationalRegime",
        "deliveryType",
        "deliveryDays",
        "deliveryBatchDays",
        "deliveryDate",
        "paymentDelayDays",
    }
)

DISABLED_DECISION_REASONS = frozenset(
    {
        "Коммерческие условия. Отсрочка платежа 90 дней и более",
    }
)


def is_tender_field_enabled(field_name: str) -> bool:
    return field_name not in DISABLED_TENDER_FIELDS


def enabled_tender_fields(field_names: Iterable[str]) -> list[str]:
    return [field_name for field_name in field_names if is_tender_field_enabled(field_name)]


def is_decision_reason_enabled(reason: str) -> bool:
    return reason not in DISABLED_DECISION_REASONS


def remove_disabled_tender_fields(
    fields: dict[str, object],
    meta: dict[str, object] | None = None,
) -> None:
    """Drop discontinued values from a result while preserving active fields."""
    for field_name in DISABLED_TENDER_FIELDS:
        fields.pop(field_name, None)
        if meta is not None:
            meta.pop(field_name, None)
