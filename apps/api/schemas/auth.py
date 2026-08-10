from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    is_superuser: bool = False
    roles: list[str] = Field(default_factory=list)
    menus: list[str] = Field(default_factory=list, description="可访问菜单 href")
