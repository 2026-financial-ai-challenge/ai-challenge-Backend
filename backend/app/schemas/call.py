from typing import Literal

from pydantic import BaseModel


class StartCallResponse(BaseModel):
    callId: str
    status: Literal["calling"]
