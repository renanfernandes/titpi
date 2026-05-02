"""
Re-run GPT identification on detections with NULL/unknown common_name for a given date.
Usage: python3 reidentify.py [YYYY-MM-DD]   (defaults to today)
"""
import sys
import sqlite3
import datetime
from bird_id import identify_image

DB_PATH = "/home/titpi/titpi/detections/detections.db"
date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    """SELECT id, photo_path, species FROM detections
       WHERE date(timestamp) = ?
         AND (common_name IS NULL OR common_name = 'Unknown' OR lower(common_name) LIKE '%unknown%')
       ORDER BY timestamp""",
    (date_str,)
).fetchall()

print(f"Re-identifying {len(rows)} detections for {date_str} ...")

updated = 0
for row in rows:
    photo = row["photo_path"]
    label = row["species"] or "bird"
    print(f"  [{row['id']}] {photo} ...", end=" ", flush=True)
    try:
        result = identify_image(photo, label)
    except Exception as e:
        print(f"ERROR: {e}")
        continue

    if result and result.get("common_name") and result["common_name"].lower() not in ("unknown", "none"):
        conn.execute(
            "UPDATE detections SET common_name=?, species=?, gpt_confidence=? WHERE id=?",
            (result["common_name"], result.get("name"), result.get("score") or result.get("confidence"), row["id"])
        )
        conn.commit()
        print(f"→ {result['common_name']} ({result.get('score') or result.get('confidence', '?')})")
        updated += 1
    else:
        print("→ still unknown, skipping")

print(f"\nDone. Updated {updated}/{len(rows)} detections.")
