"""
Compute and store the Bird of the Day for a given date.

Selection priority:
  1. A species seen for the first time ever (new species sighting) always wins.
     If multiple new species appeared today, the one with the most visits wins.
  2. If no new species, the most-visited species wins.

Usage:
    python3 compute_botd.py              # computes for today
    python3 compute_botd.py 2026-04-22  # computes for a specific date
"""

import sys
import os
import shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database


def score_species(counts, rows):
    """Return weighted score per species.

    Score = visit_count + (starred_detections * 3).
    Stars act as a quality signal: each starred detection adds +3, so
    a species needs ~3 more raw visits to overcome each star another species has.
    """
    star_bonus = {}
    for r in rows:
        name = r.get("common_name") or r.get("species")
        if not name or name.lower() in ("unknown", "none"):
            continue
        if r.get("starred"):
            star_bonus[name] = star_bonus.get(name, 0) + 3
    return {name: counts[name] + star_bonus.get(name, 0) for name in counts}


def best_photo(rows, winner):
    """Return the best photo row for the winning species.

    Starred photos take priority; confidence breaks ties among equal-starred photos.
    """
    candidates = [
        r for r in rows
        if (r.get("common_name") or r.get("species")) == winner
        and r.get("photo_path")
    ]
    candidates.sort(
        key=lambda r: (r.get("starred") or 0, r.get("gpt_confidence") or 0),
        reverse=True,
    )
    return candidates[0] if candidates else None


def compute(date_str=None):
    import datetime
    if date_str is None:
        date_str = datetime.date.today().isoformat()

    rows = database.get_today(date_str)
    if not rows:
        print(f"No detections for {date_str}, nothing to do.")
        return

    # Count visits per species (exclude Unknown/None)
    counts = {}
    for r in rows:
        name = r.get("common_name") or r.get("species")
        if not name or name.lower() in ("unknown", "none"):
            continue
        counts[name] = counts.get(name, 0) + 1

    if not counts:
        print(f"No identified species for {date_str}.")
        return

    weighted = score_species(counts, rows)

    # Find species seen for the first time ever (not in DB before today)
    known_before = database.get_known_species(before_date=date_str)
    new_species = {name for name in counts if name not in known_before}

    if new_species:
        # Pick the new species with the highest weighted score today
        winner = max(new_species, key=lambda n: weighted[n])
        first_time = True
    else:
        winner = max(weighted, key=weighted.get)
        first_time = False

    visit_count = counts[winner]

    # Best photo: starred photos take priority; break ties with gpt_confidence
    candidates = [
        r for r in rows
        if (r.get("common_name") or r.get("species")) == winner
        and r.get("photo_path")
    ]
    candidates.sort(key=lambda r: (r.get("starred") or 0, r.get("gpt_confidence") or 0), reverse=True)
    best = best_photo(rows, winner)

    photo_path = best["photo_path"] if best else None
    species = best.get("species") if best else winner

    database.set_bird_of_day(
        date_str=date_str,
        species=species,
        common_name=winner,
        photo_path=photo_path,
        visit_count=visit_count,
    )

    # Copy best photo to bird_of_the_day directory
    if photo_path:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        botd_dir = os.path.join(base_dir, "detections", "bird_of_the_day")
        os.makedirs(botd_dir, exist_ok=True)
        ext = os.path.splitext(photo_path)[1] or ".jpg"
        dest = os.path.join(botd_dir, f"{date_str}{ext}")
        src = photo_path if os.path.isabs(photo_path) else os.path.join(base_dir, photo_path)
        if os.path.isfile(src):
            shutil.copy2(src, dest)
            print(f"Copied BOTD photo → {dest}")
        else:
            print(f"Warning: BOTD photo not found at {src}")

    tag = " ⭐ FIRST SIGHTING EVER!" if first_time else ""
    print(f"Bird of the Day for {date_str}: {winner} ({visit_count} visit(s)){tag} — {os.path.basename(photo_path or '')}")


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    compute(date_arg)
