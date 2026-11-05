# TitPi — Utility Scripts

Helper scripts for setup, maintenance, and troubleshooting. None of these are required for normal operation; the systemd services (`watcher.py`, `web.py`, `compute_botd.py`) run everything automatically.

All scripts are designed to be run from this directory:

```bash
python3 tools/<script>.py
# or from the Pi:
ssh titpi@<pi-ip> "python3 /home/titpi/titpi/tools/<script>.py"
```

---

## dashboard_preview.py - Read-only dashboard preview

Preview the local dashboard on macOS using real data from an existing TitPi:

```bash
python3 tools/dashboard_preview.py --upstream http://10.0.0.80:8080 --port 8082
```

Open http://127.0.0.1:8082. The server is localhost-only and proxies GET requests for data, photos, and video. It does not read configuration credentials and rejects real edits, stars, and deletion requests. Use another port if occupied; stop with Ctrl+C.

---

## preview.py — Live camera preview

Streams a live MJPEG feed over HTTP so you can aim and focus the camera before starting the main service.

```bash
python3 tools/preview.py              # plain video stream
python3 tools/preview.py --detections # overlay AI bounding boxes + labels
```

Open **http://\<pi-ip\>:8081** in a browser. Press `Ctrl+C` to stop.

> Useful when setting up a new camera or adjusting the feeder position.

---

## backfill.py — Backfill the database from existing files

Scans the `detections/` directory for photos that are not yet in the database, runs identification on each one, and inserts the results. Records that already exist (matched by timestamp) are skipped.

```bash
python3 tools/backfill.py
```

> Useful after a fresh install when you have existing detection photos but an empty database.

---

## reidentify.py — Re-identify unknown detections

Re-runs the local classifier and GPT fallback on detections for a given date that still have a `NULL` or `Unknown` species. Useful when the AI service was down or returned a poor result.

```bash
python3 tools/reidentify.py              # re-identifies today's unknowns
python3 tools/reidentify.py 2026-05-30   # re-identifies a specific date
```
