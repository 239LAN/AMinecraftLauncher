"""relay 会话令牌。"""

from __future__ import annotations

import hashlib
import secrets
import time

SESSION_TTL = 3600


def hash_token(token: str) -> str:
    """会话令牌只存 SHA256，避免数据库泄露后可直接使用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session() -> tuple[str, float]:
    """生成新会话令牌，返回 (明文令牌, 过期时间)。"""
    token = secrets.token_urlsafe(32)
    return token, time.time() + SESSION_TTL
