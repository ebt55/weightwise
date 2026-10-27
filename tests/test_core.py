from __future__ import annotations

import os
import shutil
import time
import unittest
import uuid
from pathlib import Path

from dedupe_core import (
    WEIGHT_EXTENSIONS,
    ScanCancelled,
    rebuild_result_after_delete,
    scan_duplicates,
)


def write_file(path: Path, data: bytes, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".testtmp"


def make_test_root() -> Path:
    TEST_TMP_ROOT.mkdir(exist_ok=True)
    root = TEST_TMP_ROOT / f"core-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    return root


class ScanCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_test_root()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_byte_identical_files_become_one_group(self) -> None:
        payload = b"weight-tensor-payload" * 4000
        original = write_file(self.root / "models" / "base.safetensors", payload)
        copy = write_file(self.root / "archive" / "base copy.safetensors", payload)
        different = bytearray(payload)
        different[-1] ^= 0xFF
        write_file(self.root / "models" / "different.safetensors", bytes(different))
        write_file(self.root / "notes.txt", payload)
        write_file(self.root / "empty.safetensors", b"")

        result = scan_duplicates(str(self.root))

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(group.size, len(payload))
        self.assertEqual(len(group.files), 2)
        self.assertEqual({file.name for file in group.files}, {"base.safetensors", "base copy.safetensors"})
        self.assertEqual(result.matched_files, 3)
        self.assertEqual(result.candidate_files, 3)
        self.assertGreaterEqual(result.files_hashed, 2)
        self.assertEqual(result.skipped_empty, 1)
        self.assertEqual(result.errors_count, 0)
        self.assertEqual(group.recommended_keep_id, group.files[0].file_id if group.files[0].name == "base.safetensors" else group.files[1].file_id)
        self.assertEqual(len(group.default_delete_ids), 1)
        self.assertNotIn(group.recommended_keep_id, group.default_delete_ids)
        self.assertIn(str(original), {file.path for file in group.files})
        self.assertIn(str(copy), {file.path for file in group.files})

    def test_weights_only_can_be_disabled(self) -> None:
        payload = b"same-content" * 100
        write_file(self.root / "a" / "model.safetensors", payload)
        write_file(self.root / "b" / "model.safetensors", payload)
        write_file(self.root / "a" / "same.txt", payload)
        write_file(self.root / "b" / "same.txt", payload)

        weights_only = scan_duplicates(str(self.root), weights_only=True)
        self.assertEqual({len(group.files) for group in weights_only.groups}, {2})
        self.assertEqual(len(weights_only.groups), 1)

        all_files = scan_duplicates(str(self.root), weights_only=False)
        self.assertEqual(len(all_files.groups), 1)
        self.assertEqual(len(all_files.groups[0].files), 4)

    def test_non_recursive_scan_ignores_subfolders(self) -> None:
        payload = b"nested duplicate" * 100
        write_file(self.root / "top.safetensors", payload)
        write_file(self.root / "nested" / "copy.safetensors", payload)

        recursive = scan_duplicates(str(self.root), recursive=True)
        top_only = scan_duplicates(str(self.root), recursive=False)

        self.assertEqual(len(recursive.groups), 1)
        self.assertEqual(len(top_only.groups), 0)
        self.assertEqual(top_only.matched_files, 1)

    def test_smart_keep_prefers_clean_name_over_copy_marker(self) -> None:
        payload = b"keeper-selection" * 200
        now = time.time()
        write_file(self.root / "backup" / "model copy.safetensors", payload, now - 2000)
        clean = write_file(self.root / "model.safetensors", payload, now - 1000)
        write_file(self.root / "old" / "model.safetensors", payload, now - 3000)

        result = scan_duplicates(str(self.root))
        group = result.groups[0]
        kept = next(file for file in group.files if file.file_id == group.recommended_keep_id)

        self.assertEqual(kept.path, str(clean))
        self.assertFalse(kept.copyish)
        self.assertIn("recommended", kept.badges)
        self.assertEqual(len(group.default_delete_ids), 2)

    def test_hard_links_are_flagged_and_do_not_inflate_reclaimable_space(self) -> None:
        payload = b"hard-linked-weights" * 500
        original = write_file(self.root / "model.safetensors", payload)
        hardlink = self.root / "alias.safetensors"
        try:
            os.link(original, hardlink)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        result = scan_duplicates(str(self.root))
        group = result.groups[0]

        self.assertEqual(len(group.files), 2)
        self.assertTrue(all(file.hardlink for file in group.files))
        self.assertEqual(group.reclaimable_bytes, 0)
        self.assertTrue(any("hardlink" in file.badges for file in group.files))

    def test_cache_is_used_on_second_scan(self) -> None:
        payload = b"cached" * 2000
        write_file(self.root / "a.safetensors", payload)
        write_file(self.root / "b.safetensors", payload)
        cache: dict = {}

        first = scan_duplicates(str(self.root), hash_cache=cache)
        second = scan_duplicates(str(self.root), hash_cache=cache)

        self.assertEqual(len(first.groups), 1)
        self.assertEqual(len(second.groups), 1)
        self.assertEqual(first.cache_hits, 0)
        self.assertEqual(second.cache_hits, 2)
        self.assertEqual(second.bytes_read, 0)

    def test_progress_reaches_complete_phase(self) -> None:
        payload = b"progress" * 200
        write_file(self.root / "a.safetensors", payload)
        write_file(self.root / "b.safetensors", payload)
        events: list[dict] = []

        scan_duplicates(str(self.root), progress=events.append)

        self.assertTrue(events)
        self.assertEqual(events[-1]["phase"], "complete")
        self.assertEqual(events[-1]["progress"], 100)

    def test_extension_matching_is_case_insensitive(self) -> None:
        self.assertIn(".safetensors", WEIGHT_EXTENSIONS)
        write_file(self.root / "A.SafeTensors", b"one")
        write_file(self.root / "b.SafeTensors", b"one")
        result = scan_duplicates(str(self.root))
        self.assertEqual(len(result.groups), 1)

    def test_smart_keep_avoids_older_newer_copy_markers(self) -> None:
        payload = b"dated-copies" * 300
        now = time.time()
        clean = write_file(self.root / "flux-turbo.safetensors", payload, now - 1000)
        write_file(self.root / "flux-turbo (older).safetensors", payload, now - 9000)
        write_file(self.root / "flux-turbo (newer).safetensors", payload, now)

        result = scan_duplicates(str(self.root))
        group = result.groups[0]
        kept = next(file for file in group.files if file.file_id == group.recommended_keep_id)
        self.assertEqual(kept.path, str(clean))

    def test_progress_callback_can_cancel_scan(self) -> None:
        payload = b"cancel-me" * 5000
        write_file(self.root / "a.safetensors", payload)
        write_file(self.root / "b.safetensors", payload)
        cancelled = False

        def progress(event: dict) -> None:
            nonlocal cancelled
            if event.get("phase") == "hashing":
                cancelled = True

        with self.assertRaises(ScanCancelled):
            scan_duplicates(
                str(self.root),
                progress=progress,
                cancel=lambda: cancelled,
            )

    def test_result_rebuild_after_deletion(self) -> None:
        payload = b"delete-me" * 1000
        paths = [
            write_file(self.root / f"copy{i}.safetensors", payload)
            for i in range(3)
        ]
        result = scan_duplicates(str(self.root))
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.removed_files, 0)

        os.remove(paths[0])
        rebuild_result_after_delete(result, {str(paths[0])})

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(len(group.files), 2)
        self.assertEqual(len(group.default_delete_ids), 1)
        self.assertEqual(result.matched_files, 2)
        self.assertEqual(result.redundant_copies, 1)
        self.assertEqual(result.removed_files, 1)


if __name__ == "__main__":
    unittest.main()
