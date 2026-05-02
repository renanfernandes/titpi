# TitPi — AI Bird Feeder Camera

An edge-AI bird surveillance system built on **Raspberry Pi Zero 2W** and the **Sony IMX500** AI camera. Detects visitors in real time using on-chip inference, identifies species with GPT-4o vision, and serves a live analytics dashboard.

## Features

- **On-chip object detection** — IMX500 runs SSD MobileNet v2 at ~10 FPS with near-zero CPU load
- **Spike-based triggering** — adaptive baseline calibration prevents false positives from lighting changes
- **Species identification** — best photo sent to GPT-4o-mini (GitHub Models API) for species ID
- **High-res capture** — 2028×1520 stills + H264 video (10–120s) on each confirmed detection
- **Web dashboard** — real-time stats, species charts, 365-day activity heatmap, photo lightbox
- **Bird of the Day** — daily ranking that prioritizes first-ever sightings
- **Email alerts** — per-detection notifications with photo and video attachments
- **Auto-recovery** — systemd services with crash restart + frozen-pipeline detection

## Hardware

| Component | Model |
|-----------|-------|
| Board | Raspberry Pi Zero 2W |
| Camera | Sony IMX500 (AI Camera) |
| OS | Raspberry Pi OS Bookworm 64-bit |

## Architecture

```
IMX500 (on-chip COCO detection)
  │
  ▼
watcher.py ── spike detection + baseline calibration
  │             captures photo + video on confirmation
  ▼
bird_id.py ── GitHub Models API (GPT-4o-mini vision)
  │             returns species, confidence, notes
  ▼
database.py ── SQLite (detections + bird_of_day tables)
  │
  ├─▶ notifier.py     email alerts with attachments
  ├─▶ compute_botd.py  daily "Bird of the Day" (7 PM)
  └─▶ web.py           Flask dashboard on port 8080
```

## Project Structure

```
watcher.py            Main detection loop (always-on)
bird_id.py            GPT vision species identification
database.py           SQLite schema and queries
web.py                Flask dashboard backend
compute_botd.py       Bird of the Day selection (daily)
notifier.py           Email notifications
backfill.py           Re-identify past detections in batch
preview.py            Live MJPEG preview stream for setup
run.sh                Auto-restart wrapper
config.json           Runtime configuration (not tracked)
config_example.json   Configuration template
templates/
  dashboard.html      Single-page dashboard (Bootstrap + Chart.js)
titpi.service         Systemd unit — watcher
titpi-web.service     Systemd unit — web dashboard
titpi-botd.service    Systemd unit — bird of the day (one-shot)
titpi-botd.timer      Systemd timer — triggers botd at 19:00
```

## Installation

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y imx500-all python3-picamera2 ffmpeg python3-flask python3-requests python3-pil python3-numpy
```

### 2. Clone and configure

```bash
git clone https://github.com/your-user/titpi.git /home/titpi/titpi
cd /home/titpi/titpi
cp config_example.json config.json
# Edit config.json with your GitHub token, email credentials, and paths
```

### 3. Set up systemd services

```bash
sudo cp titpi.service titpi-web.service titpi-botd.service titpi-botd.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now titpi.service titpi-web.service titpi-botd.timer
```

The dashboard will be available at `http://<pi-ip>:8080`.

## Configuration

Copy `config_example.json` to `config.json` and fill in your values:

| Section | Key | Description |
|---------|-----|-------------|
| `camera` | `confidence_threshold` | Minimum IMX500 detection score (default: 0.25) |
| `camera` | `spike_threshold` | Margin above baseline to trigger (default: 0.20) |
| `camera` | `confirmation_frames` | Consecutive spike frames required (default: 2) |
| `camera` | `cooldown` | Seconds between detections (default: 10) |
| `camera` | `target_labels` | COCO classes to detect (default: bird, person, dog) |
| `github` | `token` | GitHub PAT with Models permission |
| `github` | `model` | Vision model name (default: gpt-4o-mini) |
| `email` | `password` | SMTP app password for notifications |

## Development

### Sync files to the Pi

```bash
rsync -az --progress ./ titpi@titpi.local:/home/titpi/titpi
```

### Restart after changes

```bash
ssh titpi@titpi.local "sudo systemctl restart titpi.service"
```

### Live camera preview (for aiming/setup)

```bash
ssh titpi@titpi.local "python3 /home/titpi/titpi/preview.py"
# Open http://<pi-ip>:8080 in browser
```

### Backfill missing identifications

```bash
ssh titpi@titpi.local "python3 /home/titpi/titpi/backfill.py"
```

## License

## Syncing the files
rsync -az --progress \   
        ./ titpi@10.0.1.60:/home/titpi/titpi