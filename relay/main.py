"""relay 进程入口。

    AML_ROLE=node   python -m relay.main     # 监听 3375，提供房间服务
    AML_ROLE=master python -m relay.main     # 监听 3380，提供节点列表
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from relay.config import RelayConfig, load_config
from relay.master import create_master_app
from relay.node import create_node_app


def create_app(
    config: RelayConfig | None = None,
    start_background: bool = True,
) -> FastAPI:
    """按 AML_ROLE 构建应用；主节点不占用数据库。"""
    cfg = config or load_config()
    if cfg.is_master:
        return create_master_app(cfg)
    return create_node_app(cfg, start_background=start_background)


CONFIG = load_config()
app = create_app(CONFIG)


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("AML_API_HOST", "0.0.0.0"), port=CONFIG.api_port)
