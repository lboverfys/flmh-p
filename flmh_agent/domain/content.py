from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flmh_agent.domain.models import ContentBrief


def build_content_request(
    product: Mapping[str, Any],
    user_request: str,
    content_type: str,
    target_language: str | None = None,
) -> ContentBrief:
    title = str(product.get("title") or product["id"])
    body = f"商品：{title}\n用户需求：{user_request.strip()}"
    if target_language:
        body += f"\n目标语种：{target_language}"
    return ContentBrief(
        content_type=content_type,
        title=f"{title} 内容生产请求",
        body=body,
        target_language=target_language,
        source="INPUT_REQUEST",
        provider="NONE",
    )
