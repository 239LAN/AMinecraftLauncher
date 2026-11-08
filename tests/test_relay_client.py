"""联机客户端：frpc 配置生成与局域网端口检测。"""

import tempfile
import unittest
from pathlib import Path

from engine import relay

HOST_FRPC = {
    "server_addr": "relay.example.com",
    "server_port": 3300,
    "tls": True,
    "auth_token": "token-1",
    "proxy": {
        "name": "aml_ROOM1",
        "type": "stcp",
        "secret_key": "secret",
        "local_ip": "127.0.0.1",
        "local_port": 25565,
    },
}


class FrpcConfigTest(unittest.TestCase):
    def test_host_config(self) -> None:
        text = relay.frpc_config_text(HOST_FRPC, kind="host")
        self.assertIn('serverAddr = "relay.example.com"', text)
        self.assertIn("serverPort = 3300", text)
        self.assertIn("transport.tls.enable = true", text)
        self.assertIn("[[proxies]]", text)
        self.assertIn('secretKey = "secret"', text)
        self.assertIn("localPort = 25565", text)

    def test_visitor_config(self) -> None:
        frpc = {
            **HOST_FRPC,
            "proxy": {
                "name": "aml_v_ROOM1",
                "type": "stcp",
                "role": "visitor",
                "server_name": "aml_ROOM1",
                "secret_key": "secret",
                "bind_addr": "127.0.0.1",
                "bind_port": 25601,
            },
        }
        text = relay.frpc_config_text(frpc, kind="guest")
        self.assertIn("[[visitors]]", text)
        self.assertIn('serverName = "aml_ROOM1"', text)
        self.assertIn("bindPort = 25601", text)
        self.assertNotIn("[[proxies]]", text)


class LanPortTest(unittest.TestCase):
    def write_log(self, text: str) -> Path:
        root = Path(tempfile.mkdtemp())
        path = root / "latest.log"
        path.write_text(text, encoding="utf-8")
        return path

    def test_detects_latest_port(self) -> None:
        log = self.write_log(
            "[12:00:00] [Server thread/INFO]: Local game hosted on port 51234\n"
            "[12:05:00] [Server thread/INFO]: Local game hosted on port 51888\n"
        )
        self.assertEqual(relay.detect_lan_port(log), 51888)

    def test_missing_log_falls_back(self) -> None:
        self.assertEqual(relay.detect_lan_port(Path("no-such.log")), 25565)

    def test_detects_new_lan_log_text(self) -> None:
        # 26.x 的日志文案，旧正则匹配不到会退回默认 25565。
        log = self.write_log(
            "[23:30:04] [Render thread/INFO]: Published LAN server on port 51234\n"
        )
        self.assertEqual(relay.detect_lan_port(log), 51234)

    def test_visitor_port_is_free(self) -> None:
        port = relay.pick_free_port()
        self.assertGreaterEqual(port, relay.DEFAULT_BIND_RANGE[0])
        self.assertLessEqual(port, relay.DEFAULT_BIND_RANGE[1])


if __name__ == "__main__":
    unittest.main()
