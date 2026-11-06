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

### Settings page: first-time setup

The dashboard's gear opens `/settings`. The initial editor covers only LED matrix URL, Spotify account (`all`, `default`, or a named profile), and display duration (1–3600 seconds). An empty URL disables uploads. It never reads email/GitHub credentials into the browser.

Settings load directly without an admin password. Anyone who can reach the dashboard can read and modify these matrix settings. Keep the dashboard restricted to the trusted internal network; do not expose the Flask server directly to the Internet. Saves/restarts still require same-origin JSON requests, validation, and conflict detection. Email and GitHub credentials are never returned by this editor.

Deploy the code from macOS:

```bash
./deploy.sh 10.0.0.80
```

Open `http://10.0.0.80:8080/settings`, or use the dashboard gear. No password or environment file is required for editing. Older `TITPI_SETTINGS_PASSWORD_HASH` values are ignored and may be removed from the optional web environment file.

### Saving and applying

Change a field and Save. The server validates the three fields, locks writes, preserves unrelated JSON keys, atomically writes the configuration, and keeps the previous contents as `config_lcd.json.bak`. Conflicting edits return an error rather than overwriting a newer version; use the reload icon to fetch current values. Reload asks for confirmation before discarding unsaved changes. A save does not contact the matrix or restart recording.

Existing matrix configuration and its backups are excluded from deployment sync. A local `config_lcd.json` is copied only if the Pi has none. Edit through Settings (or on the Pi) after that; changing the macOS copy no longer replaces an existing Pi configuration.

By default, apply changes over SSH:

```bash
sudo systemctl restart titpi.service
systemctl is-active titpi.service titpi-web.service
journalctl -u titpi.service -n 30 --no-pager
```

To enable the separate **Apply and restart** button, set `TITPI_ALLOW_RESTART=1` in `/home/titpi/.config/titpi/web.env`. This is optional and is outside the deployment directory. Create/edit the file on the Pi:

```bash
install -d -m 700 "/home/titpi/.config/titpi"
touch "/home/titpi/.config/titpi/web.env"
chmod 600 "/home/titpi/.config/titpi/web.env"
nano "/home/titpi/.config/titpi/web.env"
```

Grant only the fixed command required by the endpoint:

```bash
sudo visudo -f /etc/sudoers.d/titpi-web-restart
```

Add this rule:

```sudoers
titpi ALL=(root) NOPASSWD: /usr/bin/systemctl restart titpi.service
```

Validate the rule and reload the web service's environment:

```bash
sudo visudo -c
sudo cp "/home/titpi/titpi/services/titpi-web.service" "/etc/systemd/system/titpi-web.service"
sudo systemctl daemon-reload
sudo systemctl restart titpi-web.service
```

Normal update deployments do not install updated systemd units; the copy/reload above ensures the optional environment file is read. Anyone on the trusted LAN can request a watcher restart once this option is enabled.

The button asks for confirmation because restarting interrupts an active recording. The server uses a fixed argument list, noninteractive sudo, and a 20-second timeout; it accepts no service names or shell commands from the browser. A successful response means systemd accepted the restart, not that the camera subsequently initialized successfully. Check the service status/logs after applying.

### Validation and rollback

On macOS, run the isolated tests with a Python environment containing Flask:

```bash
.venv/bin/python -m unittest tools.test_settings -v
bash -n deploy.sh
```

The tests use temporary configuration files, mock database initialization and restart commands, and never contact the matrix. The read-only dashboard preview can show the settings screen but cannot load settings, save, or restart anything.

After deploying, check the page loads settings without a password, save a harmless duration change, and confirm the backup exists before applying. A subsequent real detection verifies the photo duration and Spotify restoration; API tests do not prove controller behavior.

To roll back the most recent matrix save on the Pi, preserve the current file first, then restore its backup and restart the watcher:

```bash
cd "/home/titpi/titpi"
cp -p config_lcd.json "config_lcd.json.before-rollback-$(date +%Y%m%d-%H%M%S)"
cp -p config_lcd.json.bak config_lcd.json
sudo systemctl restart titpi.service
systemctl is-active titpi.service
```

To disable browser-triggered restart without changing matrix behavior, set `TITPI_ALLOW_RESTART=0` in the optional environment file and restart `titpi-web.service`. Editing remains available to the LAN; restrict dashboard access at the network/firewall if needed.

## titpi-botd.service — Bird of the Day (one-shot)

Runs `compute_botd.py` once when triggered by the timer. Not started directly.

## titpi-botd.timer — Daily 7 PM trigger

Fires `titpi-botd.service` every day at 19:00. `Persistent=true` means it will catch up if the Pi was off at that time.

```bash
sudo systemctl status titpi-botd.timer
systemctl list-timers titpi-botd.timer   # shows next scheduled run
```
