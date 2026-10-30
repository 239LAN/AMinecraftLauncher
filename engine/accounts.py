"""多账户管理 + 外置登录（Authlib-Injector / Yggdrasil）+ 皮肤服务。

设计要点：
- 账户「元数据」（id / 类型 / 名称 / UUID / 头像等）以 JSON 明文保存，
  便于界面快速列出账户；「令牌」只进系统凭据管理器（``secrets``），
  不落明文配置。
- 复用 :class:`engine.auth_client.AuthResult` 作为令牌载体，不重复定义。
- 外置登录按 Yggdrasil 协议实现，配合 authlib-injector 的
  ``-javaagent`` 参数使用。
"""

from __future__ import annotations

import base64
import json
import re
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import net
from .auth_client import AuthResult
from .models import Account

# --------------------------------------------------------------------------- 常量
MOJANG_UUID_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
SESSIONSERVER_PROFILE_URL = (
    "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
)
CRAFATAR_AVATAR_URL = "https://crafatar.com/avatars/{uuid}?size={size}&overlay"
CRAFATAR_BODY_URL = "https://crafatar.com/renders/body/{uuid}?size={size}"
AUTHLIB_INJECTOR_META = "https://authlib-injector.yushi.moe/artifact/latest.json"

# 标准 8-4-4-4-12 UUID 与 32 位无横线 UUID。
_STANDARD_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class AccountError(Exception):
    """账户相关的可预期失败（缺账户、缺令牌、外置登录被拒等）。"""


# ------------------------------------------------------------------------ 数据模型
@dataclass
class StoredAccount:
    """账户清单里的一条元数据（不含令牌）。

    - ``kind`` 为 ``offline`` / ``microsoft`` / ``external``；
    - ``id`` 唯一：微软/外置用 uuid，离线用 ``offline:{name}``；
    - ``avatar`` 为头像 URL，可为空，由调用方用 :class:`SkinService` 补全；
    - ``server`` 仅外置登录使用，记录其 API 根地址。
    """

    id: str
    kind: str  # offline / microsoft / external
    name: str
    uuid: str = ""
    avatar: str = ""
    server: str = ""
    last_used: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "uuid": self.uuid,
            "avatar": self.avatar,
            "server": self.server,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoredAccount":
        return cls(
            id=data.get("id", ""),
            kind=data.get("kind", "offline"),
            name=data.get("name", ""),
            uuid=data.get("uuid", ""),
            avatar=data.get("avatar", ""),
            server=data.get("server", ""),
            last_used=float(data.get("last_used") or 0.0),
        )


# ------------------------------------------------------------------------ 账户清单
def _token_key(account_id: str) -> str:
    """令牌在 secrets 中的键名。"""
    return f"account:{account_id}"


class AccountStore:
    """多账户清单：元数据存 JSON，令牌只存 secrets（keyring）。

    JSON 文件结构为 ``{"active": "<id>", "accounts": [ ... ]}``；
    为兼容旧数据，load 也接受裸列表（此时无激活账户）。
    """

    def __init__(self, path: Path, secrets: Any) -> None:
        self.path = Path(path)
        self.secrets = secrets
        self._accounts: list[StoredAccount] = []
        self._active_id: str = ""
        self.load()

    # ---------------------------------------------------------------- 持久化
    def load(self) -> list[StoredAccount]:
        """从 JSON 读取账户清单，返回账户列表（同时刷新内部状态）。"""
        self._accounts = []
        self._active_id = ""
        if not self.path.exists():
            return list(self._accounts)

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return list(self._accounts)

        if isinstance(raw, dict):
            self._active_id = str(raw.get("active") or "")
            items = raw.get("accounts") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []

        self._accounts = [
            StoredAccount.from_dict(item) for item in items if isinstance(item, dict)
        ]
        return list(self._accounts)

    def save(self) -> None:
        """把账户清单写回 JSON（原子性由调用方按需保证）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self._active_id,
            "accounts": [account.as_dict() for account in self._accounts],
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------ 增删查
    def add(self, account: StoredAccount) -> StoredAccount:
        """新增账户；按 ``id`` 去重，已存在则整体更新字段。"""
        for index, existing in enumerate(self._accounts):
            if existing.id == account.id:
                self._accounts[index] = account
                self.save()
                return account
        self._accounts.append(account)
        self.save()
        return account

    def remove(self, account_id: str) -> None:
        """移除账户，并清掉 secrets 里对应的令牌。"""
        self._accounts = [a for a in self._accounts if a.id != account_id]
        if self._active_id == account_id:
            self._active_id = ""
        self.secrets.delete(_token_key(account_id))
        self.save()

    def get(self, account_id: str) -> Optional[StoredAccount]:
        for account in self._accounts:
            if account.id == account_id:
                return account
        return None

    # ------------------------------------------------------------------ 激活态
    def set_active(self, account_id: str) -> None:
        self._active_id = account_id
        self.save()

    def active(self) -> Optional[StoredAccount]:
        if not self._active_id:
            return None
        return self.get(self._active_id)

    # -------------------------------------------------------------------- 令牌
    def set_tokens(self, account_id: str, result: AuthResult) -> None:
        """把令牌存入 secrets，键名 ``account:{id}``。"""
        self.secrets.set(_token_key(account_id), json.dumps(result.to_dict()))

    def get_tokens(self, account_id: str) -> Optional[AuthResult]:
        """从 secrets 读取令牌；不存在或解析失败返回 None。"""
        raw = self.secrets.get(_token_key(account_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return AuthResult.from_dict(data)

    # ---------------------------------------------------------------- 转 Account
    def to_account(self, account_id: str) -> Account:
        """把清单条目转成启动所需的 :class:`engine.models.Account`。

        离线账户直接走 ``Account.offline_account``；微软/外置账户需要
        令牌存在（缺失则抛 :class:`AccountError`）。
        """
        stored = self.get(account_id)
        if stored is None:
            raise AccountError(f"未找到账户：{account_id}")

        if stored.kind == "offline":
            return Account.offline_account(stored.name)

        tokens = self.get_tokens(account_id)
        if tokens is None:
            raise AccountError(f"账户 {account_id} 缺少令牌，请重新登录。")

        user_type = "mojang" if stored.kind == "external" else "msa"
        return Account(
            username=stored.name,
            uuid=stored.uuid,
            access_token=tokens.access_token or "0",
            user_type=user_type,
            offline=False,
        )


# ---------------------------------------------------------------- 外置登录（Yggdrasil）
class ExternalAuthClient:
    """Authlib-Injector / Yggdrasil 协议客户端。

    ``server`` 为该外置登录服务的 API 根地址（不含 ``/authserver``）。
    """

    def __init__(self, server: str, *, timeout: int = 30) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout

    def authenticate(self, username: str, password: str) -> AuthResult:
        """用户名密码认证，返回 :class:`AuthResult`。"""
        client_token = _uuid.uuid4().hex
        status, payload = net.request_json(
            f"{self.server}/authserver/authenticate",
            json_body={
                "username": username,
                "password": password,
                "clientToken": client_token,
                "agent": {"name": "Minecraft", "version": 1},
                "requestUser": True,
            },
            timeout=self.timeout,
        )
        return self._parse(payload, fallback_client=client_token)

    def refresh(self, result: AuthResult) -> AuthResult:
        """用已保存的令牌刷新会话。"""
        status, payload = net.request_json(
            f"{self.server}/authserver/refresh",
            json_body={
                "accessToken": result.access_token,
                "clientToken": result.refresh_token,
                "requestUser": True,
            },
            timeout=self.timeout,
        )
        return self._parse(payload, fallback_client=result.refresh_token)

    def jvm_argument(self, jar_path: Path) -> str:
        """生成 authlib-injector 的 ``-javaagent`` 参数。"""
        return f"-javaagent:{jar_path}={self.server}"

    def download_injector(self, dest_dir: Path) -> Path:
        """从官方元数据下载 authlib-injector jar，返回落盘路径。"""
        status, meta = net.get_json_auth(AUTHLIB_INJECTOR_META, timeout=self.timeout)
        # 元数据里下载地址字段名先按 download_url 取，缺失再退回 url。
        url = (meta.get("download_url") or meta.get("url")) if status == 200 else None
        if not url:
            raise AccountError("获取 authlib-injector 下载地址失败。")

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "authlib-injector.jar"
        net.download_file(url, dest, timeout=self.timeout)
        return dest

    def _parse(self, payload: dict, fallback_client: str = "") -> AuthResult:
        """把 Yggdrasil 响应转成 AuthResult；错误响应抛 AccountError。"""
        if "error" in payload:
            detail = payload.get("errorMessage") or payload.get("error")
            raise AccountError(f"外置登录失败：{detail}")

        profile = payload.get("selectedProfile")
        if not payload.get("accessToken") or not profile:
            raise AccountError("外置登录响应缺少 accessToken 或 selectedProfile。")

        return AuthResult(
            access_token=payload["accessToken"],
            refresh_token=payload.get("clientToken") or fallback_client,
            uuid=normalize_uuid(profile.get("id", "")),
            name=profile.get("name", ""),
            expires_at=0.0,
        )


# --------------------------------------------------------------------------- 皮肤
class SkinService:
    """Mojang 档案 / 皮肤纹理查询，以及 crafatar 头像与全身图。"""

    def __init__(self, *, timeout: int = 30) -> None:
        self.timeout = timeout

    def lookup_uuid(self, name: str) -> Optional[str]:
        """按用户名查 UUID；查不到返回 None。"""
        status, payload = net.get_json_auth(
            MOJANG_UUID_URL.format(name=name), timeout=self.timeout
        )
        if status != 200 or "id" not in payload:
            return None
        return normalize_uuid(payload["id"])

    def profile(self, uuid: str) -> dict:
        """获取会话服务器上的档案（含 properties）。"""
        status, payload = net.get_json_auth(
            SESSIONSERVER_PROFILE_URL.format(uuid=uuid), timeout=self.timeout
        )
        if status != 200:
            return {}
        return payload

    def skin_url(self, uuid: str) -> Optional[str]:
        """解析档案中的 textures 属性，返回皮肤 PNG 地址。"""
        payload = self.profile(uuid)
        for prop in payload.get("properties") or []:
            if prop.get("name") != "textures":
                continue
            try:
                decoded = base64.b64decode(prop.get("value", "")).decode("utf-8")
                textures = json.loads(decoded).get("textures") or {}
            except (ValueError, TypeError, json.JSONDecodeError):
                return None
            url = (textures.get("SKIN") or {}).get("url")
            if not url:
                return None
            # 纹理地址官方为 http，统一升级到 https。
            return url.replace("http://", "https://", 1)
        return None

    def avatar_url(self, uuid: str, *, size: int = 64) -> str:
        return CRAFATAR_AVATAR_URL.format(uuid=uuid, size=size)

    def body_url(self, uuid: str, *, size: int = 128) -> str:
        return CRAFATAR_BODY_URL.format(uuid=uuid, size=size)


# --------------------------------------------------------------------------- 工具
def normalize_uuid(raw: str) -> str:
    """32 位无横线 UUID -> 标准 8-4-4-4-12；已标准或非法则原样返回。"""
    if not raw:
        return raw
    if _STANDARD_UUID.match(raw):
        return raw
    compact = raw.replace("-", "")
    if len(compact) == 32:
        try:
            return str(_uuid.UUID(compact))
        except ValueError:
            return raw
    return raw