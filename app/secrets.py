"""密钥存储：令牌与 API Key 只进系统凭据管理器（keyring），不写明文配置。

keyring 后端不可用时（例如无 D-Bus 的服务器环境）降级为进程内存，
并对外暴露 ``available=False``，让界面明确提示用户。
"""

from __future__ import annotations

from typing import Optional

SERVICE_NAME = "minicraftlauncher"


class SecretStore:
    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service
        self._memory: dict[str, str] = {}
        self._backend_name = "memory"
        self._available = self._probe()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def _probe(self) -> bool:
        try:
            import keyring

            backend = keyring.get_keyring()
            if "fail" in type(backend).__module__:
                return False
            self._backend_name = type(backend).__module__.split(".")[-1]
            return True
        except Exception:  # noqa: BLE001 - 任意后端异常都视为不可用
            return False

    def get(self, key: str) -> Optional[str]:
        if not self._available:
            return self._memory.get(key)
        try:
            import keyring

            return keyring.get_password(self.service, key)
        except Exception:  # noqa: BLE001
            return self._memory.get(key)

    def set(self, key: str, value: str) -> None:
        if not value:
            self.delete(key)
            return
        if not self._available:
            self._memory[key] = value
            return
        import keyring

        keyring.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        self._memory.pop(key, None)
        if not self._available:
            return
        try:
            import keyring

            keyring.delete_password(self.service, key)
        except Exception:  # noqa: BLE001 - 不存在时忽略
            pass

    def masked(self, key: str) -> str:
        """返回脱敏后的值，供界面展示。"""
        value = self.get(key)
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}{'*' * 8}{value[-4:]}"