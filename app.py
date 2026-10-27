"""WeightWise - a local UI for finding and removing duplicate model weights.

Run with ``python app.py``. The server binds only to 127.0.0.1 and every API
request requires a per-launch token embedded in the local UI.
"""

from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dedupe_core import (
    DuplicateGroup,
    FileRecord,
    ScanCancelled,
    ScanResult,
    hash_file_for_verification,
    rebuild_result_after_delete,
    scan_duplicates,
    WEIGHT_EXTENSIONS,
)

APP_NAME = "WeightWise"
APP_VERSION = "1.0.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_DIR = Path(__file__).resolve().parent / "data"
MAX_BODY_BYTES = 1_000_000
MAX_DELETE_FILES = 5000
MAX_ERROR_DETAILS = 30

API_TOKEN_HEADER = "X-WeightWise-Token"


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


class RecycleError(Exception):
    pass


@dataclass
class PlannedDeletion:
    group: DuplicateGroup
    selected: list[FileRecord]
    keeper: FileRecord
    verify_files: list[FileRecord]


class JsonHashCache:
    """Small, best-effort persistent hash cache keyed by path + size + mtime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                self.entries = entries
        except (OSError, ValueError, TypeError):
            self.entries = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the cache from growing without bound on very large libraries.
            if len(self.entries) > 100_000:
                trimmed = dict(list(self.entries.items())[-100_000:])
                self.entries = trimmed
            temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "entries": self.entries},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(temp, self.path)
        except OSError as exc:
            print(f"[cache] Could not save hash cache: {exc}", file=sys.stderr)

    def discard(self, path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        self.entries.pop(key, None)


class AppState:
    def __init__(self, default_root: str, cache_file: Path) -> None:
        candidate_root = os.path.abspath(os.path.expanduser(default_root))
        self.default_root = (
            candidate_root if os.path.isdir(candidate_root) else os.getcwd()
        )
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.scan_job: dict[str, Any] | None = None
        self.result: ScanResult | None = None
        self.deleting = False
        self.cache = JsonHashCache(cache_file)

    def start_scan(self, options: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.deleting:
                raise ApiError(409, "A deletion is in progress. Wait for it to finish.")
            if self.scan_job and self.scan_job.get("status") == "running":
                raise ApiError(409, "A scan is already running.")

            job: dict[str, Any] = {
                "id": secrets.token_hex(12),
                "status": "running",
                "phase": "starting",
                "message": "Preparing scan",
                "progress": None,
                "files_found": 0,
                "files_to_hash": 0,
                "files_hashed": 0,
                "bytes_processed": 0,
                "bytes_total": 0,
                "current_file": "",
                "error": "",
                "started_monotonic": time.monotonic(),
                "cancel_event": threading.Event(),
            }
            self.scan_job = job
        thread = threading.Thread(
            target=self._run_scan,
            args=(job, options),
            name="weightwise-scan",
            daemon=True,
        )
        thread.start()
        return self._public_job(job)

    def _progress(self, job: dict[str, Any], payload: dict[str, Any]) -> None:
        with self.lock:
            if job.get("status") != "running":
                return
            for key in (
                "phase",
                "message",
                "progress",
                "files_found",
                "files_to_hash",
                "files_hashed",
                "bytes_processed",
                "bytes_total",
                "current_file",
            ):
                if key in payload:
                    job[key] = payload[key]

    def _run_scan(self, job: dict[str, Any], options: dict[str, Any]) -> None:
        try:
            result = scan_duplicates(
                options["root"],
                recursive=options["recursive"],
                weights_only=options["weights_only"],
                include_hidden=options["include_hidden"],
                hash_cache=self.cache.entries,
                progress=lambda payload: self._progress(job, payload),
                cancel=job["cancel_event"].is_set,
            )
            self.cache.save()
            with self.lock:
                if job["cancel_event"].is_set():
                    job.update(
                        status="cancelled",
                        phase="cancelled",
                        message="Scan cancelled",
                        progress=None,
                        current_file="",
                    )
                    return
                self.result = result
                job.update(
                    status="complete",
                    phase="complete",
                    message="Scan complete",
                    progress=100,
                    files_found=result.matched_files,
                    bytes_processed=result.matched_bytes,
                    bytes_total=result.matched_bytes,
                    current_file="",
                )
        except ScanCancelled:
            with self.lock:
                job.update(
                    status="cancelled",
                    phase="cancelled",
                    message="Scan cancelled",
                    progress=None,
                    current_file="",
                )
        except Exception as exc:
            traceback.print_exc()
            with self.lock:
                job.update(
                    status="error",
                    phase="error",
                    message="Scan failed",
                    error=str(exc),
                    progress=None,
                    current_file="",
                )

    def _public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            payload = {
                "id": job["id"],
                "status": job["status"],
                "phase": job["phase"],
                "message": job["message"],
                "progress": job["progress"],
                "files_found": job["files_found"],
                "files_to_hash": job["files_to_hash"],
                "files_hashed": job["files_hashed"],
                "bytes_processed": job["bytes_processed"],
                "bytes_total": job["bytes_total"],
                "current_file": job["current_file"],
                "error": job["error"],
                "elapsed_ms": int(
                    (time.monotonic() - job["started_monotonic"]) * 1000
                ),
            }
        return payload

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if not self.scan_job or self.scan_job["id"] != job_id:
                raise ApiError(404, "That scan no longer exists.")
            return self._public_job(self.scan_job)

    def cancel_scan(self) -> dict[str, Any]:
        with self.lock:
            if not self.scan_job or self.scan_job["status"] != "running":
                raise ApiError(409, "There is no running scan to cancel.")
            self.scan_job["cancel_event"].set()
            self.scan_job["message"] = "Cancelling scan..."
            return self._public_job(self.scan_job)

    def get_result(self) -> dict[str, Any] | None:
        with self.lock:
            if self.result is None:
                return None
            return self.result.to_dict()

    def delete_copies(
        self,
        *,
        raw_groups: Any,
        mode: str,
        confirm_text: str,
    ) -> dict[str, Any]:
        if mode not in {"recycle", "permanent"}:
            raise ApiError(400, "Unknown deletion mode.")
        if mode == "permanent" and confirm_text != "DELETE":
            raise ApiError(400, 'Type "DELETE" to authorize permanent deletion.')
        if mode == "recycle" and os.name != "nt":
            raise ApiError(400, "Recycle Bin deletion is only available on Windows.")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ApiError(400, "No copies were selected.")

        with self.lock:
            if self.deleting:
                raise ApiError(409, "A deletion is already in progress.")
            if self.scan_job and self.scan_job.get("status") == "running":
                raise ApiError(409, "Wait for the current scan to finish.")
            if self.result is None:
                raise ApiError(409, "Run a scan before deleting files.")
            self.deleting = True
            result = self.result

        try:
            plans = self._plan_deletions(result, raw_groups)
            self._verify_plans(plans)
            return self._execute_deletions(plans, mode)
        finally:
            with self.lock:
                self.deleting = False

    def _plan_deletions(
        self, result: ScanResult, raw_groups: list[Any]
    ) -> list[PlannedDeletion]:
        groups_by_id = {group.id: group for group in result.groups}
        selection_map: dict[str, set[str]] = {}
        requested_total = 0

        for item in raw_groups:
            if not isinstance(item, dict):
                raise ApiError(400, "Malformed deletion selection.")
            group_id = item.get("group_id")
            file_ids = item.get("file_ids")
            if not isinstance(group_id, str) or not isinstance(file_ids, list):
                raise ApiError(400, "Malformed deletion selection.")
            clean_ids = {file_id for file_id in file_ids if isinstance(file_id, str)}
            if len(clean_ids) != len(file_ids):
                raise ApiError(400, "Malformed file selection.")
            if not clean_ids:
                continue
            selection_map.setdefault(group_id, set()).update(clean_ids)
            requested_total += len(clean_ids)

        if requested_total == 0:
            raise ApiError(400, "No copies were selected.")
        if requested_total > MAX_DELETE_FILES:
            raise ApiError(400, "Too many copies selected in one operation.")

        plans: list[PlannedDeletion] = []
        for group_id, file_ids in selection_map.items():
            group = groups_by_id.get(group_id)
            if group is None:
                raise ApiError(
                    409, "The results are out of date. Run the scan again."
                )
            files_by_id = {file.file_id: file for file in group.files}
            if not file_ids.issubset(files_by_id):
                raise ApiError(
                    409, "The results are out of date. Run the scan again."
                )
            selected = [
                file for file in group.files if file.file_id in file_ids
            ]
            remaining = [
                file for file in group.files if file.file_id not in file_ids
            ]
            if not remaining:
                raise ApiError(
                    409,
                    "WeightWise will never delete every copy in a group. Keep at least one.",
                )
            if not selected:
                continue

            # Keep the recommended copy if possible; otherwise keep one other
            # verified copy. Only the selected copies and this keeper need to be
            # re-hashed before deletion.
            keeper = next(
                (
                    file
                    for file in remaining
                    if file.file_id == group.recommended_keep_id
                ),
                remaining[0],
            )
            verify_files = list(selected)
            if not any(file.file_id == keeper.file_id for file in verify_files):
                verify_files.append(keeper)
            plans.append(
                PlannedDeletion(
                    group=group,
                    selected=selected,
                    keeper=keeper,
                    verify_files=verify_files,
                )
            )

        if not plans:
            raise ApiError(400, "No copies were selected.")
        return plans

    def _verify_plans(self, plans: list[PlannedDeletion]) -> None:
        failures: list[dict[str, str]] = []
        seen: set[str] = set()
        for plan in plans:
            expected = plan.group.digest
            for record in plan.verify_files:
                key = os.path.normcase(os.path.abspath(record.path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    if os.path.islink(record.path) or not os.path.isfile(record.path):
                        raise OSError("file is missing or is no longer a regular file")
                    before = os.stat(record.path)
                    digest = hash_file_for_verification(record.path)
                    after = os.stat(record.path)
                    if (
                        before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                    ):
                        raise ValueError("file changed while it was being verified")
                    if not hmac.compare_digest(digest, expected):
                        raise ValueError("file contents changed since the scan")
                except (OSError, ValueError) as exc:
                    failures.append({"path": record.path, "error": str(exc)})
                except Exception as exc:  # defensive: never delete on unknown failure
                    failures.append({"path": record.path, "error": str(exc)})

        if failures:
            raise ApiError(
                409,
                "One or more files changed since the scan. Nothing was deleted.",
                failures[:MAX_ERROR_DETAILS],
            )

    def _execute_deletions(
        self, plans: list[PlannedDeletion], mode: str
    ) -> dict[str, Any]:
        before_reclaim = {
            plan.group.id: plan.group.reclaimable_bytes for plan in plans
        }
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        moved_bytes = 0

        for plan in plans:
            for record in plan.selected:
                try:
                    if mode == "recycle":
                        move_to_recycle_bin(record.path)
                    else:
                        os.remove(record.path)
                    if os.path.lexists(record.path):
                        raise OSError("file still exists after the delete request")
                    successes.append({"path": record.path, "size": record.size})
                    moved_bytes += record.size
                    self.cache.discard(record.path)
                except Exception as exc:
                    failures.append({"path": record.path, "error": str(exc)})

        deleted_paths = {item["path"] for item in successes}
        with self.lock:
            result = self.result
            if result is None:
                raise ApiError(500, "Results disappeared during deletion.")
            rebuild_result_after_delete(result, deleted_paths)
            after_groups = {group.id: group for group in result.groups}
            after_reclaim = sum(
                after_groups[group_id].reclaimable_bytes
                if group_id in after_groups
                else 0
                for group_id in before_reclaim
            )
            freed_estimate = max(
                0, sum(before_reclaim.values()) - after_reclaim
            )
            result_payload = result.to_dict()

        self.cache.save()

        if not failures:
            if mode == "recycle":
                message = (
                    f"Moved {len(successes)} copies to the Recycle Bin. "
                    "Empty the bin to reclaim the space."
                )
            else:
                message = f"Permanently deleted {len(successes)} copies."
        else:
            message = (
                f"Deleted {len(successes)} of {len(successes) + len(failures)} selected copies. "
                "Review the errors below."
            )

        return {
            "deleted_count": len(successes),
            "failed_count": len(failures),
            "mode": mode,
            "moved_bytes": moved_bytes,
            "freed_estimate_bytes": freed_estimate if mode == "permanent" else 0,
            "message": message,
            "deleted": successes,
            "errors": failures[:MAX_ERROR_DETAILS],
            "result": result_payload,
        }


def move_to_recycle_bin(path: str) -> None:
    """Send one file to the Windows Recycle Bin without a confirmation dialog."""
    if os.name != "nt":
        raise RecycleError("Recycle Bin is only supported on Windows")
    if not os.path.isfile(path):
        raise RecycleError("file no longer exists")

    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    FOF_NOCONFIRMMKDIR = 0x0200

    # pFrom must be double-NUL terminated.
    source = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0")
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    operation.pTo = None
    operation.fFlags = (
        FOF_ALLOWUNDO
        | FOF_NOCONFIRMATION
        | FOF_SILENT
        | FOF_NOERRORUI
        | FOF_NOCONFIRMMKDIR
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if operation.fAnyOperationsAborted:
        raise RecycleError("Recycle Bin operation was aborted by Windows")
    if result != 0:
        raise RecycleError(f"Windows could not move the file to the Recycle Bin (code {result})")
    if os.path.lexists(path):
        raise RecycleError("Windows reported success, but the file is still present")


def reveal_path(path: str) -> None:
    absolute = os.path.abspath(path)
    if os.name == "nt":
        if os.path.isdir(absolute):
            subprocess.Popen(["explorer.exe", absolute])
        else:
            subprocess.Popen(["explorer.exe", f"/select,{absolute}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", absolute])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(absolute) or absolute])


def _windows_drives() -> list[str]:
    drives: list[str] = []
    try:
        for drive in os.listdrives():  # Python 3.12+
            drives.append(drive)
    except (AttributeError, OSError):
        pass
    if not drives:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            candidate = f"{letter}:\\"
            if os.path.isdir(candidate):
                drives.append(candidate)
    return drives


def browse_directory(path: str | None, default_root: str) -> dict[str, Any]:
    if path is None or not str(path).strip():
        target = default_root
    else:
        target = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))

    if not os.path.exists(target):
        raise ApiError(400, "That folder does not exist.")
    if not os.path.isdir(target):
        raise ApiError(400, "That path is a file, not a folder.")

    directories: list[dict[str, str]] = []
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=True):
                        directories.append(
                            {"name": entry.name, "path": entry.path}
                        )
                except OSError:
                    continue
    except OSError as exc:
        raise ApiError(403, f"Could not read that folder: {exc}") from exc

    directories.sort(key=lambda item: item["name"].casefold())
    truncated = len(directories) > 400
    directories = directories[:400]

    parent = os.path.dirname(target.rstrip("\\/"))
    if not parent or os.path.normcase(parent) == os.path.normcase(target):
        parent = None
    elif os.name == "nt" and len(target.rstrip("\\/")) <= 2:
        parent = None

    home = os.path.expanduser("~")
    quick_candidates = [
        ("Home", home),
        ("Desktop", os.path.join(home, "Desktop")),
        ("Downloads", os.path.join(home, "Downloads")),
        ("Documents", os.path.join(home, "Documents")),
        ("This folder", default_root),
    ]
    quick_access: list[dict[str, str]] = []
    seen_quick: set[str] = set()
    for label, candidate in quick_candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen_quick:
            continue
        seen_quick.add(key)
        quick_access.append({"label": label, "path": os.path.abspath(candidate)})

    drives = (
        [{"label": drive, "path": drive} for drive in _windows_drives()]
        if os.name == "nt"
        else []
    )
    return {
        "path": target,
        "parent": parent,
        "directories": directories,
        "truncated": truncated,
        "quick_access": quick_access,
        "drives": drives,
    }


def _load_static(name: str) -> bytes:
    file_path = WEB_DIR / name
    with open(file_path, "rb") as handle:
        return handle.read()


class WeightWiseServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        # On Windows SO_REUSEADDR can let a second process bind the same port,
        # producing split-brain servers. Claim the port exclusively instead.
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Browsers routinely close speculative keep-alive connections. Those
        # are not server failures and should not dump a traceback into the app
        # console the user is watching.
        error = sys.exc_info()[1]
        if isinstance(
            error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
        ):
            return
        super().handle_error(request, client_address)

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        state: AppState,
    ) -> None:
        super().__init__(server_address, handler)
        self.state = state


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WeightWise"
    sys_version = ""

    @property
    def app(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep routine polling quiet; only surface real request/server failures.
        status = args[1] if len(args) > 1 else ""
        if str(status).startswith("5") or str(status).startswith("4"):
            super().log_message(fmt, *args)

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        }

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _check_host(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self._allowed_hosts():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "This server only answers loopback requests."},
            )
            return False
        return True

    def _check_api_token(self) -> bool:
        if not self._origin_allowed():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin."})
            return False
        supplied = self.headers.get(API_TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, self.app.token):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "This page is not authorized. Reload the local app."},
            )
            return False
        return True

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; form-action 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._security_headers(content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._send(
            HTTPStatus.NOT_FOUND,
            b"Not found",
            "text/plain; charset=utf-8",
        )

    def _parse_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ApiError(415, "Send JSON with an application/json content type.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "Invalid Content-Length.") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, "Invalid JSON body.") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "Expected a JSON object.")
        return payload

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        path = self._path()

        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"app": APP_NAME, "version": APP_VERSION, "status": "ok"},
            )
            return

        if path in ("/", "/index.html"):
            self._serve_index()
            return

        if path in ("/styles.css", "/app.js", "/favicon.svg"):
            try:
                content = _load_static(path.lstrip("/"))
            except OSError:
                self._not_found()
                return
            content_type = {
                "/styles.css": "text/css; charset=utf-8",
                "/app.js": "text/javascript; charset=utf-8",
                "/favicon.svg": "image/svg+xml",
            }[path]
            self._send(HTTPStatus.OK, content, content_type)
            return

        if path.startswith("/api/"):
            if not self._check_api_token():
                return
            try:
                if path == "/api/config":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "app_name": APP_NAME,
                            "version": APP_VERSION,
                            "default_root": self.app.default_root,
                            "weight_extensions": sorted(WEIGHT_EXTENSIONS),
                            "can_recycle": os.name == "nt",
                            "platform": "windows" if os.name == "nt" else os.name,
                        },
                    )
                    return
                if path == "/api/result":
                    result = self.app.get_result()
                    self._send_json(HTTPStatus.OK, {"result": result})
                    return
                if path == "/api/scan/status":
                    query = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query
                    )
                    job_id = (query.get("job_id") or [""])[0]
                    if not job_id:
                        raise ApiError(400, "Missing job_id.")
                    self._send_json(
                        HTTPStatus.OK, {"job": self.app.get_job(job_id)}
                    )
                    return
                if path == "/api/browse":
                    query = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query
                    )
                    requested = (query.get("path") or [None])[0]
                    self._send_json(
                        HTTPStatus.OK,
                        browse_directory(requested, self.app.default_root),
                    )
                    return
                raise ApiError(404, "Unknown API endpoint.")
            except ApiError as exc:
                self._send_json(
                    exc.status,
                    {"error": exc.message, "details": exc.details},
                )
                return
            except Exception:
                traceback.print_exc()
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Unexpected server error."},
                )
                return

        self._not_found()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        path = self._path()
        if not path.startswith("/api/"):
            self._not_found()
            return
        if not self._check_api_token():
            return

        try:
            data = self._parse_body()
            if path == "/api/scan":
                self._handle_start_scan(data)
                return
            if path == "/api/scan/cancel":
                self._send_json(HTTPStatus.OK, {"job": self.app.cancel_scan()})
                return
            if path == "/api/delete":
                self._handle_delete(data)
                return
            if path == "/api/reveal":
                self._handle_reveal(data)
                return
            raise ApiError(404, "Unknown API endpoint.")
        except ApiError as exc:
            self._send_json(
                exc.status,
                {"error": exc.message, "details": exc.details},
            )
        except Exception:
            traceback.print_exc()
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Unexpected server error."},
            )

    def _handle_start_scan(self, data: dict[str, Any]) -> None:
        raw_root = data.get("path")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ApiError(400, "Choose a folder to scan.")
        cleaned_root = raw_root.strip().strip('"')
        root = os.path.abspath(os.path.expanduser(cleaned_root))
        if not os.path.isdir(root):
            raise ApiError(400, "That folder does not exist or is not accessible.")
        booleans = {}
        for key, default in (
            ("recursive", True),
            ("weights_only", True),
            ("include_hidden", False),
        ):
            value = data.get(key, default)
            if not isinstance(value, bool):
                raise ApiError(400, f"{key} must be true or false.")
            booleans[key] = value
        job = self.app.start_scan(
            {
                "root": root,
                **booleans,
            }
        )
        self._send_json(HTTPStatus.ACCEPTED, {"job": job})

    def _handle_delete(self, data: dict[str, Any]) -> None:
        if data.get("confirm") is not True:
            raise ApiError(400, "Confirm the review before deleting files.")
        mode = data.get("mode", "recycle")
        if not isinstance(mode, str):
            raise ApiError(400, "Invalid deletion mode.")
        confirm_text = data.get("confirm_text", "")
        if not isinstance(confirm_text, str):
            raise ApiError(400, "Invalid confirmation.")
        payload = self.app.delete_copies(
            raw_groups=data.get("groups"),
            mode=mode,
            confirm_text=confirm_text,
        )
        self._send_json(HTTPStatus.OK, payload)

    def _handle_reveal(self, data: dict[str, Any]) -> None:
        raw_path = data.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ApiError(400, "Missing path.")
        path = os.path.abspath(raw_path)
        with self.app.lock:
            result = self.app.result
            root = result.root if result else None
            valid_paths = (
                {file.path for group in result.groups for file in group.files}
                if result
                else set()
            )
        if path != root and path not in valid_paths:
            raise ApiError(403, "That path is not part of the current scan.")
        if not os.path.exists(path):
            raise ApiError(404, "That path no longer exists.")
        try:
            reveal_path(path)
        except OSError as exc:
            raise ApiError(500, f"Could not open Explorer: {exc}") from exc
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _serve_index(self) -> None:
        try:
            content = _load_static("index.html").decode("utf-8")
        except OSError:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                b"UI files are missing. Reinstall or re-extract WeightWise.",
                "text/plain; charset=utf-8",
            )
            return
        content = content.replace("__APP_TOKEN__", self.app.token)
        content = content.replace("__APP_VERSION__", APP_VERSION)
        body = content.encode("utf-8")
        self._send(
            HTTPStatus.OK,
            body,
            "text/html; charset=utf-8",
        )


def existing_instance_url(port: int) -> str | None:
    url = f"http://{HOST}:{port}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{url}/api/health", timeout=1.2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("app") == APP_NAME:
            return url
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="WeightWise - find and remove byte-identical model weights.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEIGHTWISE_PORT", DEFAULT_PORT)),
        help=f"Local port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the browser automatically.",
    )
    parser.add_argument(
        "--root",
        default=os.getcwd(),
        help="Default folder shown in the UI (default: current directory).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = DATA_DIR
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    state = AppState(args.root, data_dir / "hash-cache.json")
    try:
        server = WeightWiseServer((HOST, args.port), RequestHandler, state)
    except OSError as exc:
        if args.port:
            existing = existing_instance_url(args.port)
            if existing:
                print(f"{APP_NAME} is already running at {existing}")
                if not args.no_browser:
                    webbrowser.open(existing)
                return 0
        print(
            f"Could not start {APP_NAME} on port {args.port}: {exc}\n"
            f"Try another port: python app.py --port {args.port + 1}",
            file=sys.stderr,
        )
        return 1

    port = server.server_address[1]
    url = f"http://{HOST}:{port}"
    print()
    print(f"  {APP_NAME} {APP_VERSION}")
    print(f"  Duplicate weight scanner running at {url}")
    print("  Keep this window open while you use the app. Press Ctrl+C to stop.")
    print("  Files are read locally; nothing is uploaded.")
    print(flush=True)
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print(f"\n{APP_NAME} stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
