from pydantic import BaseModel, Field


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=512)


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=512)
