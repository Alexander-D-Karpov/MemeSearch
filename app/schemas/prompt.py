from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# Shared properties
class PromptBase(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompt_text: Optional[str] = None
    is_default: Optional[bool] = False
    is_active: Optional[bool] = True


# Properties to receive via API on creation
class PromptCreate(PromptBase):
    name: str
    prompt_text: str


# Properties to receive via API on update
class PromptUpdate(PromptBase):
    pass


# Properties to return via API
class Prompt(PromptBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
