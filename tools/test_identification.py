"""Isolated checks: python3 -m unittest tools.test_identification -v."""

import ast
import io
import base64
import json
import os
import tempfile
import time
import sys
import uuid
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import bird_id
import notifier
import hermes_ssh
from identification_queue import IdentificationQueue


ROOT = Path(__file__).resolve().parents[1]


class IdentificationTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse((ROOT / "watcher.py").read_text())

    def identify_best(self, namespace):
        namespace.setdefault("REMOTE_QUEUED", False)
        function = next(node for node in self.tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "identify_best")
        exec(compile(ast.Module(body=[function], type_ignores=[]), "watcher.py", "exec"), namespace)
        return namespace["identify_best"]

    def test_disabled_remote_is_never_called(self):
        with patch.object(bird_id, "_try_local", return_value=None), \
                patch.object(bird_id, "REMOTE_ENABLED", False), \
                patch.object(bird_id, "_try_remote") as post:
            self.assertIsNone(bird_id.identify_image("missing.jpg", "person"))
            post.assert_not_called()

    def test_identification_does_not_email_before_fallback(self):
        for provider in ('none', 'hermes_ssh', 'openai', 'claude'):
            with self.subTest(provider=provider):
                events = []
                namespace = {'os': os, 'log': Mock(), 'REMOTE_ENABLED': provider in ('openai', 'claude'),
                             'REMOTE_QUEUED': provider == 'hermes_ssh',
                             'identify_image': Mock(side_effect=lambda *args, **kwargs: events.append('local')),
                             '_rate_limited_identify': Mock(side_effect=lambda *args: events.append('remote'))}
                result = self.identify_best(namespace)('missing.jpg', ['extra.jpg'], 'bird')
                self.assertEqual(events[:2], ['local', 'local'])
                self.assertEqual(events[2:], ['remote'] if namespace['REMOTE_ENABLED'] else [])
                if provider == 'hermes_ssh':
                    self.assertEqual(result['status'], 'queued')

    def test_confident_local_result_does_not_duplicate_normal_email(self):
        species = {'name': 'Test species', 'common_name': 'Test bird', 'score': 0.8, 'source': 'local'}
        namespace = {'os': os, 'log': Mock(), 'REMOTE_ENABLED': True, 'REMOTE_QUEUED': False,
                     'identify_image': Mock(return_value=species), '_rate_limited_identify': Mock()}
        self.assertEqual(self.identify_best(namespace)('missing.jpg', [], 'bird'), species)
        namespace['_rate_limited_identify'].assert_not_called()

    def test_local_unknown_email_does_not_invent_zero_confidence(self):
        from email import message_from_string
        species = {'common_name': None, 'name': None, 'score': None,
                   'source': 'local', 'diagnostic_status': 'unknown'}
        with patch.object(notifier, 'EMAIL_FROM', 'sender@example.invalid'), \
                patch.object(notifier, 'EMAIL_PASSWORD', 'test-placeholder'), \
                patch.object(notifier, 'EMAIL_TO', 'recipient@example.invalid'), \
                patch.object(notifier.smtplib, 'SMTP') as smtp:
            self.assertTrue(notifier.send_detection_email('bird', 0.44, species=species))
            message = message_from_string(smtp.return_value.__enter__.return_value.sendmail.call_args.args[2])
            self.assertEqual(message['Subject'], 'TitPi: Local diagnostic - Unknown species')
            html = next(part for part in message.walk() if part.get_content_type() == 'text/html')
            body = html.get_payload(decode=True).decode(html.get_content_charset() or 'utf-8')
            self.assertIn('Local model', body)
            self.assertIn('BIRD', body)
            self.assertIn('0.44', body)
            self.assertNotIn('Identification confidence:', body)

    def test_watcher_queues_hermes_without_sending_email(self):
        events = []
        queue = Mock()
        queue.enqueue.side_effect = lambda payload: events.append('queue') or 'job-id'
        namespace = {'os': os, 'log': Mock(), 'REMOTE_ENABLED': False, 'REMOTE_QUEUED': True,
                     'identify_image': Mock(return_value=None), '_rate_limited_identify': Mock(),
                     'send_diagnostic_email': Mock(side_effect=lambda *args: events.append('email') or True),
                     'send_detection_email': Mock(), 'IdentificationQueue': Mock(return_value=queue),
                     'database': Mock(), 'time': time, 'confirmed': 'bird', 'conf_score': 0.44,
                     'photo_path': 'missing.jpg', 'extra_photos': [], 'video_path': 'video.mp4',
                     'baseline_median': 0.22, 'visit_duration': 12}
        self.identify_best(namespace)
        loop = next(node.body[0] for node in self.tree.body if isinstance(node, ast.Try)
                    and node.body and isinstance(node.body[0], ast.While))
        index = next(index for index, node in enumerate(loop.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'species' for target in node.targets))
        exec(compile(ast.Module(body=loop.body[index:], type_ignores=[]), 'watcher.py', 'exec'), namespace)
        self.assertEqual(events, ['queue'])
        namespace['send_diagnostic_email'].assert_not_called()
        namespace['send_detection_email'].assert_not_called()
        namespace['database'].log_detection.assert_not_called()

    def test_watcher_sends_one_email_after_selected_provider_finishes(self):
        loop = next(node.body[0] for node in self.tree.body if isinstance(node, ast.Try)
                    and node.body and isinstance(node.body[0], ast.While))
        index = next(index for index, node in enumerate(loop.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'species' for target in node.targets))
        code = compile(ast.Module(body=loop.body[index:], type_ignores=[]), 'watcher.py', 'exec')
        for provider in ('local', 'none', 'openai', 'claude'):
            for status in ('identified', 'empty', 'inconclusive', 'error'):
                with self.subTest(provider=provider, status=status):
                    events = []
                    result = {'status': status, 'name': 'Test species', 'common_name': 'Test bird',
                              'score': 0.8, 'source': provider}
                    namespace = {'os': os, 'log': Mock(), 'REMOTE_ENABLED': provider in ('openai', 'claude'),
                                 'REMOTE_QUEUED': False, 'database': Mock(), 'time': time,
                                 'confirmed': 'bird', 'conf_score': 0.44, 'photo_path': 'missing.jpg',
                                 'extra_photos': [], 'video_path': 'video.mp4', 'baseline_median': 0.22,
                                 'visit_duration': 12}
                    def local_result(*args, **kwargs):
                        events.append('local')
                        return result if provider == 'local' and status == 'identified' else None
                    def remote_result(*args):
                        events.append('remote')
                        return result
                    namespace.update(identify_image=Mock(side_effect=local_result),
                                     _rate_limited_identify=Mock(side_effect=remote_result),
                                     send_detection_email=Mock(side_effect=lambda *args: events.append('email') or True),
                                     send_diagnostic_email=Mock(side_effect=lambda *args: events.append('email') or True))
                    self.identify_best(namespace)
                    exec(code, namespace)
                    expected = ['local', 'remote', 'email'] if namespace['REMOTE_ENABLED'] else ['local', 'email']
                    self.assertEqual(events, expected)
                    self.assertEqual(namespace['send_detection_email'].call_count +
                                     namespace['send_diagnostic_email'].call_count, 1)
                    if namespace['REMOTE_ENABLED'] and status != 'identified':
                        self.assertEqual(namespace['send_diagnostic_email'].call_args.args[4]['source'], provider)
                        namespace['database'].log_detection.assert_not_called()

    def test_final_diagnostic_mail_failure_is_not_retried(self):
        loop = next(node.body[0] for node in self.tree.body if isinstance(node, ast.Try)
                    and node.body and isinstance(node.body[0], ast.While))
        index = next(index for index, node in enumerate(loop.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'species' for target in node.targets))
        code = compile(ast.Module(body=loop.body[index + 1:], type_ignores=[]), 'watcher.py', 'exec')
        for failure in (False, RuntimeError('private-test-token')):
            options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            namespace = {'species': {'status': 'error', 'source': 'openai'}, 'log': Mock(), 'time': time,
                         'confirmed': 'bird', 'conf_score': 0.44, 'photo_path': 'missing.jpg',
                         'video_path': None, 'send_diagnostic_email': Mock(**options), 'database': Mock(),
                         'send_detection_email': Mock()}
            exec(code, namespace)
            namespace['send_diagnostic_email'].assert_called_once()
            namespace['send_detection_email'].assert_not_called()
            namespace['database'].log_detection.assert_not_called()

    def test_local_success_skips_cloud(self):
        result = {"common_name": "Test bird", "name": "Test", "score": 0.8, "source": "local"}
        with patch.object(bird_id, "_try_local", return_value=result), \
                patch.object(bird_id, "_try_remote") as post:
            self.assertEqual(bird_id.identify_image("missing.jpg", local_only=True), result)
            post.assert_not_called()

    def test_unresolved_visit_preserves_all_photos_without_cloud_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / f"image_{index}.jpg" for index in range(39)]
            for photo in photos:
                Image.new("RGB", (16, 16), "black").save(photo)
            namespace = {"os": os, "log": Mock(),
                         "REMOTE_ENABLED": False,
                         "identify_image": Mock(return_value=None), "_rate_limited_identify": Mock()}
            self.assertIsNone(self.identify_best(namespace)(str(photos[0]), list(map(str, photos[1:])), "person"))
            self.assertEqual(namespace["identify_image"].call_count, 39)
            self.assertTrue(all(call.kwargs == {"local_only": True}
                                for call in namespace["identify_image"].call_args_list))
            namespace["_rate_limited_identify"].assert_not_called()
            self.assertTrue(all(photo.exists() for photo in photos))

    def test_ssh_queue_checks_all_photos_without_network_or_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            photos = [Path(directory) / f'image_{index}.jpg' for index in range(3)]
            for photo in photos:
                Image.new('RGB', (16, 16)).save(photo)
            namespace = {'os': os, 'log': Mock(), 'REMOTE_ENABLED': False, 'REMOTE_QUEUED': True,
                         'identify_image': Mock(return_value=None), '_rate_limited_identify': Mock()}
            result = self.identify_best(namespace)(str(photos[0]), list(map(str, photos[1:])), 'bird')
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(namespace['identify_image'].call_count, 3)
            namespace['_rate_limited_identify'].assert_not_called()
            self.assertTrue(all(photo.exists() for photo in photos))

    def test_final_outcomes_send_once_but_only_identified_visitors_reach_database(self):
        loop = next(node.body[0] for node in self.tree.body if isinstance(node, ast.Try)
                    and node.body and isinstance(node.body[0], ast.While))
        index = next(index for index, node in enumerate(loop.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "species" for target in node.targets))
        namespace = {"species": None, "log": Mock(), "send_detection_email": Mock(return_value=True),
                 "send_diagnostic_email": Mock(return_value=True),
                     "database": Mock(), "time": time, "confirmed": "person", "conf_score": 0.44,
                     "photo_path": "retained.jpg", "video_path": "retained.mp4",
                     "baseline_median": 0.22, "visit_duration": 122}
        code = compile(ast.Module(body=loop.body[index + 1:], type_ignores=[]), "watcher.py", "exec")
        for outcome in (None, {"status": "empty"}, {"status": "inconclusive"}, {"status": "error"}):
            namespace["species"] = outcome
            exec(code, namespace)
            namespace["send_detection_email"].assert_not_called()
            namespace["send_diagnostic_email"].assert_called_once()
            namespace["send_diagnostic_email"].reset_mock()
            namespace["database"].log_detection.assert_not_called()
        visitor = {"status": "identified", "common_name": "Test bird", "name": "Test species", "score": 0.8}
        namespace["species"] = visitor
        exec(code, namespace)
        namespace["send_detection_email"].assert_called_once_with("person", 0.44, "retained.jpg", "retained.mp4", visitor)
        namespace["send_diagnostic_email"].assert_not_called()
        self.assertEqual(namespace["database"].log_detection.call_args.kwargs["common_name"], "Test bird")

    def test_cloud_unresolved_outcomes_preserve_all_photos(self):
        for status in ("empty", "inconclusive", "error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                photos = [Path(directory) / f"image_{index}.jpg" for index in range(3)]
                for photo in photos:
                    Image.new("RGB", (16, 16), "black").save(photo)
                outcome = {"status": status}
                namespace = {"os": os, "log": Mock(), "REMOTE_ENABLED": True,
                             "identify_image": Mock(return_value=None),
                             "_rate_limited_identify": Mock(return_value=outcome)}
                self.assertEqual(self.identify_best(namespace)(str(photos[0]), list(map(str, photos[1:])), "bird"), outcome)
                namespace["_rate_limited_identify"].assert_called_once()
                self.assertEqual(namespace["identify_image"].call_count, 3)
                self.assertTrue(all(photo.exists() for photo in photos))

    def test_unidentified_email_has_camera_label_and_timeout(self):
        with patch.object(notifier, "EMAIL_FROM", "sender@example.invalid"), \
                patch.object(notifier, "EMAIL_PASSWORD", "test-placeholder"), \
                patch.object(notifier, "EMAIL_TO", "recipient@example.invalid"), \
                patch.object(notifier.smtplib, "SMTP") as smtp:
            self.assertTrue(notifier.send_detection_email("person", 0.44))
            smtp.assert_called_once_with(notifier.SMTP_HOST, notifier.SMTP_PORT, timeout=30)
            body = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
            self.assertIn("Unidentified visitor", body)
            self.assertIn("PERSON", body)
            self.assertIn("0.44", body)

    def test_outcome_opt_in_preserves_legacy_none_contract(self):
        for status in ("empty", "inconclusive", "error"):
            with self.subTest(status=status), patch.object(bird_id, "REMOTE_ENABLED", True), \
                    patch.object(bird_id, "_try_local", return_value=None), \
                    patch.object(bird_id, "_try_remote", return_value={"status": status}):
                self.assertIsNone(bird_id.identify_image("image.jpg"))
                self.assertEqual(bird_id.identify_image("image.jpg", return_outcome=True)["status"], status)

    def test_subject_prefix_covers_normal_and_diagnostic_email(self):
        for species, expected in ((None, "Unidentified visitor stopped by!"),
                                  ({"common_name": "Test bird", "name": "Test species", "score": 0.8}, "Test bird stopped by!"),
                                  ({"common_name": None, "name": None, "score": None,
                                    "source": "hermes_ssh", "diagnostic_status": "empty"}, "Hermes diagnostic - No bird")):
            with self.subTest(expected=expected), patch.object(notifier, "EMAIL_SUBJECT_PREFIX", "TitPi - Garden"), \
                    patch.object(notifier, "EMAIL_FROM", "sender@example.invalid"), \
                    patch.object(notifier, "EMAIL_PASSWORD", "test-placeholder"), \
                    patch.object(notifier, "EMAIL_TO", "recipient@example.invalid"), \
                    patch.object(notifier.smtplib, "SMTP") as smtp:
                self.assertTrue(notifier.send_detection_email("bird", 0.8, species=species))
                body = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
                self.assertIn(f"Subject: TitPi - Garden: {expected}", body)

    def test_lcd_notifier_also_bounds_smtp(self):
        tree = ast.parse((ROOT / "notifier_lcd.py").read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "send_detection_email")
        namespace = {**vars(notifier), "EMAIL_FROM": "sender@example.invalid",
                     "EMAIL_PASSWORD": "test-placeholder", "EMAIL_TO": "recipient@example.invalid",
                     "EMAIL_SUBJECT_PREFIX": "TitPi - Balcony",
                     "display_on_lcd": Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "notifier_lcd.py", "exec"), namespace)
        with patch.object(notifier.smtplib, "SMTP") as smtp:
            self.assertTrue(namespace["send_detection_email"]("person", 0.44))
            smtp.assert_called_once_with(notifier.SMTP_HOST, notifier.SMTP_PORT, timeout=30)
            body = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
            self.assertIn("Subject: TitPi - Balcony: Unidentified visitor stopped by!", body)
        namespace["display_on_lcd"].assert_called_once_with(None)


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}))

    def test_default_json_prompt_matches_configuration_templates(self):
        example = json.loads((ROOT / "config_example.json").read_text())
        template = (ROOT / "deploy.sh").read_text().split("<< ENDCONFIG\n", 1)[1].split("\nENDCONFIG", 1)[0]
        self.assertEqual(example["identification"]["prompt"], bird_id.DEFAULT_PROMPT)
        self.assertEqual(json.loads(template)["identification"]["prompt"], bird_id.DEFAULT_PROMPT)
        self.assertTrue(bird_id.DEFAULT_PROMPT.startswith("Examine the attached image carefully."))
        self.assertIn("identify which bird and species", bird_id.DEFAULT_PROMPT)
        self.assertIn("Return ONLY a JSON object", bird_id.DEFAULT_PROMPT)
        self.assertIn("use inconclusive rather than empty", bird_id.DEFAULT_PROMPT)
        self.assertEqual(example['hermes_ssh'], hermes_ssh.DEFAULT_SETTINGS)
        self.assertEqual(json.loads(template)['hermes_ssh'], hermes_ssh.DEFAULT_SETTINGS)

    def test_confident_local_result_prevents_every_remote_provider(self):
        local = {"common_name": "Test bird", "name": "Test species", "score": 0.9, "source": "local"}
        for provider in ("none", "openai", "claude"):
            with self.subTest(provider=provider), patch.object(bird_id, "REMOTE_PROVIDER", provider), \
                    patch.object(bird_id, "REMOTE_ENABLED", provider != "none"), \
                    patch.object(bird_id, "_try_local", return_value=local), patch.object(bird_id, "_try_remote") as remote:
                self.assertEqual(bird_id.identify_image("visitor.jpg"), local)
                remote.assert_not_called()

    def test_local_only_scan_and_none_never_send_requests(self):
        with patch.object(bird_id, "_try_local", return_value=None), patch.object(bird_id, "_try_remote") as remote:
            with patch.object(bird_id, "REMOTE_ENABLED", True):
                self.assertIsNone(bird_id.identify_image("visitor.jpg", local_only=True))
            with patch.object(bird_id, "REMOTE_ENABLED", False):
                self.assertIsNone(bird_id.identify_image("visitor.jpg"))
            remote.assert_not_called()

    def test_local_classifier_cannot_be_disabled_and_threshold_is_used(self):
        classifier = Mock()
        classifier.classify.return_value = [{"name": "Test species", "common_name": "Test bird", "score": 0.69}]
        with patch.dict(bird_id._local_config, {"enabled": False}), patch.object(bird_id, "LOCAL_MIN_CONFIDENCE", 0.7), \
                patch.object(bird_id, "_get_classifier", return_value=classifier), \
                patch.object(bird_id, "REMOTE_ENABLED", True), patch.object(bird_id, "_try_remote", return_value={"status": "inconclusive"}) as remote:
            self.assertEqual(bird_id.identify_image("visitor.jpg", return_outcome=True)["status"], "inconclusive")
            classifier.classify.assert_called_once()
            remote.assert_called_once()
            remote.reset_mock()
            classifier.classify.return_value[0]["score"] = 0.71
            self.assertEqual(bird_id.identify_image("visitor.jpg")["source"], "local")
            remote.assert_not_called()

    def test_only_selected_provider_runs_after_local_failure(self):
        prompt = "Which bird species is visible, or is there no bird?"
        for provider in ("openai", "claude"):
            with self.subTest(provider=provider), patch.object(bird_id, "REMOTE_PROVIDER", provider), \
                    patch.object(bird_id, "_remote_config", {"prompt": prompt}), \
                    patch.object(bird_id, "REMOTE_ENABLED", True), patch.object(bird_id, "_try_local", return_value=None), \
                    patch.object(bird_id, "_try_vision_api", return_value={"status": "inconclusive"}) as api:
                outcome = bird_id.identify_image("visitor.jpg", return_outcome=True)
                self.assertEqual(outcome["status"], "inconclusive")
                api.assert_called_once_with("visitor.jpg", provider)

    def test_direct_api_request_formats_and_validated_results(self):
        identification = {"status": "identified", "common_name": "Test bird", "name": "Test species", "confidence": 0.8, "category": "bird"}
        for provider in ("openai", "claude"):
            response = Mock(status_code=200)
            text = json.dumps(identification)
            response.json.return_value = ({"choices": [{"message": {"content": text}}]} if provider == "openai"
                                          else {"content": [{"type": "text", "text": text}]})
            with self.subTest(provider=provider), patch.object(bird_id, "_config", {provider: {"api_key": "test-api-key"}}), \
                    patch.object(bird_id, "_remote_config", {"prompt": "Identify this bird species, or report no bird."}), \
                    patch.object(bird_id, "encode_image", return_value="test-base64"), \
                    patch.object(bird_id.requests, "post", return_value=response) as post:
                result = bird_id._try_vision_api("visitor.jpg", provider)
                self.assertEqual((result["source"], result["common_name"], result["score"]), (provider, "Test bird", 0.8))
                payload = json.loads(post.call_args.kwargs["data"])
                content = payload["messages"][0]["content"]
                text_block = next(block for block in content if block["type"] == "text")
                self.assertTrue(text_block["text"].startswith("Identify this bird species, or report no bird."))
                if provider == "openai":
                    self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,test-base64")
                    self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-api-key")
                else:
                    self.assertEqual(content[0]["source"], {"type": "base64", "media_type": "image/jpeg", "data": "test-base64"})
                    self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "test-api-key")
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
                response.close.assert_called_once()

    def test_bird_only_result_states(self):
        for status in ("empty", "inconclusive"):
            body = {"status": status, "common_name": None, "name": None, "confidence": 0, "category": "unknown"}
            self.assertEqual(bird_id._parse_remote_result(json.dumps(body), "openai")["status"], status)
            body["category"] = "bird"
            with self.assertRaises(ValueError):
                bird_id._parse_remote_result(json.dumps(body), "openai")
        body = {"status": "identified", "common_name": "Unidentified bird", "name": None, "confidence": 0.4, "category": "bird"}
        self.assertEqual(bird_id._parse_remote_result(json.dumps(body), "claude")["name"], "Unknown")
        body["category"] = "person"
        with self.assertRaises(ValueError):
            bird_id._parse_remote_result(json.dumps(body), "claude")

    def test_direct_api_retries_close_responses_and_redact_secrets(self):
        for provider in ("openai", "claude"):
            result = json.dumps({"status": "empty", "common_name": None, "name": None, "category": "unknown", "confidence": 0})
            accepted = Mock(status_code=200)
            accepted.json.return_value = ({"choices": [{"message": {"content": result}}]} if provider == "openai"
                                          else {"content": [{"type": "text", "text": result}]})
            transient = Mock(status_code=503)
            permanent = Mock(status_code=401)
            with self.subTest(provider=provider), patch.object(bird_id, "_config", {provider: {"api_key": "private-test-api-key"}}), \
                    patch.object(bird_id, "_remote_config", {"max_attempts": 2}), \
                    patch.object(bird_id, "encode_image", return_value="test-base64"), \
                    patch.object(bird_id.requests, "post", side_effect=[transient, accepted]) as post, \
                    patch.object(bird_id.time, "sleep") as backoff, patch("builtins.print") as log:
                self.assertEqual(bird_id._try_vision_api("visitor.jpg", provider)["status"], "empty")
                self.assertEqual(post.call_args_list[0], post.call_args_list[1])
                transient.close.assert_called_once()
                accepted.close.assert_called_once()
                backoff.assert_called_once_with(1)
                post.reset_mock()
                post.side_effect = [permanent]
                self.assertEqual(bird_id._try_vision_api("visitor.jpg", provider)["status"], "error")
                post.assert_called_once()
                permanent.close.assert_called_once()
                post.reset_mock()
                post.side_effect = bird_id.requests.exceptions.Timeout("private-test-api-key")
                self.assertEqual(bird_id._try_vision_api("visitor.jpg", provider)["status"], "error")
                self.assertEqual(post.call_count, 2)
                self.assertNotIn("private-test-api-key", str(log.call_args_list))

    def test_api_keys_required_and_malformed_identification_stays_error(self):
        with patch.object(bird_id, "_config", {}), patch.object(bird_id.requests, "post") as post:
            self.assertEqual(bird_id._try_vision_api("visitor.jpg", "claude")["status"], "error")
            post.assert_not_called()
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"status":"empty","common_name":"Bird","confidence":0}'}}]}
        with patch.object(bird_id, "_config", {"openai": {"api_key": "test-api-key"}}), \
                patch.object(bird_id, "encode_image", return_value="test-base64"), \
                patch.object(bird_id.requests, "post", return_value=response):
            self.assertEqual(bird_id._try_vision_api("visitor.jpg", "openai")["status"], "error")


class VisionImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.photo = Path(self.directory.name) / "visitor.jpg"
        Image.new("RGB", (2400, 1600), "white").save(self.photo)
    def test_image_is_resized_and_encoded_as_jpeg(self):
        encoded = bird_id.encode_image(self.photo)
        self.assertFalse(encoded.startswith("data:"))
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
            self.assertEqual(image.size, (1280, 853))
            self.assertEqual(image.format, "JPEG")

    def test_oversized_or_missing_images_are_rejected(self):
        with patch.object(bird_id.os.path, "getsize", return_value=10 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                bird_id.encode_image(self.photo)
        with self.assertRaises(FileNotFoundError):
            bird_id.encode_image("missing.jpg")


class HermesSSHTests(unittest.TestCase):
    def test_remote_runner_validates_events_times_out_and_removes_only_job_folder(self):
        for mode in ('result', 'tool', 'timeout'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                folder = root / ('titpi-hermes-' + uuid.uuid4().hex)
                folder.mkdir()
                (folder / 'visitor.jpg').write_bytes(b'test-jpeg')
                executable = root / 'fake-hermes'
                body = ('import threading; threading.Event().wait(5)' if mode == 'timeout' else
                        'import json; print(json.dumps(' + repr({'type': 'tool_call'} if mode == 'tool' else
                        {'type': 'result', 'exit_code': 0, 'text': '{"status":"empty"}'}) + '))')
                executable.write_text('#!' + sys.executable + '\n' + body + '\n')
                executable.chmod(0o700)
                completed = hermes_ssh.subprocess.run([sys.executable, '-c', hermes_ssh._RUN_REMOTE],
                    input=json.dumps({'folder': str(folder), 'hermes_path': str(executable),
                                      'budget': 0.1 if mode == 'timeout' else 2, 'prompt': 'test'}).encode(),
                    capture_output=True, timeout=5)
                self.assertEqual(completed.returncode, 0 if mode == 'result' else (124 if mode == 'timeout' else 1))
                self.assertFalse(folder.exists())
                self.assertTrue(executable.exists())
                if mode == 'result':
                    self.assertEqual(json.loads(completed.stdout)['text'], '{"status":"empty"}')

    def test_safe_settings_reject_shell_hosts_and_unsafe_paths(self):
        for field, value in [('host', '-oProxyCommand=bad'), ('host', 'host;id'),
                             ('user', 'user;id'), ('key_path', 'relative'),
                             ('remote_temp_dir', '/tmp/../etc'), ('hermes_path', '/tmp/bad\npath'),
                             ('timeout', True), ('connect_timeout', 31), ('max_attempts', 0),
                             ('min_confidence', float('nan'))]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    hermes_ssh.validate_settings({**hermes_ssh.DEFAULT_SETTINGS, field: value})

    def test_image_transfer_result_and_cleanup_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('test-key-not-a-real-credential')
            key.chmod(0o600)
            photo = Path(directory) / 'bird.jpg'
            Image.new('RGB', (1600, 1000)).save(photo)
            content = {'status': 'empty', 'common_name': None, 'name': None, 'category': 'unknown', 'confidence': 0}
            response = Mock(stdout=json.dumps({'text': json.dumps(content), 'model': 'test-model'}).encode())
            with patch.object(hermes_ssh, '_ssh', side_effect=[Mock(), response, Mock()]) as ssh, \
                    patch.object(hermes_ssh.subprocess, 'run') as scp:
                result = hermes_ssh.identify_remote(photo, {'key_path': str(key)}, 'Describe this image')
            self.assertEqual(result['status'], 'empty')
            self.assertEqual(result['source'], 'hermes_ssh')
            self.assertEqual(scp.call_args.args[0][0], 'scp')
            self.assertIn('StrictHostKeyChecking=yes', scp.call_args.args[0])
            self.assertLessEqual(scp.call_args.kwargs['timeout'], 60)
            self.assertIn('bot_room', ssh.call_args_list[1].args[1])
            envelope = json.loads(ssh.call_args_list[1].kwargs['input'])
            self.assertLess(envelope['budget'], 60)
            self.assertTrue(envelope['prompt'].startswith('Describe this image'))
            self.assertIn('shutil.rmtree', ssh.call_args_list[-1].args[1])

    def test_transfer_timeout_cleans_up_without_parsing_or_secret_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('test-key')
            key.chmod(0o600)
            photo = Path(directory) / 'bird.jpg'
            Image.new('RGB', (16, 16)).save(photo)
            with patch.object(hermes_ssh, '_ssh') as ssh, \
                    patch.object(hermes_ssh.subprocess, 'run', side_effect=hermes_ssh.subprocess.TimeoutExpired('scp', 1)):
                with self.assertRaises(hermes_ssh.subprocess.TimeoutExpired):
                    hermes_ssh.identify_remote(photo, {'key_path': str(key)}, 'Identify bird')
                self.assertEqual(ssh.call_count, 2)


class WorkerTests(unittest.TestCase):
    def test_diagnostic_results_send_one_email_without_database_or_matrix_publication(self):
        import identification_worker
        from email import message_from_string
        outcomes = [
            ({'status': 'identified', 'name': 'Unknown', 'common_name': 'Unidentified bird', 'score': 0.8}, 'Unknown species'),
            ({'status': 'identified', 'name': 'Test species', 'common_name': 'Test bird', 'score': 0.2}, 'Low confidence'),
            ({'status': 'empty', 'name': None, 'common_name': None, 'score': 0}, 'No bird'),
            ({'status': 'inconclusive', 'name': None, 'common_name': None, 'score': 0}, 'Inconclusive'),
            (TimeoutError('private-test-token'), 'Identification error'),
        ]
        for outcome, label in outcomes:
            with self.subTest(outcome=label), tempfile.TemporaryDirectory() as directory:
                queue = IdentificationQueue(str(Path(directory) / 'queue.db'))
                photo = Path(directory) / 'photo.jpg'
                Image.new('RGB', (16, 16)).save(photo)
                queue.enqueue({'timestamp': '2026-11-10 12:00:00', 'photo_path': str(photo),
                               'label': 'bird', 'spike_score': 0.6, 'baseline_score': 0.2, 'visit_duration': 12})
                job = queue.claim()
                options = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': {**outcome, 'source': 'hermes_ssh'}}
                with patch.object(identification_worker, 'identify_remote', **options):
                    identification_worker.process_job(queue, job, {})
                with queue.connect() as connection:
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='detections'").fetchone())
                lcd = Mock()
                with patch.dict(sys.modules, {'notifier_lcd': lcd}), \
                        patch.object(notifier, 'EMAIL_FROM', 'sender@example.invalid'), \
                        patch.object(notifier, 'EMAIL_PASSWORD', 'test-placeholder'), \
                        patch.object(notifier, 'EMAIL_TO', 'recipient@example.invalid'), \
                        patch.object(notifier.smtplib, 'SMTP') as smtp:
                    self.assertTrue(identification_worker.deliver_pending(queue))
                    self.assertFalse(identification_worker.deliver_pending(queue))
                    sendmail = smtp.return_value.__enter__.return_value.sendmail
                    sendmail.assert_called_once()
                    message = message_from_string(sendmail.call_args.args[2])
                    self.assertEqual(message['Subject'], 'TitPi: Hermes diagnostic - ' + label)
                    html = next(part for part in message.walk() if part.get_content_type() == 'text/html')
                    body = html.get_payload(decode=True).decode(html.get_content_charset() or 'utf-8')
                    self.assertIn(label, body)
                    self.assertIn('No visitor record created', body)
                    self.assertNotIn('private-test-token', body)
                    self.assertTrue(any(part.get_content_type() == 'image/jpeg' for part in message.walk()))
                    lcd.send_detection_email.assert_not_called()

    def test_only_confident_species_publish_and_notification_is_not_repeated(self):
        import database
        import identification_worker
        with tempfile.TemporaryDirectory() as directory, patch.object(database, 'DB_PATH', str(Path(directory) / 'queue.db')):
            database.init_db()
            queue = IdentificationQueue(database.DB_PATH)
            for index, (name, score, publish) in enumerate([('Unknown', 0.9, False), ('Test species', 0.69, False), ('Test species', 0.9, True)]):
                payload = {'timestamp': f'2026-11-08 12:00:0{index}', 'photo_path': f'image_{index}.jpg',
                           'spike_score': 0.6, 'baseline_score': 0.2, 'visit_duration': 12, 'label': 'bird'}
                queue.enqueue(payload)
                job = queue.claim()
                result = {'status': 'identified', 'name': name, 'common_name': 'Test bird', 'score': score}
                with patch.object(identification_worker, 'identify_remote', return_value=result):
                    identification_worker.process_job(queue, job, {'hermes_ssh': {'notify_all': False}})
                self.assertEqual(database.count_all(), 1 if publish else 0)
            with patch.object(identification_worker, 'send_notification', return_value=True) as mail:
                self.assertTrue(identification_worker.deliver_pending(queue))
                self.assertFalse(identification_worker.deliver_pending(queue))
                mail.assert_called_once()

    def test_failed_transport_preserves_job_without_publication(self):
        import identification_worker
        with tempfile.TemporaryDirectory() as directory:
            queue = IdentificationQueue(str(Path(directory) / 'queue.db'))
            queue.enqueue({'timestamp': '2026-11-08 12:00:00', 'photo_path': 'image.jpg'})
            job = queue.claim()
            with patch.object(identification_worker, 'identify_remote', side_effect=TimeoutError):
                identification_worker.process_job(queue, job, {'hermes_ssh': {'notify_all': False}})
            self.assertIsNone(queue.claim_notification())
            with queue.connect() as connection:
                row = connection.execute('SELECT state, error FROM identification_jobs').fetchone()
                self.assertEqual(tuple(row), ('failed', 'TimeoutError'))


class QueueTests(unittest.TestCase):
    def test_diagnostic_mail_does_not_create_detection_and_is_claimed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = IdentificationQueue(str(Path(directory) / 'queue.db'))
            queue.enqueue({'timestamp': '2026-11-10 12:00:00', 'photo_path': 'unknown.jpg'})
            job = queue.claim()
            self.assertTrue(queue.complete(job['id'], {'status': 'inconclusive'}, notify=True))
            self.assertFalse(queue.complete(job['id'], {'status': 'inconclusive'}, notify=True))
            self.assertEqual(queue.claim_notification()['species']['status'], 'inconclusive')
            self.assertIsNone(queue.claim_notification())

    def test_persistent_fifo_deduplication_and_interrupted_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'queue.db')
            queue = IdentificationQueue(path)
            first = {'timestamp': '2026-11-08 12:00:00', 'photo_path': 'first.jpg'}
            second = {'timestamp': '2026-11-08 12:01:00', 'photo_path': 'second.jpg'}
            first_id = queue.enqueue(first)
            self.assertEqual(queue.enqueue(first), first_id)
            queue.enqueue(second)
            self.assertEqual(queue.claim()['photo_path'], 'first.jpg')
            restarted = IdentificationQueue(path)
            self.assertEqual(restarted.recover(), 1)
            self.assertEqual(restarted.claim()['photo_path'], 'second.jpg')
            self.assertIsNone(restarted.claim())
            with restarted.connect() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM identification_jobs').fetchone()[0], 2)
                self.assertEqual(connection.execute('SELECT error FROM identification_jobs WHERE id=?', (first_id,)).fetchone()[0], 'worker_interrupted')

    def test_result_and_notification_are_committed_once(self):
        import database
        with tempfile.TemporaryDirectory() as directory, patch.object(database, 'DB_PATH', str(Path(directory) / 'queue.db')):
            database.init_db()
            queue = IdentificationQueue(database.DB_PATH)
            payload = {'timestamp': '2026-11-08 12:00:00', 'photo_path': 'first.jpg',
                       'spike_score': 0.6, 'baseline_score': 0.2, 'visit_duration': 12}
            job_id = queue.enqueue(payload)
            queue.claim()
            species = {'name': 'Test species', 'common_name': 'Test bird', 'score': 0.9}
            self.assertTrue(queue.complete(job_id, species, publish=True))
            self.assertFalse(queue.complete(job_id, species, publish=True))
            self.assertEqual(database.count_all(), 1)
            self.assertEqual(queue.claim_notification()['id'], job_id)
            self.assertIsNone(IdentificationQueue(database.DB_PATH).claim_notification())
            queue.finish_notification(job_id, True)


if __name__ == "__main__":
    unittest.main()