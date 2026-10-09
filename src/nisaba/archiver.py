#!/usr/bin/env python3
"""Resumable cold archive of a ChatGPT account.

Writes ONLY to --output. Five passes: discover -> conversation JSON ->
markdown render -> project files -> conversation attachments -> indexes.
Resumes from `<output>/progress.json`; files already on disk are skipped.

Uses the live-tested ChatGPTHttpClient (Origin/Referer, retries,
auto-refresh of access token via session cookie on 401/403).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Repo root on sys.path when launched as a script.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from nisaba.markdown import (
    conversation_to_markdown,
    extract_file_references,
)
from nisaba.timeutil import to_iso_utc
from nisaba.client import (
    ChatGPTAuthError,
    ChatGPTHttpClient,
    normalize_listing_item,
)


DEFAULT_OUTPUT = Path(
    r"C:\Users\Timur Shchukin\Downloads\ChatGPT_Archive_2026-08-18"
)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
MAX_ITEM_RETRIES = 5
RETRYABLE_CLASSES = {"NETWORK", "OTHER", "RATE_LIMIT", "FILE_DOWNLOAD"}
LISTING_SIDECARS = ("listing_regular", "listing_archived")
MAX_ITEM_RETRIES = 5
RETRYABLE_CLASSES = {"NETWORK", "OTHER", "RATE_LIMIT", "FILE_DOWNLOAD"}
LISTING_SIDECARS = ("listing_regular", "listing_archived")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(text: str, limit: int = 80) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in " ._-" else "_"
        for ch in (text or "")
    ).strip(" ._")
    return (cleaned or "untitled")[:limit]


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, ChatGPTAuthError):
        return "AUTH"
    msg = str(exc).lower()
    name = type(exc).__name__
    if "429" in msg or "rate" in msg:
        return "RATE_LIMIT"
    if "404" in msg or "not found" in msg:
        return "MISSING"
    if "403" in msg or "forbidden" in msg:
        return "FORBIDDEN"
    if "incomplete" in msg or "network" in msg or "timeout" in name.lower():
        return "NETWORK"
    if "file" in msg or "download" in msg:
        return "FILE_DOWNLOAD"
    if "json" in msg or "schema" in msg:
        return "SCHEMA"
    return "OTHER"


def _failed_is_conversation(item: Dict[str, Any]) -> bool:
    kind = str(item.get("type") or item.get("object_type") or "")
    return kind.startswith("conversation")


def _terminal_for_error(kind: str, retries: int) -> str:
    if kind == "MISSING":
        return "DELETED_REMOTE"
    if kind == "FORBIDDEN":
        return "INACCESSIBLE"
    if kind == "AUTH":
        return "INACCESSIBLE"
    if kind in RETRYABLE_CLASSES and retries < MAX_ITEM_RETRIES:
        return "PENDING"
    return "FAILED_FINAL"


def _min_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    vals = [x for x in (a, b) if x]
    return min(vals) if vals else None


def _max_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    vals = [x for x in (a, b) if x]
    return max(vals) if vals else None


def _pid_alive(pid: Any) -> bool:
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    try:
        os.kill(pid_i, 0)
    except OSError:
        return False
    return True


def _load_last_error(store: "ArchiveStore") -> str | None:
    if not store.errors_path.is_file():
        return None
    last = None
    for line in store.errors_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            last = line
    if not last:
        return None
    try:
        rec = json.loads(last)
        return f"{rec.get('class')}:{rec.get('object_type')}:{rec.get('message')}"
    except Exception:
        return last[:200]


class ArchiveStore:
    def __init__(self, root: Path):
        self.root = root
        self.reports = root / "reports"
        self.progress_path = root / "progress.json"
        self.latest_path = self.reports / "latest_progress.json"
        self.errors_path = root / "errors.jsonl"
        self.conversations_jsonl = root / "conversations.jsonl"
        self.files_jsonl = root / "files.jsonl"
        self.projects_json = root / "projects.json"
        self.account_manifest = root / "account_manifest.json"
        self.lock_path = self.reports / "worker.lock"
        self.pid_path = self.reports / "worker.pid"
        self.human_log = self.reports / "archive.log"
        self.root.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        (self.root / "conversations").mkdir(exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)

    def load_progress(self) -> Dict[str, Any]:
        if not self.progress_path.is_file():
            return {
                "status": "RUNNING",
                "auth_status": "OK",
                "discovered_conversations": [],
                "completed_conversation_ids": [],
                "completed_markdown_ids": [],
                "completed_file_ids": [],
                "completed_project_ids": [],
                "completed_project_file_ids": [],
                "failed_items": [],
                "unique_conversations": 0,
                "conversation_json_done": 0,
                "markdown_done": 0,
                "projects_done": 0,
                "files_done": 0,
                "files_failed": 0,
                "bytes_downloaded": 0,
                "last_success_at": None,
                "last_checkpoint": None,
                "pass": "discover",
            }
        data = json.loads(self.progress_path.read_text(encoding="utf-8"))
        for key in LISTING_SIDECARS:
            sidecar = self.reports / f"{key}.json"
            if key not in data and sidecar.is_file():
                try:
                    data[key] = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    data[key] = []
        return data

    def _latest_payload(self, progress: Dict[str, Any]) -> Dict[str, Any]:
        failed = progress.get("failed_items") or []
        conv_failed = sum(1 for x in failed if _failed_is_conversation(x))
        status_map = progress.get("conversation_status") or {}
        pending = sum(1 for v in status_map.values() if v == "PENDING")
        return {
            "status": progress.get("status"),
            "pid": progress.get("pid"),
            "worker_started_at": progress.get("worker_started_at"),
            "heartbeat_at": progress.get("heartbeat_at"),
            "auth_status": progress.get("auth_status"),
            "phase": progress.get("pass"),
            "pass": progress.get("pass"),
            "archive_root": str(self.root),
            "conversations_discovered": progress.get("unique_conversations", 0),
            "conversations_complete": progress.get("conversation_json_done", 0),
            "conversations_failed": conv_failed,
            "conversations_pending": pending,
            "files_discovered": progress.get("files_discovered", 0),
            "files_complete": progress.get("files_done", 0),
            "files_failed": progress.get("files_failed", 0),
            "attachments_discovered": progress.get("attachments_discovered", 0),
            "attachment_discovery_status": progress.get("attachment_discovery_status"),
            "pagination_complete": progress.get("pagination_complete"),
            "current_item": progress.get("current_item"),
            "last_remote_watermark": progress.get("last_remote_watermark"),
            "oldest_remote_timestamp_seen": progress.get("oldest_remote_timestamp_seen"),
            "newest_remote_timestamp_seen": progress.get("newest_remote_timestamp_seen"),
            "last_error": _load_last_error(self),
            "resume_checkpoint": progress.get("last_checkpoint"),
            "duplicate_worker_count": progress.get("duplicate_worker_count", 0),
            "unique_conversations": progress.get("unique_conversations", 0),
            "conversation_json_done": progress.get("conversation_json_done", 0),
            "markdown_done": progress.get("markdown_done", 0),
            "projects_done": progress.get("projects_done", 0),
            "files_done": progress.get("files_done", 0),
            "bytes_downloaded": progress.get("bytes_downloaded", 0),
            "last_checkpoint": progress.get("last_checkpoint"),
        }

    def heartbeat(self, progress: Dict[str, Any], *, full: bool = False) -> None:
        progress["heartbeat_at"] = _now_iso()
        progress["pid"] = os.getpid()
        progress["archive_root"] = str(self.root)
        if full:
            progress["last_checkpoint"] = progress["heartbeat_at"]
        latest = self._latest_payload(progress)
        _atomic_write_json(self.latest_path, latest)
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        if full:
            slim = dict(progress)
            for key in LISTING_SIDECARS:
                blob = slim.get(key)
                if isinstance(blob, list) and blob:
                    _atomic_write_json(self.reports / f"{key}.json", blob)
                slim.pop(key, None)
            _atomic_write_json(self.progress_path, slim)
            self.append_log(
                f"status={latest.get('status')} pass={latest.get('pass')} "
                f"discovered={latest.get('conversations_discovered')} "
                f"complete={latest.get('conversations_complete')} "
                f"item={latest.get('current_item')}"
            )

    def save_progress(self, progress: Dict[str, Any]) -> None:
        self.heartbeat(progress, full=True)

    def append_log(self, line: str) -> None:
        self.reports.mkdir(parents=True, exist_ok=True)
        with self.human_log.open("a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} pid={os.getpid()} {line}\n")

    def acquire_lock(self) -> bool:
        self.reports.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_file():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            old = data.get("pid")
            if old and _pid_alive(old) and int(old) != os.getpid():
                return False
        payload = {
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "command": "chatgpt_full_archive.py",
        }
        _atomic_write_json(self.lock_path, payload)
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release_lock(self) -> None:
        try:
            if not self.lock_path.is_file():
                return
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) == os.getpid():
                self.lock_path.unlink()
        except Exception:
            return

    def conv_dir(self, cid: str) -> Path:
        d = self.root / "conversations" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "files").mkdir(exist_ok=True)
        return d

    def project_dir(self, pid: str, name: str) -> Path:
        d = self.root / "projects" / f"{pid}__{_safe_name(name)}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "files").mkdir(exist_ok=True)
        return d

    def record_error(
        self,
        *,
        kind: str,
        object_type: str,
        object_id: str,
        message: str,
    ) -> None:
        rec = {
            "ts": _now_iso(),
            "class": kind,
            "object_type": object_type,
            "object_id": object_id,
            "message": (message or "")[:400],
        }
        if JWT_RE.search(rec["message"]):
            rec["message"] = "[redacted]"
        _append_jsonl(self.errors_path, rec)


def _write_readme(root: Path) -> None:
    path = root / "README_ARCHIVE.md"
    if path.is_file():
        return
    path.write_text(
        "\n".join([
            "# ChatGPT cold archive",
            "",
            "Standalone account archive produced by Nisaba.",
            "",
            "Authoritative conversation body: `conversations/<uuid>/conversation.json`.",
            "Markdown is a local convenience rendering.",
            "",
            "Resume:",
            "",
            "```",
            "python scripts/ops/chatgpt_full_archive.py --resume --output <this-dir>",
            "```",
            "",
        ]),
        encoding="utf-8",
    )


def _session_alive(client: ChatGPTHttpClient) -> bool:
    """403 on one object is not always expiry; probe listing."""
    try:
        client.list_conversations(offset=0, limit=1, archived=False)
        return True
    except ChatGPTAuthError:
        return False


def _list_conversations_retry(
    client: ChatGPTHttpClient,
    *,
    progress: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> Dict[str, Any]:
    last: Exception | None = None
    for attempt in range(6):
        try:
            return client.list_conversations(**kwargs)
        except ChatGPTAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if progress is not None:
                progress["listing_retries"] = int(progress.get("listing_retries") or 0) + 1
            time.sleep(min(30, 3 * (attempt + 1)))
    if progress is not None:
        progress["listing_failures"] = int(progress.get("listing_failures") or 0) + 1
    assert last is not None
    raise last


def paginate_conversations(
    client: ChatGPTHttpClient,
    *,
    archived: bool,
    store: Optional[ArchiveStore] = None,
    progress: Optional[Dict[str, Any]] = None,
    listing_key: str = "regular",
) -> List[Dict[str, Any]]:
    """List until remote pagination is exhausted. Always restarts at offset 0.

    Restarting is required so newly updated conversations at the head of
    ``order=updated`` are not skipped after a crash at a high offset.
    """
    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    offset = 0
    exhausted = False
    while True:
        page = _list_conversations_retry(
            client, progress=progress, offset=offset, limit=28,
            archived=archived, order="updated",
        )
        batch = page.get("items") or []
        if not batch:
            exhausted = True
            break
        for raw in batch:
            item = normalize_listing_item(raw, archived=archived)
            cid = item["conversation_id"]
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(item)
            if progress is not None:
                ts = item.get("update_time") or item.get("create_time")
                progress["oldest_remote_timestamp_seen"] = _min_iso(
                    progress.get("oldest_remote_timestamp_seen"), ts,
                )
                progress["newest_remote_timestamp_seen"] = _max_iso(
                    progress.get("newest_remote_timestamp_seen"), ts,
                )
                if progress.get("newest_remote_timestamp_seen"):
                    progress["last_remote_watermark"] = progress["newest_remote_timestamp_seen"]
        offset += len(batch)
        if progress is not None and store is not None:
            progress[f"listing_{listing_key}"] = items
            progress[f"offset_{listing_key}"] = offset
            progress["unique_conversations"] = max(
                int(progress.get("unique_conversations") or 0),
                len(seen),
            )
            progress["current_item"] = f"discover:{listing_key}:offset={offset}"
            store.save_progress(progress)
        if not page.get("has_more"):
            exhausted = True
            break
    if progress is not None:
        flags = dict(progress.get("pagination_flags") or {})
        flags[listing_key] = {
            "pagination_complete": exhausted,
            "offset": offset,
            "count": len(items),
            "empty_terminal_page": not bool(batch) if offset else exhausted,
        }
        progress["pagination_flags"] = flags
    return items


def discover(client: ChatGPTHttpClient, store: ArchiveStore, progress: Dict[str, Any], args) -> None:
    print("PASS 1 discover", flush=True)
    progress["pass"] = "discover"
    progress["status"] = "RUNNING"
    store.save_progress(progress)

    regular = paginate_conversations(
        client, archived=False, store=store, progress=progress, listing_key="regular",
    )
    archived: List[Dict[str, Any]] = []
    if args.include_archived:
        archived = paginate_conversations(
            client, archived=True, store=store, progress=progress, listing_key="archived",
        )

    projects: List[Dict[str, Any]] = []
    project_convs: Dict[str, List[Dict[str, Any]]] = {}
    if args.include_projects:
        projects = client.list_projects()
        for i, proj in enumerate(projects, 1):
            pid = proj["id"]
            try:
                project_convs[pid] = client.list_project_conversations(pid)
            except ChatGPTAuthError:
                raise
            except Exception as e:
                store.record_error(
                    kind=_classify_error(e),
                    object_type="project_conversations",
                    object_id=pid,
                    message=str(e),
                )
                project_convs[pid] = []
            if i % 10 == 0:
                progress["projects_done"] = i
                store.save_progress(progress)

    merged: Dict[str, Dict[str, Any]] = {}
    memberships: Dict[str, List[Dict[str, str]]] = {}

    def add_listing(item: Dict[str, Any], source: str) -> None:
        cid = item["conversation_id"]
        if not cid:
            return
        prev = merged.get(cid)
        if prev is None:
            merged[cid] = dict(item)
        else:
            if not prev.get("project_id") and item.get("project_id"):
                prev["project_id"] = item.get("project_id")
                prev["project_name"] = item.get("project_name")
            if item.get("archived"):
                prev["archived"] = True
        memberships.setdefault(cid, [])
        tag = {"source": source}
        if item.get("project_id"):
            tag["project_id"] = item["project_id"]
            tag["project_name"] = item.get("project_name") or ""
        memberships[cid].append(tag)

    for it in regular:
        add_listing(it, "regular")
    for it in archived:
        add_listing(it, "archived")
    for proj in projects:
        pid = proj["id"]
        pname = proj.get("name") or ""
        for raw in project_convs.get(pid, []):
            item = normalize_listing_item(
                raw, project_id=pid, project_name=pname,
            )
            add_listing(item, "project")

    for cid, item in merged.items():
        item["memberships"] = memberships.get(cid, [])

    index = {
        "regular_conversations": len(regular),
        "archived_conversations": len(archived),
        "project_conversations": sum(len(v) for v in project_convs.values()),
        "unique_conversation_uuids": len(merged),
        "projects": len(projects),
        "project_files": sum(len(p.get("files") or []) for p in projects),
        "discovered_at": _now_iso(),
    }
    _atomic_write_json(store.root / "discovery.json", index)
    _atomic_write_json(store.projects_json, {
        "projects": projects,
        "project_conversation_counts": {
            pid: len(project_convs.get(pid, [])) for pid in project_convs
        },
    })
    for proj in projects:
        pid = proj["id"]
        pdir = store.project_dir(pid, proj.get("name") or "")
        payload = dict(proj)
        payload["conversations"] = [
            normalize_listing_item(
                raw, project_id=pid, project_name=proj.get("name") or "",
            )
            for raw in project_convs.get(pid, [])
        ]
        _atomic_write_json(pdir / "project.json", payload)
        _atomic_write_json(pdir / "conversations.json", payload["conversations"])
        instructions = proj.get("instructions") or proj.get("description") or ""
        if instructions:
            (pdir / "instructions.txt").write_text(str(instructions), encoding="utf-8")

    # Rewrite conversations.jsonl from merged index (resume-safe replace).
    tmp = store.conversations_jsonl.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for cid, item in merged.items():
            f.write(json.dumps({
                "conversation_id": cid,
                "title": item.get("title"),
                "create_time": item.get("create_time"),
                "update_time": item.get("update_time"),
                "archived": bool(item.get("archived")),
                "project_id": item.get("project_id"),
                "project_name": item.get("project_name"),
                "memberships": item.get("memberships") or [],
            }, ensure_ascii=False) + "\n")
    tmp.replace(store.conversations_jsonl)

    _atomic_write_json(store.account_manifest, {
        "archive_root": str(store.root),
        "created_at": _now_iso(),
        "transport": "nisaba.client.ChatGPTHttpClient",
        "discovery": index,
        "note": "Secrets are never stored in this archive.",
    })

    progress["discovered_conversations"] = sorted(merged.keys())
    progress["unique_conversations"] = len(merged)
    progress["final_conversations_discovered"] = len(merged)
    progress["projects_discovered"] = len(projects)
    progress["files_discovered"] = int(index.get("project_files") or 0)
    progress["discovery"] = index
    flags = progress.get("pagination_flags") or {}
    regular_ok = bool((flags.get("regular") or {}).get("pagination_complete"))
    archived_ok = (not args.include_archived) or bool(
        (flags.get("archived") or {}).get("pagination_complete")
    )
    progress["pagination_complete"] = bool(regular_ok and archived_ok)
    progress["last_success_at"] = _now_iso()
    store.save_progress(progress)
    print(
        "discover",
        f"regular={len(regular)} archived={len(archived)} "
        f"project_convs={index['project_conversations']} "
        f"unique={len(merged)} projects={len(projects)} "
        f"pagination_complete={progress['pagination_complete']}",
        flush=True,
    )


def _json_complete(cdir: Path) -> bool:
    p = cdir / "conversation.json"
    if not p.is_file() or p.stat().st_size < 2:
        return False
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def download_jsons(
    client: ChatGPTHttpClient,
    store: ArchiveStore,
    progress: Dict[str, Any],
) -> None:
    print("PASS 2 conversation JSON", flush=True)
    progress["pass"] = "json"
    ids: List[str] = list(progress.get("discovered_conversations") or [])
    done: Set[str] = set(progress.get("completed_conversation_ids") or [])
    status_map: Dict[str, str] = dict(progress.get("conversation_status") or {})
    retry_counts: Dict[str, int] = {
        str(k): int(v) for k, v in (progress.get("retry_counts") or {}).items()
    }
    n_since_ckpt = 0
    for i, cid in enumerate(ids, 1):
        cdir = store.conv_dir(cid)
        if _json_complete(cdir):
            done.add(cid)
            status_map[cid] = "COMPLETE"
            continue
        if status_map.get(cid) in {"DELETED_REMOTE", "INACCESSIBLE", "FAILED_FINAL", "SKIPPED_WITH_REASON"}:
            continue
        try:
            body = client.fetch_conversation(cid)
            raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            (cdir / "conversation.json").write_bytes(raw)
            meta = {
                "conversation_id": cid,
                "title": body.get("title"),
                "create_time": to_iso_utc(body.get("create_time")),
                "update_time": to_iso_utc(body.get("update_time")),
                "gizmo_id": body.get("gizmo_id") or body.get("project_id"),
                "json_sha256": _sha256_bytes(raw),
                "json_bytes": len(raw),
                "archive_status": "COMPLETE",
                "classification": "COMPLETE",
                "source_acquired_at": _now_iso(),
            }
            _atomic_write_json(cdir / "metadata.json", meta)
            done.add(cid)
            status_map[cid] = "COMPLETE"
            progress["bytes_downloaded"] = int(progress.get("bytes_downloaded") or 0) + len(raw)
            progress["last_success_at"] = _now_iso()
        except ChatGPTAuthError as e:
            if not _session_alive(client):
                raise
            store.record_error(
                kind="FORBIDDEN",
                object_type="conversation",
                object_id=cid,
                message=str(e),
            )
            status_map[cid] = "INACCESSIBLE"
            progress.setdefault("failed_items", []).append(
                {"type": "conversation", "id": cid, "class": "FORBIDDEN",
                 "classification": "INACCESSIBLE", "ts": _now_iso()}
            )
            _atomic_write_json(cdir / "metadata.json", {
                "conversation_id": cid,
                "classification": "INACCESSIBLE",
                "archive_status": "INACCESSIBLE",
                "error": "FORBIDDEN",
            })
        except Exception as e:
            kind = _classify_error(e)
            retry_counts[cid] = int(retry_counts.get(cid) or 0) + 1
            terminal = _terminal_for_error(kind, retry_counts[cid])
            store.record_error(
                kind=kind,
                object_type="conversation",
                object_id=cid,
                message=str(e),
            )
            status_map[cid] = terminal
            progress.setdefault("failed_items", []).append(
                {"type": "conversation", "id": cid, "class": kind,
                 "classification": terminal, "retries": retry_counts[cid],
                 "ts": _now_iso()}
            )
            _atomic_write_json(cdir / "metadata.json", {
                "conversation_id": cid,
                "classification": terminal,
                "archive_status": terminal,
                "error": kind,
                "retries": retry_counts[cid],
                "message": str(e)[:200],
            })
        n_since_ckpt += 1
        progress["completed_conversation_ids"] = sorted(done)
        progress["conversation_json_done"] = len(done)
        progress["conversation_status"] = status_map
        progress["retry_counts"] = retry_counts
        progress["current_item"] = f"json:{cid}"
        store.heartbeat(progress, full=(n_since_ckpt >= 10 or i == len(ids)))
        if n_since_ckpt >= 10 or i == len(ids):
            print(f"json {len(done)}/{len(ids)}", flush=True)
            n_since_ckpt = 0
    progress["completed_conversation_ids"] = sorted(done)
    progress["conversation_json_done"] = len(done)
    progress["conversation_status"] = status_map
    progress["retry_counts"] = retry_counts
    store.save_progress(progress)


def write_markdown(store: ArchiveStore, progress: Dict[str, Any]) -> None:
    print("PASS 3 markdown (local)", flush=True)
    progress["pass"] = "markdown"
    ids: List[str] = list(progress.get("discovered_conversations") or [])
    done: Set[str] = set(progress.get("completed_markdown_ids") or [])
    n_since_ckpt = 0
    for cid in ids:
        cdir = store.conv_dir(cid)
        jp = cdir / "conversation.json"
        mp = cdir / "conversation.md"
        if cid in done and mp.is_file():
            continue
        if not jp.is_file():
            continue
        try:
            body = json.loads(jp.read_text(encoding="utf-8"))
            md = conversation_to_markdown(body)
            mp.write_text(md, encoding="utf-8")
            digest = _sha256_bytes(md.encode("utf-8"))
            meta_path = cdir / "metadata.json"
            meta = {}
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["markdown_sha256"] = digest
            meta["archive_status"] = "json_markdown"
            _atomic_write_json(meta_path, meta)
            done.add(cid)
        except Exception as e:
            store.record_error(
                kind="SCHEMA",
                object_type="markdown",
                object_id=cid,
                message=str(e),
            )
        n_since_ckpt += 1
        if n_since_ckpt >= 50:
            progress["completed_markdown_ids"] = sorted(done)
            progress["markdown_done"] = len(done)
            store.save_progress(progress)
            n_since_ckpt = 0
    progress["completed_markdown_ids"] = sorted(done)
    progress["markdown_done"] = len(done)
    store.save_progress(progress)
    print(f"markdown {len(done)}/{len(ids)}", flush=True)


def _save_file(
    store: ArchiveStore,
    dest: Path,
    payload: bytes,
    *,
    file_id: str,
    filename: str,
    ctype: str,
    conversation_id: Optional[str],
    project_id: Optional[str],
    progress: Dict[str, Any],
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    rec = {
        "file_id": file_id,
        "conversation_id": conversation_id,
        "project_id": project_id,
        "original_name": filename,
        "media_type": ctype,
        "size": len(payload),
        "sha256": _sha256_bytes(payload),
        "local_path": str(dest.relative_to(store.root)).replace("\\", "/"),
        "download_status": "ok",
        "error": None,
        "ts": _now_iso(),
    }
    _append_jsonl(store.files_jsonl, rec)
    progress["files_done"] = int(progress.get("files_done") or 0) + 1
    progress["bytes_downloaded"] = int(progress.get("bytes_downloaded") or 0) + len(payload)
    progress["last_success_at"] = _now_iso()


def download_project_files(
    client: ChatGPTHttpClient,
    store: ArchiveStore,
    progress: Dict[str, Any],
) -> None:
    print("PASS 4 project files", flush=True)
    progress["pass"] = "project_files"
    if not store.projects_json.is_file():
        return
    data = json.loads(store.projects_json.read_text(encoding="utf-8"))
    projects = data.get("projects") or []
    done: Set[str] = set(progress.get("completed_project_file_ids") or [])
    completed_projects: Set[str] = set(progress.get("completed_project_ids") or [])
    for proj in projects:
        pid = proj.get("id") or ""
        pdir = store.project_dir(pid, proj.get("name") or "")
        convs = []
        cp = pdir / "conversations.json"
        if cp.is_file():
            convs = json.loads(cp.read_text(encoding="utf-8"))
        conv_id = (convs[0].get("conversation_id") if convs else "") or ""
        files = proj.get("files") or []
        for fobj in files:
            if not isinstance(fobj, dict):
                continue
            fid = str(fobj.get("file_id") or fobj.get("id") or "")
            if not fid or fid in done:
                continue
            key = f"project:{pid}:{fid}"
            if key in done:
                continue
            name = fobj.get("name") or fid
            dest = pdir / "files" / f"{_safe_name(fid, 40)}__{_safe_name(name, 60)}"
            if dest.is_file() and dest.stat().st_size > 0:
                done.add(key)
                continue
            try:
                payload, filename, ctype = client.download_file(fid, conv_id or pid)
                _save_file(
                    store, dest, payload,
                    file_id=fid, filename=filename or name, ctype=ctype,
                    conversation_id=conv_id or None, project_id=pid,
                    progress=progress,
                )
                done.add(key)
            except ChatGPTAuthError as e:
                if not _session_alive(client):
                    raise
                store.record_error(
                    kind="FORBIDDEN",
                    object_type="project_file",
                    object_id=fid,
                    message=str(e),
                )
                _append_jsonl(store.files_jsonl, {
                    "file_id": fid,
                    "project_id": pid,
                    "download_status": "failed",
                    "error": "FORBIDDEN",
                    "ts": _now_iso(),
                })
                progress["files_failed"] = int(progress.get("files_failed") or 0) + 1
                done.add(key)
            except Exception as e:
                store.record_error(
                    kind=_classify_error(e),
                    object_type="project_file",
                    object_id=fid,
                    message=str(e),
                )
                _append_jsonl(store.files_jsonl, {
                    "file_id": fid,
                    "project_id": pid,
                    "download_status": "failed",
                    "error": _classify_error(e),
                    "ts": _now_iso(),
                })
                progress["files_failed"] = int(progress.get("files_failed") or 0) + 1
        completed_projects.add(pid)
        progress["completed_project_ids"] = sorted(completed_projects)
        progress["completed_project_file_ids"] = sorted(done)
        progress["projects_done"] = len(completed_projects)
        store.save_progress(progress)
    store.save_progress(progress)


def download_conversation_files(
    client: ChatGPTHttpClient,
    store: ArchiveStore,
    progress: Dict[str, Any],
) -> None:
    print("PASS 5 conversation files", flush=True)
    progress["pass"] = "attachments"
    ids: List[str] = list(progress.get("discovered_conversations") or [])
    done: Set[str] = set(progress.get("completed_file_ids") or [])
    discovered_keys: Set[str] = set(progress.get("attachment_ids") or [])
    n_since_ckpt = 0
    for cid in ids:
        jp = store.conv_dir(cid) / "conversation.json"
        if not jp.is_file():
            continue
        try:
            body = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        refs = extract_file_references(body)
        for ref in refs:
            fid = ref.get("file_id") or ""
            if fid:
                discovered_keys.add(f"{cid}:{fid}")
        progress["attachments_discovered"] = len(discovered_keys)
        progress["attachment_ids"] = sorted(discovered_keys)
        meta_path = store.conv_dir(cid) / "metadata.json"
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        saved = 0
        for ref in refs:
            fid = ref.get("file_id") or ""
            key = f"{cid}:{fid}"
            if not fid or key in done:
                continue
            dest = store.conv_dir(cid) / "files" / _safe_name(fid, 80)
            existing = [
                p for p in dest.parent.glob(_safe_name(fid, 80) + "*")
                if p.is_file() and p.stat().st_size > 0
            ]
            if existing:
                done.add(key)
                continue
            try:
                payload, filename, ctype = client.download_file(fid, cid)
                ext = Path(filename).suffix
                if ext and not dest.name.endswith(ext):
                    dest = dest.with_name(dest.name + ext)
                _save_file(
                    store, dest, payload,
                    file_id=fid, filename=filename, ctype=ctype,
                    conversation_id=cid, project_id=None,
                    progress=progress,
                )
                done.add(key)
                saved += 1
            except ChatGPTAuthError as e:
                if not _session_alive(client):
                    raise
                store.record_error(
                    kind="FORBIDDEN",
                    object_type="conversation_file",
                    object_id=key,
                    message=str(e),
                )
                _append_jsonl(store.files_jsonl, {
                    "file_id": fid,
                    "conversation_id": cid,
                    "download_status": "failed",
                    "error": "FORBIDDEN",
                    "ts": _now_iso(),
                })
                progress["files_failed"] = int(progress.get("files_failed") or 0) + 1
                done.add(key)
            except Exception as e:
                store.record_error(
                    kind=_classify_error(e),
                    object_type="conversation_file",
                    object_id=key,
                    message=str(e),
                )
                _append_jsonl(store.files_jsonl, {
                    "file_id": fid,
                    "conversation_id": cid,
                    "download_status": "failed",
                    "error": _classify_error(e),
                    "ts": _now_iso(),
                })
                progress["files_failed"] = int(progress.get("files_failed") or 0) + 1
                kind = _classify_error(e)
                if kind not in RETRYABLE_CLASSES:
                    done.add(key)
        meta["file_count"] = saved
        meta["file_refs"] = len(refs)
        _atomic_write_json(meta_path, meta)
        n_since_ckpt += 1
        if n_since_ckpt >= 25:
            progress["completed_file_ids"] = sorted(done)
            store.save_progress(progress)
            print(
                f"files done={progress.get('files_done')} "
                f"failed={progress.get('files_failed')}",
                flush=True,
            )
            n_since_ckpt = 0
    progress["completed_file_ids"] = sorted(done)
    ids_with_json = [
        cid for cid in ids
        if (store.conv_dir(cid) / "conversation.json").is_file()
    ]
    if ids and len(ids_with_json) == len(ids):
        discovered = int(progress.get("attachments_discovered") or 0)
        progress["attachment_discovery_status"] = (
            "ATTACHMENT_DISCOVERY_COMPLETE_ZERO" if discovered == 0
            else "ATTACHMENT_DISCOVERY_COMPLETE"
        )
    elif ids_with_json:
        progress["attachment_discovery_status"] = "ATTACHMENT_PASS_PARTIAL"
    else:
        progress["attachment_discovery_status"] = "ATTACHMENT_PASS_NOT_RUN"
    store.save_progress(progress)


def build_indexes(store: ArchiveStore, progress: Dict[str, Any]) -> None:
    print("PASS indexes", flush=True)
    progress["pass"] = "indexes"
    ids: List[str] = list(progress.get("discovered_conversations") or [])
    status_map: Dict[str, str] = dict(progress.get("conversation_status") or {})
    listing: Dict[str, Dict[str, Any]] = {}
    if store.conversations_jsonl.is_file():
        for line in store.conversations_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = rec.get("conversation_id")
            if cid:
                listing[str(cid)] = rec
    conv_index = []
    for cid in ids:
        cdir = store.conv_dir(cid)
        jp = cdir / "conversation.json"
        mp = cdir / "conversation.md"
        meta = {}
        if (cdir / "metadata.json").is_file():
            try:
                meta = json.loads((cdir / "metadata.json").read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        item = listing.get(cid) or {}
        classification = status_map.get(cid) or meta.get("classification")
        if _json_complete(cdir):
            classification = "COMPLETE"
        elif not classification:
            classification = "PENDING"
        status_map[cid] = classification
        conv_index.append({
            "id": cid,
            "title": meta.get("title") or item.get("title"),
            "project": meta.get("gizmo_id") or item.get("project_id"),
            "project_name": item.get("project_name"),
            "created_at": meta.get("create_time") or item.get("create_time"),
            "updated_at": meta.get("update_time") or item.get("update_time"),
            "local_path": str((cdir / "conversation.json").relative_to(store.root)).replace("\\", "/") if jp.is_file() else None,
            "markdown_path": str(mp.relative_to(store.root)).replace("\\", "/") if mp.is_file() else None,
            "status": classification,
            "source_type": "chatgpt_conversation",
            "content_hash": meta.get("json_sha256"),
            "error": meta.get("error"),
        })
    _atomic_write_json(store.reports / "conversation_index.json", conv_index)

    projects = []
    if store.projects_json.is_file():
        projects = (json.loads(store.projects_json.read_text(encoding="utf-8")).get("projects") or [])
    proj_index = []
    for proj in projects:
        pid = proj.get("id") or ""
        pdir = store.project_dir(pid, proj.get("name") or "")
        proj_index.append({
            "id": pid,
            "title": proj.get("name"),
            "created_at": proj.get("created_at"),
            "updated_at": proj.get("updated_at"),
            "local_path": str((pdir / "project.json").relative_to(store.root)).replace("\\", "/") if (pdir / "project.json").is_file() else None,
            "status": "ARCHIVED" if (pdir / "project.json").is_file() else "DISCOVERED",
            "source_type": "chatgpt_project",
            "file_count": len(proj.get("files") or []),
        })
    _atomic_write_json(store.reports / "project_index.json", proj_index)

    file_index = []
    if store.files_jsonl.is_file():
        for line in store.files_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            file_index.append({
                "id": rec.get("file_id"),
                "title": rec.get("original_name"),
                "project": rec.get("project_id"),
                "conversation_id": rec.get("conversation_id"),
                "created_at": rec.get("ts"),
                "local_path": rec.get("local_path"),
                "status": rec.get("download_status"),
                "source_type": "chatgpt_file",
                "content_hash": rec.get("sha256"),
                "error": rec.get("error"),
                "size": rec.get("size"),
                "media_type": rec.get("media_type"),
            })
    _atomic_write_json(store.reports / "file_index.json", file_index)

    failed = []
    for rec in (progress.get("failed_items") or []):
        failed.append({
            "id": rec.get("id") or rec.get("object_id"),
            "status": rec.get("classification") or rec.get("class"),
            "source_type": rec.get("type") or rec.get("object_type"),
            "error": rec.get("class"),
            "retries": rec.get("retries"),
        })
    for cid, st in status_map.items():
        if st and st != "COMPLETE":
            failed.append({
                "id": cid,
                "status": st,
                "source_type": "conversation",
            })
    _atomic_write_json(store.reports / "failed_index.json", failed)
    progress["conversation_status"] = status_map

    manifest = {}
    if store.account_manifest.is_file():
        try:
            manifest = json.loads(store.account_manifest.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest.update({
        "archive_root": str(store.root),
        "updated_at": _now_iso(),
        "transport": "nisaba.client.ChatGPTHttpClient",
        "note": "Secrets are never stored in this archive. Re-open locally without the live API.",
        "pagination_complete": progress.get("pagination_complete"),
        "final_conversations_discovered": progress.get("final_conversations_discovered") or len(ids),
        "oldest_remote_timestamp_seen": progress.get("oldest_remote_timestamp_seen"),
        "newest_remote_timestamp_seen": progress.get("newest_remote_timestamp_seen"),
        "last_remote_watermark": progress.get("last_remote_watermark"),
        "attachment_discovery_status": progress.get("attachment_discovery_status"),
        "indexes": {
            "conversations": "reports/conversation_index.json",
            "projects": "reports/project_index.json",
            "files": "reports/file_index.json",
            "failed": "reports/failed_index.json",
        },
    })
    _atomic_write_json(store.account_manifest, manifest)
    store.save_progress(progress)


def verify_local(store: ArchiveStore, progress: Dict[str, Any]) -> Dict[str, Any]:
    ids = list(progress.get("discovered_conversations") or [])
    status_map: Dict[str, str] = dict(progress.get("conversation_status") or {})
    json_ok = 0
    md_ok = 0
    dup = 0
    seen: Set[str] = set()
    missing_json = 0
    unclassified = []
    pending = []
    counts: Dict[str, int] = {}
    files_claimed = 0
    files_exist = 0
    sha_mismatch = 0
    secret_hits = 0
    if store.conversations_jsonl.is_file():
        for line in store.conversations_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = rec.get("conversation_id")
            if cid in seen:
                dup += 1
            seen.add(cid)
    disk_ids = []
    conv_root = store.root / "conversations"
    if conv_root.is_dir():
        disk_ids = [p.name for p in conv_root.iterdir() if p.is_dir()]
    for cid in ids:
        jp = store.conv_dir(cid) / "conversation.json"
        mp = store.conv_dir(cid) / "conversation.md"
        if _json_complete(store.conv_dir(cid)):
            json_ok += 1
            status_map[cid] = "COMPLETE"
        else:
            missing_json += 1
        if mp.is_file():
            md_ok += 1
        st = status_map.get(cid)
        if not st:
            unclassified.append(cid)
        else:
            counts[st] = counts.get(st, 0) + 1
            if st == "PENDING":
                pending.append(cid)
    if store.files_jsonl.is_file():
        for line in store.files_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("download_status") != "ok":
                continue
            files_claimed += 1
            rel = rec.get("local_path") or ""
            p = store.root / rel
            if p.is_file():
                files_exist += 1
                if rec.get("sha256") and _sha256_file(p) != rec["sha256"]:
                    sha_mismatch += 1
    for p in (store.progress_path, store.latest_path, store.account_manifest, store.projects_json):
        if p.is_file() and JWT_RE.search(p.read_text(encoding="utf-8", errors="replace")):
            secret_hits += 1
    project_files_discovered = int(
        (progress.get("discovery") or {}).get("project_files")
        or progress.get("files_discovered")
        or 0
    )
    project_files_terminal = len(progress.get("completed_project_file_ids") or [])
    invariants = {
        "A_pagination_exhausted": bool(progress.get("pagination_complete")),
        "B_every_discovered_classified": not unclassified,
        "C_complete_has_readable_json": json_ok == counts.get("COMPLETE", json_ok) if ids else True,
        "D_local_ids_unique": dup == 0,
        "E_no_duplicate_dirs": len(disk_ids) == len(set(disk_ids)),
        "F_project_files_terminal": (
            project_files_discovered == 0
            or project_files_terminal >= project_files_discovered
            or int(progress.get("files_done") or 0) + int(progress.get("files_failed") or 0) >= project_files_discovered
        ),
        "G_attachment_discovery_classified": bool(progress.get("attachment_discovery_status")),
        "H_markdown_for_complete": md_ok >= json_ok,
        "I_indexes_present": all(
            (store.reports / name).is_file()
            for name in (
                "conversation_index.json",
                "project_index.json",
                "file_index.json",
                "failed_index.json",
            )
        ),
        "J_no_pending_ordinary_work": not pending,
        "K_failures_listed": True,
        "L_manifest_offline": store.account_manifest.is_file(),
    }
    full_ok = all(invariants.values())
    report = {
        "unique_conversations": len(ids),
        "json_ok": json_ok,
        "markdown_ok": md_ok,
        "duplicate_uuids": dup,
        "missing_or_unreadable_json": missing_json,
        "unclassified_ids": unclassified[:50],
        "unclassified_count": len(unclassified),
        "pending_count": len(pending),
        "classification_counts": counts,
        "files_claimed_ok": files_claimed,
        "files_exist": files_exist,
        "sha256_mismatch": sha_mismatch,
        "secret_hits_in_manifests": secret_hits,
        "bytes_downloaded": progress.get("bytes_downloaded", 0),
        "files_failed": progress.get("files_failed", 0),
        "files_done": progress.get("files_done", 0),
        "files_discovered": progress.get("files_discovered") or project_files_discovered,
        "projects": progress.get("projects_discovered") or progress.get("projects_done"),
        "pagination_complete": progress.get("pagination_complete"),
        "attachment_discovery_status": progress.get("attachment_discovery_status"),
        "oldest_remote_timestamp_seen": progress.get("oldest_remote_timestamp_seen"),
        "newest_remote_timestamp_seen": progress.get("newest_remote_timestamp_seen"),
        "last_remote_watermark": progress.get("last_remote_watermark"),
        "invariants": invariants,
        "full_archive_complete": full_ok,
    }
    progress["conversation_status"] = status_map
    return report


def write_final_report(store: ArchiveStore, progress: Dict[str, Any], local: Dict[str, Any]) -> None:
    disc = progress.get("discovery") or {}
    lines = [
        "# ChatGPT archive final report",
        "",
        f"Written: {_now_iso()}",
        f"Status: {progress.get('status')}",
        f"Auth: {progress.get('auth_status')}",
        "",
        f"projects discovered: {disc.get('projects', local.get('projects'))}",
        f"unique conversations discovered: {local.get('unique_conversations')}",
        f"active (regular listing): {disc.get('regular_conversations')}",
        f"archived listing: {disc.get('archived_conversations')}",
        f"project conversation listings: {disc.get('project_conversations')}",
        f"conversation JSON saved: {local.get('json_ok')}",
        f"Markdown saved: {local.get('markdown_ok')}",
        f"files discovered (ok claims): {local.get('files_claimed_ok')}",
        f"files saved on disk: {local.get('files_exist')}",
        f"files failed: {local.get('files_failed')}",
        f"total bytes downloaded: {local.get('bytes_downloaded')}",
        f"duplicate UUIDs: {local.get('duplicate_uuids')}",
        f"SHA256 mismatches: {local.get('sha256_mismatch')}",
        f"JWT-like hits in manifests: {local.get('secret_hits_in_manifests')}",
        f"pagination_complete: {local.get('pagination_complete')}",
        f"attachment_discovery: {local.get('attachment_discovery_status')}",
        f"classification_counts: {local.get('classification_counts')}",
        f"invariants: {local.get('invariants')}",
        f"full_archive_complete: {local.get('full_archive_complete')}",
        "",
        "This folder is the archive. No ZIP was created.",
        "",
    ]
    (store.reports / "final_report.md").write_text("\n".join(lines), encoding="utf-8")
    _atomic_write_json(store.reports / "final_verify.json", local)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Resumable full ChatGPT account archive")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", action="store_false", dest="resume")
    p.add_argument("--include-archived", action="store_true", default=True)
    p.add_argument("--include-projects", action="store_true", default=True)
    p.add_argument("--include-files", action="store_true", default=True)
    p.add_argument("--no-files", action="store_false", dest="include_files")
    p.add_argument("--json-status", action="store_true")
    p.add_argument("--verify-only", action="store_true",
                   help="Local verification against existing archive; no API calls")
    p.add_argument("--throttle", type=float, default=None,
                   help="Seconds between API calls (default: env or 1.0)")
    return p.parse_args(argv)


def _full_archive_line(progress: Dict[str, Any], local: Dict[str, Any]) -> str:
    if progress.get("auth_status") in {"BLOCKED_AUTH", "AUTH_EXPIRED", "AUTH_MISSING"}:
        return (
            "FULL_ARCHIVE: BLOCKED_AUTH — refresh LITOPS_CHATGPT_SESSION_TOKEN "
            "or LITOPS_CHATGPT_BEARER_TOKEN"
        )
    if local.get("full_archive_complete"):
        return "FULL_ARCHIVE: COMPLETE"
    remaining = []
    if not progress.get("pagination_complete"):
        remaining.append("remote pagination not proved exhausted")
    pending = int(local.get("pending_count") or 0)
    missing = int(local.get("missing_or_unreadable_json") or 0)
    if pending:
        remaining.append(f"{pending} PENDING conversations")
    if missing:
        remaining.append(f"{missing} conversations without readable JSON")
    md_ok = int(local.get("markdown_ok") or 0)
    json_ok = int(local.get("json_ok") or 0)
    if md_ok < json_ok:
        remaining.append(f"markdown {md_ok}/{json_ok}")
    files_disc = int(local.get("files_discovered") or 0)
    files_done = int(local.get("files_done") or 0)
    files_failed = int(local.get("files_failed") or 0)
    if files_disc and files_done + files_failed < files_disc:
        remaining.append(f"project files {files_done}+{files_failed}/{files_disc}")
    if not progress.get("attachment_discovery_status"):
        remaining.append("attachment discovery not classified")
    if remaining:
        return "FULL_ARCHIVE: INCOMPLETE — " + "; ".join(remaining)
    if not (local.get("invariants") or {}).get("I_indexes_present"):
        return "FULL_ARCHIVE: INCOMPLETE — indexes missing"
    return "FULL_ARCHIVE: INCOMPLETE — verification invariants not all true"


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    out = args.output.expanduser().resolve()
    store = ArchiveStore(out)
    _write_readme(out)
    if args.verify_only:
        progress = store.load_progress()
        try:
            build_indexes(store, progress)
        except Exception as exc:  # noqa: BLE001
            store.record_error(
                kind="OTHER", object_type="indexes", object_id="verify",
                message=str(exc),
            )
        local = verify_local(store, progress)
        write_final_report(store, progress, local)
        print(_full_archive_line(progress, local), flush=True)
        if args.json_status:
            print(json.dumps(local, ensure_ascii=False))
        return 0 if local.get("full_archive_complete") else 1
    if not store.acquire_lock():
        print("ALREADY_RUNNING", flush=True)
        return 8
    progress = store.load_progress() if args.resume else {
        "status": "RUNNING",
        "auth_status": "OK",
        "discovered_conversations": [],
        "completed_conversation_ids": [],
        "completed_markdown_ids": [],
        "completed_file_ids": [],
        "completed_project_ids": [],
        "completed_project_file_ids": [],
        "failed_items": [],
        "bytes_downloaded": 0,
    }
    progress["status"] = "RUNNING"
    progress["auth_status"] = "OK"
    progress["pid"] = os.getpid()
    if not progress.get("worker_started_at"):
        progress["worker_started_at"] = _now_iso()
    progress["duplicate_worker_count"] = 0
    store.save_progress(progress)

    throttle = args.throttle
    if throttle is None:
        throttle = float(os.environ.get("LITOPS_CHATGPT_THROTTLE_SECONDS", "1") or "1")

    try:
        client = ChatGPTHttpClient.from_env()
        if throttle is not None:
            client._throttle = max(0.0, float(throttle))
    except ChatGPTAuthError as e:
        progress["status"] = "BLOCKED_AUTH"
        progress["auth_status"] = "BLOCKED_AUTH"
        store.save_progress(progress)
        store.release_lock()
        print("STATUS BLOCKED_AUTH", type(e).__name__, file=sys.stderr)
        print(
            "FULL_ARCHIVE: BLOCKED_AUTH — refresh LITOPS_CHATGPT_SESSION_TOKEN "
            "or LITOPS_CHATGPT_BEARER_TOKEN",
            flush=True,
        )
        return 2

    try:
        # Always re-list until pagination exhaustion is proved.
        discover(client, store, progress, args)

        download_jsons(client, store, progress)
        write_markdown(store, progress)
        if args.include_files:
            download_project_files(client, store, progress)
            download_conversation_files(client, store, progress)
        for _ in range(2):
            download_jsons(client, store, progress)
            write_markdown(store, progress)
            if args.include_files:
                download_project_files(client, store, progress)
                download_conversation_files(client, store, progress)

        build_indexes(store, progress)
        local = verify_local(store, progress)
        if local.get("full_archive_complete"):
            progress["status"] = "COMPLETE"
        else:
            progress["status"] = "INCOMPLETE"
        progress["auth_status"] = "OK"
        store.save_progress(progress)
        write_final_report(store, progress, local)
        print("STATUS", progress["status"], flush=True)
        print(_full_archive_line(progress, local), flush=True)
        if args.json_status:
            print(json.dumps({
                "status": progress.get("status"),
                "unique_conversations": progress.get("unique_conversations"),
                "conversation_json_done": progress.get("conversation_json_done"),
                "markdown_done": progress.get("markdown_done"),
                "files_done": progress.get("files_done"),
                "files_failed": progress.get("files_failed"),
                "pagination_complete": progress.get("pagination_complete"),
                "attachment_discovery_status": progress.get("attachment_discovery_status"),
            }, ensure_ascii=False))
        return 0 if progress["status"] == "COMPLETE" else 1
    except ChatGPTAuthError:
        try:
            write_markdown(store, progress)
            build_indexes(store, progress)
        except Exception:
            pass
        progress["status"] = "BLOCKED_AUTH"
        progress["auth_status"] = "BLOCKED_AUTH"
        store.save_progress(progress)
        print("STATUS BLOCKED_AUTH", flush=True)
        print(
            "FULL_ARCHIVE: BLOCKED_AUTH — refresh LITOPS_CHATGPT_SESSION_TOKEN "
            "or LITOPS_CHATGPT_BEARER_TOKEN",
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        progress["status"] = "STOPPED"
        store.save_progress(progress)
        print("STATUS STOPPED", flush=True)
        print("FULL_ARCHIVE: INCOMPLETE — worker interrupted", flush=True)
        return 130
    except Exception as e:
        progress["status"] = "FAILED"
        store.record_error(
            kind=_classify_error(e),
            object_type="archive",
            object_id="run",
            message=str(e),
        )
        try:
            build_indexes(store, progress)
        except Exception:
            pass
        store.save_progress(progress)
        print("STATUS FAILED", type(e).__name__, flush=True)
        print(f"FULL_ARCHIVE: INCOMPLETE — worker FAILED {type(e).__name__}", flush=True)
        return 1
    finally:
        store.release_lock()


if __name__ == "__main__":
    sys.exit(main())
