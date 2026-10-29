"""Request ids and the API's single error shape: {"error": {"code", "message", "request_id"}}."""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from codemind.core.errors import CodemindError

log = logging.getLogger(__name__)

# A caller may pass its own X-Request-ID to trace a request end to end. Anything else is replaced,
# so log lines and response headers never carry arbitrary client text.
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")

HTTP_ERROR_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = {"error": {"code": code, "message": message, "request_id": request_id, **extra}}
    return JSONResponse(
        body, status_code=status, headers={**(headers or {}), "X-Request-ID": request_id or ""}
    )


def _request_id(request: Request) -> str:
    given = request.headers.get("x-request-id", "")
    return given if REQUEST_ID.fullmatch(given) else uuid.uuid4().hex


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = _request_id(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(CodemindError)
    async def codemind_error(request: Request, exc: CodemindError) -> JSONResponse:
        return _error(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return _error(request, 422, "validation_error", "The request is invalid.", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Unknown routes, wrong methods and the like: same shape as every other error.
        code = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        try:
            default = HTTPStatus(exc.status_code).phrase
        except ValueError:
            default = "Request failed."
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else default
        return _error(request, exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(request, 500, "internal_error", "Something went wrong.")
