from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from apps.api.database import DatabaseUnavailable


class ProblemDetails(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str | None = None
    request_id: str | None = None
    errors: list[dict[str, Any]] | None = None


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str,
        *,
        problem_type: str = "about:blank",
        code: str | None = None,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.code = code or (
            problem_type.rsplit(":", 1)[-1]
            if problem_type != "about:blank"
            else "request_failed"
        )
        self.headers = headers or {}
        self.errors = errors


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id[:127]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def _response(
    request: Request, problem: ProblemDetails, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=problem.status,
        media_type="application/problem+json",
        headers=headers,
    )


def install_problem_handlers(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, error: ApiProblem) -> JSONResponse:
        return _response(
            request,
            ProblemDetails(
                type=error.problem_type,
                title=error.title,
                status=error.status,
                code=error.code,
                detail=error.detail,
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                errors=error.errors,
            ),
            error.headers,
        )

    @app.exception_handler(DatabaseUnavailable)
    async def database_problem_handler(
        request: Request,
        error: DatabaseUnavailable,
    ) -> JSONResponse:
        return _response(
            request,
            ProblemDetails(
                type="urn:research-asset-hub:problem:database-degraded",
                title="数据库尚未就绪",
                status=503,
                code="database_degraded",
                detail=str(error),
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
            ),
            {"Retry-After": "5"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_problem_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return _response(
            request,
            ProblemDetails(
                type="urn:research-asset-hub:problem:validation",
                title="请求参数无效",
                status=422,
                code="validation_error",
                detail="One or more request fields are invalid.",
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                errors=[
                    {
                        "location": list(item["loc"]),
                        "message": item["msg"],
                        "code": item["type"],
                    }
                    for item in error.errors()
                ],
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_problem_handler(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        titles = {
            404: "接口不存在",
            405: "请求方法不允许",
        }
        return _response(
            request,
            ProblemDetails(
                type="urn:research-asset-hub:problem:http",
                title=titles.get(error.status_code, "HTTP 请求失败"),
                status=error.status_code,
                code="http_error",
                detail=str(error.detail),
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
            ),
            dict(error.headers or {}),
        )

    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_problem_handler(
        request: Request,
        error: SQLAlchemyError,
    ) -> JSONResponse:
        return _response(
            request,
            ProblemDetails(
                type="urn:research-asset-hub:problem:database-operation",
                title="数据库操作失败",
                status=503,
                code="database_operation_failed",
                detail=f"{type(error).__name__}: database operation failed.",
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
            ),
            {"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def unexpected_problem_handler(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        return _response(
            request,
            ProblemDetails(
                type="urn:research-asset-hub:problem:internal",
                title="内部请求失败",
                status=500,
                code="internal_error",
                detail=f"{type(error).__name__}: request could not be completed.",
                instance=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
            ),
        )


def not_found(entity: str, identifier: object) -> ApiProblem:
    return ApiProblem(
        404,
        f"{entity}不存在",
        f"No {entity.lower()} exists for identifier {identifier}.",
        problem_type="urn:research-asset-hub:problem:not-found",
    )


def conflict(detail: str) -> ApiProblem:
    return ApiProblem(
        409,
        "状态冲突",
        detail,
        problem_type="urn:research-asset-hub:problem:conflict",
    )
