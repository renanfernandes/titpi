# TitPi — Systemd Service Files

Systemd unit files for running TitPi as background services on the Pi. Install them with `deploy.sh` (handled automatically) or manually:

```bash
sudo cp services/*.service services/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now titpi.service titpi-web.service titpi-botd.timer
```

---

## Motion Camera Timeout Recovery

The November 15 `titpi2` logs show video saved at 11:09:29, local TFLite/XNNPACK initialization at 11:09:31, and a libcamera frontend timeout at 11:09:33. Email and the matrix Spotify restore follow at 11:10:30. The camera error therefore precedes Spotify restoration; timing alone does not establish inference, memory pressure, wiring or the matrix as the underlying cause.

`get_motion_score()` now arms the existing 15-second camera alarm around its low-resolution frame capture and clears it in `finally`. Both the normal loop and recording loop use this function. If a frame read blocks, the existing timeout handler exits with status 1 so `titpi.service` can recover through `Restart=on-failure` and its 15-second restart delay. This fixes indefinite waiting after a camera failure, not the sensor/frontend failure itself. No IMX500 path, email policy, matrix request or private JSON change is required.

Validation uses fake camera frames and an isolated one-second alarm test, never a real camera or notification:

```bash
.venv/bin/python -m unittest tools.test_settings.MotionCaptureTests -v
```

Deploy only `watcher.py`, `tools/test_settings.py` and this runbook to the motion Pi, after retaining a source backup at `/home/titpi/titpi-backups/motion-timeout-20261115`. Restart only `titpi.service` when a recording interruption is acceptable. Validate the active watcher and its startup log; actual sensor recovery still requires observation after a natural capture/frontend timeout:

```bash
ssh titpi@10.0.0.82 'systemctl is-active titpi.service && journalctl -u titpi.service -n 25 --no-pager'
```

Rollback restores the old unbounded motion read; it does not change private configuration or existing media:

```bash
ssh titpi@10.0.0.82 'sudo -n systemctl stop titpi.service && cp -a /home/titpi/titpi-backups/motion-timeout-20261115/. /home/titpi/titpi/ && sudo -n systemctl start titpi.service'
```

## Headless Motion Pi

On November 15, the Camera Module 3 Pi at `10.0.0.82` was switched from `graphical.target` to `multi-user.target`, and its LightDM display manager was stopped. No reboot, TitPi restart, camera/config change or package purge was performed. All three TitPi services remained active with the same PIDs, and the database-backed dashboard returned HTTP 200. Available RAM was about 167 MiB after the switch; this does not establish the earlier freezes' root cause. Previous-boot journals were unavailable.

This Pi uses LightDM with Labwc/Wayland and also has Xorg installed. Keeping the desktop stopped removes its runtime overhead; removing packages mainly frees disk space. The reviewed simulation below selected 17 desktop/display packages without removing Picamera2, libcamera, SSH or networking. Package resolution can change, so review the simulation again before purging:

```bash
ssh titpi@10.0.0.82 'apt-get --simulate purge lightdm labwc xserver-xorg xserver-xorg-core'
```

Optional removal, only after confirming the proposed package list in your terminal:

```bash
ssh -t titpi@10.0.0.82 'sudo apt-get purge lightdm labwc xserver-xorg xserver-xorg-core'
```

Do not run an unreviewed `apt autoremove`: camera packages use graphics-related libraries even on a headless Pi. No desktop packages were purged during the headless change. Validation requires no camera access or restart:

```bash
ssh titpi@10.0.0.82 'systemctl get-default && systemctl is-active titpi.service titpi-identification.service titpi-web.service && free -m'
curl --fail --silent --show-error http://10.0.0.82:8080/api/stats
```

Rollback while desktop packages remain installed:

```bash
ssh -t titpi@10.0.0.82 'sudo systemctl set-default graphical.target && sudo systemctl start lightdm.service'
```

If desktop packages were later purged, reinstall their Raspberry Pi desktop metapackages first, then restore graphical boot. This restores the desktop dependencies, not necessarily the exact previously installed versions or purged custom configuration:

```bash
ssh -t titpi@10.0.0.82 'sudo apt-get install rpd-wayland-core rpd-x-core lightdm && sudo systemctl set-default graphical.target && sudo systemctl start lightdm.service'
```

## Deployment Sudo Permissions

Update mode (`./deploy.sh HOST`, without a camera-mode argument) uses `sudo -n` so automation never hangs waiting for a sudo password. Before syncing, the script now uses `sudo -n -l` to verify permission for the exact worker-unit install, daemon reload, worker enable and three-service restart commands. If permission cannot be verified, it exits without syncing files or restarting services. SSH login success alone does not establish sudo access. This check is for update mode; fresh setup has separate package/service installation steps.

If an older run already printed **Code synced** followed by **sudo: a password is required**, the source files were copied but that run did not install the unit, update `database.web_host`, or restart services. Do not treat it as a completed deployment. For the new Pi at `10.0.0.82`, whose existing configuration already has that host, finish the privileged update in one interactive SSH session. Enter any password directly in your terminal, never in chat. Restarting interrupts active capture:

```bash
ssh -t titpi@10.0.0.82 '
sudo install -m 644 /home/titpi/titpi/services/titpi-identification.service /etc/systemd/system/titpi-identification.service &&
sudo systemctl daemon-reload &&
sudo systemctl enable titpi-identification.service &&
sudo systemctl restart titpi.service titpi-web.service titpi-identification.service &&
systemctl is-active titpi.service titpi-web.service titpi-identification.service
'
curl --fail --silent --show-error http://10.0.0.82:8080/api/settings/camera
```

All three services should report `active`, and the camera endpoint should report `motion`. For future unattended runs, an administrator must deliberately authorize the exact deployment commands through sudo policy; the script never edits that policy or grants unrestricted passwordless sudo. Running `sudo -v` in a different SSH session is not a reliable fix because sudo tickets can be tied to the terminal/session.

Local validation (SSH and rsync are mocked; no Pi changes):

```bash
bash -n deploy.sh
.venv/bin/python -m unittest tools.test_settings.DeploymentTests -v
```

The new check itself needs no Pi rollback because it is read-only and runs before sync. For app files copied by an older failed run, use the per-Pi source backup and rollback instructions below; private JSON and media are not replaced by the manual completion commands.

## Per-Pi Email And Complete Settings

Open `http://10.0.0.80:8080/settings` for the AI Pi or `http://10.0.0.82:8080/settings` for the Camera Module 3 Pi. Each host edits its own private JSON. In **Email**, set **Email subject prefix** to a unique camera/location label. For example, `"email": {"subject_prefix": "TitPi - Garden", ...}` produces `TitPi - Garden: Blue Tit stopped by!`; final diagnostic subjects use the same prefix. Missing/blank prefixes fall back to `TitPi`; the page requires a nonempty prefix of at most 120 characters and rejects control characters. No synthetic mail or existing-job replay is needed.

All supported JSON fields are represented: camera mode/model/thresholds/frame counts/timing/targets/capture and log paths; SMTP credentials/addresses/host/port/video attachment/subject prefix; required local classification and confidence; all remote provider/prompt/timeout/retry/SSH settings; database path and dashboard link host/listen port; and the three matrix settings. The local-classifier enabled flag is visible but locked on because local-first classification is mandatory. Passwords/API keys show only configured status; blank inputs preserve stored secrets. Unknown extension fields are preserved but not interpreted as supported settings. A regression test compares example/installer JSON fields against page/API coverage.

General saves use partial updates with validation, a version conflict check, file locking, mode-600 atomic writes and a `.bak` snapshot. They do not restart services, send email, connect to Hermes or manipulate captures. Keep the dashboard on the trusted LAN: anyone with dashboard access can change these settings, including credentials and storage paths.

After saving email, dashboard or storage settings, apply on the affected Pi when a recording interruption is acceptable. The browser's existing Apply buttons restart only the watcher, not its worker or web process. General save status therefore directs you to SSH:

```bash
PI_HOST="10.0.0.82" # Use 10.0.0.80 for the AI Pi
ssh "titpi@$PI_HOST" 'sudo -n systemctl restart titpi.service titpi-identification.service titpi-web.service && systemctl is-active titpi.service titpi-identification.service titpi-web.service'
```

Changing `database.web_host` changes email links, not the web bind address. Changing `database.web_port` requires reopening Settings on the new port after restarting web. Capture/log/database paths must be absolute; **path changes do not migrate existing photos or database records**. The page asks confirmation before saving capture/database path changes. Leave optional log/database paths blank for defaults beneath the capture directory. Take a separate data backup and plan migration before switching storage; code/config rollback cannot move or restore files.

Validation uses temporary configs and mocked email, never a real notification or camera restart:

```bash
.venv/bin/python -m unittest tools.test_identification tools.test_settings -q
curl --fail --silent --show-error "http://$PI_HOST:8080/api/settings/general"
ssh "titpi@$PI_HOST" 'cd /home/titpi/titpi && timeout 60s python3 -m unittest tools.test_settings -q'
```

The November 13 complete-settings deployment uses the source-only backup `/home/titpi/titpi-backups/complete-settings-20261113` on each Pi and restarts only web; live prefixes, credentials, modes and data remain unchanged. Source rollback below removes this update but preserves any subsequently saved JSON. Restarting all three services applies the restored notifier code and interrupts capture:

```bash
ssh "titpi@$PI_HOST" 'sudo -n systemctl stop titpi-web.service && cp -a /home/titpi/titpi-backups/complete-settings-20261113/. /home/titpi/titpi/ && sudo -n systemctl restart titpi.service titpi-identification.service titpi-web.service'
```

For a configuration-only rollback, the adjacent `config.json.bak` contains the state immediately before the most recent config save, not a full settings-session snapshot. Review it privately and restore it only while no settings saves or jobs are in progress. Never copy one Pi's JSON over the other or paste credentials into logs/chat.

## Camera Mode Settings

Open `/settings` on the Pi being configured and select **AI Camera (IMX500)** or **Camera Module 3 (motion detection)**. This is explicit configuration, not hardware auto-detection. Each Pi has its own `config.json`; changes on one do not affect the other. The default remains `imx500` when `camera.mode` is absent.

**Save camera** persists only `camera.mode` with an atomic private-file write, `.bak` backup, same-origin validation and conflict detection. Model paths, capture tuning, SMTP credentials, identification providers and all other JSON fields are preserved. Save does not restart or contact camera hardware. Save any other dirty forms before applying. **Apply and restart camera** uses the existing `TITPI_ALLOW_RESTART=1` and fixed `titpi.service` sudo rule, and asks confirmation because an active recording is interrupted. If browser restart is disabled, use SSH after saving:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl restart titpi.service'
```

Replace the host for the second Pi. Choose `motion` only on the Camera Module 3 Pi. Validate without mutating configuration or restarting:

```bash
curl --fail --silent --show-error http://10.0.0.80:8080/api/settings/camera
python3 -m unittest tools.test_settings.CameraSettingsTests -v
```

The November 13 Settings deployment preserves the live mode and restarts only web. Its source-only backup is `/home/titpi/titpi-backups/camera-settings-20261113`. Rollback below removes the new selector/API but does not revert any mode saved afterward; select the correct hardware mode and apply it before rollback if needed:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl stop titpi-web.service && cp -a /home/titpi/titpi-backups/camera-settings-20261113/. /home/titpi/titpi/ && sudo -n systemctl start titpi-web.service'
```

## Visit Deletion And Media

Single and bulk dashboard deletion remove the visit row, recorded photo/video, and capture files with the same exact watcher timestamp (including retained extra photos and the original visitor photo). Cleanup is restricted to image/video files directly within `camera.save_directory`; paths outside it and symlink targets are never removed. Files referenced by another visit are preserved, and timestamp-based discovery is skipped while another visit references that capture timestamp. Missing files are tolerated. Affected Bird of the Day cache entries are invalidated, and still-pending Hermes email notifications for removed photos are cancelled. Already-started email delivery cannot reliably be recalled.

On a filesystem error, the affected visit row is retained and the API returns HTTP 503; retry after fixing permissions/storage. Some files may already have been removed before an error, and bulk deletion may have completed earlier visits. Retrying tolerates these missing files. SQLite and filesystem deletion are not one atomic transaction. This change does not sweep orphan media from visits deleted previously.

Validation uses isolated temporary captures and a disposable SQLite database, never real visit deletion:

```bash
python3 -m unittest tools.test_settings.VisitDeletionTests -v
ssh titpi@10.0.0.80 'systemctl is-active titpi.service titpi-identification.service titpi-web.service'
```

The November 12 source backup is `/home/titpi/titpi-backups/visit-media-20261112`. Code rollback restores the old row-only deletion behavior; **it cannot restore media already deleted**. Restore lost media only from a separate media backup. No private JSON or schema changes are required:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl stop titpi-web.service && cp -a /home/titpi/titpi-backups/visit-media-20261112/. /home/titpi/titpi/ && sudo -n systemctl start titpi-web.service'
```

## titpi.service — Main detection loop

Runs `watcher.py` continuously. Restarts automatically on failure (15s delay).

```bash
sudo systemctl status titpi.service
sudo systemctl restart titpi.service
journalctl -u titpi -f          # live logs
```

### Missing AI output recovery

In IMX500 mode, the watcher exits with an error after more than 120 seconds of continuously missing AI output, allowing systemd's `Restart=on-failure` policy to recover it. Brief metadata gaps are tolerated, and valid output resets the timer. The existing unchanged-output and camera-call guards remain in place. A blocked native camera call is a separate failure mode; this change does not diagnose the underlying sensor/driver problem.

Deploy only this watcher change from the repository root on macOS when a recording interruption is acceptable:

```bash
ssh titpi@10.0.0.80 'cp -pn "/home/titpi/titpi/watcher.py" "/home/titpi/titpi/watcher.py.before-missing-output-fix"'
rsync -az "./watcher.py" titpi@10.0.0.80:/home/titpi/titpi/
ssh titpi@10.0.0.80 'sudo -n systemctl restart titpi.service && systemctl is-active titpi.service'
```

After deployment, verify initialization completes and debug detections continue; `active` alone does not prove camera progress:

```bash
ssh titpi@10.0.0.80 'journalctl -u titpi.service -n 30 --no-pager'
```

Simulated clock/output checks validate the missing-output timeout, reset, and transient-gap behavior without importing camera hardware or terminating a live process. Real camera/driver recovery still needs observation on the Pi. On timeout, the journal should report `AI pipeline produced no output for 120s, exiting for restart...` before systemd starts a new watcher.

Rollback on the Pi:

```bash
cp -p "/home/titpi/titpi/watcher.py.before-missing-output-fix" "/home/titpi/titpi/watcher.py"
sudo -n systemctl restart titpi.service
```

### Bird identification

Local TFLite classification is mandatory, even if an old config contains `local_classifier.enabled: false`. A score below `min_confidence` (default 0.3), unavailable local model, or local failure permits only the selected fallback after all captured photos have been checked locally. `identification.provider` accepts exactly `none`, `hermes_ssh`, `openai`, or `claude`; default `none` makes no remote request. A confident local result never contacts a provider. Hermes uses a separate persistent queue worker; direct APIs retain their existing synchronous flow. No Copilot SDK worker or GitHub token is used.

Configure the following sections in the existing private JSON or through `/settings`; preserve camera, email, and database settings. Credentials are omitted here:

```json
"identification": {
	"provider": "none",
	"timeout": 30,
	"max_attempts": 2,
	"prompt": "Which bird and species are visible? Report no bird if none is visible, Unidentified bird if the species is uncertain, and inconclusive if the image is unclear. Do not invent a scientific name."
},
"openai": {"model": "gpt-4.1-mini", "api_key": ""},
"claude": {"model": "claude-haiku-4-5", "api_key": ""}
```

`identification.prompt` is shared by every remote provider and editable in the web interface. OpenAI/Claude append fixed JSON response instructions and validate bird-only results. A visible but uncertain bird can be **Unidentified bird**; no bird is `empty`, while blur or occlusion is `inconclusive`. API failures are never treated as empty frames. Only accepted identifications reach the matrix and visitor rows. Hermes diagnostic emails can include all other outcomes without publishing them; direct API behavior is unchanged. Review retained files periodically. Confidence is model-reported, not calibrated.

Use `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the watcher service environment, or their private JSON fields. Environment values take precedence. Never put secrets in commands, source code, or chat. Web credentials are write-only: a blank field preserves the stored value. The configured indicator describes the web process, not proof that the watcher has the same environment. Claude Pro does **not** include Anthropic API credits; both direct APIs require separate API credentials/billing.

Direct APIs receive a JPEG resized to a maximum 1280-pixel longest side at quality 85. Retries are limited to 1–3 attempts for 408/429/5xx and transport timeouts, with 1/2-second backoff. Retries can incur repeat billing; use one attempt if that risk is unacceptable.

Setup and validation from macOS, when a service restart is acceptable:

```bash
cd "/Users/renanfernandes/Documents/github/titpi"
.venv/bin/python -m unittest tools.test_identification tools.test_settings -v
bash -n deploy.sh
ssh -o ConnectTimeout=10 titpi@10.0.0.80 'systemctl is-active titpi.service titpi-web.service; free -m'
ssh titpi@10.0.0.80 'umask 077; backup="/home/titpi/titpi-backups/before-provider-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$backup" && cp -p /home/titpi/titpi/config.json "$backup/config.json"'
./deploy.sh 10.0.0.80
```

Deploy preserves private JSON and its backups; do not copy the Mac config onto the Pi. Open `http://10.0.0.80:8080/settings`, choose a provider and save the prompt/credentials, then apply over SSH:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl restart titpi.service && systemctl is-active titpi.service'
ssh titpi@10.0.0.80 'journalctl -u titpi.service -n 30 --no-pager'
```

If noninteractive sudo is denied, run the restart in the Pi terminal. Confirm camera initialization and changing detection output; `active` alone is insufficient. Tests use fake provider/mail calls, not real paid inference. Real provider credentials, model access, and species accuracy require separate verification.

Rollback remote use by selecting `none`, saving, and restarting; local classification and retained-media protections stay enabled. To restore the latest JSON save on the Pi, first preserve the current file, review the backup for subsequent credential/config changes, then restore:

```bash
cd "/home/titpi/titpi"
umask 077
cp -p config.json "config.json.before-rollback-$(date +%Y%m%d-%H%M%S)"
cp -p config.json.bak config.json
chmod 600 config.json
sudo -n systemctl restart titpi.service
```

## titpi-web.service — Dashboard

Runs `web.py` (Flask) on port 8080. Starts after `titpi.service`.

```bash
sudo systemctl status titpi-web.service
sudo systemctl restart titpi-web.service
journalctl -u titpi-web -f
```

### Hermes SSH queue

Select `hermes_ssh` only after SSH authentication is working from the Pi to the Hermes host. Local classification remains first on every capture; only the best unresolved photo is enqueued. `titpi-identification.service` is a single lightweight worker with an exclusive lock. The queue lives in the existing detections SQLite database, independently of visitor rows. Pending jobs survive restarts and pause when another provider is selected.

The JSON `hermes_ssh` section and Settings expose host, SSH user, private-key path **on the Pi**, temporary directory **on the remote host**, Hermes executable, connection/total timeout, attempts and minimum confidence. Defaults are 10-second connection, 60-second total execution, one attempt and 0.70 confidence. Absolute paths are required; only the key path is stored, never its contents. The remote host needs Python 3, SFTP and an already-configured Hermes CLI. Its default model and credentials remain on that host.

The worker creates one private `titpi-hermes-<random-id>` directory under the configured remote directory (default `/tmp`), sends a JPEG bounded to 1280 pixels, and invokes a single image query. Prompt text travels through stdin, not shell interpolation. Host-key checking is mandatory, password/agent forwarding is disabled, and the remote process group has its own deadline. The registered empty `bot_room` toolset was verified on the target Hermes installation; JSONL tool events, malformed output or unexpected text fail closed. Revalidate that empty toolset after Hermes upgrades. No webhook or callback is used.

Only validated species with a known scientific name and confidence at or above the configured threshold reach visitor rows and the matrix. Unidentified birds, low-confidence results, empty, inconclusive and errors preserve original media without visitor publication. Confidence is model-reported, not calibrated. Detection insertion and job completion are one transaction. Notification claims are **at-most-once**, not guaranteed delivery: after an interruption during SMTP, no automatic resend is made. Interrupted running identification jobs are marked failed rather than resubmitted, avoiding unknown duplicate inference; pending jobs remain queued. Increasing attempts can incur repeat inference/billing, all within the total job budget.

For commissioning, `hermes_ssh.notify_all` defaults to `true`, exposed as **Email every Hermes result (diagnostic)** in Settings. Each newly completed Hermes job schedules one email even for unknown species, low confidence, empty, inconclusive or transport/parsing failure. Unpublished results use `TitPi: Hermes diagnostic - <outcome>` subjects and include the capture, outcome and safe error class where applicable. These emails do not upload to the matrix or insert visitor rows. Accepted species retain their normal notification flow. Existing completed jobs are not replayed. A job must still be captured and queued first: this does not email every camera frame. Startup-interrupted jobs are logged but not retrospectively emailed. Disable the checkbox (or set `notify_all` to `false`) to restrict Hermes result mail to accepted species; this does not disable the separate local diagnostic email. Saves affect the next worker job without restarting it. Preserve all other SSH fields when editing private JSON.

The notification flow sends **one final email per captured visit**, not one per inference stage. A confident local identification sends its normal email and skips remote inference. Otherwise, no preliminary local email is sent: Hermes jobs are emailed only by the worker after completion; OpenAI/Claude visits are emailed after the API returns. Empty, inconclusive or error API outcomes use a provider-labelled diagnostic email without visitor publication or matrix upload. Keep `hermes_ssh.notify_all=true` to include unresolved/error Hermes outcomes in this final email policy. With remote identification disabled, an unresolved local outcome gets one local unknown diagnostic email. Missing species confidence is omitted, not reported as zero. SMTP has a 30-second timeout; failed delivery is logged and not retried, so this is one send attempt rather than guaranteed inbox delivery. There is no historical replay. This replaces the earlier local-before-fallback commissioning flow.

Validate the running services and recent worker activity from your workstation:

```bash
ssh titpi@10.0.0.80 'systemctl is-active titpi.service titpi-identification.service titpi-web.service'
ssh titpi@10.0.0.80 'journalctl -u titpi-identification.service --since "10 minutes ago" --no-pager -n 30'
```

Disabling the diagnostic checkbox rolls back only the extra Hermes result emails. The November 10 diagnostic deployment also has a private file/config backup at `/home/titpi/titpi-backups/hermes-diagnostic-20261110-1657`. For a code rollback, the commands below restore those files, including the private JSON as it was before deployment; preserve any subsequent configuration edits separately first. They do not alter media, detections or queue history:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl stop titpi-identification.service titpi-web.service && cp -a /home/titpi/titpi-backups/hermes-diagnostic-20261110-1657/. /home/titpi/titpi/ && sudo -n systemctl start titpi-identification.service titpi-web.service'
```

To roll back only the subsequent local-before-fallback email correction, use its separate source-only snapshot. This preserves current JSON, media and database, and leaves the earlier Hermes diagnostic feature enabled:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl stop titpi.service titpi-identification.service && cp -a /home/titpi/titpi-backups/local-mail-20261110-1731/. /home/titpi/titpi/ && sudo -n systemctl start titpi.service titpi-identification.service'
```

The November 12 one-final-email change has its own backup at `/home/titpi/titpi-backups/final-email-20261112`. Its rollback restores the previous early-local-email behavior and the predeployment private JSON; preserve later configuration changes separately first:

```bash
ssh titpi@10.0.0.80 'sudo -n systemctl stop titpi.service titpi-identification.service && cp -a /home/titpi/titpi-backups/final-email-20261112/. /home/titpi/titpi/ && sudo -n systemctl start titpi.service titpi-identification.service'
```

The worker removes only its own remote subfolder after completion/failure. Disconnection or a killed remote helper may leave temporary files; `/tmp` does not guarantee timely cleanup. No separate cleanup daemon is installed. Source captures remain on the Pi even after successful identification; monitor disk usage. Keep the dashboard on a trusted LAN: access to its settings permits changing the SSH destination and executable.

Setup and validation (on the Pi; first verify the Mac mini's host fingerprint):

```bash
chmod 600 "$HOME/.ssh/id_ed25519_macmini"
ssh -i "$HOME/.ssh/id_ed25519_macmini" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 renanfernandes@10.0.0.3 'whoami; command -v python3'
cd "/home/titpi/titpi"
python3 -m unittest tools.test_identification tools.test_settings -v
systemctl is-active titpi.service titpi-web.service titpi-identification.service
journalctl -u titpi-identification.service -n 30 --no-pager
```

Deploy from macOS with `./deploy.sh 10.0.0.80`. Deployment installs/enables the worker while preserving private JSON and capture files. In Settings select Hermes SSH, review the fields, Save, then restart `titpi.service` to apply capture routing. The worker reads configuration before each job; changing providers pauses pending jobs rather than dropping them. Test a clear bird, an empty/blurred capture, unavailable host, and timeout before relying on alerts. Avoid live-image probes without authorization: they invoke the configured provider.

Rollback remote use: select `none`, save and restart the watcher; optionally stop/disable the worker. Do not delete the queue or restore a live database while services are using it:

```bash
sudo -n systemctl restart titpi.service
sudo -n systemctl disable --now titpi-identification.service
```

### Settings page: first-time setup

The dashboard's gear opens `/settings`: required local classifier, confidence threshold, four modes including queued Hermes SSH, shared bird-identification prompt, provider settings, and LED matrix URL/profile/display duration. An empty matrix URL disables uploads. Stored API keys and private-key contents are never returned to the browser.

Settings load directly without an admin password. Anyone who can reach the dashboard can change providers, prompts, credentials, and matrix settings, potentially causing paid API use. Restrict access to the trusted internal network; do not expose this Flask server to the Internet. Writes require same-origin JSON, validation, and conflict detection. Stored credentials are never returned.

Deploy the code from macOS:

```bash
./deploy.sh 10.0.0.80
```

Open `http://10.0.0.80:8080/settings`, or use the dashboard gear. No password or environment file is required for editing. Older `TITPI_SETTINGS_PASSWORD_HASH` values are ignored and may be removed from the optional web environment file.

### Saving and applying

Each form has its own Save. The server validates fields, locks writes, preserves unrelated JSON keys, atomically writes mode-600 configuration, and keeps previous contents as `config.json.bak` or `config_lcd.json.bak`. Conflicting edits return an error; reload current values. Reload confirms before discarding edits. Saving never contacts providers/the matrix or restarts recording.

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
