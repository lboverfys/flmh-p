from __future__ import annotations

import math
from typing import Any


SLOT_NAMES = ("product", "region", "budget_max")
INVALIDATION_ORDER = (
    "product_results",
    "selected_product",
    "content_brief",
    "quote",
    "confirmation",
    "content_task",
    "task_status",
)


def normalize_slots(values: dict[str, Any] | None = None) -> dict[str, Any]:
    source = values or {}
    normalized: dict[str, Any] = {"product": None, "region": "ID", "budget_max": None}
    for name in SLOT_NAMES:
        if name not in source:
            continue
        value = source[name]
        if name == "product":
            normalized[name] = str(value).strip() or None if value is not None else None
        elif name == "region":
            normalized[name] = str(value).strip().upper() or "ID" if value is not None else "ID"
        else:
            normalized[name] = _number_or_none(value)
    return normalized


def update_slots(
    current: dict[str, Any] | None,
    updates: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply slots; product is a search keyword matching a Java result by id or title."""
    before = normalize_slots(current)
    after = normalize_slots({**before, **(updates or {})})
    changed = {name for name in SLOT_NAMES if before[name] != after[name]}
    if not changed:
        return after, []

    invalidated = list(INVALIDATION_ORDER)
    return after, invalidated


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("budget_max must be a finite non-negative number")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("budget_max must be a finite non-negative number")
    number = float(value)
    return number
