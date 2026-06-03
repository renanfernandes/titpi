# TitPi — Systemd Service Files

Systemd unit files for running TitPi as background services on the Pi. Install them with `deploy.sh` (handled automatically) or manually:

```bash
sudo cp services/*.service services/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now titpi.service titpi-web.service titpi-botd.timer
```

---

## titpi.service — Main detection loop

Runs `watcher.py` continuously. Restarts automatically on failure (15s delay).

```bash
sudo systemctl status titpi.service
sudo systemctl restart titpi.service
journalctl -u titpi -f          # live logs
```

## titpi-web.service — Dashboard

Runs `web.py` (Flask) on port 8080. Starts after `titpi.service`.

```bash
sudo systemctl status titpi-web.service
sudo systemctl restart titpi-web.service
journalctl -u titpi-web -f
```

## titpi-botd.service — Bird of the Day (one-shot)

Runs `compute_botd.py` once when triggered by the timer. Not started directly.

## titpi-botd.timer — Daily 7 PM trigger

Fires `titpi-botd.service` every day at 19:00. `Persistent=true` means it will catch up if the Pi was off at that time.

```bash
sudo systemctl status titpi-botd.timer
systemctl list-timers titpi-botd.timer   # shows next scheduled run
```
