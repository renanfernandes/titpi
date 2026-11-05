# TitPi — Templates

HTML templates rendered by `notifier.py` / `notifier_lcd.py`. They use a minimal custom template engine (defined in `notifier.py`) that supports `{{ variable }}` substitution and `{% if %}...{% else %}...{% endif %}` conditionals.

---

## email.html — Detection alert email

Sent on every confirmed detection. Rendered with the following context variables:

The charcoal/green, photo-first email matches the dashboard. Subjects use `TitPi: <common name> stopped by!`, with `TitPi: Someone stopped by!` as the fallback. The body keeps the original camera category and raw detection score in a small diagnostic section, separate from the final identification confidence and source. The watcher passes its confirmed camera label rather than the generic `visitor` label.

The template uses inline styles and presentation tables, including an Outlook fixed-width fallback. Photos remain embedded via `cid:detection_photo`; no external fonts or JavaScript are required. Dynamic fields are HTML-escaped by both notifiers. Browser previews are not a substitute for checking delivery in the actual email client.

| Variable | Description |
|----------|-------------|
| `has_photo` | Boolean — controls whether the inline photo block is shown |
| `species_common_name` | Common name of the identified species (e.g. `House Finch`) |
| `species_name` | Scientific name (e.g. `Haemorhous mexicanus`) |
| `confidence_pct` | Identification confidence formatted as percentage (e.g. `87%`) |
| `source_label` | Source of the ID: `Local model` or `GitHub model` |
| `detected_label` | Raw IMX500 COCO label (e.g. `BIRD`) |
| `detection_score` | Raw IMX500 spike score (e.g. `0.72`) |
| `video_link` | URL to the video file — only shown if `attach_video` is `false` in config |
| `dashboard_link` | URL to the web dashboard |

### Deploy the email update

On macOS, from the repository root, back up the four changed files on the Pi before deploying. The internal LCD notifier is git-ignored but included by the deployment script's rsync:

```bash
ssh titpi@10.0.0.80 'for file in watcher.py notifier.py notifier_lcd.py templates/email.html; do cp -pn "/home/titpi/titpi/$file" "/home/titpi/titpi/$file.before-email-refresh" || exit 1; done'
bash ./deploy.sh 10.0.0.80
ssh titpi@10.0.0.80 'systemctl is-active titpi.service titpi-web.service'
```

Both services should report `active`. The deployment script copies other local project changes and LCD configuration too; it preserves the Pi's credentials in `config.json` while updating `database.web_host`.

To send a basic test email from the Pi without uploading an image to the matrix:

```bash
cd "/home/titpi/titpi" && timeout 45s python3 -u - <<'PY'
from notifier_lcd import send_detection_email

raise SystemExit(0 if send_detection_email("test", 0.0) else 1)
PY
```

This intentionally sends an unidentified test visitor with no photo. For the next actual detection, verify the embedded photo, friendly subject, original camera label, identification source, and video attachment or link in your mail client. `Email sent` confirms SMTP acceptance, not inbox delivery.

To roll back only the email-related files and restart the watcher:

```bash
ssh titpi@10.0.0.80 'for file in watcher.py notifier.py notifier_lcd.py templates/email.html; do cp -p "/home/titpi/titpi/$file.before-email-refresh" "/home/titpi/titpi/$file" || exit 1; done; sudo systemctl restart titpi.service'
```

## dashboard.html — Web dashboard

Single-page app served by `web.py` at port 8080. Uses Bootstrap 5 and Chart.js. Fetches all data from the `/api/*` endpoints — no server-side rendering.

- **Visits:** recent photos, species search with suggestions, date navigation, starred filtering, photo lightbox, video playback, and CSV export. Visit menus contain confidence, detection score, duration, downloads, edit, star, and delete actions. Select mode reveals checkboxes and bulk management.
- **Insights:** one shared 7/14/30/90-day or all-time period for daily visits, hourly activity, and the eight most frequent recorded labels. Label counts include unknown and non-bird records; they are not a verified species count.
- **Activity map:** the collapsible GitHub-style yearly grid retains daily counts, green intensity levels, and click-to-filter navigation to Visits. Its yearly range is independent of the Insights period.

Refreshes run every 30 seconds, preserving date/search/starred filters. Refresh is paused while a photo, calendar, visit menu, edit dialog, or bulk selection is active.

### Local preview

From the repository root on macOS:

```bash
python3 tools/dashboard_preview.py --upstream http://10.0.0.80:8080 --port 8082
```

Open http://127.0.0.1:8082. The preview serves the local template and reads real data and media from the Pi. It binds only to localhost, does not read credentials, and rejects POST/PATCH/DELETE requests. Editing and starring are intentionally unavailable in this preview; browser validation uses mocked mutation responses. Use another port if 8082 is occupied. Stop with Ctrl+C.

### Deploy and roll back

Back up the current template before using the existing deployment script:

```bash
ssh titpi@10.0.0.80 'cp -pn "/home/titpi/titpi/templates/dashboard.html" "/home/titpi/titpi/templates/dashboard.html.before-redesign"'
bash ./deploy.sh 10.0.0.80
ssh titpi@10.0.0.80 'systemctl is-active titpi.service titpi-web.service'
```

Both services should report `active`. The deployment script also copies local LCD configuration and restarts the watcher; it suppresses restart failures, so verify service status. No database migration is needed.

To restore the previous dashboard without changing detections:

```bash
ssh titpi@10.0.0.80 'cp -p "/home/titpi/titpi/templates/dashboard.html.before-redesign" "/home/titpi/titpi/templates/dashboard.html" && sudo systemctl restart titpi-web.service'
```
