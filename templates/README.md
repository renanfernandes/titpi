# TitPi — Templates

HTML templates rendered by `notifier.py` / `notifier_lcd.py`. They use a minimal custom template engine (defined in `notifier.py`) that supports `{{ variable }}` substitution and `{% if %}...{% else %}...{% endif %}` conditionals.

---

## email.html — Detection alert email

Sent on every confirmed detection. Rendered with the following context variables:

| Variable | Description |
|----------|-------------|
| `has_photo` | Boolean — controls whether the inline photo block is shown |
| `species_common_name` | Common name of the identified species (e.g. `House Finch`) |
| `species_name` | Scientific name (e.g. `Haemorhous mexicanus`) |
| `confidence_pct` | Identification confidence formatted as percentage (e.g. `87%`) |
| `source_label` | Source of the ID: `Local model` or `GPT` |
| `detected_label` | Raw IMX500 COCO label (e.g. `BIRD`) |
| `detection_score` | Raw IMX500 spike score (e.g. `0.72`) |
| `video_link` | URL to the video file — only shown if `attach_video` is `false` in config |
| `dashboard_link` | URL to the web dashboard |

## dashboard.html — Web dashboard

Single-page app served by `web.py` at port 8080. Uses Bootstrap 5 and Chart.js. Fetches all data from the `/api/*` endpoints — no server-side rendering.
