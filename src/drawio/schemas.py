from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class Node(BaseModel):
    id: UUID
    version: int
    updated_at: datetime


class Validation(BaseModel):
    pages: int
