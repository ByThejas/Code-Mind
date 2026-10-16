"""Request ids and the API's single error shape: {"error": {"code", "message", "request_id"}}."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from codemind.core.errors import CodemindError

log = logging.getLogger(__name__)


def _error(request: Request, status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = {"error": {"code": code, "message": message, "request_id": request_id, **extra}}
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": request_id or ""})


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex
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

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(request, 500, "internal_error", "Something went wrong.")
