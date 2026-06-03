import smtplib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders as email_encoders
import os
import json
import re

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "email.html")

with open(CONFIG_PATH) as _f:
    _config = json.load(_f)["email"]

SMTP_HOST = _config.get("smtp_host", "smtp.gmail.com")
SMTP_PORT = int(_config.get("smtp_port", 587))
EMAIL_FROM = _config.get("from", "")
EMAIL_PASSWORD = _config.get("password", "")
EMAIL_TO = _config.get("to", "")
ATTACH_VIDEO = _config.get("attach_video", True)

with open(CONFIG_PATH) as _wf:
    _web_cfg = json.load(_wf).get("database", {})
WEB_HOST = _web_cfg.get("web_host", "10.0.1.60")
WEB_PORT = _web_cfg.get("web_port", 8080)


def _render_template(template_str, ctx):
    """Minimal template renderer: {{ var }}, {% if var %}...{% else %}...{% endif %}."""
    # Handle conditionals (with optional else)
    def _replace_if(m):
        var = m.group(1).strip()
        body = m.group(2)
        # Split on {% else %} if present
        parts = re.split(r'\{%\s*else\s*%\}', body, maxsplit=1)
        if ctx.get(var):
            return parts[0]
        return parts[1] if len(parts) > 1 else ""
    template_str = re.sub(
        r'\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}',
        _replace_if, template_str, flags=re.DOTALL,
    )
    # Handle variables
    def _replace_var(m):
        return str(ctx.get(m.group(1).strip(), ""))
    return re.sub(r'\{\{\s*(\w+)\s*\}\}', _replace_var, template_str)


def send_detection_email(label, confidence, image_path=None, video_path=None, species=None):
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        print("[NOTIFY] Email not configured. Set TITPI_EMAIL_* env vars.")
        return False

    subject = f"TitPi Alert: {label.upper()} detected!"
    if species:
        actual = species.get('category', label)
        if actual != label:
            subject = f"TitPi Alert: {species['common_name']} (was {label.upper()}) detected!"
        else:
            subject = f"TitPi Alert: {species['common_name']} ({label.upper()}) detected!"

    # Build template context
    source = species.get('source', 'unknown') if species else ''
    source_label = 'Local model' if source == 'local' else 'GPT' if source == 'gpt' else source
    video_link = None
    if video_path and not ATTACH_VIDEO:
        video_link = f"http://{WEB_HOST}:{WEB_PORT}/video/{os.path.basename(video_path)}"
    dashboard_link = f"http://{WEB_HOST}:{WEB_PORT}/"

    ctx = {
        "has_photo": bool(image_path and os.path.exists(image_path)),
        "species_common_name": species['common_name'] if species else "",
        "species_name": species['name'] if species else "",
        "confidence_pct": f"{species['score']:.0%}" if species else "",
        "source_label": source_label,
        "detected_label": label.upper(),
        "detection_score": f"{confidence:.2f}",
        "video_link": video_link or "",
        "dashboard_link": dashboard_link,
    }

    # Render HTML
    with open(TEMPLATE_PATH) as f:
        html_body = _render_template(f.read(), ctx)

    msg = MIMEMultipart("related")
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), name=os.path.basename(image_path))
            img.add_header("Content-ID", "<detection_photo>")
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
            msg.attach(img)

    if video_path and os.path.exists(video_path) and ATTACH_VIDEO:
        with open(video_path, "rb") as f:
            video = MIMEBase("video", "mp4")
            video.set_payload(f.read())
            email_encoders.encode_base64(video)
            video.add_header(
                "Content-Disposition",
                f"attachment; filename={os.path.basename(video_path)}"
            )
            msg.attach(video)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"[NOTIFY] Email sent to {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"[NOTIFY] Email failed: {e}")
        return False
