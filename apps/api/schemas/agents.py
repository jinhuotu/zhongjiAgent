from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    promptId: str | None = Field(default=None, max_length=32)
    knowledgeBaseIds: list[str] = Field(default_factory=list, max_length=32)
    mode: Literal["fast", "deep"] = "fast"
    mcpToolIds: list[str] = Field(default_factory=list, max_length=64)
    toolsEnabled: bool = True


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    promptId: str | None = Field(default=None, max_length=32)
    knowledgeBaseIds: list[str] | None = Field(default=None, max_length=32)
    mode: Literal["fast", "deep"] | None = None
    mcpToolIds: list[str] | None = Field(default=None, max_length=64)
    toolsEnabled: bool | None = None
