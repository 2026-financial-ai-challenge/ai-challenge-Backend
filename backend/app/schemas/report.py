from typing import Any, Literal

from pydantic import BaseModel, Field


class BehaviorItem(BaseModel):
    label: str
    evidence: str = ""


class TrainingReport(BaseModel):
    score: int = Field(default=60, ge=0, le=100)
    suspected: bool
    gaveName: bool
    triedHangup: bool
    summary: str
    coaching: str
    riskBehaviors: list[BehaviorItem] = Field(default_factory=list)
    defenseBehaviors: list[BehaviorItem] = Field(default_factory=list)
    source: Literal["live", "clawops", "comparison"]


class WebTrainingReport(BaseModel):
    """웹 훈련(피싱 사이트 모사) 결과. 전화 점수와 같은 0~100 척도."""

    score: int = Field(ge=0, le=100)
    events: list[str] = Field(default_factory=list)
    riskBehaviors: list[BehaviorItem] = Field(default_factory=list)
    defenseBehaviors: list[BehaviorItem] = Field(default_factory=list)


class TranscriptTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class GetReportResponse(BaseModel):
    sessionId: str
    callId: str | None = None
    status: Literal["none", "pending", "draft", "final", "failed"]
    turns: list[TranscriptTurn] = Field(default_factory=list)
    draftTurns: list[TranscriptTurn] = Field(default_factory=list)
    unannouncedTurns: list[TranscriptTurn] = Field(default_factory=list)
    draft: TrainingReport | None = None
    unannounced: TrainingReport | None = None
    final: TrainingReport | None = None
    clawopsSummary: dict[str, Any] | None = None
    webTraining: WebTrainingReport | None = None
