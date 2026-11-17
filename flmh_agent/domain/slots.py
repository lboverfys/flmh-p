from __future__ import annotations

import math
from typing import Any


SLOT_NAMES = ("product", "region", "budget_max")
INVALIDATION_ORDER = (
    "product_results", "selected_product", "content_brief", "quote", "confirmation",
    "confirmed_quote", "content_task", "task_status", "summary",
)
SLOT_DEPENDENCIES = {
    "product_results": ("product", "region", "budget_max"),
    "selected_product": ("product", "region", "budget_max"),
    "content_brief": ("product", "region"),
    "quote": SLOT_NAMES,
    "confirmation": SLOT_NAMES,
    "confirmed_quote": SLOT_NAMES,
    "content_task": SLOT_NAMES,
    "task_status": SLOT_NAMES,
    "summary": SLOT_NAMES,
}


def normalize_slots(values: dict[str, Any] | None = None) -> dict[str, Any]:
    source = values or {}
    unknown = set(source) - set(SLOT_NAMES)
    if unknown:
        raise ValueError(f"unknown slot names: {sorted(unknown)}")
    normalized: dict[str, Any] = {"product": None, "region": "ID", "budget_max": None}
    for name in SLOT_NAMES:
        if name not in source:
            continue
        value = source[name]
        if name == "product":
            if value is not None and not isinstance(value, str):
                raise ValueError("product slot must be a string")
            normalized[name] = value.strip() or None if value is not None else None
        elif name == "region":
            if value is not None and not isinstance(value, str):
                raise ValueError("region slot must be a string")
            region = value.strip().upper() if value else "ID"
            if len(region) != 2 or not region.isalpha() or not region.isascii():
                raise ValueError("region slot must be a two-letter ISO code")
            normalized[name] = region
        else:
            normalized[name] = _number_or_none(value)
    return normalized


def update_slots(current: dict[str, Any] | None, updates: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    before = normalize_slots(current)
    unknown = set(updates or {}) - set(SLOT_NAMES)
    if unknown:
        raise ValueError(f"unknown slot names: {sorted(unknown)}")
    after = normalize_slots({**before, **(updates or {})})
    changed = {name for name in SLOT_NAMES if before[name] != after[name]}
    return after, [name for name in INVALIDATION_ORDER if changed.intersection(SLOT_DEPENDENCIES[name])]


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
    return float(value)
