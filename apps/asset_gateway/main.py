from __future__ import annotations

import html
import mimetypes
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import Cookie, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from apps.api.config import ApiSettings, get_settings
from apps.api.database import DatabaseManager, DatabaseUnavailable
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from apps.asset_gateway.security import InvalidGatewayToken, verify_gateway_token
from apps.asset_gateway.theme import (
    THEME_STYLESHEET_PATH,
    THEME_VERSION,
    ThemeInjectionError,
    inject_theme_html,
    theme_etag,
)
from packages.domain.models import Representation, RepresentationMount, Source

COOKIE_NAME = "rah_gateway_session"
PRESENTATION_COOKIE_NAME = "rah_gateway_presentation"
_SENSITIVE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".sandbox-secrets",
        "auth.json",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "token.json",
    }
)
_HTML_REPRESENTATION_TYPES = frozenset({"html", "guide", "analysis", "html_document"})


def _attribute_enabled(representation: Representation, key: str) -> bool:
    value = (representation.attributes or {}).get(key)
    return value is True or (isinstance(value, str) and value.casefold() == "true")


def _html_policy(representation: Representation | None) -> str:
    if representation is None:
        return "static"
    configured = str((representation.attributes or {}).get("sandbox_policy") or "")
    if configured in {"static", "trusted_local", "network_required"}:
        return configured
    return "network_required" if representation.network_required else "static"


def _content_security_policy(representation: Representation | None) -> str:
    policy = _html_policy(representation)
    frame_ancestors = (
        "frame-ancestors http://127.0.0.1:8765 http://localhost:8765"
    )
    if policy == "network_required":
        return (
            "default-src 'self' data: blob: https: http:; "
            "img-src 'self' data: blob: https: http:; "
            "style-src 'self' 'unsafe-inline' https: http:; "
            "script-src 'self' 'unsafe-inline' https: http:; "
            "connect-src 'self' https: http:; "
            "object-src 'none'; base-uri 'self'; "
            f"{frame_ancestors}"
        )
    if policy == "trusted_local":
        return (
            "default-src 'self' data: blob:; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; "
            f"{frame_ancestors}"
        )
    return (
        "default-src 'self' data: blob:; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'none'; "
        "connect-src 'none'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; "
        f"{frame_ancestors}"
    )


def _problem(status: int, title: str, detail: str, path: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "urn:research-asset-hub:problem:asset-gateway",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
        },
        status_code=status,
        media_type="application/problem+json",
    )


def _join_ref(base: str, remainder: str) -> str:
    combined = PurePosixPath(base.replace("\\", "/"))
    if remainder:
        combined = combined.joinpath(PurePosixPath(remainder.replace("\\", "/")))
    if combined.is_absolute() or ".." in combined.parts:
        raise UnsafeAssetPath("Requested asset path contains traversal.")
    return combined.as_posix()


def _mount_match(mount_path: str, request_path: str) -> str | None:
    prefix = mount_path.replace("\\", "/").strip("/")
    target = request_path.replace("\\", "/").strip("/")
    if not prefix:
        return target
    if target == prefix:
        return ""
    if target.startswith(prefix + "/"):
        return target[len(prefix) + 1 :]
    return None


def _is_sensitive_path(path: Path) -> bool:
    for part in path.parts:
        normalized = part.casefold()
        if normalized.startswith(".") or normalized in _SENSITIVE_NAMES:
            return True
    return False


def _resolve_registered_file(
    database: DatabaseManager,
    registry: SourceRegistry,
    representation_id: UUID,
    request_path: str,
) -> tuple[Representation, Path]:
    with database.session() as session:
        representation = session.get(Representation, representation_id)
        if representation is None or representation.deleted_at is not None:
            raise FileNotFoundError("Unknown representation.")
        strict_mounts = _attribute_enabled(representation, "strict_mounts")
        mounts = list(
            session.scalars(
                select(RepresentationMount).where(
                    RepresentationMount.representation_id == representation.id,
                    RepresentationMount.deleted_at.is_(None),
                )
            ).all()
        )
        mounts.sort(
            key=lambda item: (item.priority, len(item.mount_path)),
            reverse=True,
        )
        for mount in mounts:
            remainder = _mount_match(mount.mount_path, request_path)
            if remainder is None:
                continue
            source = session.get(Source, mount.source_id)
            if source is None or not source.enabled:
                continue
            mount_ref = PurePosixPath(mount.path_ref.replace("\\", "/"))
            mount_prefix = mount.mount_path.replace("\\", "/").strip("/")
            if representation.materialization_kind == "file" and strict_mounts:
                if not mount_prefix:
                    if remainder not in {"", mount_ref.as_posix()}:
                        continue
                    registered_ref = mount_ref.as_posix()
                else:
                    registered_ref = (
                        mount_ref.as_posix()
                        if not remainder
                        else _join_ref(mount_ref.as_posix(), remainder)
                    )
            elif representation.materialization_kind == "file" and (
                not remainder or remainder == mount_ref.as_posix()
            ):
                registered_ref = mount_ref.as_posix()
            elif representation.materialization_kind == "file":
                # File-backed HTML is exposed at its registered virtual path so
                # the browser can normalize ``../`` references while retaining
                # the signed representation prefix. The resulting request path
                # is then resolved directly under the registered root.
                registered_ref = _join_ref(".", remainder)
            else:
                registered_ref = _join_ref(mount_ref.as_posix(), remainder)
            candidate = registry.resolve(
                source.alias,
                mount.root_key,
                registered_ref,
            )
            if candidate.is_dir() and (
                representation.entrypoint
                or representation.representation_type in _HTML_REPRESENTATION_TYPES
            ):
                registered_ref = _join_ref(
                    registered_ref,
                    (
                        representation.entrypoint
                        if not remainder and representation.entrypoint
                        else "index.html"
                    ),
                )
                index_candidate = registry.resolve(
                    source.alias,
                    mount.root_key,
                    registered_ref,
                )
                if index_candidate.is_file():
                    candidate = index_candidate
            return representation, candidate
        if strict_mounts:
            raise FileNotFoundError(
                "Strict-mount representation does not expose this asset path."
            )
        if representation.source_id is None or representation.path_ref is None:
            raise FileNotFoundError("Representation has no registered local source.")
        source = session.get(Source, representation.source_id)
        if source is None or not source.enabled:
            raise FileNotFoundError("Representation source is unavailable.")
        configured = PurePosixPath(representation.path_ref.replace("\\", "/"))
        is_directory = representation.materialization_kind in {"directory", "bundle"}
        if is_directory:
            if request_path:
                remainder = request_path
            elif representation.entrypoint:
                remainder = representation.entrypoint
            elif representation.representation_type in _HTML_REPRESENTATION_TYPES:
                remainder = "index.html"
            else:
                remainder = ""
            registered_ref = _join_ref(configured.as_posix(), remainder)
        else:
            if request_path:
                if representation.representation_type not in {"html", "guide", "analysis"}:
                    raise FileNotFoundError("This representation has no child resources.")
                if request_path == configured.as_posix():
                    registered_ref = configured.as_posix()
                else:
                    registered_ref = _join_ref(".", request_path)
            else:
                registered_ref = configured.as_posix()
        candidate = registry.resolve(
            source.alias,
            representation.root_key,
            registered_ref,
        )
        if candidate.is_dir() and (
            representation.entrypoint
            or representation.representation_type in _HTML_REPRESENTATION_TYPES
        ):
            registered_ref = _join_ref(registered_ref, "index.html")
            index_candidate = registry.resolve(
                source.alias,
                representation.root_key,
                registered_ref,
            )
            if index_candidate.is_file():
                candidate = index_candidate
        return representation, candidate


def _directory_listing(
    representation_id: UUID,
    directory: Path,
    asset_path: str,
) -> HTMLResponse:
    entries = [
        entry
        for entry in directory.iterdir()
        if not entry.name.startswith(".") and not _is_sensitive_path(entry)
    ]
    entries.sort(key=lambda entry: (not entry.is_dir(), entry.name.casefold()))
    prefix = f"/assets/{representation_id}/"
    normalized_path = asset_path.strip("/")
    parent_link = ""
    if normalized_path:
        parent = PurePosixPath(normalized_path).parent.as_posix()
        parent_target = prefix if parent == "." else f"{prefix}{quote(parent)}/"
        parent_link = f'<li><a href="{html.escape(parent_target)}">../</a></li>'
    rows = []
    for entry in entries[:500]:
        name = entry.name + ("/" if entry.is_dir() else "")
        relative = (
            PurePosixPath(normalized_path).joinpath(entry.name).as_posix()
            if normalized_path
            else entry.name
        )
        href = f"{prefix}{quote(relative)}" + ("/" if entry.is_dir() else "")
        rows.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>代码目录</title>"
        "<style>body{font:15px/1.6 system-ui;margin:2rem;max-width:72rem}"
        "h1{font-size:1.25rem}ul{list-style:none;padding:0}"
        "li{border-bottom:1px solid #ddd;padding:.35rem 0}"
        "a{color:#155e75;text-decoration:none}a:hover{text-decoration:underline}</style>"
        "</head><body><h1>已登记代码目录</h1><ul>"
        f"{parent_link}{''.join(rows)}</ul></body></html>"
    )
    return HTMLResponse(document)


def _etag_matches(request: Request, etag: str) -> bool:
    supplied = request.headers.get("if-none-match")
    if supplied is None:
        return False
    return any(
        candidate.strip() in {"*", etag, f"W/{etag}"}
        for candidate in supplied.split(",")
    )


def _themed_html_response(file_path: Path, request: Request) -> Response:
    try:
        themed_content = inject_theme_html(file_path.read_bytes())
    except OSError as error:
        raise ThemeInjectionError(
            f"The registered HTML could not be read: {type(error).__name__}."
        ) from error
    etag = theme_etag(themed_content)
    headers = {
        "ETag": etag,
        "Content-Length": str(len(themed_content)),
        "X-Research-Hub-Theme-Version": THEME_VERSION,
    }
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=b"" if request.method == "HEAD" else themed_content,
        media_type="text/html; charset=utf-8",
        headers=headers,
    )


def create_app(
    settings: ApiSettings | None = None,
    *,
    database: DatabaseManager | None = None,
    registry: SourceRegistry | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    database_manager = database or DatabaseManager(app_settings)
    owns_database = database is None
    source_registry = registry or SourceRegistry.load(app_settings.sources_config_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_database:
            database_manager.dispose()

    application = FastAPI(
        title="Research Asset Hub Asset Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.database = database_manager
    application.state.registry = source_registry
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            app_settings.web_origin,
            app_settings.web_origin.replace("127.0.0.1", "localhost"),
        ],
        allow_credentials=True,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["Range", "If-None-Match", "If-Modified-Since"],
        expose_headers=[
            "Accept-Ranges",
            "Content-Length",
            "Content-Range",
            "Content-Type",
            "ETag",
            "Last-Modified",
            "X-Research-Hub-Presentation",
            "X-Research-Hub-Theme-Fallback",
            "X-Research-Hub-Theme-Version",
        ],
    )

    @application.exception_handler(DatabaseUnavailable)
    async def unavailable_handler(request: Request, error: DatabaseUnavailable) -> JSONResponse:
        return _problem(503, "数据库尚未就绪", str(error), request.url.path)

    @application.middleware("http")
    async def add_isolation_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        representation = getattr(request.state, "representation", None)
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Security-Policy"] = _content_security_policy(
            representation if isinstance(representation, Representation) else None
        )
        if isinstance(representation, Representation):
            response.headers["X-Research-Hub-HTML-Policy"] = _html_policy(
                representation
            )
        presentation = getattr(request.state, "presentation", None)
        if presentation in {"hub", "original"}:
            response.headers["X-Research-Hub-Presentation"] = presentation
        theme_fallback = getattr(request.state, "theme_fallback", None)
        if isinstance(theme_fallback, str) and theme_fallback:
            response.headers["X-Research-Hub-Theme-Fallback"] = theme_fallback
        return response

    @application.get("/health")
    def health() -> dict[str, Any]:
        readiness = database_manager.check(force=True)
        secret_ready = len(app_settings.gateway_secret) >= 32
        status = "healthy" if readiness.status == "ready" and secret_ready else "degraded"
        return {
            "service": "research-asset-hub-asset-gateway",
            "status": status,
            "database": readiness.status,
            "signing": "ready" if secret_ready else "unconfigured",
            "registered_roots": len(source_registry.describe()),
        }

    @application.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "research-asset-hub-asset-gateway",
            "status": "ready",
            "detail": "Only registered representation IDs with signed sessions are served.",
        }

    @application.get(f"/theme/{THEME_VERSION}.css")
    def theme_stylesheet() -> Response:
        return FileResponse(
            THEME_STYLESHEET_PATH,
            media_type="text/css",
            filename=None,
            content_disposition_type="inline",
        )

    @application.get("/session/{representation_id}")
    def start_session(
        representation_id: UUID,
        request: Request,
        token: str = Query(min_length=20, max_length=4096),
        presentation: str = Query(
            "original",
            pattern="^(hub|original)$",
        ),
    ) -> Response:
        try:
            verify_gateway_token(
                app_settings.gateway_secret,
                token,
                representation_id=representation_id,
            )
        except InvalidGatewayToken as error:
            return _problem(403, "资产访问令牌无效", str(error), request.url.path)
        try:
            with database_manager.session() as session:
                representation = session.get(Representation, representation_id)
                if representation is None or representation.deleted_at is not None:
                    raise FileNotFoundError
        except FileNotFoundError:
            return _problem(
                404,
                "表示层不存在",
                "No registered representation exists for this ID.",
                request.url.path,
            )
        if representation.availability != "available":
            return _problem(
                409,
                "表示层不可打开",
                "Only representations with availability=available can start a session.",
                request.url.path,
            )
        target = f"/assets/{representation_id}/"
        if (
            representation.materialization_kind == "file"
            and representation.representation_type in _HTML_REPRESENTATION_TYPES
            and representation.path_ref
        ):
            virtual_path = PurePosixPath(
                representation.path_ref.replace("\\", "/")
            )
            if virtual_path.is_absolute() or ".." in virtual_path.parts:
                return _problem(
                    403,
                    "表示层路径被拒绝",
                    "The registered representation path is unsafe.",
                    request.url.path,
                )
            target = (
                f"/assets/{representation_id}/"
                f"{quote(virtual_path.as_posix(), safe='/')}"
            )
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=app_settings.gateway_session_seconds,
            httponly=True,
            secure=False,
            samesite="strict",
            path=f"/assets/{representation_id}",
        )
        response.set_cookie(
            PRESENTATION_COOKIE_NAME,
            presentation,
            max_age=app_settings.gateway_session_seconds,
            httponly=True,
            secure=False,
            samesite="strict",
            path=f"/assets/{representation_id}",
        )
        return response

    async def serve_asset(
        representation_id: UUID,
        request: Request,
        asset_path: str,
        gateway_session: str | None,
        gateway_presentation: str | None,
    ) -> Response:
        if request.method not in {"GET", "HEAD"}:
            return _problem(
                405,
                "方法不允许",
                "The asset gateway is read-only.",
                request.url.path,
            )
        try:
            if gateway_session is None:
                raise InvalidGatewayToken("A signed gateway session is required.")
            verify_gateway_token(
                app_settings.gateway_secret,
                gateway_session,
                representation_id=representation_id,
            )
            representation, file_path = _resolve_registered_file(
                database_manager,
                source_registry,
                representation_id,
                asset_path,
            )
            request.state.representation = representation
            requested_presentation = (
                gateway_presentation
                if gateway_presentation in {"hub", "original"}
                else "original"
            )
            if (
                requested_presentation == "hub"
                and representation.representation_type
                not in _HTML_REPRESENTATION_TYPES
            ):
                request.state.presentation = "original"
                request.state.theme_fallback = (
                    "Hub presentation applies only to registered HTML representations."
                )
            else:
                request.state.presentation = requested_presentation
        except InvalidGatewayToken as error:
            return _problem(403, "资产访问会话无效", str(error), request.url.path)
        except UnsafeAssetPath as error:
            return _problem(403, "资产路径被拒绝", str(error), request.url.path)
        except FileNotFoundError:
            return _problem(
                404,
                "资产文件不存在",
                "The registered representation path does not exist.",
                request.url.path,
            )
        if _is_sensitive_path(file_path):
            return _problem(
                403,
                "敏感文件被拒绝",
                "The asset gateway does not expose credential or environment files.",
                request.url.path,
            )
        if file_path.is_dir():
            if representation.materialization_kind not in {"directory", "bundle"}:
                return _problem(
                    404,
                    "资产文件不存在",
                    "This file representation does not expose a directory listing.",
                    request.url.path,
                )
            if not request.url.path.endswith("/"):
                target = request.url.path + "/"
                if request.url.query:
                    target = f"{target}?{request.url.query}"
                return RedirectResponse(target, status_code=307)
            if request.method == "HEAD":
                return Response(status_code=200, media_type="text/html")
            try:
                return _directory_listing(
                    representation_id,
                    file_path,
                    asset_path,
                )
            except OSError:
                return _problem(
                    404,
                    "资产目录不可用",
                    "The registered directory cannot be listed.",
                    request.url.path,
                )
        if not file_path.is_file():
            return _problem(
                404,
                "资产文件不存在",
                "The registered representation path does not exist.",
                request.url.path,
            )
        media_type = (
            representation.mime_type if not asset_path else mimetypes.guess_type(file_path.name)[0]
        )
        normalized_media_type = (media_type or "").split(";", 1)[0].casefold()
        if (
            request.state.presentation == "hub"
            and representation.representation_type in _HTML_REPRESENTATION_TYPES
            and normalized_media_type == "text/html"
        ):
            try:
                return _themed_html_response(file_path, request)
            except ThemeInjectionError as error:
                request.state.presentation = "original"
                request.state.theme_fallback = str(error)
        return FileResponse(
            file_path,
            media_type=media_type,
            filename=None,
            content_disposition_type="inline",
        )

    @application.api_route(
        "/assets/{representation_id}",
        methods=["GET", "HEAD"],
    )
    async def asset_root(
        representation_id: UUID,
        request: Request,
        gateway_session: str | None = Cookie(None, alias=COOKIE_NAME),
        gateway_presentation: str | None = Cookie(
            None,
            alias=PRESENTATION_COOKIE_NAME,
        ),
    ) -> Response:
        return await serve_asset(
            representation_id,
            request,
            "",
            gateway_session,
            gateway_presentation,
        )

    @application.api_route(
        "/assets/{representation_id}/{asset_path:path}",
        methods=["GET", "HEAD"],
    )
    async def asset_child(
        representation_id: UUID,
        asset_path: str,
        request: Request,
        gateway_session: str | None = Cookie(None, alias=COOKIE_NAME),
        gateway_presentation: str | None = Cookie(
            None,
            alias=PRESENTATION_COOKIE_NAME,
        ),
    ) -> Response:
        return await serve_asset(
            representation_id,
            request,
            asset_path,
            gateway_session,
            gateway_presentation,
        )

    return application


app = create_app()


def run() -> None:
    import uvicorn

    host = os.getenv("HUB_ASSET_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("HUB_ASSET_GATEWAY_PORT", "8766"))
    uvicorn.run("apps.asset_gateway.main:app", host=host, port=port)


if __name__ == "__main__":
    run()
