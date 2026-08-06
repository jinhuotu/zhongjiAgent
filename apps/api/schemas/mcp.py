from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateMcpServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    transport: Literal["streamable_http", "sse", "stdio"]
    url: str | None = Field(default=None, max_length=1024)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    timeoutSeconds: float = Field(default=60, gt=1, le=600)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool = True


class UpdateMcpServerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    transport: Literal["streamable_http", "sse", "stdio"] | None = None
    url: str | None = Field(default=None, max_length=1024)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    timeoutSeconds: float | None = Field(default=None, gt=1, le=600)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class UpdateMcpToolRequest(BaseModel):
    enabled: bool | None = None
