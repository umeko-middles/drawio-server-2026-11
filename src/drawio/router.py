import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from .assets import EDITOR_CSP
from .model import FetchRow, NodeStore
from .schemas import Node, Validation
from .service import BLANK_XML, MAX_XML_BYTES, DiagramTooLarge, InvalidDiagram, validate_xml


WEB = Path(__file__).with_name("web")
PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def error(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers=PRIVATE)


def found(row):
    if row is None:
        raise error(404, "Draw.io node not found")
    return row


def required_version(request: Request) -> int:
    etag = request.headers.get("if-match")
    if etag is None:
        raise error(428, 'If-Match is required, for example "1"')
    match = re.fullmatch(r'"([1-9][0-9]{0,18})"', etag)
    if not match or int(match[1]) > 9223372036854775807:
        raise error(400, "If-Match must contain one quoted version number")
    return int(match[1])


async def read_diagram(request: Request, *, allow_empty=False) -> tuple[str, int]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_XML_BYTES:
            raise error(413, "Draw.io XML exceeds 4 MiB")
        body.extend(chunk)
    if allow_empty and not body:
        return BLANK_XML, 1
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {"application/xml", "text/xml"}:
        raise error(415, "Send Draw.io XML as application/xml or text/xml")
    try:
        return await run_in_threadpool(validate_xml, body)
    except DiagramTooLarge as exc:
        raise error(413, str(exc)) from exc
    except InvalidDiagram as exc:
        raise error(422, str(exc)) from exc


def xml_body(required=True):
    return {"requestBody": {"required": required, "content": {
        "application/xml": {"schema": {"type": "string"}},
        "text/xml": {"schema": {"type": "string"}},
    }}}


def create_router(*, current_owner, fetch_row: FetchRow, public_origin: str) -> APIRouter:
    """current_owner is a host FastAPI dependency returning an authenticated subject."""
    origin = urlsplit(public_origin)
    if (origin.scheme not in {"http", "https"} or not origin.hostname
            or origin.username is not None or public_origin != f"{origin.scheme}://{origin.netloc}"):
        raise ValueError("public_origin must be the browser origin without a trailing slash")

    store = NodeStore(fetch_row)
    router = APIRouter(prefix="/drawio", tags=["drawio"])
    editor_page = (WEB / "editor.html").read_text(encoding="utf-8")

    async def owner_key(owner=Depends(current_owner)) -> str:
        if not isinstance(owner, str) or not owner.strip():
            raise error(401, "An authenticated Draw.io owner is required")
        return owner

    async def same_origin(request: Request):
        if request.headers.getlist("origin") != [public_origin]:
            raise error(403, "A same-origin request is required")

    write_dependencies = [Depends(same_origin)]

    def node_response(row, response: Response):
        response.headers.update(PRIVATE)
        response.headers["ETag"] = f'"{row["version"]}"'
        return dict(row)

    def page(relative_base: str):
        return HTMLResponse(editor_page.replace("__DRAWIO_BASE__", relative_base), headers={
            **PRIVATE, "Content-Security-Policy": EDITOR_CSP, "Referrer-Policy": "no-referrer",
        })

    @router.get("/editor", response_class=HTMLResponse)
    async def editor(owner: str = Depends(owner_key)):
        return page("./")

    @router.get("/editor/{node_id}", response_class=HTMLResponse)
    async def node_editor(node_id: UUID, owner: str = Depends(owner_key)):
        found(await store.metadata(owner, node_id))
        return page("../")

    @router.get("/editor.js", include_in_schema=False)
    async def editor_script():
        return FileResponse(WEB / "editor.js", media_type="text/javascript", headers=PRIVATE)

    @router.post("/nodes", response_model=Node, status_code=201,
                 dependencies=write_dependencies, openapi_extra=xml_body(required=False))
    async def create(request: Request, response: Response, owner: str = Depends(owner_key)):
        xml, _ = await read_diagram(request, allow_empty=True)
        return node_response(await store.create(owner, xml), response)

    @router.get("/nodes/{node_id}", response_model=Node)
    async def metadata(node_id: UUID, response: Response, owner: str = Depends(owner_key)):
        return node_response(found(await store.metadata(owner, node_id)), response)

    @router.get("/nodes/{node_id}/xml", response_class=Response,
                responses={200: {"content": {"application/xml": {"schema": {"type": "string"}}}}})
    async def get_xml(node_id: UUID, owner: str = Depends(owner_key)):
        row = found(await store.read(owner, node_id))
        return Response(row["xml"], media_type="application/xml; charset=utf-8", headers={
            **PRIVATE, "ETag": f'"{row["version"]}"',
            "Content-Disposition": f'attachment; filename="{node_id}.drawio"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    @router.put("/nodes/{node_id}/xml", response_model=Node,
                dependencies=write_dependencies, openapi_extra=xml_body())
    async def put_xml(node_id: UUID, request: Request, response: Response, owner: str = Depends(owner_key)):
        version = required_version(request)
        xml, _ = await read_diagram(request)
        row = await store.update(owner, node_id, version, xml)
        if row is None:
            found(await store.metadata(owner, node_id))
            raise error(412, "Draw.io version has changed")
        return node_response(row, response)

    @router.delete("/nodes/{node_id}", status_code=204, response_class=Response,
                   dependencies=write_dependencies)
    async def delete(node_id: UUID, request: Request, owner: str = Depends(owner_key)):
        row = await store.delete(owner, node_id, required_version(request))
        if row is None:
            found(await store.metadata(owner, node_id))
            raise error(412, "Draw.io version has changed")
        return Response(status_code=204, headers=PRIVATE)

    @router.post("/validate", response_model=Validation,
                 dependencies=write_dependencies, openapi_extra=xml_body())
    async def validate(request: Request, response: Response, owner: str = Depends(owner_key)):
        _, pages = await read_diagram(request)
        response.headers.update(PRIVATE)
        return {"pages": pages}

    return router
