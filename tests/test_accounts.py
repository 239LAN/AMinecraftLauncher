"""账户管理 / 外置登录 / 皮肤服务的离线单元测试。

全部使用假网络层与内存 secrets，不访问真实 API、不触碰 keyring。

    python -m unittest tests.test_accounts -v
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import accounts
from engine.accounts import (
    AccountError,
    AccountStore,
    ExternalAuthClient,
    SkinService,
    StoredAccount,
    normalize_uuid,
)
from engine.auth_client import AuthResult
from engine.launch_builder import offline_uuid

NOTCH_UUID = "069a79f444e94726a5befca90e38aaf5"
NOTCH_UUID_DASHED = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


class FakeSecrets:
    """内存版 SecretStore，仅实现 get / set / delete。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        if not value:
            self.delete(key)
            return
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


# --------------------------------------------------------------------- 账户清单
class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "accounts.json"
        self.secrets = FakeSecrets()
        self.store = AccountStore(self.path, self.secrets)

    def test_add_get_remove_and_dedupe(self) -> None:
        account = StoredAccount(id="offline:Steve", kind="offline", name="Steve")
        returned = self.store.add(account)
        self.assertIs(returned, account)
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.store.get("offline:Steve").name, "Steve")

        # 同 id 去重，字段应被更新而不是新增。
        self.store.add(StoredAccount(id="offline:Steve", kind="offline", name="Steve2"))
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.store.get("offline:Steve").name, "Steve2")

        self.store.remove("offline:Steve")
        self.assertIsNone(self.store.get("offline:Steve"))
        self.assertEqual(self.store.load(), [])

    def test_active(self) -> None:
        self.store.add(StoredAccount(id="a", kind="offline", name="A"))
        self.assertIsNone(self.store.active())
        self.store.set_active("a")
        self.assertEqual(self.store.active().id, "a")

    def test_persistence_roundtrip(self) -> None:
        self.store.add(
            StoredAccount(
                id="ms:1",
                kind="microsoft",
                name="Notch",
                uuid=NOTCH_UUID_DASHED,
                last_used=123.0,
            )
        )
        self.store.add(StoredAccount(id="offline:Alex", kind="offline", name="Alex"))
        self.store.set_active("ms:1")

        # 从磁盘重新加载，元数据与激活态都应保持一致。
        reloaded = AccountStore(self.path, self.secrets)
        got = {item.id: item for item in reloaded.load()}
        self.assertEqual(set(got), {"ms:1", "offline:Alex"})
        self.assertEqual(got["ms:1"].uuid, NOTCH_UUID_DASHED)
        self.assertEqual(got["ms:1"].last_used, 123.0)
        self.assertEqual(reloaded.active().id, "ms:1")

    def test_tokens_roundtrip_and_remove_clears(self) -> None:
        self.store.add(StoredAccount(id="ms:1", kind="microsoft", name="Notch"))
        result = AuthResult(
            access_token="AT",
            refresh_token="RT",
            uuid=NOTCH_UUID_DASHED,
            name="Notch",
            expires_at=111.0,
        )
        self.store.set_tokens("ms:1", result)

        got = self.store.get_tokens("ms:1")
        self.assertIsNotNone(got)
        self.assertEqual(got.access_token, "AT")
        self.assertEqual(got.refresh_token, "RT")
        self.assertEqual(got.expires_at, 111.0)

        # remove 必须同时清掉 secrets 里的令牌。
        self.store.remove("ms:1")
        self.assertIsNone(self.store.get_tokens("ms:1"))

    def test_get_tokens_missing(self) -> None:
        self.assertIsNone(self.store.get_tokens("nope"))

    def test_to_account_offline(self) -> None:
        self.store.add(StoredAccount(id="offline:Steve", kind="offline", name="Steve"))
        account = self.store.to_account("offline:Steve")
        self.assertTrue(account.offline)
        self.assertEqual(account.username, "Steve")
        self.assertEqual(account.access_token, "0")
        self.assertEqual(account.uuid, offline_uuid("Steve"))

    def test_to_account_microsoft(self) -> None:
        self.store.add(
            StoredAccount(
                id="ms:1",
                kind="microsoft",
                name="Notch",
                uuid=NOTCH_UUID_DASHED,
            )
        )
        self.store.set_tokens(
            "ms:1", AuthResult("AT", "RT", NOTCH_UUID_DASHED, "Notch")
        )
        account = self.store.to_account("ms:1")
        self.assertFalse(account.offline)
        self.assertEqual(account.username, "Notch")
        self.assertEqual(account.uuid, NOTCH_UUID_DASHED)
        self.assertEqual(account.access_token, "AT")
        self.assertEqual(account.user_type, "msa")

    def test_to_account_external(self) -> None:
        self.store.add(
            StoredAccount(id="ext:1", kind="external", name="Ygg", uuid=NOTCH_UUID_DASHED)
        )
        self.store.set_tokens("ext:1", AuthResult("AT", "RT", NOTCH_UUID_DASHED, "Ygg"))
        account = self.store.to_account("ext:1")
        self.assertFalse(account.offline)
        self.assertEqual(account.user_type, "mojang")

    def test_to_account_missing_account(self) -> None:
        with self.assertRaises(AccountError):
            self.store.to_account("nope")

    def test_to_account_missing_tokens(self) -> None:
        self.store.add(StoredAccount(id="ms:2", kind="microsoft", name="X"))
        with self.assertRaises(AccountError):
            self.store.to_account("ms:2")


# ------------------------------------------------------------------ normalize_uuid
class NormalizeUuidTests(unittest.TestCase):
    def test_compact_to_standard(self) -> None:
        self.assertEqual(normalize_uuid(NOTCH_UUID), NOTCH_UUID_DASHED)

    def test_standard_unchanged(self) -> None:
        self.assertEqual(normalize_uuid(NOTCH_UUID_DASHED), NOTCH_UUID_DASHED)

    def test_illegal_unchanged(self) -> None:
        self.assertEqual(normalize_uuid("not-a-uuid"), "not-a-uuid")
        self.assertEqual(normalize_uuid(""), "")


# -------------------------------------------------------------------- 外置登录
class ExternalAuthClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ExternalAuthClient("https://auth.example.com/")

    def test_authenticate_parses_response(self) -> None:
        payload = {
            "accessToken": "AT",
            "clientToken": "CT",
            "selectedProfile": {"id": NOTCH_UUID, "name": "Notch"},
            "availableProfiles": [{"id": NOTCH_UUID, "name": "Notch"}],
        }
        with mock.patch.object(
            accounts.net, "request_json", return_value=(200, payload)
        ) as called:
            result = self.client.authenticate("user", "pass")

        self.assertEqual(result.access_token, "AT")
        self.assertEqual(result.refresh_token, "CT")
        self.assertEqual(result.uuid, NOTCH_UUID_DASHED)
        self.assertEqual(result.name, "Notch")
        self.assertEqual(result.expires_at, 0.0)

        # 请求地址与请求体应符合 Yggdrasil 协议。
        self.assertEqual(called.call_args.args[0], "https://auth.example.com/authserver/authenticate")
        body = called.call_args.kwargs["json_body"]
        self.assertEqual(body["agent"], {"name": "Minecraft", "version": 1})
        self.assertTrue(body["requestUser"])
        self.assertTrue(body["clientToken"])

    def test_authenticate_error_raises(self) -> None:
        error = {
            "error": "ForbiddenOperationException",
            "errorMessage": "Invalid credentials.",
        }
        with mock.patch.object(
            accounts.net, "request_json", return_value=(200, error)
        ):
            with self.assertRaises(AccountError) as ctx:
                self.client.authenticate("user", "bad")
        self.assertIn("Invalid credentials", str(ctx.exception))

    def test_refresh_parses_response(self) -> None:
        payload = {
            "accessToken": "AT2",
            "clientToken": "CT2",
            "selectedProfile": {"id": NOTCH_UUID, "name": "Notch"},
        }
        with mock.patch.object(
            accounts.net, "request_json", return_value=(200, payload)
        ) as called:
            result = self.client.refresh(AuthResult("old", "clt", "", "Notch"))

        self.assertEqual(result.access_token, "AT2")
        self.assertEqual(result.refresh_token, "CT2")
        self.assertEqual(called.call_args.args[0], "https://auth.example.com/authserver/refresh")
        body = called.call_args.kwargs["json_body"]
        self.assertEqual(body["accessToken"], "old")
        self.assertEqual(body["clientToken"], "clt")
        self.assertTrue(body["requestUser"])

    def test_jvm_argument(self) -> None:
        path = Path("/x/authlib-injector.jar")
        argument = self.client.jvm_argument(path)
        self.assertEqual(
            argument, f"-javaagent:{path}=https://auth.example.com"
        )


# ------------------------------------------------------------------------- 皮肤
class SkinServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SkinService()

    @staticmethod
    def _textures_property(url: str) -> dict:
        blob = {"textures": {"SKIN": {"url": url}}}
        value = base64.b64encode(json.dumps(blob).encode("utf-8")).decode("ascii")
        return {"name": "textures", "value": value}

    def test_skin_url_upgrades_to_https(self) -> None:
        profile = {
            "id": NOTCH_UUID,
            "name": "Notch",
            "properties": [
                self._textures_property("http://textures.minecraft.net/texture/abc123")
            ],
        }
        with mock.patch.object(
            accounts.net, "get_json_auth", return_value=(200, profile)
        ):
            url = self.service.skin_url(NOTCH_UUID)
        self.assertEqual(url, "https://textures.minecraft.net/texture/abc123")

    def test_skin_url_without_textures(self) -> None:
        with mock.patch.object(
            accounts.net, "get_json_auth", return_value=(200, {"properties": []})
        ):
            self.assertIsNone(self.service.skin_url(NOTCH_UUID))

    def test_avatar_and_body_urls(self) -> None:
        self.assertEqual(
            self.service.avatar_url(NOTCH_UUID, size=32),
            f"https://crafatar.com/avatars/{NOTCH_UUID}?size=32&overlay",
        )
        self.assertEqual(
            self.service.body_url(NOTCH_UUID, size=256),
            f"https://crafatar.com/renders/body/{NOTCH_UUID}?size=256",
        )

    def test_lookup_uuid_success(self) -> None:
        payload = {"id": NOTCH_UUID, "name": "Notch"}
        with mock.patch.object(
            accounts.net, "get_json_auth", return_value=(200, payload)
        ):
            self.assertEqual(self.service.lookup_uuid("Notch"), NOTCH_UUID_DASHED)

    def test_lookup_uuid_not_found(self) -> None:
        with mock.patch.object(
            accounts.net, "get_json_auth", return_value=(404, {})
        ):
            self.assertIsNone(self.service.lookup_uuid("Nobody"))


if __name__ == "__main__":
    unittest.main()