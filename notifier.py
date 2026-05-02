import smtplib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders as email_encoders
import os
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH) as _f:
    _config = json.load(_f)["email"]

SMTP_HOST = _config.get("smtp_host", "smtp.gmail.com")
SMTP_PORT = int(_config.get("smtp_port", 587))
EMAIL_FROM = _config.get("from", "")
EMAIL_PASSWORD = _config.get("password", "")
EMAIL_TO = _config.get("to", "")


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
    body = f"Detected as: {label}\nConfidence: {confidence:.2f}\n"
    if species:
        body += f"\nGPT says: {species['common_name']} ({species['name']})\n"
        body += f"Category: {species.get('category', label)}\n"
        body += f"ID confidence: {species['score']:.0%}\n"
    if image_path:
        body += f"Snapshot: {image_path}\n"
    if video_path:
        body += f"Video clip: {video_path}\n"

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), name=os.path.basename(image_path))
            msg.attach(img)

    if video_path and os.path.exists(video_path):
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
