from pydantic import BaseModel


class ParticipantCreate(BaseModel):
    phone_number: str

class ParticipantResponse(BaseModel):
    id: int
    phone_number: str

    model_config = {
        "from_attributes": True
    }