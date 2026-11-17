from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flmh_agent.domain.models import ContentBrief


def build_content_request(product: Mapping[str, Any], user_request: str, content_type: str) -> ContentBrief:
    title = str(product.get("title") or product["id"])
    return ContentBrief(
        content_type=content_type,
        title=f"{title} 内容生产请求",
        body=f"商品：{title}\n用户需求：{user_request.strip()}",
        source="INPUT_REQUEST",
        provider="NONE",
    )
