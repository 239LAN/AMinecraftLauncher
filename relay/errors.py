"""统一的错误响应：{"error": {"code": "...", "message": "..."}}。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class RelayError(Exception):
    """业务异常，由 FastAPI 异常处理器转成统一错误体。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app: FastAPI) -> None:
    """注册业务异常与参数校验异常的处理器。"""

    @app.exception_handler(RelayError)
    async def _relay_error(_: Request, exc: RelayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_payload(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        detail = errors[0].get("msg", "") if errors else ""
        message = f"请求参数不合法：{detail}" if detail else "请求参数不合法"
        return JSONResponse(status_code=422, content=error_payload("INVALID_REQUEST", message))
