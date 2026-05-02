"""
Backfill the detections database from existing photo/video files.
Run once on the Pi: python3 backfill.py

Discovers all canonical detection files (no trailing _N suffix),
optionally runs GPT vision on each photo, and inserts into the DB.
Records that already exist (same timestamp) are skipped.
"""

import os
import re
import sys
import sqlite3
import time

# Ensure we can import project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database
from bird_id import identify_image

database.init_db()

DETECTIONS_DIR = database.DB_PATH.replace("detections.db", "")
# Regex: optional label prefix + timestamp YYYYMMDD-HHMMSS (no trailing _N)
PATTERN = re.compile(r'^(?:.+_)?(\d{8}-\d{6})\.jpg$')


def parse_timestamp(ts_str):
    """Convert '20260422-135937' → '2026-04-22 13:59:37'"""
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.strptime(ts_str, "%Y%m%d-%H%M%S")
    )


def already_exists(timestamp):
    with sqlite3.connect(database.DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM detections WHERE timestamp = ?", (timestamp,)
        ).fetchone()
    return row is not None


def estimate_duration(video_path):
    """Estimate duration from file size as a rough fallback (1 MB ≈ 8s at 1Mbps)."""
    try:
        size = os.path.getsize(video_path)
        return round(size / 125000)  # bytes / (1_000_000 bits/s / 8)
    except OSError:
        return None


def main():
    files = sorted(os.listdir(DETECTIONS_DIR))
    photos = [f for f in files if PATTERN.match(f)]

    print(f"Found {len(photos)} canonical detection photos in {DETECTIONS_DIR}")
    inserted = skipped = 0

    for photo_file in photos:
        m = PATTERN.match(photo_file)
        ts_str = m.group(1)
        timestamp = parse_timestamp(ts_str)

        if already_exists(timestamp):
            print(f"  SKIP  {photo_file} (already in DB)")
            skipped += 1
            continue

        photo_path = os.path.join(DETECTIONS_DIR, photo_file)
        # Find matching video (same timestamp, any label prefix)
        video_file = next(
            (f for f in files if ts_str in f and f.endswith(".mp4") and "_" + ts_str + "." in f or f == photo_file.replace(".jpg", ".mp4")),
            None
        )
        video_path = os.path.join(DETECTIONS_DIR, video_file) if video_file and os.path.exists(os.path.join(DETECTIONS_DIR, video_file)) else None
        duration = estimate_duration(video_path) if video_path else None

        print(f"  ID    {photo_file} ... ", end="", flush=True)
        result = identify_image(photo_path, "bird")
        if result:
            species = result.get("name")
            common_name = result.get("common_name")
            gpt_conf = result.get("score")
            print(f"{common_name or species} ({gpt_conf:.0%})")
        else:
            species = common_name = gpt_conf = None
            print("(no ID)")

        database.log_detection(
            timestamp=timestamp,
            species=species,
            common_name=common_name,
            gpt_confidence=gpt_conf,
            spike_score=None,
            baseline_score=None,
            photo_path=photo_path,
            video_path=video_path,
            visit_duration=duration,
        )
        inserted += 1

    print(f"\nDone. Inserted: {inserted}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
