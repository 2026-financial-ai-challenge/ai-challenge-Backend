
from sqlalchemy import Column, Integer, String
from app.database import Base

class Participant(Base):
    __tablename__ = 'participants'

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, nullable=False, unique=True)
