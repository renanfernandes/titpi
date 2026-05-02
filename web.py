"""
TitPi web dashboard — Flask app serving bird detection stats.
Run: python3 web.py  (or via titpi-web.service on port 8080)
"""

import os
import json
import re
from datetime import date
from flask import Flask, jsonify, send_from_directory, abort, render_template, request
import database

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)

SAVE_DIR = _cfg["camera"].get("save_directory", "/home/titpi/titpi/detections")
PORT = _cfg.get("database", {}).get("web_port", 8080)

database.init_db()
app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    today = date.today().isoformat()
    today_rows = database.get_today(today)
    recent = database.get_recent(1000)
    species_counts = database.get_species_counts(30)

    # Stored bird of the day (set at 7pm by compute_botd.py)
    botd = database.get_bird_of_day(today)

    # Fallback: live count if botd not yet computed today
    if not botd:
        botd_counts = {}
        for r in today_rows:
            name = r.get("common_name") or r.get("species") or "Unknown"
            if name.lower() not in ("unknown", "none"):
                botd_counts[name] = botd_counts.get(name, 0) + 1
        if botd_counts:
            winner = max(botd_counts, key=botd_counts.get)
            # Best photo for live fallback
            candidates = [r for r in today_rows if (r.get("common_name") or r.get("species")) == winner and r.get("photo_path")]
            candidates.sort(key=lambda r: r.get("gpt_confidence") or 0, reverse=True)
            botd = {
                "common_name": winner,
                "photo_path": candidates[0]["photo_path"] if candidates else None,
                "visit_count": botd_counts[winner],
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
        "total": len(recent),
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
    return jsonify(database.get_species_counts(30))


@app.route("/api/hourly")
def api_hourly():
    return jsonify(database.get_hourly_counts(30))


@app.route("/api/daily")
def api_daily():
    return jsonify(database.get_daily_counts(30))


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
