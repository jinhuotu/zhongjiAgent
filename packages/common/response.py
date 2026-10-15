from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应包：{ code, msg, data }。"""

    code: int = 0
    msg: str = "ok"
    data: T | None = None


class PageData(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


# 成功信封：所有接口统一 {code:0,msg,data}，前端只认 code===0
def ok(data: Any = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data}


# 失败信封：HTTP 状态与业务 code 分离，异常处理器调它
def fail(code: int, msg: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}
