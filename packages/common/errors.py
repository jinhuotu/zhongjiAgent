from enum import IntEnum


class ErrorCode(IntEnum):
    OK = 0
    BAD_REQUEST = 40000
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    VALIDATION = 42200
    INTERNAL = 50000


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
