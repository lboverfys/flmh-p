from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MEDIA_TERMINAL_STATUSES = {"SUCCEEDED", "MOCKED", "FAILED", "CANCELLED"}
MEDIA_STATUSES = MEDIA_TERMINAL_STATUSES | {"QUEUED", "RUNNING", "SUBMISSION_UNKNOWN"}
MEDIA_FIELD_START = r"(?:^|[，,；;。\n])\s*(?:请)?(?:把)?\s*"
MEDIA_FIELD_PATTERNS = {
    "image_prompt": re.compile(MEDIA_FIELD_START + r"图片(?:提示词|要求)?\s*(?:改为|改成|修改为|修改成|调整为|调整成|[:：=])\s*([^，,；;。\n]+)"),
    "video_prompt": re.compile(MEDIA_FIELD_START + r"视频(?:提示词|要求)?\s*(?:改为|改成|修改为|修改成|调整为|调整成|[:：=])\s*([^，,；;。\n]+)"),
    "aspect_ratio": re.compile(MEDIA_FIELD_START + r"(?:画面比例|宽高比|比例)\s*(?:改为|改成|调整为|调整成|[:：=]|为)?\s*([^，,；;。\n]+)"),
    "duration_seconds": re.compile(MEDIA_FIELD_START + r"(?:视频)?时长\s*(?:改为|改成|调整为|调整成|[:：=]|为)?\s*([^，,；;。\n]+)"),
}


class MediaOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    video_prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    aspect_ratio: Literal["1:1", "3:4", "4:3", "9:16", "16:9"] | None = None
    duration_seconds: int | None = Field(default=None, strict=True, ge=2, le=15)

    @field_validator("image_prompt", "video_prompt")
    @classmethod
    def trim_prompt(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("media prompt must not be blank")
        return value.strip() if value is not None else None


def explicit_media_options(text: str) -> MediaOptions:
    values: dict[str, Any] = {}
    for field, pattern in MEDIA_FIELD_PATTERNS.items():
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            if field == "aspect_ratio":
                values[field] = re.sub(r"\s+", "", raw).replace("：", ":")
            elif field == "duration_seconds":
                duration = re.fullmatch(r"(\d+)\s*秒", raw)
                if not duration:
                    raise ValueError("duration_seconds must be a whole number of seconds")
                values[field] = int(duration.group(1))
            else:
                values[field] = raw
    return MediaOptions.model_validate(values)


def without_media_fields(text: str) -> str:
    for pattern in MEDIA_FIELD_PATTERNS.values():
        text = pattern.sub(" ", text)
    return text


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
    if any(node.get("status") == "QUEUED" and node.get("errorCode") == "VIDEO_CAPACITY_WAIT"
           for node in result.get("nodes", [])):
        return prefix + "当前账号的在途视频请求已达上限，新视频仍在排队、未提交；已有请求完成或核实后，可手动推进此运行。"
    return prefix + "可显式推进下一阶段；读取状态不会提交供应商请求。"
