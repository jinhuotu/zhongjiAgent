from pydantic import BaseModel, Field
from typing import Literal


class CreateBaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class UpdateBaseRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class AclGrantIn(BaseModel):
    subjectType: Literal["user", "role"]
    subjectId: int = Field(ge=1)
    canView: bool = False
    canUse: bool = False
    canManage: bool = False


class ReplaceAclRequest(BaseModel):
    grants: list[AclGrantIn] = Field(default_factory=list, max_length=500)


class UploadDocumentRequest(BaseModel):
    baseId: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=4)
    fileType: str | None = None
    size: int | None = None
    uploader: str | None = None
    tags: list[str] | None = None


class TextDocumentRequest(BaseModel):
    baseId: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=4)
    uploader: str | None = None
    tags: list[str] | None = None


class UrlDocumentRequest(BaseModel):
    baseId: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=4, max_length=1024)
    title: str | None = None
    uploader: str | None = None
    tags: list[str] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    baseId: str | None = Field(default=None, max_length=32)
    topK: int = Field(default=5, ge=1, le=50)
    minScore: float = Field(default=0.0, ge=0.0, le=1.0)
