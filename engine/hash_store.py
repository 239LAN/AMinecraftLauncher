"""内容寻址的游戏文件存储（实验性）。

目录布局（全部位于启动器 data 目录下）：

- ``mcfile/<sha1>``        扁平哈希文件库，所有游戏文件只存一份
- ``mcfile/tree.json``     相对路径 -> {sha1, size, mtime} 的文件树映射
- ``.work/``               启动器运行期间物化出来的工作目录（退出时清空）
- ``.run/<id>/``           每次启动游戏的临时目录（退出后同步并删除）

开启后 ``.minecraft`` 不再保存游戏文件；启动器关闭时全部回收进 ``mcfile``，
因此这些文件无法被其它启动器直接读取。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

TREE_VERSION = 1
HASH_CHUNK = 1 << 20

# 只读目录优先硬链接，避免每次启动复制几十 GB。
LINK_PREFIXES = ("libraries/", "assets/")


class HashStoreError(Exception):
    pass


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _linkable(rel: str) -> bool:
    """游戏只会读、不会改写的文件可以安全硬链接。"""
    return rel.endswith(".jar") or rel.startswith(LINK_PREFIXES)


def _safe_rel(rel: str) -> str:
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith("../") or "/../" in rel or rel.startswith("/"):
        raise HashStoreError(f"非法相对路径：{rel}")
    return rel


class HashStore:
    """哈希文件库 + 文件树映射。所有写操作串行化，避免并发破坏映射。"""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.tree_path = self.base / "tree.json"
        self._lock = threading.RLock()
        self._entries: Optional[dict[str, dict]] = None

    # ------------------------------------------------------------------ 映射
    def load(self) -> dict[str, dict]:
        with self._lock:
            if self._entries is None:
                self._entries = self._read_tree()
            return self._entries

    def _read_tree(self) -> dict[str, dict]:
        if not self.tree_path.exists():
            return {}
        try:
            raw = json.loads(self.tree_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        files = raw.get("files")
        return files if isinstance(files, dict) else {}

    def _write_tree(self, entries: dict[str, dict]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.tree_path.with_name("tree.json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": TREE_VERSION, "files": entries},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.tree_path)
        self._entries = entries

    def entry(self, rel: str) -> Optional[dict]:
        return self.load().get(_safe_rel(rel))

    def blob_path(self, sha1: str) -> Path:
        return self.base / sha1

    def stats(self) -> dict:
        entries = self.load()
        blobs = [p for p in self.base.glob("*") if p.is_file() and p.name != "tree.json"]
        blob_bytes = sum(p.stat().st_size for p in blobs)
        refs = {str(item.get("sha1")) for item in entries.values()}
        return {
            "enabled": True,
            "files": len(entries),
            "logical_bytes": sum(int(item.get("size") or 0) for item in entries.values()),
            "blobs": len(blobs),
            "stored_bytes": blob_bytes,
            "referenced": len(refs),
            "saved_bytes": max(
                0,
                sum(int(item.get("size") or 0) for item in entries.values()) - blob_bytes,
            ),
        }

    # ------------------------------------------------------------------ 采集
    def capture(self, root: Path, *, prune: bool = True) -> dict:
        """把工作目录同步进文件树：新增/变更入库，按需删除映射与孤立哈希。"""
        root = Path(root)
        if not root.is_dir():
            return {"added": 0, "changed": 0, "removed": 0, "blobs": 0}

        with self._lock:
            old = dict(self.load())
            new_entries: dict[str, dict] = {}
            added = changed = blobs_written = 0

            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
                for name in filenames:
                    path = Path(dirpath) / name
                    if name.endswith((".part", ".tmp")) or not path.is_file():
                        continue
                    rel = _safe_rel(path.relative_to(root).as_posix())
                    stat = path.stat()
                    prev = old.get(rel)
                    if (
                        prev
                        and int(prev.get("size") or -1) == stat.st_size
                        and abs(float(prev.get("mtime") or 0) - stat.st_mtime) < 1
                    ):
                        new_entries[rel] = prev
                        continue

                    digest = sha1_file(path)
                    blob = self.blob_path(digest)
                    if not blob.exists() or blob.stat().st_size != stat.st_size:
                        self._write_blob(path, blob)
                        blobs_written += 1
                    if prev and prev.get("sha1") == digest:
                        changed += 1
                    else:
                        added += 1
                    mtime = blob.stat().st_mtime if _linkable(rel) else stat.st_mtime
                    new_entries[rel] = {
                        "sha1": digest,
                        "size": stat.st_size,
                        "mtime": mtime,
                    }

            removed = len(set(old) - set(new_entries))
            self._write_tree(new_entries)
            if prune:
                self.gc()
            return {
                "added": added,
                "changed": changed,
                "removed": removed,
                "blobs": blobs_written,
            }

    def _write_blob(self, source: Path, blob: Path) -> None:
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_name(blob.name + ".tmp")
        shutil.copy2(source, tmp)
        os.replace(tmp, blob)

    def gc(self) -> int:
        """删除不再被文件树引用的哈希文件。"""
        with self._lock:
            entries = self.load()
            refs = {str(item.get("sha1")) for item in entries.values()}
            removed = 0
            for path in self.base.iterdir():
                if path.name == "tree.json" or not path.is_file():
                    continue
                if path.name not in refs:
                    path.unlink(missing_ok=True)
                    removed += 1
            return removed

    # ------------------------------------------------------------------ 物化
    def materialize(self, dest: Path) -> int:
        """按文件树把内容铺到 dest：只读文件硬链接，其余复制。"""
        dest = Path(dest)
        self.clear(dest)
        dest.mkdir(parents=True, exist_ok=True)
        entries = self.load()
        missing: list[str] = []
        count = 0
        for rel, item in entries.items():
            blob = self.blob_path(str(item.get("sha1")))
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not blob.exists():
                missing.append(rel)
                continue
            if target.exists():
                target.unlink()
            if _linkable(rel):
                try:
                    os.link(blob, target)
                    count += 1
                    continue
                except OSError:
                    pass
            shutil.copy2(blob, target)
            try:
                mtime = float(item.get("mtime") or 0)
                if mtime:
                    os.utime(target, (mtime, mtime))
            except OSError:
                pass
            count += 1
        if missing:
            raise HashStoreError(
                f"{len(missing)} 个文件在哈希库中缺失，请先补全文件（示例：{missing[0]}）"
            )
        return count

    def clear(self, root: Path) -> None:
        root = Path(root)
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------------ 迁移
    def migrate_in(self, game_root: Path, work_root: Path) -> dict:
        """原子启用：先入库，再改名原目录，最后物化工作目录。

        任何一步失败都会把原目录恢复回去，避免出现"文件两边都没有"。
        """
        game_root = Path(game_root)
        work_root = Path(work_root)
        result = self.capture(game_root)
        backup = game_root.with_name(game_root.name + ".migrating")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        moved = False
        if game_root.exists():
            os.replace(game_root, backup)
            moved = True
        try:
            self.clear(work_root)
            self.materialize(work_root)
        except Exception:
            if moved and backup.exists():
                shutil.rmtree(game_root, ignore_errors=True)
                os.replace(backup, game_root)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return result

    def migrate_out(self, work_root: Path, game_root: Path) -> None:
        """原子关闭：把哈希库还原成普通 .minecraft，再删除工作目录。"""
        game_root = Path(game_root)
        work_root = Path(work_root)
        # 工作目录不存在时说明本次会话没有物化，不能拿空目录覆盖文件树。
        if work_root.exists():
            self.capture(work_root)
        temp = game_root.with_name(game_root.name + ".restoring")
        self.clear(temp)
        self.materialize(temp)
        if game_root.exists():
            shutil.rmtree(game_root, ignore_errors=True)
        os.replace(temp, game_root)
        self.clear(work_root)

    # ------------------------------------------------------------------ 运行目录
    def new_run_dir(self, base: Path) -> Path:
        base = Path(base)
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"run-{int(time.time() * 1000)}-{os.getpid()}"
        self.clear(path)
        return path

    def prune_runs(self, base: Path, keep: Optional[Path] = None) -> None:
        base = Path(base)
        if not base.exists():
            return
        for path in base.iterdir():
            if path == keep:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
