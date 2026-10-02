"""Read-only MCP boundaries tested without touching a real library or source files."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from apps.mcp_server.client import MAX_IMAGE_BYTES, MAX_JSON_BYTES, HubClient, local_api_url
from apps.mcp_server.server import code_path, create_server

ASSET = "11111111-1111-4111-8111-111111111111"
REP = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def server_for(handler: Any) -> FastMCP:
    return create_server(HubClient(transport=httpx.MockTransport(handler)))


async def invoke(server: FastMCP, name: str, args: dict[str, Any]) -> Any:
    # Use SDK's argument validation without its wire conversion; the stdio test covers the wire.
    return await server._tool_manager.call_tool(name, args)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api/v1",
        "http://192.168.1.3/api/v1",
        "http://0.0.0.0/api/v1",
        "http://127.0.0.1/api/v1?x=1",
        "http://user:password@127.0.0.1/api/v1",
        "http://127.0.0.1/other",
        "file:///api/v1",
        "http://[::]/api/v1",
    ],
)
def test_remote_or_ambiguous_base_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        local_api_url(url)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:8877/api/v1", "http://127.0.0.1:8877/api/v1/"),
        ("http://[::1]:8877/api/v1/", "http://[::1]:8877/api/v1/"),
    ],
)
def test_local_base_url(url: str, expected: str) -> None:
    assert local_api_url(url) == expected


async def test_exactly_seven_tools_have_read_only_annotations_and_bounds() -> None:
    tools = await server_for(lambda request: httpx.Response(200, json={})).list_tools()
    assert {tool.name for tool in tools} == {
        "search_assets",
        "get_paper",
        "read_evidence",
        "get_figure",
        "read_code",
        "get_methodology",
        "library_status",
    }
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    search = next(tool for tool in tools if tool.name == "search_assets")
    assert search.inputSchema["properties"]["limit"]["maximum"] == 20
    assert search.inputSchema["properties"]["asset_type"]["anyOf"][0]["enum"] == [
        "paper",
        "repository",
        "figure",
        "writing_pattern",
        "code_pattern",
    ]


async def test_search_preserves_provenance_and_uses_get_only() -> None:
    seen = []
    payload = {"items": [{"source_type": "analysis", "quality_status": "pending_review"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    result = await invoke(
        server_for(handler),
        "search_assets",
        {
            "query": "视觉导航",
            "domain": "vision-language-navigation",
            "source_type": "analysis",
        },
    )
    assert result == payload
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/knowledge/search"
    assert seen[0].url.params["source_type"] == "analysis"


@pytest.mark.parametrize("asset_type", ["figure", "writing_pattern", "code_pattern"])
async def test_search_derived_assets_uses_the_same_knowledge_route(asset_type: str) -> None:
    payload = {"items": [{"asset_type": asset_type, "quality_status": "pending_review"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/knowledge/search"
        assert request.url.params["asset_type"] == asset_type
        return httpx.Response(200, json=payload)

    result = await invoke(
        server_for(handler),
        "search_assets",
        {
            "query": "evidence-backed pattern",
            "asset_type": asset_type,
        },
    )
    assert result == payload


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("search_assets", {"query": "x", "limit": 21}),
        ("search_assets", {"query": "x" * 501}),
        ("search_assets", {"query": "  "}),
        ("get_paper", {"paper_id": "../arbitrary"}),
        ("get_figure", {"figure_id": "../figure"}),
        ("read_evidence", {"paper_id": ASSET, "max_chars": 16001}),
        ("read_evidence", {"paper_id": ASSET, "page_start": 2, "page_end": 1}),
        ("read_evidence", {"paper_id": ASSET, "offset": -1}),
        ("read_code", {"asset_id": ASSET, "path": "x" * 513}),
        ("read_code", {"asset_id": ASSET, "line_start": 3, "line_end": 1}),
        ("get_methodology", {"domain": "../bad", "kind": "writing"}),
    ],
)
async def test_invalid_arguments_fail_before_http(tool: str, arguments: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid input must not reach HTTP")

    with pytest.raises(ToolError):
        await invoke(server_for(handler), tool, arguments)


async def test_evidence_request_preserves_source_and_range() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/knowledge/papers/{ASSET}/evidence"
        assert dict(request.url.params) == {
            "source_type": "original",
            "page_start": "2",
            "page_end": "3",
            "representation_id": REP,
            "offset": "10",
            "max_chars": "400",
        }
        return httpx.Response(200, json={"citation": {"page": 2}, "text": "正文"})

    result = await invoke(
        server_for(handler),
        "read_evidence",
        {
            "paper_id": ASSET,
            "representation_id": REP,
            "page_start": 2,
            "page_end": 3,
            "offset": 10,
            "max_chars": 400,
        },
    )
    assert result["citation"]["page"] == 2


@pytest.mark.parametrize("candidates", [False, True])
async def test_methodology_requires_explicit_candidate_opt_in(candidates: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["include_candidates"] == str(candidates).lower()
        return httpx.Response(200, json={"items": []})

    await invoke(
        server_for(handler),
        "get_methodology",
        {
            "domain": "image-editing",
            "kind": "figure",
            "include_candidates": candidates,
        },
    )


@pytest.mark.parametrize(
    "path",
    [
        "../x.py",
        "/x.py",
        "C:/x.py",
        "src//x.py",
        "src/./x.py",
        ".git/config",
        "src/.env",
        "src/.private/x.py",
        "src/node_modules/x.js",
        "venv/lib/x.py",
        "src/credentials.json",
        "src/id_rsa",
        "src/private.pem",
        "src/x.py:secret",
        "src/x.py\x00",
        "src /x.py",
        "src./x.py",
        "CON.py",
        "src/nul",
        "src/LPT1.txt",
        "src/*.py",
    ],
)
def test_every_code_path_component_is_checked(path: str) -> None:
    with pytest.raises(ToolError):
        code_path(path)


def code_representation(identifier: str = REP) -> dict[str, Any]:
    return {
        "id": identifier,
        "asset_id": ASSET,
        "representation_type": "repository",
        "availability": "available",
        "materialization_kind": "directory",
        "local_available": True,
    }


async def test_code_verifies_membership_and_returns_bounded_line_context() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/representations"):
            return httpx.Response(200, json={"items": [code_representation()]})
        assert request.url.path == f"/api/v1/representations/{REP}/code/file"
        assert request.url.params["path"] == "src/main.py"
        return httpx.Response(
            200,
            json={
                "representation_id": REP,
                "path": "src/main.py",
                "language": "python",
                "previewable": True,
                "line_count": 4,
                "content": "one\ntwo\nthree\nfour\n",
                "truncated": False,
            },
        )

    result = await invoke(
        server_for(handler),
        "read_code",
        {
            "asset_id": ASSET,
            "path": "src/main.py",
            "line_start": 2,
            "line_end": 3,
        },
    )
    assert result["content"] == "two\nthree\n"
    assert result["line_start"] == 2 and result["line_end"] == 3
    assert result["truncated"] is True
    assert "#L2" in result["citation"]
    assert all(request.method == "GET" for request in seen)


async def test_foreign_code_representation_is_rejected_without_file_read() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [code_representation()]})

    with pytest.raises(ToolError, match="not an available local"):
        await invoke(
            server_for(handler),
            "read_code",
            {
                "asset_id": ASSET,
                "representation_id": OTHER,
                "path": "main.py",
            },
        )
    assert len(seen) == 1


async def test_multiple_code_representations_require_selection() -> None:
    server = server_for(
        lambda request: httpx.Response(
            200,
            json={
                "items": [code_representation(), code_representation(OTHER)],
            },
        )
    )
    with pytest.raises(ToolError, match="Several local"):
        await invoke(server, "read_code", {"asset_id": ASSET})


async def test_code_tree_filters_sensitive_entries_and_limits_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/representations"):
            return httpx.Response(200, json={"items": [code_representation()]})
        assert request.url.path.endswith("/code/tree")
        assert request.url.params["path"] == "src"
        return httpx.Response(
            200,
            json={
                "entries": [{"path": "src/.env"}, {"path": "src/one.py"}, {"path": "src/two.py"}],
                "truncated": False,
            },
        )

    result = await invoke(
        server_for(handler),
        "read_code",
        {
            "asset_id": ASSET,
            "path": "src/",
            "limit": 1,
        },
    )
    assert result["entries"] == [{"path": "src/one.py"}]
    assert result["truncated"] is True


async def test_figure_returns_image_content_and_source_json() -> None:
    png = b"\x89PNG\r\n\x1a\npreview"
    metadata = {"id": f"{ASSET}-fig-1", "page": 4, "quality_status": "pending_review"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/image"):
            assert request.url.params["preview"] == "true"
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(200, json=metadata)

    result = await invoke(server_for(handler), "get_figure", {"figure_id": metadata["id"]})
    assert isinstance(result, CallToolResult)
    assert result.structuredContent == metadata
    assert result.content[1].type == "image"
    assert base64.b64decode(result.content[1].data) == png
    assert result.content[1].mimeType == "image/png"


@pytest.mark.parametrize(
    "mime, content",
    [
        ("text/html", b"<script>bad</script>"),
        ("image/svg+xml", b"<svg/>"),
        ("image/png", b"not really png"),
        ("image/jpeg", b"\x89PNG\r\n\x1a\n"),
    ],
)
async def test_figure_mime_and_signature_are_checked(mime: str, content: bytes) -> None:
    client = HubClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=content,
                headers={"content-type": mime},
            )
        )
    )
    with pytest.raises(ToolError, match="PNG or JPEG"):
        await client.get_image("knowledge/figures/example/image")


@pytest.mark.parametrize("status", [302, 404, 409, 422, 500])
async def test_http_errors_are_explicit_and_never_expose_body(status: int) -> None:
    client = HubClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"private": "secret value"},
                headers={"location": "http://example.com"},
            )
        )
    )
    with pytest.raises(ToolError, match=f"HTTP {status}") as error:
        await client.get_json("knowledge/status")
    assert "secret value" not in str(error.value)


@pytest.mark.parametrize(
    "exception, text",
    [
        (httpx.ConnectError("connection refused"), "Start Hub yourself"),
        (httpx.ReadTimeout("read timeout"), "timed out"),
    ],
)
async def test_connection_errors_do_not_start_hub(exception: Exception, text: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(ToolError, match=text):
        await HubClient(transport=httpx.MockTransport(handler)).get_json("knowledge/status")


@pytest.mark.parametrize("image, size", [(False, MAX_JSON_BYTES), (True, MAX_IMAGE_BYTES)])
async def test_response_size_is_bounded(image: bool, size: int) -> None:
    client = HubClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"x" * (size + 1),
                headers={"content-type": "image/png" if image else "application/json"},
            )
        )
    )
    with pytest.raises(ToolError, match="allowed size"):
        if image:
            await client.get_image("knowledge/figures/example/image")
        else:
            await client.get_json("knowledge/status")
