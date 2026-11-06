"""relay 子节点房间接口的离线单元测试。

    python -m unittest tests.test_relay_rooms -q

全部请求走 FastAPI TestClient，不依赖真实网络，签名由 SignedClient 自动补全。
"""

from __future__ import annotations

import secrets
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from relay import signing
from relay.config import RelayConfig
from relay.node import create_node_app

ROOM_ID_PATTERN = r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$"
JOIN_CODE_PATTERN = r"^[1-9]\d{3}$"


class SignedClient(TestClient):
    """给每个请求补上启动器签名，模拟真实客户端。"""

    def request(self, method, url, **kwargs):  # noqa: ANN001, ANN201 - 测试替身
        path = str(url).split("?")[0]
        headers = dict(kwargs.pop("headers", None) or {})
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(8)
        headers.update(
            {
                "X-AML-Ts": timestamp,
                "X-AML-Nonce": nonce,
                "X-AML-Sign": signing.sign(method, path, timestamp, nonce),
            }
        )
        return super().request(method, url, headers=headers, **kwargs)


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class RelayRoomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = RelayConfig(
            role="node",
            api_port=3375,
            node_id="node-test",
            node_name="测试节点",
            region="cn",
            frps_addr="frp.example.com",
            frps_port=3300,
            frps_token="test-frp-token",
            db_path=Path(self.tmp.name) / "relay.db",
            room_ttl=7200,
            heartbeat_timeout=45,
            max_rooms_per_host=1,
            # 测试客户端都来自同一 IP，这里放开，单个用例单独验证 IP 限制。
            max_rooms_per_ip=16,
            max_guests=8,
        )
        self.app = create_node_app(self.config, start_background=False)
        self.client = SignedClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.store = self.app.state.service.store

        self.host = self._login("房主")
        self.guest = self._login("访客一")
        self.guest2 = self._login("访客二")

    def _login(self, name: str) -> str:
        response = self.client.post("/api/v1/auth/session", json={"name": name})
        self.assertEqual(response.status_code, 200)
        return response.json()["session_token"]

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _create_room(
        self,
        token: str | None = None,
        max_players: int = 2,
        *,
        hidden: bool = False,
        provider: str = "aml",
        mods: list[dict] | None = None,
    ) -> dict:
        payload = {
            "name": "周末生存",
            "version": "1.21.1",
            "loader": "fabric",
            "max_players": max_players,
            "local_port": 25565,
            "mode": "stcp",
            "hidden": hidden,
            "provider": provider,
            "mods": mods or [],
        }
        response = self.client.post(
            "/api/v1/rooms",
            headers=self._auth(token or self.host),
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _join(self, room_id: str, token: str, bind_port: int | None = None) -> dict:
        payload = {} if bind_port is None else {"bind_port": bind_port}
        return self.client.post(
            f"/api/v1/rooms/{room_id}/join",
            headers=self._auth(token),
            json=payload,
        )

    def _lookup(self, code: str, token: str | None = None):
        return self.client.post(
            "/api/v1/rooms/lookup",
            headers=self._auth(token or self.guest),
            json={"code": code},
        )

    def test_create_room_returns_secret_frpc(self) -> None:
        data = self._create_room()
        self.assertRegex(data["room_id"], ROOM_ID_PATTERN)
        self.assertRegex(data["join_code"], JOIN_CODE_PATTERN)

        frpc = data["host_frpc"]
        self.assertEqual(frpc["server_addr"], "frp.example.com")
        self.assertEqual(frpc["server_port"], 3300)
        self.assertTrue(frpc["tls"])
        self.assertEqual(frpc["auth_token"], "test-frp-token")
        proxy = frpc["proxy"]
        self.assertEqual(proxy["name"], f"aml_{data['room_id']}")
        self.assertEqual(proxy["type"], "stcp")
        self.assertEqual(proxy["local_ip"], "127.0.0.1")
        self.assertEqual(proxy["local_port"], 25565)
        self.assertTrue(proxy["secret_key"])

    def test_lookup_does_not_leak_secret(self) -> None:
        room = self._create_room()
        response = self._lookup(room["join_code"])
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            set(data.keys()),
            {
                "room_id",
                "name",
                "host_name",
                "version",
                "loader",
                "players",
                "max_players",
                "status",
                "node_id",
                "provider",
                "hidden",
                "mod_count",
                "mods_declared",
                "mods",
                "expires_at",
            },
        )
        self.assertEqual(data["room_id"], room["room_id"])
        self.assertEqual(data["host_name"], "房主")
        self.assertEqual(data["players"], 1)
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["node_id"], "node-test")
        self.assertNotIn("secret_key", response.text)
        self.assertNotIn("auth_token", response.text)

        # 房间号固定为 4 位数字。
        loose = self._lookup(room["join_code"])
        self.assertEqual(loose.status_code, 200)

    def test_join_returns_visitor_and_full(self) -> None:
        room = self._create_room(max_players=2)
        response = self._join(room["room_id"], self.guest)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["room"]["players"], 2)
        visitor = data["visitor_frpc"]
        self.assertEqual(visitor["server_addr"], "frp.example.com")
        self.assertEqual(visitor["server_port"], 3300)
        self.assertTrue(visitor["tls"])
        proxy = visitor["proxy"]
        self.assertEqual(proxy["role"], "visitor")
        self.assertEqual(proxy["type"], "stcp")
        self.assertEqual(proxy["server_name"], f"aml_{room['room_id']}")
        self.assertTrue(proxy["name"].startswith(f"aml_v_{room['room_id']}_"))
        self.assertEqual(proxy["bind_addr"], "127.0.0.1")
        self.assertEqual(proxy["bind_port"], data["bind_port"])
        self.assertGreaterEqual(data["bind_port"], 25600)
        self.assertLess(data["bind_port"], 26000)

        full = self._join(room["room_id"], self.guest2)
        self.assertEqual(full.status_code, 409)
        self.assertEqual(full.json()["error"]["code"], "ROOM_FULL")

    def test_join_is_idempotent(self) -> None:
        room = self._create_room(max_players=3)
        first = self._join(room["room_id"], self.guest).json()
        second = self._join(room["room_id"], self.guest).json()
        self.assertEqual(first["bind_port"], second["bind_port"])
        self.assertEqual(first["visitor_frpc"]["proxy"]["name"], second["visitor_frpc"]["proxy"]["name"])
        self.assertEqual(second["room"]["players"], 2)

        # 重复 join 时显式端口应更新自己那条记录，不新增访客。
        updated = self._join(room["room_id"], self.guest, bind_port=25888).json()
        self.assertEqual(updated["bind_port"], 25888)
        self.assertEqual(updated["room"]["players"], 2)
        self.assertEqual(len(self.store.list_guests(room["room_id"])), 1)

    def test_join_expired_room(self) -> None:
        room = self._create_room()
        self.store.update_room_expiry(
            room["room_id"], time.time() - 1, time.time()
        )
        response = self._join(room["room_id"], self.guest)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "ROOM_EXPIRED")

        lookup = self._lookup(room["join_code"])
        self.assertEqual(lookup.status_code, 410)
        self.assertEqual(lookup.json()["error"]["code"], "ROOM_EXPIRED")

    def test_heartbeat_extends_expiry(self) -> None:
        room = self._create_room()
        room_id = room["room_id"]
        row = self.store.get_room(room_id)
        # 人为把到期时间缩短，验证心跳能延长。
        self.store.update_room_expiry(room_id, time.time() + 30, time.time())
        response = self.client.post(
            f"/api/v1/rooms/{room_id}/heartbeat", headers=self._auth(self.host)
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["players"], 1)
        self.assertGreater(parse_time(data["expires_at"]), time.time() + 1000)
        self.assertLessEqual(parse_time(data["expires_at"]), row["max_expires_at"])

        # 心跳最多把房间延长到创建后 2 倍 TTL，且不会缩短已有有效期。
        self.store.update_room_expiry(room_id, row["max_expires_at"] - 5, time.time())
        capped = self.client.post(
            f"/api/v1/rooms/{room_id}/heartbeat", headers=self._auth(self.host)
        ).json()
        capped_at = parse_time(capped["expires_at"])
        self.assertLessEqual(capped_at, row["max_expires_at"])
        self.assertGreaterEqual(capped_at, row["max_expires_at"] - 6)

    def test_leave_and_close(self) -> None:
        room = self._create_room(max_players=3)
        joined = self._join(room["room_id"], self.guest).json()
        self.assertEqual(joined["room"]["players"], 2)

        detail = self.client.get(
            f"/api/v1/rooms/{room['room_id']}", headers=self._auth(self.host)
        )
        self.assertEqual(detail.status_code, 200)
        data = detail.json()
        self.assertEqual(data["join_code"], room["join_code"])
        self.assertEqual(len(data["guests"]), 1)
        self.assertEqual(data["guests"][0]["name"], "访客一")
        self.assertIn("joined_at", data["guests"][0])
        self.assertNotIn("secret_key", detail.text)

        leave = self.client.post(
            f"/api/v1/rooms/{room['room_id']}/leave", headers=self._auth(self.guest)
        )
        self.assertEqual(leave.status_code, 204)
        self.assertEqual(self._lookup(room["join_code"]).json()["players"], 1)

        closed = self.client.delete(
            f"/api/v1/rooms/{room['room_id']}", headers=self._auth(self.host)
        )
        self.assertEqual(closed.status_code, 204)
        lookup = self._lookup(room["join_code"])
        self.assertEqual(lookup.status_code, 410)
        self.assertEqual(lookup.json()["error"]["code"], "ROOM_CLOSED")
        self.assertEqual(self._join(room["room_id"], self.guest).status_code, 410)

    def test_room_limit_and_forbidden(self) -> None:
        room = self._create_room()
        second = self.client.post(
            "/api/v1/rooms",
            headers=self._auth(self.host),
            json={
                "name": "第二个房间",
                "version": "1.21.1",
                "loader": "vanilla",
                "max_players": 2,
                "local_port": 25566,
                "mode": "stcp",
            },
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "ROOM_LIMIT")

        detail = self.client.get(
            f"/api/v1/rooms/{room['room_id']}", headers=self._auth(self.guest)
        )
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(detail.json()["error"]["code"], "FORBIDDEN")

    def test_ip_room_limit(self) -> None:
        self.app.state.service.config.max_rooms_per_ip = 1
        self._create_room()
        response = self.client.post(
            "/api/v1/rooms",
            headers=self._auth(self.guest2),
            json={
                "name": "同网络的第二个房间",
                "version": "1.21.1",
                "loader": "vanilla",
                "max_players": 2,
                "local_port": 25567,
                "mode": "stcp",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IP_LIMIT")

    def test_unsigned_request_rejected(self) -> None:
        plain = TestClient(self.app)
        response = plain.get("/api/v1/meta")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED")

        # 健康检查始终放行，方便探活。
        self.assertEqual(plain.get("/healthz").status_code, 200)

    def test_error_body_format(self) -> None:
        missing = self._lookup("9999")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "ROOM_NOT_FOUND")
        self.assertIn("message", missing.json()["error"])

        unauthorized = self.client.post("/api/v1/rooms", json={})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["error"]["code"], "UNAUTHORIZED")

        invalid = self.client.post(
            "/api/v1/rooms",
            headers=self._auth(self.host),
            json={"name": "缺字段"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_REQUEST")

    def test_healthz_and_meta(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        meta = self.client.get("/api/v1/meta")
        self.assertEqual(meta.status_code, 200)
        data = meta.json()
        self.assertEqual(data["node_id"], "node-test")
        self.assertEqual(data["name"], "测试节点")
        self.assertNotIn("frps", data)
        self.assertEqual(data["rooms"], 0)
        self.assertEqual(data["max_rooms"], 1)

    def test_room_codes_are_four_digit_and_unique(self) -> None:
        first = self._create_room()
        second = self._create_room(token=self.guest2)
        self.assertRegex(first["join_code"], JOIN_CODE_PATTERN)
        self.assertRegex(second["join_code"], JOIN_CODE_PATTERN)
        self.assertNotEqual(first["join_code"], second["join_code"])

    def test_hidden_room_not_in_public_list_but_searchable(self) -> None:
        hidden = self._create_room(hidden=True)
        listed = self.client.get(
            "/api/v1/rooms", headers=self._auth(self.guest)
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["rooms"], [])

        found = self._lookup(hidden["join_code"])
        self.assertEqual(found.status_code, 200)
        self.assertTrue(found.json()["hidden"])

    def test_public_room_list_requires_login(self) -> None:
        self._create_room()
        denied = self.client.get("/api/v1/rooms")
        self.assertEqual(denied.status_code, 401)

        listed = self.client.get("/api/v1/rooms", headers=self._auth(self.guest))
        self.assertEqual(listed.status_code, 200)
        rooms = listed.json()["rooms"]
        self.assertEqual(len(rooms), 1)
        self.assertFalse(rooms[0]["hidden"])

    def test_mods_round_trip_and_unavailable_provider(self) -> None:
        room = self._create_room(
            mods=[
                {
                    "filename": "sodium.jar",
                    "name": "Sodium",
                    "sha1": "abc123",
                    "modrinth_url": "https://modrinth.com/project/sodium",
                }
            ]
        )
        data = self._lookup(room["join_code"]).json()
        self.assertEqual(data["mod_count"], 1)
        self.assertTrue(data["mods_declared"])
        self.assertEqual(data["mods"][0]["filename"], "sodium.jar")
        self.assertEqual(data["mods"][0]["sha1"], "abc123")


    def test_unavailable_provider_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/rooms",
            headers=self._auth(self.host),
            json={
                "name": "测试",
                "version": "1.21.1",
                "loader": "fabric",
                "max_players": 4,
                "local_port": 25565,
                "provider": "loliafrp",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "PROVIDER_UNAVAILABLE")

    def test_lookup_requires_login(self) -> None:
        room = self._create_room()
        response = self.client.post(
            "/api/v1/rooms/lookup", json={"code": room["join_code"]}
        )
        self.assertEqual(response.status_code, 401)

    def test_lookup_is_rate_limited_on_failures(self) -> None:
        for _ in range(10):
            response = self._lookup("9998")
            self.assertEqual(response.status_code, 404)
        blocked = self._lookup("9998")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "RATE_LIMITED")

    def test_background_expiry(self) -> None:
        room = self._create_room()
        self.store.update_room_expiry(room["room_id"], time.time() - 1, 0)
        self.assertEqual(self.app.state.service.expire_rooms(), 1)
        self.assertEqual(self.store.get_room(room["room_id"])["status"], "expired")


if __name__ == "__main__":
    unittest.main()
