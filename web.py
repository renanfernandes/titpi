"""
TitPi web dashboard — Flask app serving bird detection stats.
Run: python3 web.py  (or via titpi-web.service on port 8080)
"""

import os
import json
import re
import hashlib
import tempfile
import fcntl
import subprocess
from datetime import date
from urllib.parse import urlsplit
from flask import Flask, jsonify, send_from_directory, abort, render_template, request
import database

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)

SAVE_DIR = _cfg["camera"].get("save_directory", "/home/titpi/titpi/detections")
PORT = _cfg.get("database", {}).get("web_port", 8080)

database.init_db()
app = Flask(__name__)
LCD_CONFIG_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "config_lcd.json")


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
    deleted = database.delete_detection(detection_id)
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
        if database.delete_detection(det_id):
            deleted += 1
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
