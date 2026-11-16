"""
TitPi web dashboard — Flask app serving bird detection stats.
Run: python3 web.py  (or via titpi-web.service on port 8080)
"""

import os
import json
import re
import math
import hashlib
import tempfile
import fcntl
import subprocess
from datetime import date
from urllib.parse import urlsplit
from flask import Flask, jsonify, send_from_directory, abort, render_template, request
import database
from bird_id import DEFAULT_PROMPT
from hermes_ssh import DEFAULT_SETTINGS, validate_settings

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)

SAVE_DIR = _cfg["camera"].get("save_directory", "/home/titpi/titpi/detections")
PORT = _cfg.get("database", {}).get("web_port", 8080)

database.init_db()
app = Flask(__name__)
LCD_CONFIG_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "config_lcd.json")

GENERAL_SETTINGS = {
    "camera": {
        "model_path": {"label": "IMX500 model path", "type": "path", "default": "/usr/share/imx500-models/imx500_network_efficientdet_lite0_pp.rpk"},
        "confidence_threshold": {"label": "Detection confidence (0-1)", "type": "number", "default": 0.35, "min": 0, "max": 1},
        "confirmation_frames": {"label": "Confirmation frames", "type": "integer", "default": 3, "min": 1, "max": 10000},
        "photo_delay": {"label": "Photo delay (seconds)", "type": "number", "default": 0.5, "min": 0, "max": 86400},
        "video_min_duration": {"label": "Minimum video duration (seconds)", "type": "number", "default": 10, "min": 0, "max": 86400},
        "video_max_duration": {"label": "Maximum video duration (seconds)", "type": "number", "default": 120, "min": 1, "max": 86400},
        "video_idle_timeout": {"label": "Video idle timeout (seconds)", "type": "number", "default": 5, "min": 0.1, "max": 86400},
        "photo_interval": {"label": "Photo interval (seconds)", "type": "number", "default": 3, "min": 0.1, "max": 86400},
        "cooldown": {"label": "Visit cooldown (seconds)", "type": "number", "default": 15, "min": 0, "max": 86400},
        "save_directory": {"label": "Capture directory", "type": "path", "default": "/home/titpi/titpi/detections"},
        "log_file": {"label": "Log file path", "type": "path", "default": "", "optional": True},
        "target_labels": {"label": "Detection targets", "type": "list", "default": ["bird"]},
        "spike_threshold": {"label": "Spike threshold", "type": "number", "default": 0.2, "min": 0, "max": 1000},
        "baseline_frames": {"label": "Baseline frames", "type": "integer", "default": 60, "min": 1, "max": 10000},
        "motion_threshold": {"label": "Motion threshold (pixel difference)", "type": "number", "default": 5, "min": 0, "max": 255},
        "motion_min_area": {"label": "Minimum motion area (0-1)", "type": "number", "default": 0.01, "min": 0, "max": 1},
    },
    "email": {
        "subject_prefix": {"label": "Email subject prefix", "type": "text", "default": "TitPi", "max_length": 120},
        "smtp_host": {"label": "SMTP host", "type": "host", "default": "smtp.gmail.com"},
        "smtp_port": {"label": "SMTP port", "type": "integer", "default": 587, "min": 1, "max": 65535},
        "from": {"label": "Sender address", "type": "email", "default": ""},
        "password": {"label": "SMTP password", "type": "password", "default": ""},
        "to": {"label": "Recipient address", "type": "email", "default": ""},
        "attach_video": {"label": "Attach video to email", "type": "boolean", "default": True},
    },
    "database": {
        "path": {"label": "Database file path", "type": "path", "default": "", "optional": True},
        "web_host": {"label": "Dashboard host in email links", "type": "host", "default": "10.0.1.60"},
        "web_port": {"label": "Dashboard port", "type": "integer", "default": 8080, "min": 1, "max": 65535},
    },
}


def _settings_access(write=False):
    if write and (request.headers.get("Origin") != request.host_url.rstrip("/")
                  or request.headers.get("Sec-Fetch-Site") == "cross-site"):
        return jsonify(error="Same-origin requests are required."), 403
    if write and not request.is_json:
        return jsonify(error="A JSON request is required."), 415


def _lcd_settings(content):
    lcd = json.loads(content).get("lcd", {})
    return {
        "url": lcd.get("url", ""),
        "spotify_account": lcd.get("spotify_account", "default"),
        "display_seconds": lcd.get("display_seconds", 30),
    }


def _atomic_settings_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".titpi-settings-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identification_settings(content):
    config = json.loads(content)
    remote = config.get("identification", {})
    provider = remote.get("provider", "none")
    return {
        "identification": {"provider": provider if provider in ("openai", "claude", "hermes_ssh") else "none",
                           "timeout": remote.get("timeout", 30), "max_attempts": remote.get("max_attempts", 2),
                           "prompt": remote.get("prompt") or DEFAULT_PROMPT},
        "local_classifier": {"enabled": True, "min_confidence": config.get("local_classifier", {}).get("min_confidence", 0.3)},
        "hermes_ssh": {**DEFAULT_SETTINGS, **{key: value for key, value in config.get("hermes_ssh", {}).items() if key in DEFAULT_SETTINGS}},
        "openai": {"model": config.get("openai", {}).get("model") or "gpt-4.1-mini",
                   "api_key_configured": bool(os.environ.get("OPENAI_API_KEY") or config.get("openai", {}).get("api_key"))},
        "claude": {"model": config.get("claude", {}).get("model") or "claude-haiku-4-5",
                   "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY") or config.get("claude", {}).get("api_key"))},
    }


def _validate_general_value(field, value):
    kind = field["type"]
    if kind == "boolean":
        valid = type(value) is bool
    elif kind in ("number", "integer"):
        valid = (type(value) in ((int,) if kind == "integer" else (int, float))
                 and field["min"] <= value <= field["max"] and math.isfinite(value))
    elif kind == "list":
        valid = (isinstance(value, list) and 1 <= len(value) <= 100
                 and all(isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9 -]{0,63}", item) for item in value)
                 and len(set(value)) == len(value))
    else:
        valid = (isinstance(value, str) and len(value) <= field.get("max_length", 1024)
                 and not any(ord(character) < 32 or ord(character) == 127 for character in value))
        if valid and kind == "path":
            valid = value.startswith("/") or (not value and field.get("optional", False))
        elif valid and kind == "host":
            valid = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", value))
        elif valid and kind == "email":
            valid = not value or bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", value))
        elif valid and kind == "text":
            valid = bool(value.strip())
    if not valid:
        raise ValueError("Invalid setting value")


@app.route("/api/settings/general", methods=["GET", "PATCH"])
def api_settings_general():
    denied = _settings_access(write=request.method == "PATCH")
    if denied is not None:
        return denied
    try:
        with open(CONFIG_PATH + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(CONFIG_PATH, "rb") as config_file:
                content = config_file.read()
            config = json.loads(content)
            version = hashlib.sha256(content).hexdigest()
            if request.method == "GET":
                values = {section: {key: config.get(section, {}).get(key, field["default"])
                                    for key, field in fields.items() if field["type"] != "password"}
                          for section, fields in GENERAL_SETTINGS.items()}
                return jsonify(settings=values, schema=GENERAL_SETTINGS, version=version,
                               password_configured=bool(config.get("email", {}).get("password")),
                               can_restart=os.environ.get("TITPI_ALLOW_RESTART") == "1")
            if request.content_length and request.content_length > 16384:
                return jsonify(error="Settings request is too large."), 413
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"settings", "version"}:
                return jsonify(error="Expected settings and their version."), 400
            if data["version"] != version:
                return jsonify(error="Settings changed elsewhere. Reload before saving."), 409
            changes = data["settings"]
            if not isinstance(changes, dict) or not changes or set(changes) - GENERAL_SETTINGS.keys():
                return jsonify(error="Select supported configuration fields."), 400
            for section, values in changes.items():
                if not isinstance(values, dict) or not values or set(values) - GENERAL_SETTINGS[section].keys():
                    return jsonify(error="Select supported configuration fields."), 400
                for key, value in values.items():
                    field = GENERAL_SETTINGS[section][key]
                    _validate_general_value(field, value)
                    if field["type"] == "password" and not value:
                        continue
                    if field.get("optional") and value == "":
                        config.setdefault(section, {}).pop(key, None)
                    else:
                        config.setdefault(section, {})[key] = value
            camera = config.get("camera", {})
            if camera.get("video_min_duration", 10) > camera.get("video_max_duration", 120):
                return jsonify(error="Minimum video duration cannot exceed maximum duration."), 400
            updated = (json.dumps(config, indent=4) + "\n").encode("utf-8")
            _atomic_settings_write(CONFIG_PATH + ".bak", content)
            _atomic_settings_write(CONFIG_PATH, updated)
            app.logger.info("General configuration saved; service restarts required.")
            return jsonify(ok=True, version=hashlib.sha256(updated).hexdigest(), restart_required=True)
    except (ValueError, TypeError, AttributeError):
        return jsonify(error="Invalid configuration value; check field types and limits."), 400
    except OSError:
        app.logger.error("Could not read or save general configuration.")
        return jsonify(error="Configuration unavailable; check file permissions and service logs."), 503


@app.route("/api/settings/camera", methods=["GET", "PATCH"])
def api_settings_camera():
    denied = _settings_access(write=request.method == "PATCH")
    if denied is not None:
        return denied
    try:
        with open(CONFIG_PATH + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(CONFIG_PATH, "rb") as config_file:
                content = config_file.read()
            config = json.loads(content)
            version = hashlib.sha256(content).hexdigest()
            if request.method == "GET":
                return jsonify(camera={"mode": config.get("camera", {}).get("mode", "imx500")},
                               version=version, can_restart=os.environ.get("TITPI_ALLOW_RESTART") == "1")
            if request.content_length and request.content_length > 4096:
                return jsonify(error="Settings request is too large."), 413
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"camera", "version"}:
                return jsonify(error="Expected camera mode and its version."), 400
            if data["version"] != version:
                return jsonify(error="Settings changed elsewhere. Reload before saving."), 409
            camera = data["camera"]
            if (not isinstance(camera, dict) or set(camera) != {"mode"}
                    or camera["mode"] not in ("imx500", "motion")):
                return jsonify(error="Select imx500 or motion (Camera Module 3)."), 400
            config.setdefault("camera", {})["mode"] = camera["mode"]
            updated = (json.dumps(config, indent=4) + "\n").encode("utf-8")
            _atomic_settings_write(CONFIG_PATH + ".bak", content)
            _atomic_settings_write(CONFIG_PATH, updated)
            app.logger.info("Camera mode saved; watcher restart required.")
            return jsonify(ok=True, version=hashlib.sha256(updated).hexdigest(), restart_required=True)
    except (ValueError, TypeError, AttributeError):
        return jsonify(error="Invalid camera configuration."), 400
    except OSError:
        app.logger.error("Could not read or save camera configuration.")
        return jsonify(error="Configuration unavailable; check file permissions and service logs."), 503


@app.route("/api/settings/identification", methods=["GET", "PATCH"])
def api_settings_identification():
    denied = _settings_access(write=request.method == "PATCH")
    if denied is not None:
        return denied
    try:
        with open(CONFIG_PATH + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(CONFIG_PATH, "rb") as config_file:
                content = config_file.read()
            version = hashlib.sha256(content).hexdigest()
            if request.method == "GET":
                return jsonify(**_identification_settings(content), version=version,
                               can_restart=os.environ.get("TITPI_ALLOW_RESTART") == "1")
            if request.content_length and request.content_length > 16384:
                return jsonify(error="Settings request is too large."), 413
            data = request.get_json(silent=True)
            sections = {"identification", "local_classifier", "hermes_ssh", "openai", "claude"}
            if not isinstance(data, dict) or set(data) != sections | {"version"}:
                return jsonify(error="Expected identification settings and their version."), 400
            if data["version"] != version:
                return jsonify(error="Settings changed elsewhere. Reload before saving."), 409
            remote, local = data["identification"], data["local_classifier"]
            if not isinstance(remote, dict) or set(remote) != {"provider", "timeout", "max_attempts", "prompt"}:
                return jsonify(error="Invalid remote settings."), 400
            if remote["provider"] not in ("none", "openai", "claude", "hermes_ssh"):
                return jsonify(error="Select none, openai, claude or hermes_ssh."), 400
            validate_settings(data["hermes_ssh"])
            if not isinstance(remote["prompt"], str) or not remote["prompt"].strip() or len(remote["prompt"]) > 4000:
                return jsonify(error="Enter an identification prompt of 1 to 4000 characters."), 400
            if not isinstance(local, dict) or set(local) != {"min_confidence"}:
                return jsonify(error="The local model is mandatory; only its confidence threshold may change."), 400
            confidence = local["min_confidence"]
            if type(confidence) not in (int, float) or not 0.01 <= confidence <= 1:
                return jsonify(error="Local confidence must be between 0.01 and 1."), 400
            if type(remote["timeout"]) is not int or not 1 <= remote["timeout"] <= 60:
                return jsonify(error="Timeout must be a whole number from 1 to 60 seconds."), 400
            if type(remote["max_attempts"]) is not int or not 1 <= remote["max_attempts"] <= 3:
                return jsonify(error="Attempts must be a whole number from 1 to 3."), 400
            for provider in ("openai", "claude"):
                settings = data[provider]
                if not isinstance(settings, dict) or set(settings) - {"model", "api_key"} or "model" not in settings:
                    return jsonify(error="Invalid API provider settings."), 400
                if not isinstance(settings["model"], str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", settings["model"]):
                    return jsonify(error="Enter a valid model identifier."), 400
            for settings, field in ((data["openai"], "api_key"), (data["claude"], "api_key")):
                if field in settings and (not isinstance(settings[field], str) or len(settings[field]) > 1024
                                          or any(ord(character) < 32 for character in settings[field])):
                    return jsonify(error="Invalid credential value."), 400
            config = json.loads(content)
            for section in sections:
                values = {key: value for key, value in data[section].items()
                          if key != "api_key" or value}
                config.setdefault(section, {}).update(values)
            config["local_classifier"]["enabled"] = True
            updated = (json.dumps(config, indent=4) + "\n").encode("utf-8")
            _atomic_settings_write(CONFIG_PATH + ".bak", content)
            _atomic_settings_write(CONFIG_PATH, updated)
            app.logger.info("Identification configuration saved; watcher restart required.")
            return jsonify(ok=True, version=hashlib.sha256(updated).hexdigest(), restart_required=True)
    except (ValueError, TypeError, AttributeError, KeyError):
        return jsonify(error="Invalid identification configuration."), 400
    except OSError:
        app.logger.error("Could not read or save identification configuration.")
        return jsonify(error="Configuration unavailable; check file permissions and service logs."), 503


@app.after_request
def settings_headers(response):
    if request.path == "/settings" or request.path.startswith("/api/settings"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/api/settings/lcd", methods=["GET", "PATCH"])
def api_settings_lcd():
    denied = _settings_access(write=request.method == "PATCH")
    if denied is not None:
        return denied
    try:
        with open(LCD_CONFIG_PATH + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(LCD_CONFIG_PATH, "rb") as config_file:
                content = config_file.read()
            version = hashlib.sha256(content).hexdigest()
            if request.method == "GET":
                return jsonify(lcd=_lcd_settings(content), version=version,
                               can_restart=os.environ.get("TITPI_ALLOW_RESTART") == "1")
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"lcd", "version"}:
                return jsonify(error="Expected matrix settings and their version."), 400
            if data["version"] != version:
                return jsonify(error="Settings changed elsewhere. Reload settings before saving again."), 409
            lcd = data["lcd"]
            if not isinstance(lcd, dict) or set(lcd) != {"url", "spotify_account", "display_seconds"}:
                return jsonify(error="Only the three matrix settings may be changed."), 400
            url = lcd["url"]
            account = lcd["spotify_account"]
            seconds = lcd["display_seconds"]
            if not isinstance(url, str) or len(url) > 256 or any(character.isspace() for character in url):
                return jsonify(error="Enter a valid controller origin, or leave it empty to disable uploads."), 400
            parsed = urlsplit(url)
            if url and (parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.username or parsed.password or parsed.path not in ("", "/")
                        or parsed.query or parsed.fragment or parsed.port == 0):
                return jsonify(error="Controller URL must be an HTTP(S) origin without credentials."), 400
            if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account):
                return jsonify(error="Enter all, default, or a profile name (letters, digits, _ and -)."), 400
            if type(seconds) is not int or not 1 <= seconds <= 3600:
                return jsonify(error="Display duration must be a whole number from 1 to 3600 seconds."), 400
            config = json.loads(content)
            config.setdefault("lcd", {}).update(lcd)
            updated = (json.dumps(config, indent=4) + "\n").encode("utf-8")
            _atomic_settings_write(LCD_CONFIG_PATH + ".bak", content)
            _atomic_settings_write(LCD_CONFIG_PATH, updated)
            app.logger.info("Matrix configuration saved; watcher restart required.")
            return jsonify(ok=True, version=hashlib.sha256(updated).hexdigest(), restart_required=True)
    except (ValueError, TypeError, AttributeError):
        return jsonify(error="Invalid matrix configuration or URL."), 400
    except OSError:
        app.logger.error("Could not read or save matrix configuration.")
        return jsonify(error="Configuration unavailable; check file permissions and service logs."), 503


@app.route("/api/settings/restart", methods=["POST"])
def api_settings_restart():
    denied = _settings_access(write=True)
    if denied is not None:
        return denied
    if os.environ.get("TITPI_ALLOW_RESTART") != "1":
        return jsonify(error="Browser restart is disabled. Restart titpi.service over SSH."), 403
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "titpi.service"],
            capture_output=True, timeout=20, check=False,
        )
        if result.returncode:
            return jsonify(error="Restart failed. Check service logs and restart permissions."), 503
    except (OSError, subprocess.TimeoutExpired):
        return jsonify(error="Restart could not be confirmed. Check titpi.service over SSH."), 503
    app.logger.info("Watcher restart requested through settings.")
    return jsonify(ok=True)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    today = date.today().isoformat()
    today_rows = database.get_today(today)
    total = database.count_all()
    species_counts = database.get_species_counts(30)

    # Stored bird of the day (set at 7pm by compute_botd.py)
    botd = database.get_bird_of_day(today)

    # Fallback: live count if botd not yet computed today
    if not botd:
        counts = {}
        for r in today_rows:
            name = r.get("common_name") or r.get("species") or "Unknown"
            if name.lower() not in ("unknown", "none"):
                counts[name] = counts.get(name, 0) + 1
        if counts:
            import compute_botd as _botd
            weighted = _botd.score_species(counts, today_rows)
            winner = max(weighted, key=weighted.get)
            best = _botd.best_photo(today_rows, winner)
            botd = {
                "common_name": winner,
                "photo_path": best["photo_path"] if best else None,
                "visit_count": counts[winner],
            }

    botd_photo = None
    is_new_species = False
    if botd and botd.get("photo_path"):
        botd_photo = "/photo/" + os.path.basename(botd["photo_path"])
    if botd and botd.get("common_name"):
        today_species = {botd["common_name"]}
        known_before = database.get_known_species(before_date=today)
        is_new_species = botd["common_name"] not in known_before

    return jsonify({
        "today": len(today_rows),
        "total": total,
        "bird_of_day": botd.get("common_name") if botd else None,
        "bird_of_day_photo": botd_photo,
        "bird_of_day_visits": botd.get("visit_count") if botd else None,
        "bird_of_day_new": is_new_species,
        "species_count": len(species_counts),
    })


@app.route("/api/heatmap")
def api_heatmap():
    return jsonify(database.get_heatmap(365))


@app.route("/api/by_hour/<int:hour>")
def api_by_hour(hour):
    if not 0 <= hour <= 23:
        abort(400)
    return jsonify(database.get_by_hour(hour))


@app.route("/api/by_date/<date_str>")
def api_by_date(date_str):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        abort(400)
    return jsonify(database.get_by_date(date_str))


@app.route("/api/botd/<date_str>")
def api_botd(date_str):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        abort(400)
    botd = database.get_bird_of_day(date_str)
    if botd and botd.get('photo_path'):
        botd['photo_url'] = '/photo/' + os.path.basename(botd['photo_path'])
    if not botd:
        # Fallback: derive from detections for that day
        botd = database.get_botd_fallback(date_str)
        if botd and botd.get('photo_path'):
            botd['photo_url'] = '/photo/' + os.path.basename(botd['photo_path'])
    return jsonify(botd or {})


@app.route("/api/recent")
def api_recent():
    return jsonify(database.get_recent(50))


@app.route("/api/species")
def api_species():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        abort(400)
    if days < 0:
        abort(400)
    return jsonify(database.get_species_counts(days or None))


@app.route("/api/by_species/<species_name>")
def api_by_species(species_name):
    if re.search(r'[<>"\']', species_name):
        abort(400)
    return jsonify(database.get_by_species(species_name))


@app.route("/api/hourly")
def api_hourly():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        abort(400)
    if days < 0:
        abort(400)
    return jsonify(database.get_hourly_counts(days or None))


@app.route("/api/daily")
def api_daily():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        abort(400)
    if days < 0:
        abort(400)
    return jsonify(database.get_daily_counts(days or None))


@app.route("/api/detection/<int:detection_id>", methods=["DELETE"])
def api_delete_detection(detection_id):
    try:
        deleted = database.delete_detection(detection_id)
    except OSError as error:
        app.logger.error('Visit %s media cleanup failed (%s); row retained.', detection_id, type(error).__name__)
        return jsonify({"ok": False, "error": "Media cleanup failed; visit retained. Retry deletion."}), 503
    if not deleted:
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/detection/<int:detection_id>", methods=["PATCH"])
def api_update_detection(detection_id):
    data = request.get_json(silent=True) or {}
    common_name = data.get("common_name", "").strip()
    species = data.get("species", "").strip() or None
    if not common_name:
        abort(400)
    # Basic validation: no HTML/script injection
    if re.search(r'[<>"\']', common_name + (species or "")):
        abort(400)
    database.update_detection_species(detection_id, common_name, species)
    return jsonify({"ok": True})


@app.route("/api/detections/bulk", methods=["PATCH"])
def api_bulk_update():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    common_name = data.get("common_name", "").strip()
    species = data.get("species", "").strip() or None
    if not common_name or not ids:
        abort(400)
    if re.search(r'[<>"\']', common_name + (species or "")):
        abort(400)
    # Validate ids are all integers
    if not all(isinstance(i, int) for i in ids) or len(ids) > 500:
        abort(400)
    for det_id in ids:
        database.update_detection_species(det_id, common_name, species)
    return jsonify({"ok": True, "updated": len(ids)})


@app.route("/api/detections/bulk", methods=["DELETE"])
def api_bulk_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids:
        abort(400)
    if not all(isinstance(i, int) for i in ids) or len(ids) > 500:
        abort(400)
    deleted = 0
    for det_id in ids:
        try:
            if database.delete_detection(det_id):
                deleted += 1
        except OSError as error:
            app.logger.error('Visit %s media cleanup failed (%s); row retained.', det_id, type(error).__name__)
            return jsonify({"ok": False, "deleted": deleted,
                            "error": "Media cleanup failed; remaining visits retained. Retry deletion."}), 503
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/detection/<int:detection_id>/star", methods=["POST"])
def api_toggle_star(detection_id):
    starred = database.toggle_star(detection_id)
    if starred is None:
        abort(404)
    return jsonify({"ok": True, "starred": starred})


@app.route("/api/starred")
def api_starred():
    return jsonify(database.get_starred())


@app.route("/photo/<filename>")
def serve_photo(filename):
    # Prevent path traversal
    if "/" in filename or ".." in filename:
        abort(400)
    return send_from_directory(SAVE_DIR, filename)


@app.route("/video/<filename>")
def serve_video(filename):
    if "/" in filename or ".." in filename:
        abort(400)
    return send_from_directory(SAVE_DIR, filename)


if __name__ == "__main__":
    database.init_db()
    app.run(host="0.0.0.0", port=PORT, debug=False)
