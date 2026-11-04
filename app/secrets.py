"""密钥存储：令牌与 API Key 只进系统凭据管理器（keyring），不写明文配置。

keyring 后端不可用时（例如无 D-Bus 的服务器环境）降级为进程内存，
并对外暴露 ``available=False``，让界面明确提示用户。
"""

from __future__ import annotations

from typing import Optional

SERVICE_NAME = "minicraftlauncher"

# Windows 凭据管理器的单条记录上限约 2560 字节，超出会报 WinError 1783。
# 令牌 JSON 往往超过该上限，因此按此长度分片写入多条凭据。
MAX_CHUNK = 1000


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

    def _chunk_key(self, key: str, index: int) -> str:
        return f"{key}#{index}"

    def _count_key(self, key: str) -> str:
        return f"{key}#count"

    def get(self, key: str) -> Optional[str]:
        if not self._available:
            return self._memory.get(key)
        try:
            import keyring

            raw_count = keyring.get_password(self.service, self._count_key(key))
            count = int(raw_count) if raw_count else 0
            if count:
                parts = []
                for index in range(count):
                    chunk = keyring.get_password(self.service, self._chunk_key(key, index))
                    if chunk is None:
                        return self._memory.get(key)
                    parts.append(chunk)
                return "".join(parts)
            # 兼容未分片的旧记录。
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

        chunks = [value[i:i + MAX_CHUNK] for i in range(0, len(value), MAX_CHUNK)]
        for index, chunk in enumerate(chunks):
            keyring.set_password(self.service, self._chunk_key(key, index), chunk)
        keyring.set_password(self.service, self._count_key(key), str(len(chunks)))
        # 清理旧的超长分片，避免读到过期内容。
        index = len(chunks)
        while keyring.get_password(self.service, self._chunk_key(key, index)) is not None:
            keyring.delete_password(self.service, self._chunk_key(key, index))
            index += 1
        try:
            keyring.delete_password(self.service, key)
        except Exception:  # noqa: BLE001 - 旧记录不存在时忽略
            pass

    def delete(self, key: str) -> None:
        self._memory.pop(key, None)
        if not self._available:
            return
        try:
            import keyring

            raw_count = keyring.get_password(self.service, self._count_key(key))
            count = int(raw_count) if raw_count else 0
            for index in range(count):
                try:
                    keyring.delete_password(self.service, self._chunk_key(key, index))
                except Exception:  # noqa: BLE001
                    pass
            for name in (self._count_key(key), key):
                try:
                    keyring.delete_password(self.service, name)
                except Exception:  # noqa: BLE001 - 不存在时忽略
                    pass
        except Exception:  # noqa: BLE001
            pass


    def masked(self, key: str) -> str:
        """返回脱敏后的值，供界面展示。"""
        value = self.get(key)
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}{'*' * 8}{value[-4:]}"