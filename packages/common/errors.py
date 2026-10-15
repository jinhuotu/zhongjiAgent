from enum import IntEnum


# 业务码（非 HTTP 状态）：前端提示看它，40000 段
class ErrorCode(IntEnum):
    OK = 0
    BAD_REQUEST = 40000
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    VALIDATION = 42200
    INTERNAL = 50000


# 业务异常：app.py 捕获转 {code,msg,data}；如 raise AppError(UNAUTHORIZED,"invalid token",401)
class AppError(Exception):
    def __init__(
        self,
        code: int = ErrorCode.INTERNAL,
        msg: str = "internal error",
        status_code: int = 400,
    ) -> None:
        self.code = code
        self.msg = msg
        self.status_code = status_code
        super().__init__(msg)
