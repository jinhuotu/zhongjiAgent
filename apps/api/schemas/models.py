from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import Literal

ModelType = Literal["text_chat", "multimodal_vision", "multimodal_audio", "text_embedding"]


class CreateModelConfigRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=128)
    kind: Literal["llm", "embedding"]
    modelType: ModelType = Field(
        default="text_chat",
        validation_alias=AliasChoices("modelType", "model_type"),
    )
    apiBase: str = Field(min_length=1, max_length=512)
    apiKey: str = Field(min_length=1, max_length=512)
    modelName: str = Field(min_length=1, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeoutSeconds: float = Field(default=120, gt=1, le=600)
    embeddingDim: int | None = Field(default=None, ge=32, le=8192)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    scopeFast: bool = False
    scopeDeep: bool = False
    scopeEmbedding: bool = False


class UpdateModelConfigRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    modelType: ModelType | None = Field(
        default=None,
        validation_alias=AliasChoices("modelType", "model_type"),
    )
    apiBase: str | None = Field(default=None, min_length=1, max_length=512)
    apiKey: str | None = Field(default=None, max_length=512)
    modelName: str | None = Field(default=None, min_length=1, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    clearTemperature: bool = False
    timeoutSeconds: float | None = Field(default=None, gt=1, le=600)
    embeddingDim: int | None = Field(default=None, ge=32, le=8192)
    remark: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    scopeFast: bool | None = None
    scopeDeep: bool | None = None
    scopeEmbedding: bool | None = None
