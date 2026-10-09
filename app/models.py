from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ShotPlan(BaseModel):
    position: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=80)
    duration: int = Field(ge=4, le=15)
    prompt: str = Field(min_length=10, max_length=1500)
    voiceover: str = Field(default="", max_length=300)
    overlay_text: str = Field(default="", max_length=40)
    continuity_mode: Literal["independent", "carry_last_frame"] = "independent"
    continuity_source: Literal["model", "human", "system"] = "model"
    transition_type: Literal["cut", "match_cut", "dissolve", "dip_to_black"] = "cut"
    transition_duration_ms: int = Field(default=0, ge=0, le=1000)
    entry_action: str = Field(default="", max_length=300)
    exit_action: str = Field(default="", max_length=300)
    continuity_group: Optional[str] = Field(default=None, max_length=80)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=9)
    reference_source: Literal["model", "human", "system"] = "model"


class Storyboard(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    aspect_ratio: Literal["9:16"] = "9:16"
    style: str = Field(min_length=1, max_length=500)
    shots: list[ShotPlan] = Field(min_length=1, max_length=8)

    @field_validator("style", mode="before")
    @classmethod
    def normalize_model_style(cls, value):
        # Style is descriptive metadata. Model verbosity should not invalidate
        # an otherwise usable storyboard, while the stored value stays bounded.
        return value.strip()[:500] if isinstance(value, str) else value


class QAIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    shot_position: Optional[int] = None

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value):
        normalized = str(value).strip().lower()
        if normalized in {"error", "critical", "fatal", "high"}:
            return "error"
        if normalized in {"warning", "warn", "info", "information", "notice", "medium", "low"}:
            return "warning"
        return value


class QAReport(BaseModel):
    passed: bool
    score: int = Field(ge=0, le=100)
    issues: list[QAIssue]


class QAOverrideRequest(BaseModel):
    acknowledged: Literal[True]
    reason: str = Field(min_length=2, max_length=500)


class ShotUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    duration: int = Field(ge=4, le=15)
    prompt: str = Field(min_length=10, max_length=1500)
    voiceover: str = Field(default="", max_length=300)
    overlay_text: str = Field(default="", max_length=40)
    transition_type: Optional[Literal["cut", "match_cut", "dissolve", "dip_to_black"]] = None
    transition_duration_ms: Optional[int] = Field(default=None, ge=0, le=1000)
    entry_action: Optional[str] = Field(default=None, max_length=300)
    exit_action: Optional[str] = Field(default=None, max_length=300)
    continuity_group: Optional[str] = Field(default=None, max_length=80)


class ShotRetryRequest(BaseModel):
    prompt: Optional[str] = Field(default=None, min_length=10, max_length=1500)


class ContinuityUpdate(BaseModel):
    enabled: bool


AssetType = Literal["main_product", "accessory", "packaging", "logo", "person", "scene", "style", "other"]
AssetPriority = Literal["core", "supporting"]


class ReferenceAssetUpdate(BaseModel):
    asset_type: AssetType = "main_product"
    subject_name: str = Field(default="", max_length=80)
    view_tags: list[str] = Field(default_factory=list, max_length=12)
    priority: AssetPriority = "supporting"
    description: str = Field(default="", max_length=600)
    constraints: str = Field(default="", max_length=600)
    is_primary: bool = False


class ShotReferencesUpdate(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=9)


class VideoQAIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value):
        normalized = str(value).strip().lower()
        if normalized in {"error", "critical", "fatal", "high"}:
            return "error"
        if normalized in {"warning", "warn", "info", "information", "notice", "medium", "low"}:
            return "warning"
        return value


class VideoQAReport(BaseModel):
    passed: bool
    score: int = Field(ge=0, le=100)
    issues: list[VideoQAIssue]
    recommendation: str = Field(default="", max_length=1000)
    retry_prompt: str = Field(default="", max_length=1500)
