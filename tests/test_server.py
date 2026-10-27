from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from app import (
    API_TOKEN_HEADER,
    AppState,
    RequestHandler,
    WeightWiseServer,
    existing_instance_url,
)

TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".testtmp"


class WeightWiseServerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.temp_path = TEST_TMP_ROOT / f"server-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.temp_path.mkdir()
        self.root = self.temp_path / "library"
        self.root.mkdir()
        self.state = AppState(str(self.root), self.temp_path / "cache.json")
        self.server = WeightWiseServer(("127.0.0.1", 0), RequestHandler, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.state.token

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def http(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any = None,
        token: bool = True,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers: dict[str, str] = {}
        if token:
            headers[API_TOKEN_HEADER] = self.token
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any = None,
        token: bool = True,
    ) -> tuple[int, dict]:
        status, headers, raw = self.http(
            path, method=method, payload=payload, token=token
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = {}
        return status, data

    def write(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def scan_to_completion(self) -> dict:
        status, payload = self.json_request(
            "/api/scan",
            method="POST",
            payload={
                "path": str(self.root),
                "recursive": True,
                "weights_only": True,
                "include_hidden": False,
            },
        )
        self.assertEqual(status, 202, payload)
        job_id = payload["job"]["id"]
        deadline = time.time() + 15
        last_job = None
        while time.time() < deadline:
            status, payload = self.json_request(
                f"/api/scan/status?{urllib.parse.urlencode({'job_id': job_id})}"
            )
            self.assertEqual(status, 200, payload)
            last_job = payload["job"]
            if last_job["status"] != "running":
                break
            time.sleep(0.02)
        self.assertIsNotNone(last_job)
        self.assertEqual(last_job["status"], "complete", last_job)
        status, payload = self.json_request("/api/result")
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(payload["result"])
        return payload["result"]

    def test_index_embeds_token_and_security_headers(self) -> None:
        status, headers, raw = self.http("/", token=False)
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn(self.token, text)
        self.assertNotIn("__APP_TOKEN__", text)
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_api_requires_token(self) -> None:
        status, payload = self.json_request("/api/config", token=False)
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

        status, payload = self.json_request("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("default_root", payload)
        self.assertIn(".safetensors", payload["weight_extensions"])

    def test_invalid_folder_is_rejected(self) -> None:
        missing = self.temp_path / "does-not-exist"
        status, payload = self.json_request(
            "/api/scan", method="POST", payload={"path": str(missing)}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_scan_delete_and_keep_one_copy(self) -> None:
        content = b"identical-model-weights" * 2048
        files = [
            self.write("group-a/one.safetensors", content),
            self.write("group-a/two copy.safetensors", content),
            self.write("group-b/three.safetensors", content),
        ]
        result = self.scan_to_completion()
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["file_count"], 3)
        self.assertGreater(len(group["digest"]), 32)
        ids = [file["id"] for file in group["files"]]

        # The backend must refuse to delete every copy.
        status, payload = self.json_request(
            "/api/delete",
            method="POST",
            payload={
                "confirm": True,
                "mode": "permanent",
                "confirm_text": "DELETE",
                "groups": [{"group_id": group["id"], "file_ids": ids}],
            },
        )
        self.assertEqual(status, 409, payload)
        self.assertTrue(all(path.exists() for path in files))

        # Permanent deletion needs the exact typed confirmation.
        status, payload = self.json_request(
            "/api/delete",
            method="POST",
            payload={
                "confirm": True,
                "mode": "permanent",
                "confirm_text": "nope",
                "groups": [{"group_id": group["id"], "file_ids": ids[:1]}],
            },
        )
        self.assertEqual(status, 400, payload)
        self.assertTrue(all(path.exists() for path in files))

        # Delete two copies, leaving exactly one.
        status, payload = self.json_request(
            "/api/delete",
            method="POST",
            payload={
                "confirm": True,
                "mode": "permanent",
                "confirm_text": "DELETE",
                "groups": [{"group_id": group["id"], "file_ids": ids[:2]}],
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["deleted_count"], 2)
        self.assertEqual(payload["failed_count"], 0)
        survivors = [path for path in files if path.exists()]
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0].read_bytes(), content)
        self.assertEqual(payload["result"]["groups"], [])
        self.assertEqual(payload["result"]["stats"]["duplicate_groups"], 0)
        self.assertEqual(payload["result"]["stats"]["removed_files"], 2)
        self.assertGreater(payload["freed_estimate_bytes"], 0)

    def test_changed_file_aborts_deletion(self) -> None:
        content = b"same-once" * 2048
        first = self.write("first.safetensors", content)
        second = self.write("second.safetensors", content)
        result = self.scan_to_completion()
        group = result["groups"][0]

        first.write_bytes(b"different!" * 1024)
        status, payload = self.json_request(
            "/api/delete",
            method="POST",
            payload={
                "confirm": True,
                "mode": "permanent",
                "confirm_text": "DELETE",
                "groups": [
                    {
                        "group_id": group["id"],
                        "file_ids": [next(f["id"] for f in group["files"] if f["path"] == str(first))],
                    }
                ],
            },
        )
        self.assertEqual(status, 409, payload)
        self.assertIn("Nothing was deleted", payload["error"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_browse_is_local_and_protected(self) -> None:
        status, payload = self.json_request("/api/browse", token=False)
        self.assertEqual(status, 403)

        status, payload = self.json_request(
            f"/api/browse?{urllib.parse.urlencode({'path': str(self.root)})}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], str(self.root))
        self.assertIn("directories", payload)
        self.assertIn("quick_access", payload)

        # A file is not a browsable folder.
        file_path = self.write("not-a-folder.txt", b"hello")
        status, payload = self.json_request(
            f"/api/browse?{urllib.parse.urlencode({'path': str(file_path)})}"
        )
        self.assertEqual(status, 400)

    def test_reveal_only_accepts_scanned_paths(self) -> None:
        self.write("a.safetensors", b"x")
        outside = self.temp_path / "outside"
        outside.mkdir()
        status, payload = self.json_request(
            "/api/reveal", method="POST", payload={"path": str(outside)}
        )
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

    def test_scan_status_rejects_unknown_job(self) -> None:
        status, payload = self.json_request("/api/scan/status?job_id=unknown")
        self.assertEqual(status, 404)

    def test_port_is_claimed_exclusively_and_detected(self) -> None:
        port = self.server.server_address[1]
        try:
            second = WeightWiseServer(("127.0.0.1", port), RequestHandler, self.state)
        except OSError:
            pass
        else:
            second.server_close()
            self.fail("a second server was allowed to bind the active port")
        self.assertEqual(existing_instance_url(port), self.base)

    def test_delete_requires_review_confirmation_flag(self) -> None:
        content = b"review-required" * 500
        self.write("a.safetensors", content)
        self.write("b.safetensors", content)
        result = self.scan_to_completion()
        group = result["groups"][0]
        status, payload = self.json_request(
            "/api/delete",
            method="POST",
            payload={
                "confirm": False,
                "mode": "permanent",
                "confirm_text": "DELETE",
                "groups": [
                    {
                        "group_id": group["id"],
                        "file_ids": [group["files"][0]["id"]],
                    }
                ],
            },
        )
        self.assertEqual(status, 400)

    def test_html_does_not_reference_external_assets(self) -> None:
        _, _, raw = self.http("/", token=False)
        text = raw.decode("utf-8")
        for match in re.findall(r'(?:src|href)="([^"]+)"', text):
            self.assertTrue(
                match.startswith("/") or match.startswith("#"),
                f"unexpected external asset: {match}",
            )


if __name__ == "__main__":
    unittest.main()
