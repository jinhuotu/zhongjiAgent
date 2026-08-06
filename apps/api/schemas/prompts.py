from __future__ import annotations

from pydantic import BaseModel, Field


class CreatePromptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=100000)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool = True


class UpdatePromptRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=100000)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
