from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


MEDIA_TERMINAL_STATUSES = {"SUCCEEDED", "MOCKED", "FAILED", "CANCELLED"}
MEDIA_STATUSES = MEDIA_TERMINAL_STATUSES | {"QUEUED", "RUNNING", "SUBMISSION_UNKNOWN"}


class MediaProductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str = Field(min_length=1, max_length=96)
    image_prompt: str = Field(min_length=1, max_length=2000)
    video_prompt: str = Field(min_length=1, max_length=2000)
    aspect_ratio: Literal["1:1", "3:4", "4:3", "9:16", "16:9"]
    duration_seconds: int = Field(strict=True, ge=2, le=15)
    budget_max: float = Field(ge=0, allow_inf_nan=False)


def media_status(result: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    if result.get("runId") != run_id or result.get("status") not in MEDIA_STATUSES:
        raise ValueError("media service returned an invalid run identity or status")
    if result["status"] == "MOCKED":
        if result.get("executionMode") != "MOCK_ONLY" or result.get("artifacts") != []:
            raise ValueError("mock media result must declare MOCK_ONLY and no artifacts")
        if any(node.get("outputUrl") for node in result.get("nodes", [])):
            raise ValueError("mock media nodes cannot claim generated URLs")
    return dict(result)


def summarize_media(result: Mapping[str, Any]) -> str:
    status = result["status"]
    prefix = f"媒体任务 {result['runId']}；执行模式：{result.get('executionMode')}；状态：{status}。"
    if status == "MOCKED":
        return prefix + "图片和视频流程已按本地 Mock 完成，未生成文件或 URL，未产生供应商费用。"
    if status == "SUBMISSION_UNKNOWN":
        return prefix + "供应商提交结果未知；只能先核实，未重新提交或自动重试。"
    if status == "SUCCEEDED":
        return prefix + "媒体服务报告生产完成；产物地址和来源见 mediaStatus，Agent 未另行下载或核验文件。"
    if status in {"FAILED", "CANCELLED"}:
        return prefix + "流程未完成，已保留节点状态；失败重跑需要新报价及确认。"
    return prefix + "可显式推进下一阶段；读取状态不会提交供应商请求。"
