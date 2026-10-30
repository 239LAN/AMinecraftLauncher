"""AuthClient：微软设备码流 + Xbox Live / XSTS / Minecraft 服务链路。

设计原本计划依赖 ``mcauth3``，但该包在 PyPI 上并不存在；这里按同样的协议
自行实现，并保持 ``start_auth / finish_auth / refresh_auth`` 的接口形态，
日后若要换成第三方库只需替换本模块内部实现。

令牌默认只写入 keyring（``TokenStore``），不落明文配置。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

from . import net

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

SCOPE = "XboxLive.signin offline_access"

# 公开的 Minecraft 启动器客户端 ID（非机密），可在设置页覆盖。
DEFAULT_CLIENT_ID = "00000000402b5328"
KEYRING_SERVICE = "minicraftlauncher"


class AuthError(Exception):
    """认证过程中的可预期失败（含 XSTS 未购买游戏、账号受限等）。"""

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int = 5
    message: str = ""


@dataclass
class AuthResult:
    """一次成功认证的结果（access_token 为 Minecraft 服务令牌）。"""

    access_token: str
    refresh_token: str
    uuid: str
    name: str
    expires_at: float = 0.0
    user_type: str = "msa"

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - 60

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "uuid": self.uuid,
            "name": self.name,
            "expires_at": self.expires_at,
            "user_type": self.user_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthResult":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            uuid=data.get("uuid", ""),
            name=data.get("name", ""),
            expires_at=float(data.get("expires_at") or 0),
            user_type=data.get("user_type", "msa"),
        )


# --------------------------------------------------------------------- 令牌存储
class TokenStore(Protocol):
    def load(self) -> Optional[dict]: ...
    def save(self, data: dict) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    """把令牌 JSON 存进系统凭据管理器（keyring）。"""

    def __init__(self, service: str = KEYRING_SERVICE, account: str = "msa_refresh") -> None:
        self.service = service
        self.account = account

    @property
    def available(self) -> bool:
        try:
            import keyring

            backend = keyring.get_keyring()
            return "fail" not in type(backend).__module__
        except Exception:  # noqa: BLE001 - keyring 不可用时降级
            return False

    def load(self) -> Optional[dict]:
        try:
            import keyring

            raw = keyring.get_password(self.service, self.account)
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def save(self, data: dict) -> None:
        import keyring

        keyring.set_password(self.service, self.account, json.dumps(data))

    def clear(self) -> None:
        try:
            import keyring

            keyring.delete_password(self.service, self.account)
        except Exception:  # noqa: BLE001 - 不存在时忽略
            pass


class MemoryTokenStore:
    """测试用内存实现。"""

    def __init__(self) -> None:
        self._data: Optional[dict] = None

    def load(self) -> Optional[dict]:
        return self._data

    def save(self, data: dict) -> None:
        self._data = dict(data)

    def clear(self) -> None:
        self._data = None


# ------------------------------------------------------------------------ client
XSTS_ERRORS = {
    "2148916233": "该微软账号尚未创建 Xbox 档案，请先登录 xbox.com 创建。",
    "2148916235": "该账号所在地区不支持 Xbox Live。",
    "2148916236": "该账号需要完成年龄验证（Xbox 家长控制）。",
    "2148916237": "该账号需要完成年龄验证（Xbox 家长控制）。",
    "2148916238": "该账号为未成年人账号，需先加入家庭组。",
}


class AuthClient:
    def __init__(
        self,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        timeout: int = 30,
        store: Optional[TokenStore] = None,
    ) -> None:
        self.client_id = client_id
        self.timeout = timeout
        self.store = store
        self._last_device_code: Optional[DeviceCode] = None

    # ------------------------------------------------------------- 设备码流程
    def start_auth(self) -> DeviceCode:
        status, payload = net.request_json(
            DEVICE_CODE_URL,
            form={"client_id": self.client_id, "scope": SCOPE},
            timeout=self.timeout,
        )
        if status != 200 or "device_code" not in payload:
            raise AuthError(_describe(payload, "申请设备码失败"), code=str(status))

        device = DeviceCode(
            device_code=payload["device_code"],
            user_code=payload.get("user_code", ""),
            verification_uri=payload.get("verification_uri", ""),
            expires_in=int(payload.get("expires_in") or 900),
            interval=int(payload.get("interval") or 5),
            message=payload.get("message", ""),
        )
        self._last_device_code = device
        return device

    def poll_auth(self, device_code: str) -> Optional[AuthResult]:
        """单次轮询：未完成返回 None，完成返回 AuthResult。"""
        status, payload = net.request_json(
            TOKEN_URL,
            form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id,
                "device_code": device_code,
            },
            timeout=self.timeout,
        )
        if status == 200 and payload.get("access_token"):
            return self._complete(payload)
        error = payload.get("error", "")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            return None
        if error in ("expired_token", "code_expired"):
            raise AuthError("设备码已过期，请重新获取。", code=error)
        if error == "authorization_declined":
            raise AuthError("用户拒绝了本次授权。", code=error)
        raise AuthError(_describe(payload, "认证失败"), code=error or str(status))

    def finish_auth(self, device_code: str, *, timeout: float = 300.0) -> AuthResult:
        """阻塞轮询直到完成或超时。"""
        interval = (self._last_device_code.interval if self._last_device_code else 5)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.poll_auth(device_code)
            if result is not None:
                return result
            time.sleep(interval)
        raise AuthError("等待授权超时，请重试。", code="timeout")

    def refresh_auth(self, refresh_token: str) -> AuthResult:
        status, payload = net.request_json(
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "scope": SCOPE,
            },
            timeout=self.timeout,
        )
        if status != 200 or not payload.get("access_token"):
            raise AuthError(_describe(payload, "刷新令牌失败"), code=str(status))
        return self._complete(payload, fallback_refresh=refresh_token)

    # --------------------------------------------------------------- 内部链路
    def _complete(self, ms_tokens: dict, fallback_refresh: str = "") -> AuthResult:
        ms_access = ms_tokens["access_token"]
        refresh = ms_tokens.get("refresh_token") or fallback_refresh

        xbl = self._xbox_live(ms_access)
        xsts = self._xsts(xbl["token"])
        mc = self._minecraft_login(xsts["token"], xsts["uhs"])
        profile = self.fetch_profile(mc["access_token"])

        result = AuthResult(
            access_token=mc["access_token"],
            refresh_token=refresh,
            uuid=_format_uuid(profile["id"]),
            name=profile.get("name", ""),
            expires_at=time.time() + int(mc.get("expires_in") or 86400),
        )
        self.save_tokens(result)
        return result

    def _xbox_live(self, ms_access_token: str) -> dict:
        status, payload = net.request_json(
            XBL_AUTH_URL,
            json_body={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"d={ms_access_token}",
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if status != 200 or "Token" not in payload:
            raise AuthError(_describe(payload, "Xbox Live 认证失败"), code=str(status))
        return {"token": payload["Token"], "uhs": payload["DisplayClaims"]["xui"][0]["uhs"]}

    def _xsts(self, xbl_token: str) -> dict:
        status, payload = net.request_json(
            XSTS_AUTH_URL,
            json_body={
                "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
                "RelyingParty": "rp://api.minecraftservices.com/",
                "TokenType": "JWT",
            },
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if status == 401:
            xerr = str(payload.get("XErr", ""))
            raise AuthError(
                XSTS_ERRORS.get(xerr, f"XSTS 授权被拒绝（XErr={xerr}）。"), code=xerr
            )
        if status != 200 or "Token" not in payload:
            raise AuthError(_describe(payload, "XSTS 授权失败"), code=str(status))
        return {"token": payload["Token"], "uhs": payload["DisplayClaims"]["xui"][0]["uhs"]}

    def _minecraft_login(self, xsts_token: str, uhs: str) -> dict:
        status, payload = net.request_json(
            MC_LOGIN_URL,
            json_body={"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if status != 200 or "access_token" not in payload:
            raise AuthError(_describe(payload, "Minecraft 登录失败"), code=str(status))
        return payload

    def fetch_profile(self, mc_access_token: str) -> dict:
        status, payload = net.get_json_auth(
            MC_PROFILE_URL,
            headers={"Authorization": f"Bearer {mc_access_token}"},
            timeout=self.timeout,
        )
        if status == 404:
            raise AuthError("该账号未拥有 Minecraft: Java 版。", code="NOT_OWNED")
        if status != 200 or "id" not in payload:
            raise AuthError(_describe(payload, "获取 Minecraft 档案失败"), code=str(status))
        return payload

    # ------------------------------------------------------------- 令牌持久化
    def save_tokens(self, result: AuthResult) -> None:
        if self.store:
            self.store.save(result.to_dict())

    def load_tokens(self) -> Optional[AuthResult]:
        if not self.store:
            return None
        data = self.store.load()
        return AuthResult.from_dict(data) if data else None

    def logout(self) -> None:
        if self.store:
            self.store.clear()

    def ensure_fresh(self, result: AuthResult) -> AuthResult:
        """令牌将过期时自动续期；失败则原样返回，由调用方决定是否回退离线。"""
        if not result.expired:
            return result
        try:
            return self.refresh_auth(result.refresh_token)
        except AuthError:
            return result


def _format_uuid(raw: str) -> str:
    raw = raw.replace("-", "")
    if len(raw) != 32:
        return raw
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _describe(payload: dict, prefix: str) -> str:
    detail = (
        payload.get("error_description")
        or payload.get("errorMessage")
        or payload.get("error")
        or payload.get("details")
    )
    return f"{prefix}：{detail}" if detail else prefix