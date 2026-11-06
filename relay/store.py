"""房间与会话的 SQLite 存储。

服务以单进程运行，读写统一加锁；sqlite3 连接允许跨线程（后台清理任务会用到）。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    uuid       TEXT NOT NULL,
    name       TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id        TEXT PRIMARY KEY,
    join_code      TEXT NOT NULL UNIQUE,
    host_uuid      TEXT NOT NULL,
    host_name      TEXT NOT NULL,
    host_ip        TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL,
    version        TEXT NOT NULL,
    loader         TEXT NOT NULL,
    max_players    INTEGER NOT NULL,
    status         TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    local_port     INTEGER NOT NULL,
    secret_key     TEXT NOT NULL,
    hidden         INTEGER NOT NULL DEFAULT 0,
    provider       TEXT NOT NULL DEFAULT 'aml',
    mods           TEXT NOT NULL DEFAULT '[]',
    mods_declared  INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    max_expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rooms_code ON rooms(join_code);
CREATE INDEX IF NOT EXISTS idx_rooms_host ON rooms(host_uuid, status);

CREATE TABLE IF NOT EXISTS guests (
    room_id    TEXT NOT NULL,
    guest_uuid TEXT NOT NULL,
    guest_name TEXT NOT NULL,
    bind_port  INTEGER,
    joined_at  REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, guest_uuid)
);
"""

ROOM_FIELDS = (
    "room_id",
    "join_code",
    "host_uuid",
    "host_name",
    "host_ip",
    "name",
    "version",
    "loader",
    "max_players",
    "status",
    "node_id",
    "local_port",
    "secret_key",
    "hidden",
    "provider",
    "mods",
    "mods_declared",
    "created_at",
    "updated_at",
    "expires_at",
    "max_expires_at",
)


class RelayStore:
    """房间、访客、会话的持久化。"""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_columns()
            self._conn.commit()

    def _migrate_columns(self) -> None:
        """兼容旧库：补齐新增的房间字段。"""
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(rooms)").fetchall()
        }
        additions = {
            "hidden": "INTEGER NOT NULL DEFAULT 0",
            "provider": "TEXT NOT NULL DEFAULT 'aml'",
            "mods": "TEXT NOT NULL DEFAULT '[]'",
            "mods_declared": "INTEGER NOT NULL DEFAULT 0",
            "host_ip": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE rooms ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row(self, sql: str, params: tuple = ()) -> dict | None:
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # ---- 会话 ----

    def save_session(self, token_hash: str, uuid: str, name: str, expires_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token_hash, uuid, name, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, uuid, name, expires_at),
            )
            self._conn.commit()

    def get_session(self, token_hash: str, now: float) -> dict | None:
        with self._lock:
            return self._row(
                "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            )

    # ---- 房间 ----

    def create_room(self, room: dict) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO rooms ({', '.join(ROOM_FIELDS)}) VALUES ({', '.join('?' for _ in ROOM_FIELDS)})",
                tuple(room[key] for key in ROOM_FIELDS),
            )
            self._conn.commit()

    def get_room(self, room_id: str) -> dict | None:
        with self._lock:
            return self._row("SELECT * FROM rooms WHERE room_id = ?", (room_id,))

    def get_room_by_code(self, join_code: str) -> dict | None:
        with self._lock:
            return self._row("SELECT * FROM rooms WHERE join_code = ?", (join_code,))

    def count_host_rooms(self, host_uuid: str) -> int:
        """同账号当前未关闭（open 且未过期）的房间数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM rooms WHERE host_uuid = ? AND status = 'open'",
                (host_uuid,),
            ).fetchone()
            return int(row[0])

    def count_ip_rooms(self, host_ip: str) -> int:
        """同一公网 IP 当前未关闭的房间数。"""
        if not host_ip:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM rooms WHERE host_ip = ? AND status = 'open'",
                (host_ip,),
            ).fetchone()
            return int(row[0])

    def count_open_rooms(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM rooms WHERE status = 'open'").fetchone()
            return int(row[0])

    def list_public_rooms(self, now: float, heartbeat_timeout: int, limit: int = 60) -> list[dict]:
        """联机页公开房间：排除隐藏房间、过期与心跳超时的房间。"""
        with self._lock:
            return self._rows(
                "SELECT * FROM rooms WHERE status = 'open' AND hidden = 0 "
                "AND expires_at > ? AND updated_at + ? > ? "
                "ORDER BY created_at DESC LIMIT ?",
                (now, heartbeat_timeout, now, max(1, int(limit))),
            )

    def used_join_codes(self) -> set[str]:
        """已使用的 4 位数字房间号（含历史房间，避免重号）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT join_code FROM rooms WHERE length(join_code) = 4"
            ).fetchall()
            return {str(row[0]) for row in rows}

    def count_guests(self, room_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM guests WHERE room_id = ?", (room_id,)).fetchone()
            return int(row[0])

    def list_guests(self, room_id: str) -> list[dict]:
        with self._lock:
            return self._rows(
                "SELECT * FROM guests WHERE room_id = ? ORDER BY joined_at",
                (room_id,),
            )

    def get_guest(self, room_id: str, guest_uuid: str) -> dict | None:
        with self._lock:
            return self._row(
                "SELECT * FROM guests WHERE room_id = ? AND guest_uuid = ?",
                (room_id, guest_uuid),
            )

    def upsert_guest(self, room_id: str, guest: dict, max_players: int) -> bool:
        """加入访客；已存在则幂等更新。房间已满返回 False。"""
        with self._lock:
            existing = self.get_guest(room_id, guest["guest_uuid"])
            if existing:
                self._conn.execute(
                    "UPDATE guests SET guest_name = ?, bind_port = ?, updated_at = ? "
                    "WHERE room_id = ? AND guest_uuid = ?",
                    (
                        guest["guest_name"],
                        guest["bind_port"],
                        guest["updated_at"],
                        room_id,
                        guest["guest_uuid"],
                    ),
                )
                self._conn.commit()
                return True
            # 房主也算一名玩家，因此访客上限是 max_players - 1。
            if self.count_guests(room_id) >= max(0, max_players - 1):
                return False
            self._conn.execute(
                "INSERT INTO guests (room_id, guest_uuid, guest_name, bind_port, joined_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    guest["guest_uuid"],
                    guest["guest_name"],
                    guest["bind_port"],
                    guest["joined_at"],
                    guest["updated_at"],
                ),
            )
            self._conn.commit()
            return True

    def remove_guest(self, room_id: str, guest_uuid: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM guests WHERE room_id = ? AND guest_uuid = ?",
                (room_id, guest_uuid),
            )
            self._conn.commit()

    def set_room_status(self, room_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE rooms SET status = ? WHERE room_id = ?", (status, room_id))
            self._conn.commit()

    def update_room_expiry(self, room_id: str, expires_at: float, updated_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rooms SET expires_at = ?, updated_at = ? WHERE room_id = ?",
                (expires_at, updated_at, room_id),
            )
            self._conn.commit()

    def used_bind_ports(self) -> set[int]:
        """仍处于开放房间中已占用的访客本地端口。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT g.bind_port FROM guests g JOIN rooms r ON r.room_id = g.room_id "
                "WHERE r.status = 'open' AND g.bind_port IS NOT NULL"
            ).fetchall()
            return {int(row[0]) for row in rows}

    def expire_stale_rooms(self, now: float, heartbeat_timeout: int) -> int:
        """把心跳超时或已过期的房间标记为 expired，返回处理条数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE rooms SET status = 'expired' "
                "WHERE status = 'open' AND (expires_at <= ? OR updated_at + ? <= ?)",
                (now, heartbeat_timeout, now),
            )
            self._conn.commit()
            return cur.rowcount
