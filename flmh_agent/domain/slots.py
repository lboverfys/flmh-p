from __future__ import annotations

import math
import re
from typing import Any


SLOT_NAMES = ("product", "region", "budget_max")
INVALIDATION_ORDER = (
    "product_results", "selected_product", "content_brief", "quote", "confirmation",
    "confirmed_quote", "content_task", "task_status", "summary", "product_history", "product_detail",
    "media_request", "media_run", "media_status", "product_daily",
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
    "product_history": ("product",),
    "product_detail": ("product",),
    "media_request": SLOT_NAMES,
    "media_run": SLOT_NAMES,
    "media_status": SLOT_NAMES,
    "product_daily": ("product",),
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
            if normalized[name] is not None and len(normalized[name]) > 80 and not is_product_identifier(normalized[name]):
                raise ValueError("product slot must be at most 80 characters")
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


def is_product_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", value)
                and (value.isdigit() or any(char.isdigit() for char in value) and any(char in value for char in "-_:")))


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
