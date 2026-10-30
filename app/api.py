"""API 层：FastAPI 薄调度 + SSE 进度流。

只做「参数校验 -> 调用 LauncherService -> 返回结果/任务号」，
不含任何 MC 业务逻辑。挂载在 NiceGUI 底层的 FastAPI 应用上。
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

from fastapi import Body
from fastapi.responses import JSONResponse, StreamingResponse
from nicegui import app
from pydantic import BaseModel

from .services import LauncherService


class InstallRequest(BaseModel):
    version: str
    include_assets: bool = True


class LaunchRequest(BaseModel):
    version: str
    offline: bool = True
    username: str = "Steve"
    account_id: Optional[str] = None
    memory_mb: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    dry_run: bool = False


class ModInstallRequest(BaseModel):
    project_id: str
    source: str = "modrinth"
    name: str = ""
    game_version: Optional[str] = None
    loader: Optional[str] = None


class DeviceCodeRequest(BaseModel):
    device_code: str


class LoaderInstallRequest(BaseModel):
    loader: str
    game_version: str
    loader_version: str


class JavaTestRequest(BaseModel):
    path: str


class JavaInstallRequest(BaseModel):
    major: int


class ContentToggleRequest(BaseModel):
    kind: str
    filename: str
    enabled: bool
    version_id: Optional[str] = None


class ContentDeleteRequest(BaseModel):
    kind: str
    filename: str
    version_id: Optional[str] = None


class ContentImportRequest(BaseModel):
    kind: str
    path: str
    version_id: Optional[str] = None
    overwrite: bool = False


class ContentInstallRequest(BaseModel):
    kind: str
    project_id: str
    source: str = "modrinth"
    name: str = ""
    game_version: Optional[str] = None
    loader: Optional[str] = None
    version_id: Optional[str] = None


class ModpackRequest(BaseModel):
    path: str


class ModpackInstallRequest(BaseModel):
    path: str
    version_id: str


class ModpackExportRequest(BaseModel):
    version_id: str
    dest: str
    name: str = ""
    pack_version: str = "1.0"


class OfflineAccountRequest(BaseModel):
    name: str


class AccountIdRequest(BaseModel):
    account_id: str


class ExternalLoginRequest(BaseModel):
    server: str
    username: str
    password: str


class SaveDeleteRequest(BaseModel):
    name: str
    version_id: Optional[str] = None


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把引擎异常转成 400 JSON，避免把堆栈直接抛给前端。

    必须用 ``functools.wraps`` 保留原签名，否则 FastAPI 会把 ``*args/**kwargs``
    当成查询参数，接口全部失效。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - API 边界统一兜底
            return JSONResponse(status_code=400, content={"error": str(exc)})

    return wrapper


def register_api(service: LauncherService) -> None:
    # ------------------------------------------------------------------ 版本
    @app.get("/api/versions")
    @_guard
    def api_versions(snapshots: bool = False) -> dict:
        return {"versions": service.list_versions(include_snapshots=snapshots)}

    # ------------------------------------------------------------------ 安装
    @app.post("/api/install")
    @_guard
    def api_install(request: InstallRequest) -> dict:
        task = service.start_install(request.version, include_assets=request.include_assets)
        return {"task_id": task.id, "name": task.name}

    @app.get("/api/progress/{task_id}")
    def api_progress(task_id: str) -> StreamingResponse:
        return StreamingResponse(
            service.tasks.stream(task_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/tasks")
    @_guard
    def api_tasks() -> dict:
        return {"tasks": service.tasks.list_snapshots()}

    @app.get("/api/tasks/{task_id}")
    @_guard
    def api_task(task_id: str) -> dict:
        snapshot = service.tasks.snapshot(task_id)
        return snapshot or {"error": "task not found"}

    @app.post("/api/tasks/{task_id}/cancel")
    @_guard
    def api_task_cancel(task_id: str) -> dict:
        return {"cancelled": service.tasks.cancel(task_id)}

    # ------------------------------------------------------------------ 启动
    @app.post("/api/launch")
    @_guard
    def api_launch(request: LaunchRequest) -> dict:
        return service.launch(
            request.version,
            offline=request.offline,
            username=request.username,
            account_id=request.account_id,
            memory_mb=request.memory_mb,
            width=request.width,
            height=request.height,
            dry_run=request.dry_run,
        )

    # ------------------------------------------------------------------ 认证
    @app.post("/api/auth/start")
    @_guard
    def api_auth_start() -> dict:
        return service.auth_start()

    @app.post("/api/auth/poll")
    @_guard
    def api_auth_poll(request: DeviceCodeRequest) -> dict:
        return service.auth_poll(request.device_code)

    @app.post("/api/auth/complete")
    @_guard
    def api_auth_complete(request: DeviceCodeRequest) -> dict:
        return service.auth_complete(request.device_code)

    @app.post("/api/auth/refresh")
    @_guard
    def api_auth_refresh() -> dict:
        return service.auth_refresh()

    @app.get("/api/auth/status")
    @_guard
    def api_auth_status() -> dict:
        return {"account": service.account_info()}

    @app.post("/api/auth/logout")
    @_guard
    def api_auth_logout() -> dict:
        return service.auth_logout()

    # ------------------------------------------------------------------ Mod
    @app.get("/api/mods/search")
    @_guard
    def api_mods_search(
        query: str,
        source: Optional[str] = None,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        return {
            "results": service.search_mods(
                query,
                source=source,
                game_version=game_version,
                loader=loader,
                limit=limit,
            )
        }

    @app.post("/api/mods/install")
    @_guard
    def api_mods_install(request: ModInstallRequest) -> dict:
        task = service.start_mod_install(
            request.source,
            request.project_id,
            name=request.name,
            game_version=request.game_version,
            loader=request.loader,
        )
        return {"task_id": task.id, "name": task.name}

    # ------------------------------------------------------------------ 配置
    @app.get("/api/config")
    @_guard
    def api_config_get() -> dict:
        return service.config_view()

    @app.post("/api/config")
    @_guard
    def api_config_set(changes: dict = Body(...)) -> dict:
        return service.update_config(changes)

    @app.get("/api/logs")
    @_guard
    def api_logs() -> dict:
        logs, revision = service.tasks.global_logs()
        return {"logs": logs, "revision": revision}

    # --------------------------------------------------------------- 本地版本
    @app.get("/api/versions/local")
    @_guard
    def api_local_versions() -> dict:
        return {"versions": service.local_versions()}

    @app.get("/api/versions/{version_id}/settings")
    @_guard
    def api_version_settings(version_id: str) -> dict:
        return service.version_settings(version_id)

    @app.post("/api/versions/{version_id}/settings")
    @_guard
    def api_version_settings_set(version_id: str, changes: dict = Body(...)) -> dict:
        return service.update_version_settings(version_id, changes)

    # ---------------------------------------------------------------- 加载器
    @app.get("/api/loaders/{loader}/versions")
    @_guard
    def api_loader_versions(loader: str, game_version: str) -> dict:
        return {"versions": service.loader_versions(loader, game_version)}

    @app.post("/api/loaders/install")
    @_guard
    def api_loader_install(request: LoaderInstallRequest) -> dict:
        task = service.start_loader_install(
            request.loader, request.game_version, request.loader_version
        )
        return {"task_id": task.id, "name": task.name}

    # ------------------------------------------------------------------ Java
    @app.get("/api/java/scan")
    @_guard
    def api_java_scan() -> dict:
        return {"runtimes": service.java_scan()}

    @app.get("/api/java/available")
    @_guard
    def api_java_available(major: int) -> dict:
        return {"runtimes": service.java_available(major)}

    @app.post("/api/java/test")
    @_guard
    def api_java_test(request: JavaTestRequest) -> dict:
        return service.test_java(request.path)

    @app.post("/api/java/install")
    @_guard
    def api_java_install(request: JavaInstallRequest) -> dict:
        task = service.start_java_install(request.major)
        return {"task_id": task.id, "name": task.name}

    # ------------------------------------------------------------------ 内容
    @app.get("/api/content/list")
    @_guard
    def api_content_list(kind: str, version_id: Optional[str] = None) -> dict:
        return {"items": service.content_list(kind, version_id)}

    @app.get("/api/content/search")
    @_guard
    def api_content_search(
        kind: str,
        query: str,
        source: Optional[str] = None,
        game_version: Optional[str] = None,
        loader: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        return {
            "results": service.search_content(
                kind,
                query,
                source=source,
                game_version=game_version,
                loader=loader,
                limit=limit,
                offset=offset,
            )
        }

    @app.post("/api/content/toggle")
    @_guard
    def api_content_toggle(request: ContentToggleRequest) -> dict:
        return service.content_toggle(
            request.kind, request.filename, request.enabled, request.version_id
        )

    @app.post("/api/content/delete")
    @_guard
    def api_content_delete(request: ContentDeleteRequest) -> dict:
        return service.content_delete(request.kind, request.filename, request.version_id)

    @app.post("/api/content/import")
    @_guard
    def api_content_import(request: ContentImportRequest) -> dict:
        return service.content_import(
            request.kind,
            request.path,
            version_id=request.version_id,
            overwrite=request.overwrite,
        )

    @app.post("/api/content/install")
    @_guard
    def api_content_install(request: ContentInstallRequest) -> dict:
        task = service.start_content_install(
            request.kind,
            request.source,
            request.project_id,
            name=request.name,
            game_version=request.game_version,
            loader=request.loader,
            version_id=request.version_id,
        )
        return {"task_id": task.id, "name": task.name}

    # ------------------------------------------------------------ 存档与截图
    @app.get("/api/saves")
    @_guard
    def api_saves(version_id: Optional[str] = None) -> dict:
        return {"saves": service.list_saves(version_id)}

    @app.post("/api/saves/delete")
    @_guard
    def api_save_delete(request: SaveDeleteRequest) -> dict:
        return service.delete_save(request.name, request.version_id)

    @app.get("/api/screenshots")
    @_guard
    def api_screenshots(version_id: Optional[str] = None) -> dict:
        return {"screenshots": service.list_screenshots(version_id)}

    # ---------------------------------------------------------------- 整合包
    @app.post("/api/modpack/inspect")
    @_guard
    def api_modpack_inspect(request: ModpackRequest) -> dict:
        return service.inspect_modpack(request.path)

    @app.post("/api/modpack/install")
    @_guard
    def api_modpack_install(request: ModpackInstallRequest) -> dict:
        task = service.start_modpack_install(request.path, version_id=request.version_id)
        return {"task_id": task.id, "name": task.name}

    @app.post("/api/modpack/export")
    @_guard
    def api_modpack_export(request: ModpackExportRequest) -> dict:
        return service.export_modpack(
            request.version_id,
            request.dest,
            name=request.name,
            pack_version=request.pack_version,
        )

    # -------------------------------------------------------------- 崩溃分析
    @app.get("/api/crash/analyze")
    @_guard
    def api_crash_analyze(version_id: Optional[str] = None) -> dict:
        return service.analyze_crash(version_id)

    # ---------------------------------------------------------------- 多账户
    @app.get("/api/accounts")
    @_guard
    def api_accounts() -> dict:
        return {"accounts": service.accounts()}

    @app.post("/api/accounts/offline")
    @_guard
    def api_account_offline(request: OfflineAccountRequest) -> dict:
        return {"account": service.account_add_offline(request.name)}

    @app.post("/api/accounts/active")
    @_guard
    def api_account_active(request: AccountIdRequest) -> dict:
        return service.account_set_active(request.account_id)

    @app.post("/api/accounts/remove")
    @_guard
    def api_account_remove(request: AccountIdRequest) -> dict:
        return service.account_remove(request.account_id)

    @app.get("/api/accounts/skin")
    @_guard
    def api_account_skin(account_id: str) -> dict:
        return service.account_skin(account_id)

    @app.post("/api/accounts/external")
    @_guard
    def api_account_external(request: ExternalLoginRequest) -> dict:
        return {"account": service.external_login(request.server, request.username, request.password)}

    @app.post("/api/accounts/injector")
    @_guard
    def api_account_injector() -> dict:
        return service.download_injector()

    # ------------------------------------------------------------------ 镜像
    @app.get("/api/mirror")
    @_guard
    def api_mirror() -> dict:
        return service.mirror_status()

    @app.post("/api/mirror/probe")
    @_guard
    def api_mirror_probe() -> dict:
        return service.probe_mirror()

    # ------------------------------------------------------------------ 能力
    @app.get("/api/supports")
    @_guard
    def api_supports() -> dict:
        return service.supports()

    service.log(
        "API 已挂载：/api/versions、/api/install、/api/progress/{task_id}、/api/launch、"
        "/api/loaders/*、/api/java/*、/api/content/*、/api/mods/*、/api/accounts/*、"
        "/api/auth/*、/api/modpack/*、/api/crash/analyze、/api/mirror、/api/supports"
    )