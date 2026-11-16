"""Isolated settings regression tests: python3 -m unittest tools.test_settings."""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import database

with patch.object(database, "init_db"):
    import web


class VisitDeletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / 'captures'
        self.media.mkdir()
        self.enterContext(patch.object(database, 'DB_PATH', str(self.root / 'test.db')))
        self.enterContext(patch.object(database, '_cfg', {'camera': {'save_directory': str(self.media)}}))
        database.init_db()
        self.client = web.app.test_client()

    def add_visit(self, photo, video=None):
        with database._connect() as connection:
            cursor = connection.execute(
                'INSERT INTO detections (timestamp, photo_path, video_path) VALUES (?, ?, ?)',
                ('2026-11-12 12:00:00', str(photo) if photo else None, str(video) if video else None))
            return cursor.lastrowid

    def test_single_delete_removes_photo_video_and_extra_visit_photos(self):
        names = ['visitor_20261112-120000.jpg', 'visitor_20261112-120000.mp4',
                 'bird_20261112-120000_0.jpg', 'bird_20261112-120000_12.jpg']
        for name in names + ['bird_20261112-120001_0.jpg', 'detections.db']:
            (self.media / name).write_bytes(b'test')
        visit = self.add_visit(self.media / names[2], self.media / names[1])
        response = self.client.delete(f'/api/detection/{visit}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(not (self.media / name).exists() for name in names))
        self.assertTrue((self.media / 'bird_20261112-120001_0.jpg').exists())
        self.assertTrue((self.media / 'detections.db').exists())
        self.assertEqual(self.client.delete(f'/api/detection/{visit}').status_code, 404)

    def test_bulk_delete_tolerates_missing_media(self):
        photo = self.media / 'visitor_20261112-120000.jpg'
        photo.write_bytes(b'test')
        first = self.add_visit(photo)
        second = self.add_visit(self.media / 'missing.jpg', self.media / 'missing.mp4')
        response = self.client.delete('/api/detections/bulk', json={'ids': [first, second]})
        self.assertEqual(response.get_json(), {'ok': True, 'deleted': 2})
        self.assertFalse(photo.exists())

    def test_shared_media_is_preserved_until_last_reference(self):
        photo = self.media / 'visitor_20261112-120000.jpg'
        extra = self.media / 'bird_20261112-120000_0.jpg'
        photo.write_bytes(b'test')
        extra.write_bytes(b'test')
        first, second = self.add_visit(photo), self.add_visit(photo)
        self.assertTrue(database.delete_detection(first))
        self.assertTrue(photo.exists())
        self.assertTrue(extra.exists())
        self.assertTrue(database.delete_detection(second))
        self.assertFalse(photo.exists())
        self.assertFalse(extra.exists())

    def test_outside_paths_and_symlink_targets_are_not_deleted(self):
        outside = self.root / 'outside.jpg'
        outside.write_bytes(b'test')
        visit = self.add_visit(outside, self.media / '..' / 'outside.jpg')
        self.assertTrue(database.delete_detection(visit))
        self.assertTrue(outside.exists())
        link = self.media / 'visitor_20261112-120000.jpg'
        link.symlink_to(outside)
        visit = self.add_visit(link)
        self.assertTrue(database.delete_detection(visit))
        self.assertFalse(link.is_symlink())
        self.assertTrue(outside.exists())

    def test_cleanup_error_keeps_visit_for_retry(self):
        photo = self.media / 'visitor_20261112-120000.jpg'
        photo.write_bytes(b'test')
        visit = self.add_visit(photo)
        with patch.object(Path, 'unlink', side_effect=PermissionError('test')):
            with self.assertRaises(PermissionError):
                database.delete_detection(visit)
        with database._connect() as connection:
            self.assertIsNotNone(connection.execute('SELECT id FROM detections WHERE id=?', (visit,)).fetchone())
        self.assertTrue(database.delete_detection(visit))

    def test_delete_invalidates_cached_photo_and_cancels_pending_email(self):
        from identification_queue import IdentificationQueue
        photo = self.media / 'visitor_20261112-120000.jpg'
        photo.write_bytes(b'test')
        visit = self.add_visit(photo)
        database.set_bird_of_day('2026-11-12', 'Test species', 'Test bird', str(photo), 1)
        queue = IdentificationQueue(database.DB_PATH)
        queue.enqueue({'timestamp': '2026-11-12 12:00:00', 'photo_path': str(photo)})
        job = queue.claim()
        queue.complete(job['id'], {'status': 'identified'}, notify=True)
        self.assertTrue(database.delete_detection(visit))
        self.assertIsNone(queue.claim_notification())
        with database._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM bird_of_day').fetchone()[0], 0)

    def test_same_timestamp_shared_capture_keeps_extra_photos(self):
        photo = self.media / 'visitor_20261112-120000.jpg'
        extra = self.media / 'bird_20261112-120000_0.jpg'
        photo.write_bytes(b'test')
        extra.write_bytes(b'test')
        first = self.add_visit(photo)
        second = self.add_visit(extra)
        self.assertTrue(database.delete_detection(first))
        self.assertTrue(extra.exists())
        self.assertTrue(database.delete_detection(second))
        self.assertFalse(extra.exists())

    def test_api_reports_cleanup_failure_and_retains_failed_visit(self):
        photo = self.media / 'visitor_20261112-120000.jpg'
        photo.write_bytes(b'test')
        visit = self.add_visit(photo)
        with patch.object(Path, 'unlink', side_effect=PermissionError('test')):
            response = self.client.delete(f'/api/detection/{visit}')
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.get_json()['ok'])
            response = self.client.delete('/api/detections/bulk', json={'ids': [visit]})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()['deleted'], 0)
        self.assertTrue(database.delete_detection(visit))


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

    def test_removed_callback_is_unavailable(self):
        self.assertEqual(self.client.post("/hermes-callback", json={"result": "test"}).status_code, 404)

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


class IdentificationSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.original = {"camera": {"keep": True}, "email": {"password": "private-smtp-marker"},
                         "local_classifier": {"enabled": False, "min_confidence": 0.3},
                         "openai": {"api_key": "private-openai-marker"},
                         "claude": {"api_key": "private-claude-marker"}}
        self.path.write_text(json.dumps(self.original))
        self.enterContext(patch.object(web, "CONFIG_PATH", str(self.path)))
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}))
        self.client = web.app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def payload(self):
        body = self.client.get("/api/settings/identification").get_json()
        body.pop("can_restart")
        body["local_classifier"].pop("enabled")
        for provider in ("openai", "claude"):
            body[provider].pop("api_key_configured")
        return body

    def test_read_redacts_all_credentials_and_local_is_required(self):
        response = self.client.get("/api/settings/identification")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"private-", response.data)
        body = response.get_json()
        self.assertTrue(body["local_classifier"]["enabled"])
        self.assertTrue(body["openai"]["api_key_configured"])
        self.assertTrue(body["claude"]["api_key_configured"])
        self.assertEqual(body["identification"]["prompt"], web.DEFAULT_PROMPT)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_four_modes_preserve_secrets_and_unrelated_settings(self):
        for provider in ("none", "hermes_ssh", "openai", "claude"):
            body = self.payload()
            body["identification"]["provider"] = provider
            body["identification"]["prompt"] = "Which bird species is visible, or is there no bird?"
            body["local_classifier"]["min_confidence"] = 0.7
            body["openai"]["api_key"] = ""
            response = self.client.patch("/api/settings/identification", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved["identification"]["provider"], provider)
            self.assertEqual(saved["identification"]["prompt"], body["identification"]["prompt"])
            self.assertEqual(saved["local_classifier"], {"enabled": True, "min_confidence": 0.7})
            for section in ("camera", "email"):
                self.assertEqual(saved[section], self.original[section])
            for section, key in (("openai", "api_key"), ("claude", "api_key")):
                self.assertEqual(saved[section][key], self.original[section][key])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(Path(str(self.path) + ".bak").exists())

    def test_ssh_fields_save_without_connecting_and_reject_unsafe_values(self):
        body = self.payload()
        body['identification']['provider'] = 'hermes_ssh'
        body['hermes_ssh'].update({'host': 'macmini.local', 'user': 'renanfernandes',
                                  'key_path': '/home/titpi/.ssh/dedicated key', 'remote_temp_dir': '/tmp',
                                  'timeout': 90, 'min_confidence': 0.8, 'notify_all': False})
        with patch.object(web.subprocess, 'run') as run:
            self.assertEqual(self.client.patch('/api/settings/identification', json=body, headers=self.headers).status_code, 200)
            run.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())['hermes_ssh'], body['hermes_ssh'])
        before = self.path.read_bytes()
        for field, value in [('host', 'host;id'), ('user', '-option'), ('remote_temp_dir', '/tmp/../etc'),
                             ('key_path', 'relative'), ('hermes_path', '/tmp/bad\ncommand'),
                             ('timeout', 301), ('connect_timeout', True), ('max_attempts', 4),
                             ('min_confidence', 0), ('notify_all', 'true')]:
            invalid = self.payload()
            invalid['hermes_ssh'][field] = value
            self.assertEqual(self.client.patch('/api/settings/identification', json=invalid, headers=self.headers).status_code, 400)
            self.assertEqual(self.path.read_bytes(), before)

    def test_replace_key_without_echo_and_conflict(self):
        body = self.payload()
        body["claude"]["api_key"] = "new-private-marker"
        response = self.client.patch("/api/settings/identification", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"new-private-marker", response.data)
        self.assertEqual(json.loads(self.path.read_text())["claude"]["api_key"], "new-private-marker")
        self.assertEqual(self.client.patch("/api/settings/identification", json=body, headers=self.headers).status_code, 409)

    def test_invalid_values_and_local_disable_do_not_write(self):
        invalid = [("identification", "provider", "copilot"), ("local_classifier", "enabled", False),
                   ("identification", "prompt", ""), ("identification", "prompt", "x" * 4001),
                   ("local_classifier", "min_confidence", True), ("local_classifier", "min_confidence", 0),
                   ("local_classifier", "min_confidence", 1.1), ("identification", "timeout", 61),
                   ("identification", "max_attempts", True), ("openai", "api_key", "bad\nkey"),
                   ("openai", "model", "invalid model")]
        before = self.path.read_bytes()
        for section, field, value in invalid:
            with self.subTest(section=section, field=field):
                body = self.payload()
                body[section][field] = value
                self.assertEqual(self.client.patch("/api/settings/identification", json=body, headers=self.headers).status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_same_origin_json_and_backup_failure(self):
        body = self.payload()
        self.assertEqual(self.client.patch("/api/settings/identification", json=body, headers={"Origin": "http://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.patch("/api/settings/identification", data="{}", headers=self.headers).status_code, 415)
        before = self.path.read_bytes()
        with patch.object(web, "_atomic_settings_write", side_effect=PermissionError):
            self.assertEqual(self.client.patch("/api/settings/identification", json=body, headers=self.headers).status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)


class CameraSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.json'
        self.original = {'camera': {'model_path': '/models/model.rpk', 'cooldown': 15},
                         'email': {'password': 'private-marker'}, 'identification': {'provider': 'hermes_ssh'}}
        self.path.write_text(json.dumps(self.original))
        self.enterContext(patch.object(web, 'CONFIG_PATH', str(self.path)))
        self.client = web.app.test_client()
        self.headers = {'Origin': 'http://localhost'}

    def payload(self, mode='motion'):
        return {'camera': {'mode': mode}, 'version': self.client.get('/api/settings/camera').get_json()['version']}

    def test_default_and_read_only_mode_are_filtered(self):
        response = self.client.get('/api/settings/camera')
        self.assertEqual(response.get_json()['camera'], {'mode': 'imx500'})
        self.assertNotIn(b'private-marker', response.data)
        self.assertNotIn(b'model_path', response.data)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_page_has_explicit_camera_modes_and_separate_save_restart(self):
        page = self.client.get('/settings')
        self.assertIn(b'id="camera-mode"', page.data)
        self.assertIn(b'Camera Module 3 (motion detection)', page.data)
        self.assertIn(b'AI Camera (IMX500)', page.data)
        self.assertIn(b'id="camera-save"', page.data)
        self.assertIn(b'id="camera-apply"', page.data)
        self.assertIn(b'cameraVersion = result.version; identificationVersion = result.version', page.data)
        self.assertIn(b'identificationVersion = result.version; cameraVersion = result.version', page.data)

    def test_modes_preserve_settings_backup_and_never_restart_on_save(self):
        for mode in ('motion', 'imx500'):
            before = self.path.read_bytes()
            body = self.payload(mode)
            with patch.object(web.subprocess, 'run') as run:
                response = self.client.patch('/api/settings/camera', json=body, headers=self.headers)
                run.assert_not_called()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()['restart_required'])
            self.assertEqual(Path(str(self.path) + '.bak').read_bytes(), before)
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved['camera'], {**self.original['camera'], 'mode': mode})
            self.assertEqual(saved['email'], self.original['email'])
            self.assertEqual(saved['identification'], self.original['identification'])
            self.assertEqual(self.client.get('/api/settings/camera').get_json()['camera']['mode'], mode)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_values_and_extra_fields_do_not_write(self):
        before = self.path.read_bytes()
        for mode in ('auto', 'usb', '', None, True, 1, [], {}):
            body = self.payload(mode)
            self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers=self.headers).status_code, 400)
            self.assertEqual(self.path.read_bytes(), before)
        body = self.payload()
        body['camera']['model_path'] = '/other'
        self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers=self.headers).status_code, 400)

    def test_conflicts_origin_and_backup_failures(self):
        body = self.payload()
        self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers={'Origin': 'http://evil.invalid'}).status_code, 403)
        self.assertEqual(self.client.patch('/api/settings/camera', data='{}', headers=self.headers).status_code, 415)
        before = self.path.read_bytes()
        with patch.object(web, '_atomic_settings_write', side_effect=PermissionError):
            self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers=self.headers).status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.patch('/api/settings/camera', json=body, headers=self.headers).status_code, 409)


class MotionCaptureTests(unittest.TestCase):
    def motion_function(self, camera, alarm):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'watcher.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'get_motion_score')
        namespace = {'picam2': camera, 'signal': Mock(alarm=alarm), 'np': Mock(),
                     'prev_frame': None, 'MOTION_THRESHOLD': 5, 'MOTION_MIN_AREA': 0.01,
                     'DEBUG': False}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'watcher.py', 'exec'), namespace)
        return namespace['get_motion_score']

    def test_motion_frame_read_arms_then_clears_camera_timeout(self):
        camera, alarm = Mock(), Mock()
        camera.capture_array.return_value.ndim = 2
        camera.capture_array.return_value.astype.return_value.shape = (240, 320)
        self.assertEqual(self.motion_function(camera, alarm)(), (None, 0.0))
        camera.capture_array.assert_called_once_with('lores')
        self.assertEqual([call.args[0] for call in alarm.call_args_list], [15, 0])

    def test_motion_timeout_is_cleared_when_capture_raises(self):
        camera, alarm = Mock(), Mock()
        camera.capture_array.side_effect = RuntimeError('camera failed')
        with self.assertRaises(RuntimeError):
            self.motion_function(camera, alarm)()
        self.assertEqual([call.args[0] for call in alarm.call_args_list], [15, 0])

    def test_blocked_motion_capture_exits_for_systemd_restart(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'watcher.py').read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('_timeout_exit', 'get_motion_score')]
        script = '''import os, signal, threading
from unittest.mock import Mock
original_alarm = signal.alarm
signal.alarm = lambda seconds: original_alarm(1 if seconds else 0)
picam2 = Mock()
picam2.capture_array.side_effect = lambda *args: threading.Event().wait()
log = Mock()
log.error.side_effect = lambda message: print(message, flush=True)
prev_frame = None
'''
        script += ast.unparse(ast.Module(body=functions, type_ignores=[]))
        script += '\nsignal.signal(signal.SIGALRM, _timeout_exit)\nget_motion_score()\n'
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('exiting for restart', result.stdout)
        self.assertFalse(result.stderr)


class DeploymentTests(unittest.TestCase):
    def run_deploy(self, sudo_allowed):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            logfile = directory / 'commands.log'
            ssh = directory / 'ssh'
            ssh.write_text('#!/bin/bash\n'
                           'printf "ssh:%s\\n" "$*" >> "$TEST_DEPLOY_LOG"\n'
                           'if [[ "$*" == *"sudo -n -l"* ]]; then exit "$TEST_SUDO_EXIT"; fi\n'
                           'exit 0\n')
            rsync = directory / 'rsync'
            rsync.write_text('#!/bin/bash\nprintf "rsync\\n" >> "$TEST_DEPLOY_LOG"\n')
            ssh.chmod(0o700)
            rsync.chmod(0o700)
            environment = {**os.environ, 'PATH': str(directory) + os.pathsep + os.environ['PATH'],
                           'TEST_DEPLOY_LOG': str(logfile), 'TEST_SUDO_EXIT': '0' if sudo_allowed else '1'}
            result = subprocess.run(['/bin/bash', str(root / 'deploy.sh'), '192.0.2.82'],
                                    env=environment, capture_output=True, text=True, timeout=10)
            return result, logfile.read_text()

    def test_denied_sudo_stops_before_sync_or_service_changes(self):
        result, commands = self.run_deploy(False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('No files synced and no services restarted', result.stdout)
        self.assertNotIn('\nrsync\n', commands)
        self.assertNotIn('ssh:titpi@192.0.2.82 sudo -n install', commands)
        self.assertNotIn('ssh:titpi@192.0.2.82 sudo -n systemctl restart', commands)

    def test_allowed_sudo_is_checked_before_update_sync(self):
        result, commands = self.run_deploy(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(commands.index('sudo -n -l'), commands.index('\nrsync\n'))
        self.assertIn('sudo -n install -m 644', commands)
        self.assertIn('sudo -n systemctl restart titpi.service titpi-web.service titpi-identification.service', commands)


class GeneralSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.json'
        self.original = {'camera': {'mode': 'motion', 'custom_tuning': 42},
                         'email': {'password': 'private-smtp-marker'},
                         'openai': {'api_key': 'private-api-marker'}, 'unrelated': {'retained': True}}
        self.path.write_text(json.dumps(self.original))
        self.enterContext(patch.object(web, 'CONFIG_PATH', str(self.path)))
        self.client = web.app.test_client()
        self.headers = {'Origin': 'http://localhost'}

    def payload(self, changes):
        return {'settings': changes, 'version': self.client.get('/api/settings/general').get_json()['version']}

    def test_get_defaults_schema_and_secret_redaction(self):
        response = self.client.get('/api/settings/general')
        result = response.get_json()
        self.assertEqual(result['settings']['email']['subject_prefix'], 'TitPi')
        self.assertTrue(result['password_configured'])
        self.assertNotIn('password', result['settings']['email'])
        self.assertNotIn(b'private-smtp-marker', response.data)
        self.assertNotIn(b'private-api-marker', response.data)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_all_template_json_fields_have_page_controls(self):
        root = Path(__file__).resolve().parents[1]
        example = json.loads((root / 'config_example.json').read_text())
        template = (root / 'deploy.sh').read_text().split('<< ENDCONFIG\n', 1)[1].split('\nENDCONFIG', 1)[0]
        covered = {section: set(fields) for section, fields in web.GENERAL_SETTINGS.items()}
        covered['camera'].add('mode')
        identification = web._identification_settings(b'{}')
        for section, fields in identification.items():
            covered[section] = {key.removesuffix('_configured') for key in fields}
        covered['lcd'] = set(web._lcd_settings(b'{}'))
        for config in (example, json.loads(template)):
            for section, fields in config.items():
                self.assertLessEqual(set(fields), covered[section], section)
        self.assertIn('log_file', covered['camera'])
        self.assertIn('path', covered['database'])
        page = self.client.get('/settings').data
        self.assertIn(b'id="general-form"', page)
        self.assertIn(b'Object.entries(generalSchema[section])', page)
        self.assertIn(b'input.name = `${section}.${key}`', page)
        self.assertIn(b'generalVersion = result.version; cameraVersion = result.version; identificationVersion = result.version', page)
        self.assertIn(b'local-required" type="checkbox" checked disabled', page)

    def test_partial_save_preserves_unrelated_values_and_never_restarts(self):
        before = self.path.read_bytes()
        body = self.payload({'email': {'subject_prefix': 'TitPi - Garden', 'password': ''},
                             'camera': {'cooldown': 25, 'target_labels': ['bird', 'person']},
                             'database': {'web_host': 'titpi-garden.local', 'web_port': 8081}})
        with patch.object(web.subprocess, 'run') as run:
            response = self.client.patch('/api/settings/general', json=body, headers=self.headers)
            run.assert_not_called()
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['email'], {'subject_prefix': 'TitPi - Garden', 'password': 'private-smtp-marker'})
        self.assertEqual(saved['camera']['mode'], 'motion')
        self.assertEqual(saved['camera']['custom_tuning'], 42)
        self.assertEqual(saved['openai'], self.original['openai'])
        self.assertEqual(saved['unrelated'], self.original['unrelated'])
        self.assertEqual(Path(str(self.path) + '.bak').read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.patch('/api/settings/general', json=body, headers=self.headers).status_code, 409)

    def test_every_field_accepts_valid_value_and_password_is_write_only(self):
        settings = self.client.get('/api/settings/general').get_json()['settings']
        settings['email']['password'] = 'new-private-marker'
        response = self.client.patch('/api/settings/general', json=self.payload(settings), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'new-private-marker', response.data)
        self.assertNotIn(b'new-private-marker', self.client.get('/api/settings/general').data)
        self.assertEqual(json.loads(self.path.read_text())['email']['password'], 'new-private-marker')

    def test_invalid_values_do_not_write(self):
        before = self.path.read_bytes()
        cases = [('email', 'subject_prefix', 'bad\r\nBcc: other'), ('email', 'subject_prefix', ''),
                 ('email', 'subject_prefix', 'x' * 121), ('email', 'smtp_port', True),
                 ('email', 'smtp_host', 'host;id'), ('email', 'from', 'bad address'),
                 ('email', 'attach_video', 'true'), ('email', 'password', 'bad\npassword'),
                 ('camera', 'baseline_frames', 0), ('camera', 'cooldown', -1),
                 ('email', 'smtp_port', 10 ** 400),
                 ('camera', 'confidence_threshold', float('nan')), ('camera', 'motion_min_area', 2),
                 ('camera', 'target_labels', []), ('camera', 'target_labels', ['bird', 'bird']),
                 ('camera', 'target_labels', [{}]), ('camera', 'save_directory', 'relative'),
                 ('camera', 'video_min_duration', 121), ('camera', 'mode', 'usb'),
                 ('database', 'web_port', 65536), ('database', 'path', '/tmp/bad\x00file')]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                body = self.payload({section: {key: value}})
                self.assertEqual(self.client.patch('/api/settings/general', json=body, headers=self.headers).status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_same_origin_size_json_and_backup_failure(self):
        body = self.payload({'email': {'subject_prefix': 'Garden'}})
        self.assertEqual(self.client.patch('/api/settings/general', json=body).status_code, 403)
        self.assertEqual(self.client.patch('/api/settings/general', data='{}', headers=self.headers).status_code, 415)
        huge = self.payload({'email': {'subject_prefix': 'x' * 17000}})
        self.assertEqual(self.client.patch('/api/settings/general', json=huge, headers=self.headers).status_code, 413)
        before = self.path.read_bytes()
        with patch.object(web, '_atomic_settings_write', side_effect=PermissionError):
            self.assertEqual(self.client.patch('/api/settings/general', json=body, headers=self.headers).status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)

    def test_blank_optional_paths_restore_automatic_defaults(self):
        self.path.write_text(json.dumps({**self.original, 'database': {'path': '/old/database.db'},
                                        'camera': {'log_file': '/old/watcher.log'}}))
        body = self.payload({'database': {'path': ''}, 'camera': {'log_file': ''}})
        self.assertEqual(self.client.patch('/api/settings/general', json=body, headers=self.headers).status_code, 200)
        saved = json.loads(self.path.read_text())
        self.assertNotIn('path', saved['database'])
        self.assertNotIn('log_file', saved['camera'])


if __name__ == "__main__":
    unittest.main()