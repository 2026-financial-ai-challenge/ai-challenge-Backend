from typing import Literal

from pydantic import BaseModel


class StartCallResponse(BaseModel):
    callId: str | None = None
    status: Literal["waiting", "calling"] = "waiting"
