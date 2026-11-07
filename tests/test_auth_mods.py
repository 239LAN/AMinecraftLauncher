"""认证与 Mod 源的离线单元测试（用假的网络层，不访问真实 API）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import auth_client, mod_provider, updater
from engine.auth_client import (
    AuthClient,
    AuthError,
    AuthResult,
    MemoryTokenStore,
)
from engine.mod_provider import (
    CurseForgeProvider,
    ModProviderError,
    ModrinthProvider,
    build_provider,
)

from app.services import launcher_update_available, pick_updated_file

# --------------------------------------------------------------------- 认证
DEVICE_CODE_PAYLOAD = {
    "device_code": "dev-code",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/link",
    "expires_in": 900,
    "interval": 5,
    "message": "go to the link",
}

MS_TOKEN_PAYLOAD = {
    "access_token": "ms-access",
    "refresh_token": "ms-refresh",
    "expires_in": 3600,
}

XBL_PAYLOAD = {
    "Token": "xbl-token",
    "DisplayClaims": {"xui": [{"uhs": "uhs-value"}]},
}
XSTS_PAYLOAD = {
    "Token": "xsts-token",
    "DisplayClaims": {"xui": [{"uhs": "uhs-value"}]},
}
MC_LOGIN_PAYLOAD = {"access_token": "mc-access", "expires_in": 86400}
PROFILE_PAYLOAD = {"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}


def _fake_request_json(url: str, **kwargs):
    routes = {
        auth_client.DEVICE_CODE_URL: (200, DEVICE_CODE_PAYLOAD),
        auth_client.TOKEN_URL: (200, MS_TOKEN_PAYLOAD),
        auth_client.XBL_AUTH_URL: (200, XBL_PAYLOAD),
        auth_client.XSTS_AUTH_URL: (200, XSTS_PAYLOAD),
        auth_client.MC_LOGIN_URL: (200, MC_LOGIN_PAYLOAD),
    }
    if url not in routes:
        raise AssertionError(f"未预期的请求：{url}")
    return routes[url]


def _fake_get_json_auth(url: str, **kwargs):
    if url == auth_client.MC_PROFILE_URL:
        return 200, PROFILE_PAYLOAD
    if url.startswith(mod_provider.MODRINTH_API):
        raise AssertionError("本测试不应访问 Modrinth")
    return 404, {}


class AuthResultTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        original = AuthResult(
            access_token="a",
            refresh_token="r",
            uuid="u",
            name="n",
            expires_at=123.0,
        )
        restored = AuthResult.from_dict(original.to_dict())
        self.assertEqual(restored.access_token, "a")
        self.assertEqual(restored.refresh_token, "r")
        self.assertEqual(restored.expires_at, 123.0)

    def test_expired(self) -> None:
        live = AuthResult("a", "r", "u", "n", expires_at=time.time() + 3600)
        self.assertFalse(live.expired)
        # 剩余不足 60 秒即视为需要续期。
        soon = AuthResult("a", "r", "u", "n", expires_at=time.time() + 30)
        self.assertTrue(soon.expired)
        # 未记录过期时间时不主动续期。
        unknown = AuthResult("a", "r", "u", "n", expires_at=0)
        self.assertFalse(unknown.expired)


class AuthClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryTokenStore()
        self.client = AuthClient(store=self.store)
        self._patchers = [
            mock.patch.object(auth_client.net, "request_json", side_effect=_fake_request_json),
            mock.patch.object(auth_client.net, "get_json_auth", side_effect=_fake_get_json_auth),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_device_code_flow(self) -> None:
        device = self.client.start_auth()
        self.assertEqual(device.user_code, "ABCD-EFGH")
        self.assertEqual(device.interval, 5)

        result = self.client.finish_auth(device.device_code, timeout=5)
        self.assertEqual(result.name, "Notch")
        # 微软返回 32 位无横线 UUID，这里应格式化成带横线形式。
        self.assertEqual(result.uuid, "069a79f4-44e9-4726-a5be-fca90e38aaf5")
        self.assertEqual(result.access_token, "mc-access")

        # 令牌应写入 store。
        stored = self.client.load_tokens()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.name, "Notch")

    def test_poll_pending_then_ok(self) -> None:
        pending = {"error": "authorization_pending"}
        with mock.patch.object(
            auth_client.net, "request_json", return_value=(400, pending)
        ):
            self.assertIsNone(self.client.poll_auth("dev-code"))

    def test_poll_expired_raises(self) -> None:
        with mock.patch.object(
            auth_client.net, "request_json", return_value=(400, {"error": "expired_token"})
        ):
            with self.assertRaises(AuthError):
                self.client.poll_auth("dev-code")

    def test_logout_clears_store(self) -> None:
        self.client.start_auth()
        self.client.finish_auth("dev-code", timeout=5)
        self.assertIsNotNone(self.client.load_tokens())
        self.client.logout()
        self.assertIsNone(self.client.load_tokens())

    def test_ensure_fresh_keeps_valid_token(self) -> None:
        live = AuthResult("a", "r", "u", "n", expires_at=time.time() + 3600)
        self.assertIs(self.client.ensure_fresh(live), live)

    def test_xsts_error_is_translated(self) -> None:
        with mock.patch.object(
            auth_client.net,
            "request_json",
            return_value=(401, {"XErr": 2148916233}),
        ):
            with self.assertRaises(AuthError) as ctx:
                self.client._xsts("xbl-token")
        self.assertIn("Xbox 档案", str(ctx.exception))


# ------------------------------------------------------------------ Mod 源
class ProviderFactoryTests(unittest.TestCase):
    def test_build_known_sources(self) -> None:
        self.assertIsInstance(build_provider("modrinth"), ModrinthProvider)
        self.assertIsInstance(build_provider("curseforge"), CurseForgeProvider)

    def test_unknown_source(self) -> None:
        with self.assertRaises(ModProviderError):
            build_provider("nope")


class CurseForgeTests(unittest.TestCase):
    def test_api_uses_mcimirror_without_key(self) -> None:
        # CF 走 mcimirror 公共镜像，不需要也不应发送 x-api-key。
        provider = CurseForgeProvider()
        self.assertEqual(provider.download_headers(), {})
        with mock.patch.object(
            mod_provider.net, "get_json_auth", return_value=(200, {"data": []})
        ) as called:
            provider.search("jei")
        self.assertTrue(
            called.call_args.args[0].startswith(
                "https://mod.mcimirror.top/curseforge/v1/mods/search"
            )
        )
        headers = called.call_args.kwargs["headers"]
        self.assertNotIn("x-api-key", headers)

    def test_mirror_cdn_url(self) -> None:
        edge = "https://edge.forgecdn.net/files/1234/5678/jei.jar"
        self.assertEqual(
            mod_provider.mirror_cdn_url(edge),
            "https://mod.mcimirror.top/files/1234/5678/jei.jar",
        )
        # mediafilez 不在镜像范围，保持原样。
        media = "https://mediafilez.forgecdn.net/files/1/2/a.jar"
        self.assertEqual(mod_provider.mirror_cdn_url(media), media)

    def test_search_parsing(self) -> None:
        payload = {
            "data": [
                {
                    "id": 12345,
                    "name": "JEI",
                    "summary": "item viewer",
                    "downloadCount": 900,
                    "authors": [{"name": "mezz"}],
                    "logo": {"thumbnailUrl": "https://cdn/x.png"},
                    "links": {"websiteUrl": "https://cf/jei"},
                }
            ]
        }
        with mock.patch.object(
            mod_provider.net, "get_json_auth", return_value=(200, payload)
        ) as called:
            results = CurseForgeProvider().search("jei", loader="forge")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "JEI")
        self.assertEqual(results[0].author, "mezz")
        self.assertEqual(results[0].project_id, "12345")
        # forge -> modLoaderType=1 应出现在查询串里。
        url = called.call_args.args[0]
        self.assertIn("modLoaderType=1", url)


class ModrinthTests(unittest.TestCase):
    def test_search_parsing(self) -> None:
        payload = {
            "hits": [
                {
                    "project_id": "AANobbMI",
                    "title": "Sodium",
                    "author": "jellysquid3",
                    "description": "fps",
                    "downloads": 255631722,
                    "icon_url": "https://cdn/sodium.webp",
                    "slug": "sodium",
                }
            ]
        }
        with mock.patch.object(
            mod_provider.net, "get_json_auth", return_value=(200, payload)
        ) as called:
            results = ModrinthProvider().search("sodium", game_version="1.20.1", loader="fabric")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].project_id, "AANobbMI")
        self.assertEqual(results[0].page_url, "https://modrinth.com/mod/sodium")
        # 关键词、游戏版本、加载器都应进入 facets。
        url = called.call_args.args[0]
        self.assertIn("versions%3A1.20.1", url)
        self.assertIn("categories%3Afabric", url)

    def test_resolve_files_parsing(self) -> None:
        payload = [
            {
                "id": "ver1",
                "version_number": "1.0",
                "game_versions": ["1.20.1"],
                "files": [
                    {
                        "filename": "sodium.jar",
                        "url": "https://cdn/sodium.jar",
                        "size": 42,
                        "hashes": {"sha1": "f" * 40},
                    }
                ],
            }
        ]
        with mock.patch.object(mod_provider.net, "get_json_auth", return_value=(200, payload)):
            files = ModrinthProvider().resolve_files("AANobbMI", game_version="1.20.1")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "sodium.jar")
        self.assertEqual(files[0].sha1, "f" * 40)
        self.assertEqual(files[0].size, 42)

    def test_download_headers_empty(self) -> None:
        self.assertEqual(ModrinthProvider().download_headers(), {})


class ModUpdateSelectionTest(unittest.TestCase):
    """更新判断只认哈希与发布时间，不受 jar 内版本号影响。"""

    @staticmethod
    def _file(sha1: str, published: str, version: str = "") -> mod_provider.ModFile:
        return mod_provider.ModFile(
            source="modrinth",
            project_id="proj",
            file_id=sha1,
            filename="mod.jar",
            url="https://cdn/mod.jar",
            sha1=sha1,
            version=version,
            published_at=published,
        )

    def test_picks_latest_publish_time(self) -> None:
        current = self._file("a" * 40, "2026-01-01T00:00:00Z")
        files = [
            current,
            # jar 里写的版本号比平台版本号小，但发布时间确实更晚。
            self._file("c" * 40, "2026-03-01T00:00:00Z", "1.0.0"),
            self._file("b" * 40, "2026-02-01T00:00:00Z", "9.9.9"),
        ]
        picked = pick_updated_file(files, current.sha1, current.published_at)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.sha1, "c" * 40)

    def test_older_build_is_not_an_update(self) -> None:
        files = [
            self._file("a" * 40, "2026-05-01T00:00:00Z"),
            self._file("b" * 40, "2026-04-01T00:00:00Z"),
        ]
        self.assertIsNone(pick_updated_file(files, "a" * 40, "2026-05-01T00:00:00Z"))

    def test_falls_back_without_publish_time(self) -> None:
        files = [self._file("a" * 40, ""), self._file("b" * 40, "")]
        picked = pick_updated_file(files, "a" * 40, "")
        self.assertEqual(picked.sha1, "b" * 40)


class CurseForgeFingerprintTest(unittest.TestCase):
    """CurseForge 指纹会先剔除空白字节，再做 Murmur2。"""

    def test_whitespace_bytes_are_stripped(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.jar"
            spaced = Path(tmp) / "spaced.jar"
            plain.write_bytes(b"PK\x03\x04content")
            spaced.write_bytes(b"PK\x03\x04 con\ttent\r\n")
            self.assertEqual(
                mod_provider.murmur2_fingerprint(plain),
                mod_provider.murmur2_fingerprint(spaced),
            )
            other = Path(tmp) / "other.jar"
            other.write_bytes(b"PK\x03\x04other")
            self.assertNotEqual(
                mod_provider.murmur2_fingerprint(plain),
                mod_provider.murmur2_fingerprint(other),
            )


class LauncherUpdateAvailabilityTest(unittest.TestCase):
    """启动器更新：新版本算更新；同版本仅当附件标识变化时算更新。"""

    def test_newer_version(self) -> None:
        self.assertTrue(launcher_update_available("1.6.0", "1.5.3", "id", ""))

    def test_same_version_without_history_is_not_update(self) -> None:
        self.assertFalse(launcher_update_available("1.5.3", "1.5.3", "id", ""))

    def test_same_version_same_asset_is_not_update(self) -> None:
        self.assertFalse(
            launcher_update_available("1.5.3", "1.5.3", "1.5.3|a.exe|10|x|1", "1.5.3|a.exe|10|x|1")
        )

    def test_same_release_changed_asset_counts_as_update(self) -> None:
        self.assertTrue(
            launcher_update_available("1.5.3", "1.5.3", "1.5.3|a.exe|11|y|2", "1.5.3|a.exe|10|x|1")
        )

    def test_identity_changes_with_asset_fields(self) -> None:
        base = {"version": "1.5.3", "asset_name": "a.exe", "size": 10, "asset_updated_at": "x", "asset_id": 1}
        changed = dict(base, size=11)
        self.assertNotEqual(updater.asset_identity(base), updater.asset_identity(changed))


if __name__ == "__main__":
    unittest.main()