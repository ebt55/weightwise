"""Content-addressed duplicate detection for model weight files.

The scanner deliberately ignores filenames and timestamps when deciding whether
two files are duplicates. Files are grouped by byte size first, then every file
in a same-size bucket is hashed in full with SHA-256. Only files with the same
size *and* the same full-content digest become a duplicate group.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, MutableMapping

CHUNK_SIZE = 8 * 1024 * 1024

WEIGHT_EXTENSIONS = frozenset(
    {
        ".safetensors",
        ".ckpt",
        ".pt",
        ".pth",
        ".bin",
        ".gguf",
        ".ggml",
        ".onnx",
        ".ort",
        ".engine",
        ".plan",
        ".trt",
        ".tflite",
        ".h5",
        ".hdf5",
        ".pb",
        ".msgpack",
        ".npz",
        ".npy",
        ".model",
        ".params",
        ".pkl",
        ".pickle",
        ".mlmodel",
        ".sft",
        ".litertlm",
        ".task",
        ".tflitemodel",
    }
)

COPY_MARKER_RE = re.compile(
    r"(?ix)"
    r"(?:\(\s*\d+\s*\))"                       # "weights (2).safetensors"
    r"|(?<![a-z0-9])(copy|backup|bak|old|older|newer|temp|tmp|duplicate|dup)(?![a-z0-9])"
    r"|(?:[\s._-]copy\d*$)"
)
SAFE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class ScanCancelled(Exception):
    """Raised internally when the user cancels a scan."""


class FileChangedError(Exception):
    """Raised when a file changes while it is being fingerprinted."""


def cache_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_weight_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in WEIGHT_EXTENSIONS


def looks_like_copy(filename: str) -> bool:
    stem = os.path.splitext(filename)[0]
    return bool(COPY_MARKER_RE.search(stem))


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _to_iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def _is_hidden(path: str, name: str, stat_result: os.stat_result) -> bool:
    if name.startswith("."):
        return True
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))


def _is_reparse_point(path: str) -> bool:
    """Best-effort check for symlinks, junctions and other Windows reparse points."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _inode_key(record: "FileRecord") -> tuple[int, int] | str:
    """A hashable identity for hard-link aware reclaim calculations."""
    if record.inode:
        return (record.device, record.inode)
    return cache_key(record.path)


@dataclass(slots=True)
class FileRecord:
    path: str
    relative_path: str
    name: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    digest: str | None = None
    file_id: str = ""
    from_cache: bool = False
    hardlink: bool = False
    copyish: bool = False
    badges: list[str] = field(default_factory=list)

    @property
    def directory(self) -> str:
        parent = os.path.dirname(self.relative_path)
        return parent if parent else "."

    @property
    def mtime_ms(self) -> int:
        return self.mtime_ns // 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.file_id,
            "name": self.name,
            "path": self.path,
            "relative_path": self.relative_path,
            "directory": self.directory,
            "size": self.size,
            "mtime_ms": self.mtime_ms,
            "mtime_iso": _to_iso(self.mtime_ms),
            "hardlink": self.hardlink,
            "copyish": self.copyish,
            "badges": list(self.badges),
        }


@dataclass(slots=True)
class DuplicateGroup:
    id: str
    digest: str
    size: int
    files: list[FileRecord]
    recommended_keep_id: str = ""
    recommended_reason: str = ""
    reclaimable_bytes: int = 0
    default_delete_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "digest": self.digest,
            "size": self.size,
            "file_count": len(self.files),
            "recommended_keep_id": self.recommended_keep_id,
            "recommended_reason": self.recommended_reason,
            "reclaimable_bytes": self.reclaimable_bytes,
            "default_delete_ids": list(self.default_delete_ids),
            "files": [file.to_dict() for file in self.files],
        }


@dataclass
class ScanResult:
    root: str
    options: dict[str, Any]
    groups: list[DuplicateGroup]
    matched_files: int
    candidate_files: int
    files_hashed: int
    cache_hits: int
    bytes_read: int
    skipped_empty: int
    skipped_symlinks: int
    errors_count: int
    errors: list[dict[str, str]]
    started_at: str
    completed_at: str
    duration_ms: int
    removed_files: int = 0

    @property
    def duplicate_files(self) -> int:
        return sum(len(group.files) for group in self.groups)

    @property
    def redundant_copies(self) -> int:
        return sum(max(0, len(group.files) - 1) for group in self.groups)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(group.reclaimable_bytes for group in self.groups)

    @property
    def matched_bytes(self) -> int:
        return sum(file.size for group in self.groups for file in group.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "options": dict(self.options),
            "groups": [group.to_dict() for group in self.groups],
            "stats": {
                "matched_files": self.matched_files,
                "candidate_files": self.candidate_files,
                "files_hashed": self.files_hashed,
                "cache_hits": self.cache_hits,
                "bytes_read": self.bytes_read,
                "duplicate_files": self.duplicate_files,
                "redundant_copies": self.redundant_copies,
                "duplicate_groups": len(self.groups),
                "reclaimable_bytes": self.reclaimable_bytes,
                "matched_bytes": self.matched_bytes,
                "skipped_empty": self.skipped_empty,
                "skipped_symlinks": self.skipped_symlinks,
                "errors_count": self.errors_count,
                "errors": list(self.errors),
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "duration_ms": self.duration_ms,
                "removed_files": self.removed_files,
            },
        }


def _score_keeper(record: FileRecord) -> tuple[int, int, int, int, str]:
    """Lower is better.

    Prefer names with no copy/backup markers, then shallower locations, then the
    oldest timestamp. This matches how people tend to organise a model library:
    a clean file at the top of the library is usually the one worth keeping.
    """
    depth = record.relative_path.count(os.sep)
    return (
        1 if record.copyish else 0,
        depth,
        record.mtime_ns,
        len(record.relative_path),
        record.relative_path.casefold(),
    )


def refresh_group(group: DuplicateGroup) -> None:
    """Recompute keeper, hard-link flags, badges and reclaimable space in place."""
    if not group.files:
        group.recommended_keep_id = ""
        group.recommended_reason = ""
        group.default_delete_ids = []
        group.reclaimable_bytes = 0
        return

    counts: dict[Any, int] = {}
    for record in group.files:
        counts[_inode_key(record)] = counts.get(_inode_key(record), 0) + 1
    for record in group.files:
        record.hardlink = counts.get(_inode_key(record), 1) > 1

    recommended = min(group.files, key=_score_keeper)
    group.recommended_keep_id = recommended.file_id

    oldest_ns = min(file.mtime_ns for file in group.files)
    shallowest = min(file.relative_path.count(os.sep) for file in group.files)
    if not recommended.copyish and recommended.relative_path.count(os.sep) == shallowest:
        group.recommended_reason = "clean name in a top-level folder"
    elif not recommended.copyish:
        group.recommended_reason = "clean name with no copy markers"
    elif recommended.mtime_ns == oldest_ns:
        group.recommended_reason = "oldest copy in the group"
    else:
        group.recommended_reason = "shortest, cleanest available path"

    for record in group.files:
        badges: list[str] = []
        if record.file_id == group.recommended_keep_id:
            badges.append("recommended")
        if record.mtime_ns == oldest_ns:
            badges.append("oldest")
        if record.hardlink:
            badges.append("hardlink")
        if record.copyish:
            badges.append("copyish")
        record.badges = badges

    unique_inodes = len({_inode_key(file) for file in group.files})
    group.reclaimable_bytes = max(0, unique_inodes - 1) * group.size
    group.default_delete_ids = [
        file.file_id
        for file in group.files
        if file.file_id != group.recommended_keep_id
    ]


def _group_from_matches(
    digest: str, files: list[FileRecord], group_index: int
) -> DuplicateGroup:
    group = DuplicateGroup(
        id=f"g{group_index:03d}-{digest[:8]}",
        digest=digest,
        size=files[0].size,
        files=files,
    )
    refresh_group(group)
    return group


def _cache_lookup(
    hash_cache: MutableMapping[str, Any] | None, record: FileRecord
) -> str | None:
    if hash_cache is None:
        return None
    entry = hash_cache.get(cache_key(record.path))
    if not isinstance(entry, dict):
        return None
    digest = entry.get("digest")
    if (
        entry.get("size") == record.size
        and entry.get("mtime_ns") == record.mtime_ns
        and isinstance(digest, str)
        and SAFE_DIGEST_RE.fullmatch(digest)
    ):
        return digest
    return None


def _sha256_file(
    record: FileRecord,
    *,
    on_chunk: Callable[[int], None],
    cancel: CancelCheck | None,
) -> str:
    digest = hashlib.sha256()
    total_read = 0
    with open(record.path, "rb", buffering=0) as handle:
        while True:
            if cancel is not None and cancel():
                raise ScanCancelled()
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total_read += len(chunk)
            on_chunk(len(chunk))

    try:
        current = os.lstat(record.path)
    except OSError as exc:
        raise FileChangedError(f"file disappeared during scan: {exc}") from exc
    if stat.S_ISLNK(current.st_mode):
        raise FileChangedError("path became a symbolic link during scan")
    if current.st_size != record.size or current.st_mtime_ns != record.mtime_ns:
        raise FileChangedError("file changed while it was being read")
    if record.inode and current.st_ino and current.st_ino != record.inode:
        raise FileChangedError("file was replaced while it was being read")
    if total_read != record.size:
        raise FileChangedError("file size changed while it was being read")
    return digest.hexdigest()


def hash_file_for_verification(path: str, cancel: CancelCheck | None = None) -> str:
    """Hash a file without trusting any cached or previously recorded metadata."""
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while True:
            if cancel is not None and cancel():
                raise ScanCancelled()
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_duplicates(
    root: str,
    *,
    recursive: bool = True,
    weights_only: bool = True,
    include_hidden: bool = False,
    hash_cache: MutableMapping[str, Any] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> ScanResult:
    """Scan ``root`` for byte-identical files and return duplicate groups."""
    started_monotonic = time.monotonic()
    started_at = _iso_now()
    absolute_root = os.path.abspath(os.path.expanduser(os.fspath(root)))
    if not os.path.isdir(absolute_root):
        raise ValueError(f"Directory does not exist or is not a folder: {absolute_root}")

    errors: list[dict[str, str]] = []
    errors_count = 0

    def add_error(path: str, stage: str, message: str) -> None:
        nonlocal errors_count
        errors_count += 1
        if len(errors) < 200:
            errors.append({"path": path, "stage": stage, "message": message})

    last_emit = 0.0

    def emit(force: bool = False, **payload: Any) -> None:
        nonlocal last_emit
        if progress is None:
            return
        now = time.monotonic()
        if not force and now - last_emit < 0.12:
            return
        last_emit = now
        progress(payload)

    def check_cancel() -> bool:
        return bool(cancel is not None and cancel())

    emit(
        force=True,
        phase="discovering",
        message="Mapping the directory",
        files_found=0,
        current_file="",
        files_hashed=0,
        bytes_processed=0,
        bytes_total=0,
        progress=None,
    )

    records: list[FileRecord] = []
    skipped_empty = 0
    skipped_symlinks = 0

    def on_walk_error(exc: OSError) -> None:
        add_error(getattr(exc, "filename", absolute_root) or absolute_root, "discovery", str(exc))

    for current_dir, dir_names, file_names in os.walk(
        absolute_root, topdown=True, onerror=on_walk_error, followlinks=False
    ):
        if check_cancel():
            raise ScanCancelled()

        if not recursive:
            dir_names[:] = []
        else:
            filtered_dirs: list[str] = []
            for name in sorted(dir_names):
                directory_path = os.path.join(current_dir, name)
                if not include_hidden:
                    if name.startswith("."):
                        continue
                    try:
                        dir_stat = os.lstat(directory_path)
                    except OSError as exc:
                        add_error(directory_path, "discovery", str(exc))
                        continue
                    if _is_hidden(directory_path, name, dir_stat):
                        continue
                # Never follow junctions/symlinks or other reparse points.
                if _is_reparse_point(directory_path):
                    continue
                filtered_dirs.append(name)
            dir_names[:] = filtered_dirs

        for filename in sorted(file_names):
            if check_cancel():
                raise ScanCancelled()
            full_path = os.path.join(current_dir, filename)
            if weights_only and not is_weight_file(filename):
                continue
            if _is_reparse_point(full_path):
                skipped_symlinks += 1
                continue
            try:
                st = os.lstat(full_path)
            except OSError as exc:
                add_error(full_path, "discovery", str(exc))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if not include_hidden and _is_hidden(full_path, filename, st):
                continue
            if st.st_size == 0:
                skipped_empty += 1
                continue

            relative = os.path.relpath(full_path, absolute_root)
            record = FileRecord(
                path=full_path,
                relative_path=relative,
                name=filename,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                device=int(getattr(st, "st_dev", 0) or 0),
                inode=int(getattr(st, "st_ino", 0) or 0),
                copyish=looks_like_copy(filename),
            )
            record.file_id = f"f{len(records) + 1:06d}"
            records.append(record)
            emit(
                files_found=len(records),
                current_file=relative,
                message="Mapping the directory",
            )

    if check_cancel():
        raise ScanCancelled()

    records.sort(key=lambda record: record.relative_path.casefold())

    by_size: dict[int, list[FileRecord]] = {}
    for record in records:
        by_size.setdefault(record.size, []).append(record)
    candidates = [
        record
        for same_size in by_size.values()
        if len(same_size) > 1
        for record in same_size
    ]
    candidates.sort(key=lambda record: record.relative_path.casefold())
    total_hash_bytes = sum(record.size for record in candidates)

    emit(
        force=True,
        phase="hashing" if candidates else "finalizing",
        message=(
            "Fingerprinting same-size candidates"
            if candidates
            else "Checking for matching files"
        ),
        files_found=len(records),
        files_to_hash=len(candidates),
        files_hashed=0,
        bytes_processed=0,
        bytes_total=total_hash_bytes,
        progress=3 if candidates else 95,
        current_file="",
    )

    processed_bytes = 0
    hashed_count = 0
    cache_hits = 0
    actual_bytes_read = 0

    def advance_cache(record: FileRecord, cached_digest: str) -> None:
        nonlocal processed_bytes, hashed_count, cache_hits
        record.digest = cached_digest
        record.from_cache = True
        cache_hits += 1
        hashed_count += 1
        processed_bytes += record.size
        emit(
            files_found=len(records),
            files_to_hash=len(candidates),
            files_hashed=hashed_count,
            bytes_processed=processed_bytes,
            bytes_total=total_hash_bytes,
            progress=(3 + (processed_bytes / total_hash_bytes) * 92)
            if total_hash_bytes
            else 95,
            current_file=record.relative_path,
            message="Fingerprinting same-size candidates",
        )

    for index, record in enumerate(candidates):
        if check_cancel():
            raise ScanCancelled()

        cached_digest = _cache_lookup(hash_cache, record)
        if cached_digest is not None:
            advance_cache(record, cached_digest)
            continue

        base_processed = processed_bytes
        record_bytes = 0

        def on_chunk(size: int) -> None:
            nonlocal processed_bytes, record_bytes
            record_bytes += size
            processed_bytes = base_processed + record_bytes
            emit(
                files_found=len(records),
                files_to_hash=len(candidates),
                files_hashed=hashed_count,
                bytes_processed=processed_bytes,
                bytes_total=total_hash_bytes,
                progress=(3 + (processed_bytes / total_hash_bytes) * 92)
                if total_hash_bytes
                else 95,
                current_file=record.relative_path,
                message="Fingerprinting same-size candidates",
                record_index=index + 1,
            )

        try:
            digest = _sha256_file(record, on_chunk=on_chunk, cancel=cancel)
        except ScanCancelled:
            raise
        except FileChangedError as exc:
            add_error(record.path, "hashing", str(exc))
            processed_bytes = base_processed + record.size
            continue
        except OSError as exc:
            add_error(record.path, "hashing", str(exc))
            processed_bytes = base_processed + record.size
            emit(
                files_found=len(records),
                files_to_hash=len(candidates),
                files_hashed=hashed_count,
                bytes_processed=processed_bytes,
                bytes_total=total_hash_bytes,
                progress=(3 + (processed_bytes / total_hash_bytes) * 92)
                if total_hash_bytes
                else 95,
                current_file=record.relative_path,
                message="Fingerprinting same-size candidates",
            )
            continue

        record.digest = digest
        hashed_count += 1
        actual_bytes_read += record.size
        if hash_cache is not None:
            hash_cache[cache_key(record.path)] = {
                "size": record.size,
                "mtime_ns": record.mtime_ns,
                "digest": digest,
            }
        emit(
            files_found=len(records),
            files_to_hash=len(candidates),
            files_hashed=hashed_count,
            bytes_processed=processed_bytes,
            bytes_total=total_hash_bytes,
            progress=(3 + (processed_bytes / total_hash_bytes) * 92)
            if total_hash_bytes
            else 95,
            current_file=record.relative_path,
            message="Fingerprinting same-size candidates",
        )

    if check_cancel():
        raise ScanCancelled()

    emit(
        force=True,
        phase="finalizing",
        message="Building duplicate groups",
        files_found=len(records),
        files_hashed=hashed_count,
        bytes_processed=processed_bytes,
        bytes_total=total_hash_bytes,
        progress=96,
        current_file="",
    )

    by_content: dict[tuple[int, str], list[FileRecord]] = {}
    for record in candidates:
        if record.digest is not None:
            by_content.setdefault((record.size, record.digest), []).append(record)

    matches = [
        (digest, files)
        for (_, digest), files in by_content.items()
        if len(files) > 1
    ]
    matches.sort(
        key=lambda item: (
            -((len({_inode_key(file) for file in item[1]}) - 1) * item[1][0].size),
            -item[1][0].size,
            item[1][0].relative_path.casefold(),
        )
    )

    groups: list[DuplicateGroup] = []
    for group_index, (digest, files) in enumerate(matches, start=1):
        files.sort(key=lambda record: record.relative_path.casefold())
        groups.append(_group_from_matches(digest, files, group_index))

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    result = ScanResult(
        root=absolute_root,
        options={
            "recursive": recursive,
            "weights_only": weights_only,
            "include_hidden": include_hidden,
        },
        groups=groups,
        matched_files=len(records),
        candidate_files=len(candidates),
        files_hashed=hashed_count,
        cache_hits=cache_hits,
        bytes_read=actual_bytes_read,
        skipped_empty=skipped_empty,
        skipped_symlinks=skipped_symlinks,
        errors_count=errors_count,
        errors=errors,
        started_at=started_at,
        completed_at=_iso_now(),
        duration_ms=duration_ms,
    )
    emit(
        force=True,
        phase="complete",
        message="Scan complete",
        files_found=len(records),
        files_hashed=hashed_count,
        bytes_processed=processed_bytes,
        bytes_total=total_hash_bytes,
        progress=100,
        current_file="",
    )
    return result


def rebuild_result_after_delete(
    result: ScanResult, deleted_paths: set[str]
) -> None:
    """Remove successfully deleted files from a result and refresh all totals."""
    deleted_norm = {cache_key(path) for path in deleted_paths}
    new_groups: list[DuplicateGroup] = []
    files_removed = 0

    for group in result.groups:
        kept = [
            file
            for file in group.files
            if cache_key(file.path) not in deleted_norm
        ]
        files_removed += len(group.files) - len(kept)
        if len(kept) < 2:
            continue
        group.files = kept
        refresh_group(group)
        new_groups.append(group)

    result.groups = new_groups
    result.matched_files = max(0, result.matched_files - files_removed)
    result.removed_files += files_removed
    result.completed_at = _iso_now()
