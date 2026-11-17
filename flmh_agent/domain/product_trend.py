from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


TREND_METRICS = ("sales7d", "relatedAuthorCount")


def compare_product_snapshots(metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare same-source count snapshots; historical currency is unavailable."""
    if len(metrics) < 2:
        return {"status": "INSUFFICIENT_SNAPSHOTS", "changes": {}, "unavailableMetrics": list(TREND_METRICS)}

    samples: dict[datetime, Mapping[str, Any]] = {}
    for metric in metrics:
        captured_at = _parse_timestamp(metric.get("capturedAt"))
        if captured_at is None:
            return {"status": "INVALID_TIMESTAMP", "changes": {}, "unavailableMetrics": list(TREND_METRICS)}
        previous = samples.get(captured_at)
        if previous is None or _version(metric) > _version(previous):
            samples[captured_at] = metric

    ordered = sorted(samples.items(), key=lambda item: item[0])
    if len(ordered) < 2:
        return {"status": "NO_ELAPSED_INTERVAL", "changes": {}, "unavailableMetrics": list(TREND_METRICS)}

    (previous_at, previous), (current_at, current) = ordered[-2:]
    elapsed_hours = (current_at - previous_at).total_seconds() / 3600
    observation = {
        "previousCapturedAt": previous_at.isoformat(),
        "currentCapturedAt": current_at.isoformat(),
        "elapsedHours": elapsed_hours,
        "previousSource": previous.get("source"),
        "currentSource": current.get("source"),
        "previousSourceRef": previous.get("sourceRef"),
        "currentSourceRef": current.get("sourceRef"),
    }
    sources = (previous.get("source"), current.get("source"))
    if any(not isinstance(source, str) or not source.strip() for source in sources):
        return {**observation, "status": "MISSING_SOURCE", "changes": {},
                "unavailableMetrics": list(TREND_METRICS)}
    if sources[0] != sources[1]:
        return {**observation, "status": "SOURCE_CHANGED", "changes": {},
                "unavailableMetrics": list(TREND_METRICS)}
    changes: dict[str, dict[str, float | None]] = {}
    unavailable: list[str] = []
    for name in TREND_METRICS:
        previous_value = _number(previous.get(name))
        current_value = _number(current.get(name))
        if previous_value is None or current_value is None:
            unavailable.append(name)
            continue
        changes[name] = {
            "previous": previous_value,
            "current": current_value,
            "delta": current_value - previous_value,
            "relativeChange": (current_value - previous_value) / previous_value if previous_value > 0 else None,
        }

    return {
        **observation,
        "status": "AVAILABLE" if changes else "MISSING_METRIC_VALUES",
        "changes": changes,
        "unavailableMetrics": unavailable,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _version(metric: Mapping[str, Any]) -> int:
    value = metric.get("metricVersion")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None
