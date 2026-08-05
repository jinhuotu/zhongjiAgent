from pydantic import BaseModel, Field


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    displayName: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    department: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    roleCodes: list[str] = Field(default_factory=list)
    isActive: bool = True
    isSuperuser: bool = False
    remark: str | None = None


class UpdateUserRequest(BaseModel):
    displayName: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    department: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    roleCodes: list[str] | None = None
    isActive: bool | None = None
    isSuperuser: bool | None = None
    remark: str | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=6, max_length=128)
