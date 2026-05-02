"""
SQLite database for bird feeder detections.
"""

import sqlite3
import os
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)

DB_PATH = _cfg.get("database", {}).get(
    "path",
    os.path.join(_cfg["camera"].get("save_directory", "/home/titpi/titpi/detections"), "detections.db")
)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TEXT    NOT NULL,
                species        TEXT,
                common_name    TEXT,
                gpt_confidence REAL,
                spike_score    REAL,
                baseline_score REAL,
                photo_path     TEXT,
                video_path     TEXT,
                visit_duration REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bird_of_day (
                date        TEXT PRIMARY KEY,
                species     TEXT,
                common_name TEXT,
                photo_path  TEXT,
                visit_count INTEGER
            )
        """)
        conn.commit()


def log_detection(timestamp, species, common_name, gpt_confidence,
                  spike_score, baseline_score, photo_path, video_path, visit_duration):
    with _connect() as conn:
        conn.execute("""
            INSERT INTO detections
                (timestamp, species, common_name, gpt_confidence,
                 spike_score, baseline_score, photo_path, video_path, visit_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, species, common_name, gpt_confidence,
              spike_score, baseline_score, photo_path, video_path, visit_duration))
        conn.commit()


def get_recent(limit=50):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detections ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_today(date_str):
    """date_str: YYYY-MM-DD"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detections WHERE timestamp LIKE ? ORDER BY timestamp DESC",
            (f"{date_str}%",)
        ).fetchall()
    return [dict(r) for r in rows]


def get_species_counts(days=30):
    with _connect() as conn:
        rows = conn.execute("""
            SELECT COALESCE(common_name, 'Unknown') as name, COUNT(*) as count
            FROM detections
            WHERE timestamp >= datetime('now', ?)
            GROUP BY name ORDER BY count DESC
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_hourly_counts(days=30):
    with _connect() as conn:
        rows = conn.execute("""
            SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, COUNT(*) as count
            FROM detections
            WHERE timestamp >= datetime('now', ?)
            GROUP BY hour ORDER BY hour
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_daily_counts(days=30):
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date(timestamp) as day, COUNT(*) as count
            FROM detections
            WHERE timestamp >= date('now', ?)
            GROUP BY day ORDER BY day
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_heatmap(days=365):
    """Return daily counts for the past N days for heatmap rendering."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date(timestamp) as day, COUNT(*) as count
            FROM detections
            WHERE timestamp >= date('now', ?)
            GROUP BY day ORDER BY day
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_by_hour(hour):
    """Return detections where the hour matches (0-23)."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM detections
            WHERE CAST(strftime('%H', timestamp) AS INTEGER) = ?
            ORDER BY timestamp DESC
        """, (hour,)).fetchall()
    return [dict(r) for r in rows]


def get_by_date(date_str):
    """Return detections for a specific date (YYYY-MM-DD)."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM detections
            WHERE date(timestamp) = ?
            ORDER BY timestamp DESC
        """, (date_str,)).fetchall()
    return [dict(r) for r in rows]


def set_bird_of_day(date_str, species, common_name, photo_path, visit_count):
    """Upsert the bird of the day record for a given date."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO bird_of_day (date, species, common_name, photo_path, visit_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                species=excluded.species,
                common_name=excluded.common_name,
                photo_path=excluded.photo_path,
                visit_count=excluded.visit_count
        """, (date_str, species, common_name, photo_path, visit_count))
        conn.commit()


def get_bird_of_day(date_str=None):
    """Return the bird_of_day row for given date (defaults to today)."""
    if date_str is None:
        import datetime
        date_str = datetime.date.today().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM bird_of_day WHERE date = ?", (date_str,)
        ).fetchone()
    return dict(row) if row else None


def get_botd_fallback(date_str):
    """Derive a best-bird for a date directly from detections (used when bird_of_day table has no entry)."""
    with _connect() as conn:
        # Most frequent species that day
        top = conn.execute(
            """SELECT common_name, species, COUNT(*) as cnt
               FROM detections
               WHERE date(timestamp) = ? AND (common_name IS NOT NULL OR species IS NOT NULL)
               GROUP BY coalesce(common_name, species)
               ORDER BY cnt DESC LIMIT 1""",
            (date_str,)
        ).fetchone()
        if not top:
            return None
        name = top["common_name"] or top["species"]
        # Latest photo for that species that day
        photo_row = conn.execute(
            """SELECT photo_path FROM detections
               WHERE date(timestamp) = ? AND (common_name = ? OR species = ?)
                 AND photo_path IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (date_str, name, name)
        ).fetchone()
    return {
        "common_name": name,
        "species": top["species"],
        "visit_count": top["cnt"],
        "photo_path": photo_row["photo_path"] if photo_row else None,
    }


def delete_detection(detection_id):
    """Delete a detection row by id. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM detections WHERE id = ?", (detection_id,))
        conn.commit()
    return cur.rowcount > 0


def update_detection_species(detection_id, common_name, species=None):
    """Update common_name (and optionally species/scientific name) for a detection."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET common_name = ?, species = COALESCE(?, species) WHERE id = ?",
            (common_name, species, detection_id)
        )
        conn.commit()


def get_known_species(before_date=None):
    """Return set of common_name/species seen on any date before before_date."""
    with _connect() as conn:
        if before_date:
            rows = conn.execute(
                "SELECT DISTINCT common_name, species FROM detections WHERE date(timestamp) < ?",
                (before_date,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT common_name, species FROM detections"
            ).fetchall()
    known = set()
    for r in rows:
        if r["common_name"]:
            known.add(r["common_name"])
        if r["species"]:
            known.add(r["species"])
    known.discard("Unknown")
    known.discard("None")
    return known
