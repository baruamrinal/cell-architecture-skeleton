"""One error shape for every API: ``{"error": <code>, "message": <text>}``.

On ``/graphql`` (the web UI, docs/plan.md §2.4) the same error is a GraphQL error:
``{"errors": [{"message", "extensions": {"code", "status", "retryAfter"}}]}``, with the
HTTP status and ``Retry-After`` header kept, so the UI handles one shape.
"""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}
GRAPHQL_PATH = "/graphql"


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


def graphql_error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """The same error, shaped as a GraphQL response (``Retry-After`` also in extensions)."""
    headers = dict(headers or {})
    extensions: dict[str, object] = {"code": code, "status": status}
    retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if retry is not None and retry.isdigit():
        extensions["retryAfter"] = int(retry)
    return JSONResponse(
        {"errors": [{"message": message, "extensions": extensions}], "data": None},
        status_code=status,
        headers=headers,
    )


def error_for(
    path: str, status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """The error in the shape the caller of ``path`` expects."""
    shape = graphql_error_response if path == GRAPHQL_PATH else error_response
    return shape(status, code, message, headers)


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)  # noqa: S101 - narrowing for the type checker
    return error_for(request.url.path, exc.status, exc.code, exc.message, exc.headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Never echo submitted values back: they can be secrets (e.g. cell credentials).
    details = [
        {key: value for key, value in error.items() if key not in ("input", "ctx", "url")}
        for error in exc.errors()
    ]
    return JSONResponse(
        {
            "error": "validation_error",
            "message": "request validation failed",
            "details": jsonable_encoder(details),
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
