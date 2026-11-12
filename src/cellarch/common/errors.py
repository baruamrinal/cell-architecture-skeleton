"""One error shape for every API: ``{"error": <code>, "message": <text>}``."""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


def error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": code, "message": message}, status_code=status, headers=dict(headers or {})
    )


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)  # noqa: S101 - narrowing for the type checker
    return error_response(exc.status, exc.code, exc.message, exc.headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    return JSONResponse(
        {
            "error": "validation_error",
            "message": "request validation failed",
            "details": jsonable_encoder(exc.errors()),
        },
        status_code=422,
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    return error_response(
        exc.status_code,
        _HTTP_CODES.get(exc.status_code, "http_error"),
        str(exc.detail),
        exc.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
