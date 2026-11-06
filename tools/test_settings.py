"""Isolated settings regression tests: python3 -m unittest tools.test_settings."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import database

with patch.object(database, "init_db"):
    import web


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config_lcd.json"
        self.original = {
            "lcd": {"url": "http://10.0.0.81:5000", "spotify_account": "all", "display_seconds": 30},
            "unrelated": "private-test-marker",
        }
        self.path.write_text(json.dumps(self.original))
        self.enterContext(patch.object(web, "LCD_CONFIG_PATH", str(self.path)))
        self.enterContext(patch.dict(os.environ, {"TITPI_ALLOW_RESTART": "0"}))
        self.client = web.app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def payload(self, **updates):
        loaded = self.client.get("/api/settings/lcd", headers=self.headers).get_json()
        return {"lcd": {**loaded["lcd"], **updates}, "version": loaded["version"]}

    def test_no_password_required(self):
        with patch.dict(os.environ, {"TITPI_SETTINGS_PASSWORD_HASH": ""}):
            self.assertEqual(self.client.get("/api/settings/lcd").status_code, 200)
            response = self.client.patch("/api/settings/lcd", json=self.payload(display_seconds=45), headers=self.headers)
            self.assertEqual(response.status_code, 200)

    def test_page_loads_directly_and_read_is_filtered(self):
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'admin-password', page.data)
        self.assertNotIn(b'unlock-form', page.data)
        self.assertNotIn(b'X-TitPi-Password', page.data)
        self.assertIn(b'loadSettings();', page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        response = self.client.get("/api/settings/lcd", headers=self.headers)
        self.assertNotIn(b"private-test-marker", response.data)
        self.assertEqual(set(response.get_json()), {"lcd", "version", "can_restart"})

    def test_save_backup_conflict_and_permissions(self):
        body = self.payload(display_seconds=45)
        response = self.client.patch("/api/settings/lcd", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["lcd"]["display_seconds"], 45)
        self.assertEqual(saved["unrelated"], self.original["unrelated"])
        self.assertEqual(json.loads(Path(str(self.path) + ".bak").read_text()), self.original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.patch("/api/settings/lcd", json=body, headers=self.headers).status_code, 409)

    def test_reject_cross_origin_and_non_json(self):
        body = self.payload()
        for origin in ("http://evil.invalid", "null", ""):
            with self.subTest(origin=origin):
                headers = {**self.headers, "Origin": origin}
                self.assertEqual(self.client.patch("/api/settings/lcd", json=body, headers=headers).status_code, 403)
        self.assertEqual(self.client.patch("/api/settings/lcd", data="{}", headers=self.headers).status_code, 415)

    def test_invalid_values_cannot_modify_file(self):
        invalid = [
            {"url": "file:///tmp/test"}, {"url": "http://user:password@example.com"},
            {"url": "http://example.com:99999"}, {"url": "http://example.com/api/upload"},
            {"url": "http://example.com?token=secret"}, {"url": "http://bad host"},
            {"spotify_account": ""}, {"spotify_account": "all; reboot"},
            {"display_seconds": 0}, {"display_seconds": 3601},
            {"display_seconds": True}, {"display_seconds": 1.5}, {"display_seconds": "30"},
        ]
        before = self.path.read_bytes()
        for values in invalid:
            with self.subTest(values=values):
                response = self.client.patch("/api/settings/lcd", json=self.payload(**values), headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_reject_extra_fields(self):
        body = self.payload()
        body["lcd"]["smtp_password"] = "not-a-real-password"
        self.assertEqual(self.client.patch("/api/settings/lcd", json=body, headers=self.headers).status_code, 400)

    def test_disabled_url_and_named_profile(self):
        response = self.client.patch("/api/settings/lcd", json=self.payload(url="", spotify_account="renan"), headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_backup_failure_preserves_original(self):
        before = self.path.read_bytes()
        with patch.object(web, "_atomic_settings_write", side_effect=PermissionError):
            response = self.client.patch("/api/settings/lcd", json=self.payload(display_seconds=45), headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)

    def test_restart_is_explicit_and_fixed(self):
        with patch.object(web.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(self.client.post("/api/settings/restart", json={}, headers=self.headers).status_code, 403)
            run.assert_not_called()
            with patch.dict(os.environ, {"TITPI_ALLOW_RESTART": "1"}):
                self.assertEqual(self.client.post("/api/settings/restart", json={}, headers=self.headers).status_code, 200)
            run.assert_called_once_with(["sudo", "-n", "/usr/bin/systemctl", "restart", "titpi.service"], capture_output=True, timeout=20, check=False)

    def test_restart_errors_are_safe(self):
        with patch.dict(os.environ, {"TITPI_ALLOW_RESTART": "1"}):
            for result in (Mock(returncode=1), subprocess.TimeoutExpired("systemctl", 20), OSError()):
                with self.subTest(result=type(result).__name__):
                    with patch.object(web.subprocess, "run") as run:
                        if isinstance(result, Exception):
                            run.side_effect = result
                        else:
                            run.return_value = result
                        self.assertEqual(self.client.post("/api/settings/restart", json={}, headers=self.headers).status_code, 503)


if __name__ == "__main__":
    unittest.main()