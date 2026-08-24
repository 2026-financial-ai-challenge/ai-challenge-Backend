from typing import Any, Literal

from pydantic import BaseModel, Field


class BehaviorItem(BaseModel):
    label: str
    evidence: str = ""


class TrainingReport(BaseModel):
    suspected: bool
    gaveName: bool
    triedHangup: bool
    summary: str
    coaching: str
    riskBehaviors: list[BehaviorItem] = Field(default_factory=list)
    defenseBehaviors: list[BehaviorItem] = Field(default_factory=list)
    source: Literal["live", "clawops"]


class TranscriptTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class GetReportResponse(BaseModel):
    sessionId: str
    callId: str | None = None
    status: Literal["none", "pending", "draft", "final", "failed"]
    turns: list[TranscriptTurn] = Field(default_factory=list)
    draft: TrainingReport | None = None
    final: TrainingReport | None = None
    clawopsSummary: dict[str, Any] | None = None
