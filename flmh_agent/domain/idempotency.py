from __future__ import annotations

import hashlib
import json
from typing import Any


def idempotency_key(*, user_request: str, product_id: str, content_type: str, budget_max: float | None, region: str) -> str:
    payload: dict[str, Any] = {
        "user_request": " ".join(user_request.split()),
        "product_id": product_id.strip(),
        "content_type": content_type.strip(),
        "budget_max": budget_max,
        "region": region.strip().upper(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
