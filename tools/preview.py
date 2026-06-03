#!/usr/bin/env python3
"""
Live camera preview with AI detection overlay.
Streams MJPEG over HTTP so you can view in any browser at http://<pi-ip>:8080
Useful for positioning/focusing the camera before running watcher.py.

Usage:
    python3 preview.py              # plain video stream
    python3 preview.py --detections # overlay AI bounding boxes + labels
"""

import io
import os
import sys
import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np
from picamera2 import Picamera2
from PIL import Image, ImageDraw, ImageFont

# --- CONFIG ---
_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, _ROOT)
CONFIG_PATH = os.path.join(_ROOT, "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)["camera"]

CAMERA_MODE = _cfg.get("mode", "imx500")
STREAM_PORT = 8081
STREAM_SIZE = (640, 480)
SHOW_DETECTIONS = "--detections" in sys.argv and CAMERA_MODE == "imx500"
MODEL_PATH = _cfg.get("model_path", "/usr/share/imx500-models/imx500_network_efficientdet_lite0_pp.rpk")
CONFIDENCE_THRESHOLD = 0.40

if CAMERA_MODE == "imx500":
    from picamera2.devices import IMX500

COCO_LABELS = {
    0: "person", 1: "person", 2: "bicycle", 3: "car", 4: "motorcycle", 5: "airplane",
    6: "bus", 7: "train", 8: "truck", 9: "boat", 10: "traffic light",
    11: "fire hydrant", 13: "stop sign", 14: "parking meter", 15: "bench",
    16: "bird", 17: "cat", 18: "dog", 19: "horse", 20: "sheep", 21: "cow",
    22: "elephant", 23: "bear", 24: "zebra", 25: "giraffe", 27: "backpack",
    28: "umbrella", 31: "handbag", 32: "tie", 33: "suitcase", 34: "frisbee",
    35: "skis", 36: "snowboard", 37: "sports ball", 38: "kite",
    39: "baseball bat", 40: "baseball glove", 41: "skateboard",
    42: "surfboard", 43: "tennis racket", 44: "bottle", 46: "wine glass",
    47: "cup", 48: "fork", 49: "knife", 50: "spoon", 51: "bowl",
    52: "banana", 53: "apple", 54: "sandwich", 55: "orange", 56: "broccoli",
    57: "carrot", 58: "hot dog", 59: "pizza", 60: "donut", 61: "cake",
    62: "chair", 63: "couch", 64: "potted plant", 65: "bed",
    67: "dining table", 70: "toilet", 72: "tv", 73: "laptop", 74: "mouse",
    75: "remote", 76: "keyboard", 77: "cell phone", 78: "microwave",
    79: "oven", 80: "toaster", 81: "sink", 82: "refrigerator", 84: "book",
    85: "clock", 86: "vase", 87: "scissors", 88: "teddy bear",
    89: "hair drier", 90: "toothbrush",
}

TARGET_LABELS = {"bird", "person", "dog"}

# Box colors: green for targets, gray for others
COLOR_TARGET = (0, 255, 0)
COLOR_OTHER = (128, 128, 128)

# --- Globals ---
latest_frame = None
frame_lock = threading.Lock()


def get_font():
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        return ImageFont.load_default()


def draw_detections(image, np_outputs):
    """Draw bounding boxes and labels on a PIL Image."""
    if np_outputs is None:
        return image

    boxes = np_outputs[0][0]
    scores = np_outputs[1][0]
    classes = np_outputs[2][0].astype(int)
    num_dets = int(np_outputs[3][0].item())
    font = get_font()

    draw = ImageDraw.Draw(image)
    w, h = image.size

    for i in range(num_dets):
        score = float(scores[i])
        if score < CONFIDENCE_THRESHOLD:
            continue

        class_id = int(classes[i])
        label = COCO_LABELS.get(class_id, f"?{class_id}")
        is_target = label in TARGET_LABELS
        color = COLOR_TARGET if is_target else COLOR_OTHER

        # Boxes are normalized [ymin, xmin, ymax, xmax]
        ymin, xmin, ymax, xmax = boxes[i]
        x1, y1 = int(xmin * w), int(ymin * h)
        x2, y2 = int(xmax * w), int(ymax * h)

        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
        text = f"{label} {score:.0%}"
        bbox = draw.textbbox((x1, y1), text, font=font)
        draw.rectangle([bbox[0] - 1, bbox[1] - 1, bbox[2] + 1, bbox[3] + 1], fill=color)
        draw.text((x1, y1), text, fill=(0, 0, 0), font=font)

    return image


def capture_loop(picam2, imx500=None):
    """Continuously capture frames and store the latest JPEG."""
    global latest_frame
    while True:
        array = picam2.capture_array()
        image = Image.fromarray(array).convert("RGB")

        if SHOW_DETECTIONS and imx500 is not None:
            metadata = picam2.capture_metadata()
            np_outputs = imx500.get_outputs(metadata, add_batch=True)
            image = draw_detections(image, np_outputs)

        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=70)
        with frame_lock:
            latest_frame = buf.getvalue()

        time.sleep(0.05)


class MJPEGHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            mode = "with AI detections" if SHOW_DETECTIONS else "plain"
            html = f"""<!DOCTYPE html>
<html><head><title>TitPi Preview</title>
<style>body{{background:#111;color:#eee;text-align:center;font-family:sans-serif;margin:2em}}
img{{max-width:100%;border:2px solid #333;border-radius:8px}}</style></head>
<body><h2>TitPi Camera Preview ({mode})</h2>
<img src="/stream"><p>Adjust camera position and focus, then Ctrl+C to stop.</p></body></html>"""
            self.wfile.write(html.encode())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with frame_lock:
                        frame = latest_frame
                    if frame is None:
                        time.sleep(0.1)
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # suppress per-request logs


def main():
    imx500 = None
    if CAMERA_MODE == "imx500":
        print("Initializing IMX500 AI Sensor...")
        imx500 = IMX500(MODEL_PATH)
        picam2 = Picamera2(imx500.camera_num)
    else:
        print("Initializing Camera Module 3...")
        picam2 = Picamera2()

    config = picam2.create_preview_configuration(main={"size": STREAM_SIZE})
    picam2.configure(config)
    picam2.start()
    if CAMERA_MODE == "motion":
        from libcamera import controls
        picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
    time.sleep(2)

    # Start capture thread
    t = threading.Thread(target=capture_loop, args=(picam2, imx500), daemon=True)
    t.start()

    # Wait for first frame
    while latest_frame is None:
        time.sleep(0.1)

    import socket
    ip = socket.gethostbyname(socket.gethostname())
    mode_str = 'AI detections ON' if SHOW_DETECTIONS else f'plain video ({CAMERA_MODE})'
    print(f"\n--- TitPi Camera Preview ---")
    print(f"  Stream: http://{ip}:{STREAM_PORT}")
    print(f"  Mode:   {mode_str}")
    print(f"  Press Ctrl+C to stop.\n")

    server = HTTPServer(("0.0.0.0", STREAM_PORT), MJPEGHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping preview...")
    finally:
        server.server_close()
        picam2.stop()


if __name__ == "__main__":
    main()
