"""子节点（node）的 HTTP 接口。"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from pydantic import BaseModel, Field

from relay.config import RelayConfig
from relay.errors import RelayError, register_error_handlers
from relay.service import NodeService
from relay.signing import install_signature_guard
from relay.store import RelayStore

EXPIRY_INTERVAL = 10.0


class SessionRequest(BaseModel):
    name: str = Field(default="", max_length=32)


class CreateRoomRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    loader: str = Field(default="vanilla", max_length=64)
    max_players: int = Field(default=8, ge=2, le=16)
    local_port: int = Field(ge=1024, le=65535)
    mode: str = "stcp"
    provider: str = Field(default="aml", max_length=32)
    hidden: bool = False
    mods: list[dict] = Field(default_factory=list)
    mods_declared: bool = False


class LookupRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)


class JoinRequest(BaseModel):
    bind_port: int | None = Field(default=None, ge=1024, le=65535)


def create_node_app(
    config: RelayConfig,
    start_background: bool = True,
) -> FastAPI:
    """构建子节点应用；测试可关闭后台清理任务。"""
    store = RelayStore(config.db_path)
    service = NodeService(config, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(_expiry_loop(service)) if start_background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            store.close()

    app = FastAPI(title="AML Relay Node", version="1.0", lifespan=lifespan)
    app.state.service = service
    register_error_handlers(app)
    install_signature_guard(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "role": "node", "node_id": config.node_id}

    app.include_router(_build_router())
    return app


async def _expiry_loop(service: NodeService) -> None:
    """每 10 秒把心跳超时或已过期的房间标记为 expired。"""
    while True:
        await asyncio.sleep(EXPIRY_INTERVAL)
        await asyncio.to_thread(service.expire_rooms)


def _service(request: Request) -> NodeService:
    return request.app.state.service


def require_session(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """从 Authorization 头解析会话，失败返回 401 统一错误体。"""
    return _service(request).authenticate(authorization)


def _build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/meta")
    def meta(request: Request) -> dict:
        return _service(request).meta()

    @router.post("/auth/session")
    async def auth_session(payload: SessionRequest, request: Request) -> dict:
        return await asyncio.to_thread(_service(request).create_session, payload.name)

    @router.post("/rooms")
    def create_room(
        payload: CreateRoomRequest,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        client_ip = request.client.host if request.client else ""
        return _service(request).create_room(
            session,
            name=payload.name,
            version=payload.version,
            loader=payload.loader,
            max_players=payload.max_players,
            local_port=payload.local_port,
            mode=payload.mode,
            provider=payload.provider,
            hidden=payload.hidden,
            mods=payload.mods,
            mods_declared=payload.mods_declared,
            client_ip=client_ip,
        )

    @router.get("/rooms")
    def list_public_rooms(
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        return {"rooms": _service(request).list_public(session)}

    @router.post("/rooms/lookup")
    def lookup(
        payload: LookupRequest,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        client_ip = ""
        if request.client:
            client_ip = request.client.host
        return _service(request).lookup(payload.code, client_ip=client_ip)

    @router.post("/rooms/{room_id}/join")
    def join_room(
        room_id: str,
        payload: JoinRequest,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        return _service(request).join(session, room_id, payload.bind_port)

    @router.post("/rooms/{room_id}/heartbeat")
    def heartbeat(
        room_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        return _service(request).heartbeat(session, room_id)

    @router.post("/rooms/{room_id}/leave", status_code=204)
    def leave_room(
        room_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ) -> None:
        _service(request).leave(session, room_id)

    @router.delete("/rooms/{room_id}", status_code=204)
    def close_room(
        room_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ) -> None:
        _service(request).close(session, room_id)

    @router.get("/rooms/{room_id}/info")
    def room_info(
        room_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        return _service(request).room_info(session, room_id)

    @router.get("/rooms/{room_id}")
    def room_detail(
        room_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ) -> dict:
        return _service(request).detail(session, room_id)

    return router
