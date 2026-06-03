import time
import os
import sys
import json
import signal
import logging
import numpy as np
from collections import deque
import statistics
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
#from notifier import send_detection_email
from notifier_lcd import send_detection_email
import threading as _threading
from bird_id import identify_image

# Rate-limit GPT calls: at most one per 30s across all detections
_gpt_lock = _threading.Lock()
_gpt_last_call = 0.0
GPT_MIN_INTERVAL = 30.0  # seconds

def _rate_limited_identify(photo_path, label):
    global _gpt_last_call
    with _gpt_lock:
        since = time.time() - _gpt_last_call
        if since < GPT_MIN_INTERVAL:
            wait = GPT_MIN_INTERVAL - since
            log.info(f"[GPT] Throttling: waiting {wait:.1f}s before next API call")
            time.sleep(wait)
        _gpt_last_call = time.time()
    return identify_image(photo_path, label)
import database

# --- Configuration ---
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH) as _f:
    _cfg = json.load(_f)["camera"]

DEBUG = "--debug" in sys.argv
CAMERA_MODE   = _cfg.get("mode", "imx500")  # "imx500" or "motion"
CONFIDENCE    = _cfg.get("confidence_threshold", 0.35)
SAVE_DIR      = _cfg.get("save_directory", "/home/titpi/titpi/detections")
MODEL_PATH    = _cfg.get("model_path")
MIN_VID       = _cfg.get("video_min_duration", 10)
MAX_VID       = _cfg.get("video_max_duration", 120)
IDLE_TIMEOUT  = _cfg.get("video_idle_timeout", 5)
PHOTO_DELAY   = _cfg.get("photo_delay", 0.5)
PHOTO_INTERVAL= _cfg.get("photo_interval", 3)
CONFIRM_FRAMES= _cfg.get("confirmation_frames", 3)
COOLDOWN      = _cfg.get("cooldown", 15)
LOG_PATH      = _cfg.get("log_file", os.path.join(SAVE_DIR, "watcher.log"))
TARGET_LABELS = set(_cfg.get("target_labels", ["bird"]))
SPIKE_THRESHOLD = _cfg.get("spike_threshold", 0.20)
BASELINE_FRAMES = _cfg.get("baseline_frames", 60)
# Motion mode settings
MOTION_THRESHOLD = _cfg.get("motion_threshold", 5.0)  # mean pixel diff to count as motion
MOTION_MIN_AREA  = _cfg.get("motion_min_area", 0.01)   # fraction of frame that must change

os.makedirs(SAVE_DIR, exist_ok=True)

# --- Logging (file + stdout, only our logger) ---
log = logging.getLogger("watcher")
log.setLevel(logging.DEBUG if DEBUG else logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_fh = logging.FileHandler(LOG_PATH)
_fh.setFormatter(_fmt)
log.addHandler(_sh)
log.addHandler(_fh)

# COCO labels
COCO = {
    0: "person", 1: "person", 2: "bicycle", 3: "car", 4: "motorcycle",
    5: "airplane", 6: "bus", 7: "train", 8: "truck", 9: "boat",
    10: "traffic light", 11: "fire hydrant", 13: "stop sign",
    14: "parking meter", 15: "bench", 16: "bird", 17: "cat", 18: "dog",
    19: "horse", 20: "sheep", 21: "cow", 22: "elephant", 23: "bear",
    24: "zebra", 25: "giraffe", 27: "backpack", 28: "umbrella",
    31: "handbag", 32: "tie", 33: "suitcase", 34: "frisbee", 35: "skis",
    36: "snowboard", 37: "sports ball", 38: "kite", 39: "baseball bat",
    40: "baseball glove", 41: "skateboard", 42: "surfboard",
    43: "tennis racket", 44: "bottle", 46: "wine glass", 47: "cup",
    48: "fork", 49: "knife", 50: "spoon", 51: "bowl", 52: "banana",
    53: "apple", 54: "sandwich", 55: "orange", 56: "broccoli",
    57: "carrot", 58: "hot dog", 59: "pizza", 60: "donut", 61: "cake",
    62: "chair", 63: "couch", 64: "potted plant", 65: "bed",
    67: "dining table", 70: "toilet", 72: "tv", 73: "laptop", 74: "mouse",
    75: "remote", 76: "keyboard", 77: "cell phone", 78: "microwave",
    79: "oven", 80: "toaster", 81: "sink", 82: "refrigerator", 84: "book",
    85: "clock", 86: "vase", 87: "scissors", 88: "teddy bear",
    89: "hair drier", 90: "toothbrush",
}

# --- Camera setup ---
imx500 = None
prev_frame = None

if CAMERA_MODE == "imx500":
    from picamera2.devices import IMX500
    log.info("Initializing IMX500...")
    imx500 = IMX500(MODEL_PATH)
    picam2 = Picamera2(imx500.camera_num)
else:
    log.info("Initializing Camera Module 3 (motion mode)...")
    picam2 = Picamera2()

preview_cfg = picam2.create_preview_configuration(
    main={"size": (640, 480)},
    lores={"size": (320, 240), "format": "YUV420"} if CAMERA_MODE == "motion" else {},
)
still_cfg = picam2.create_still_configuration(main={"size": (2028, 1520)})
picam2.configure(preview_cfg)
picam2.start()
if CAMERA_MODE == "motion":
    from libcamera import controls
    picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
time.sleep(2)
database.init_db()

log.info("--- Watcher Active ---")
log.info(f"Mode: {CAMERA_MODE}")
if CAMERA_MODE == "imx500":
    log.info(f"Model: {os.path.basename(MODEL_PATH)}")
log.info(f"Targets: {', '.join(TARGET_LABELS)} | Threshold: {CONFIDENCE}")
log.info(f"Save: {SAVE_DIR} | Log: {LOG_PATH}")

# If capture_metadata() hangs (camera crash), exit so systemd can restart us
def _timeout_exit(signum, frame):
    log.error("Camera hung (no response for 15s), exiting for restart...")
    os._exit(1)  # Force exit, bypass finally (picam2.stop() would also hang)

signal.signal(signal.SIGALRM, _timeout_exit)


def get_best_target(np_outputs):
    """Return (label, score) of the best target detection, or (None, 0)."""
    scores = np_outputs[1][0]
    classes = np_outputs[2][0].astype(int)
    n = int(np_outputs[3][0].item())

    if DEBUG and int(time.time()) % 5 == 0 and getattr(get_best_target, '_t', 0) != int(time.time()):
        get_best_target._t = int(time.time())
        top = min(3, n)
        parts = [f"{COCO.get(int(classes[i]), '?')}: {scores[i]:.2f}" for i in range(top)]
        log.debug(f"Top {top}: {', '.join(parts)}")

    best_label, best_score = None, 0.0
    for i in range(n):
        s = float(scores[i])
        if s < CONFIDENCE:
            continue
        lbl = COCO.get(int(classes[i]))
        if lbl and lbl in TARGET_LABELS and s > best_score:
            best_label, best_score = lbl, s
    return best_label, best_score


def get_top_score(np_outputs):
    """Return (label, score) of the single highest-scoring detection, any class."""
    scores = np_outputs[1][0]
    classes = np_outputs[2][0].astype(int)
    n = int(np_outputs[3][0].item())

    if DEBUG and int(time.time()) % 5 == 0 and getattr(get_top_score, '_t', 0) != int(time.time()):
        get_top_score._t = int(time.time())
        top = min(3, n)
        parts = [f"{COCO.get(int(classes[i]), '?')}: {scores[i]:.2f}" for i in range(top)]
        log.debug(f"Top {top}: {', '.join(parts)}")

    if n == 0:
        return None, 0.0
    best_i = int(np.argmax(scores[:n]))
    return COCO.get(int(classes[best_i]), "?"), float(scores[best_i])


def get_motion_score():
    """Compute motion magnitude from consecutive lores frames. Returns (label, score)."""
    global prev_frame
    lores = picam2.capture_array("lores")
    # Use only Y channel (first 320x240 bytes of YUV420)
    gray = lores[:240, :320].astype(np.float32) if lores.ndim == 3 else lores.astype(np.float32)
    if len(gray.shape) == 3:
        gray = gray[:, :, 0]

    if prev_frame is None:
        prev_frame = gray
        return None, 0.0

    diff = np.abs(gray - prev_frame)
    prev_frame = gray

    # Fraction of pixels that changed significantly
    changed = np.count_nonzero(diff > (MOTION_THRESHOLD * 255 / 100)) / diff.size
    # Mean intensity change as a 0–1 score
    mean_change = float(diff.mean()) / 255.0

    # Combined score: needs both enough area changing and enough magnitude
    if changed < MOTION_MIN_AREA:
        return None, 0.0

    score = min(1.0, mean_change * 10)  # scale up to usable range
    if DEBUG and int(time.time()) % 5 == 0:
        log.debug(f"Motion: changed={changed:.3f} mean={mean_change:.4f} score={score:.2f}")

    return "motion", score


def capture_photo(path):
    """Capture a high-res still. Falls back to preview resolution on failure."""
    try:
        picam2.switch_mode_and_capture_file(still_cfg, path)
        picam2.stop()
        picam2.configure(preview_cfg)
        picam2.start()
        if CAMERA_MODE == "motion":
            from libcamera import controls
            picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
        time.sleep(1)
        # Flush stale frames so encoder starts with clean timestamps
        for _ in range(5):
            signal.alarm(15)
            picam2.capture_metadata()
            signal.alarm(0)
        return True
    except Exception as e:
        log.warning(f"Still capture failed ({e}), using preview capture")
        try:
            picam2.stop()
        except Exception:
            pass
        picam2.configure(preview_cfg)
        picam2.start()
        time.sleep(0.5)
        try:
            picam2.capture_file(path)
            return True
        except Exception as e2:
            log.error(f"Preview capture also failed: {e2}")
            return False


def record_video(path, label, ts, baseline_median=0.0):
    """Record video while target is still visible. Returns (video_path, extra_photos)."""
    extra_photos = []
    try:
        encoder = H264Encoder(bitrate=1000000)
        output = FfmpegOutput(path, audio=False)
        picam2.start_encoder(encoder, output)

        rec_start = time.time()
        last_activity = rec_start
        last_photo = rec_start
        count = 0

        while True:
            elapsed = time.time() - rec_start
            if elapsed >= MAX_VID:
                log.info(f"Max video duration ({MAX_VID}s)")
                break

            if time.time() - last_photo >= PHOTO_INTERVAL:
                count += 1
                p = f"{SAVE_DIR}/{label}_{ts}_{count}.jpg"
                try:
                    picam2.capture_file(p)
                    extra_photos.append(p)
                except Exception:
                    pass
                last_photo = time.time()

            if CAMERA_MODE == "imx500":
                signal.alarm(15)
                meta = picam2.capture_metadata()
                out = imx500.get_outputs(meta, add_batch=True)
                signal.alarm(0)
                if out is not None:
                    _, score = get_top_score(out)
                    if score > baseline_median:
                        last_activity = time.time()
            else:
                _, score = get_motion_score()
                if score > baseline_median:
                    last_activity = time.time()

            if elapsed >= MIN_VID and (time.time() - last_activity) >= IDLE_TIMEOUT:
                log.info(f"No activity for {IDLE_TIMEOUT}s, stopping")
                break

            time.sleep(0.1)

        picam2.stop_encoder(encoder)
        log.info(f"Video saved: {path} ({time.time() - rec_start:.0f}s, {len(extra_photos)} photos)")
        return path, extra_photos
    except Exception as e:
        log.error(f"Video recording failed: {e}")
        return None, extra_photos


def identify_best(photo_path, extra_photos, label):
    """Run identification on photos, return best_species dict.
    
    Tries each photo with local classifier first. If a confident local
    match is found, returns immediately. Only falls back to GPT on the
    single best-quality photo if local fails on all of them.
    """
    all_photos = [photo_path] + extra_photos
    best_species, best_photo, best_score = None, photo_path, -1

    # Pass 1: try local classifier on each photo (fast, no network)
    for p in all_photos:
        try:
            from PIL import Image as _Image
            _img = _Image.open(p)
            _img.save(p, quality=95, subsampling=0)
        except Exception:
            pass
        result = _rate_limited_identify(p, label)
        if result and result.get("score", 0) > best_score:
            best_score = result["score"]
            best_species = result
            best_photo = p
            log.info(f"Best photo: {os.path.basename(p)} (conf={best_score:.0%})")
            # If local classifier is confident, stop early
            if result.get("source") == "local" and best_score >= 0.3:
                break

    # Clean up extras we didn't pick
    for p in extra_photos:
        if p != best_photo:
            try:
                os.remove(p)
            except OSError:
                pass

    # Move best to the canonical path
    if best_photo != photo_path:
        try:
            os.replace(best_photo, photo_path)
        except OSError:
            pass

    return best_species


# --- Main loop ---
baseline = deque(maxlen=BASELINE_FRAMES)
spike_streak = 0
last_capture = 0
last_output_change = time.time()
last_output_sig = None
FROZEN_TIMEOUT = 120  # exit if model output unchanged for 2 minutes

try:
    while True:
        if CAMERA_MODE == "imx500":
            signal.alarm(15)
            meta = picam2.capture_metadata()
            out = imx500.get_outputs(meta, add_batch=True)
            signal.alarm(0)

            if out is None:
                time.sleep(0.1)
                continue

            # Detect frozen AI pipeline: same output for too long
            sig = bytes(out[1][0].data) if hasattr(out[1][0], 'data') else out[1][0].tobytes()
            if sig != last_output_sig:
                last_output_sig = sig
                last_output_change = time.time()
            elif time.time() - last_output_change > FROZEN_TIMEOUT:
                log.error(f"AI pipeline frozen for {FROZEN_TIMEOUT}s, exiting for restart...")
                os._exit(1)

            top_label, top_score = get_top_score(out)
        else:
            top_label, top_score = get_motion_score()
            if top_label is None:
                time.sleep(0.1)
                continue

        # Update rolling baseline (frozen while a spike is active)
        if spike_streak == 0 and top_score > 0:
            baseline.append(top_score)

        # Spike detection: trigger when top score exceeds baseline by threshold
        calibrated = len(baseline) >= BASELINE_FRAMES // 2
        baseline_median = statistics.median(baseline) if baseline else 0.0
        if calibrated and top_score > baseline_median + SPIKE_THRESHOLD:
            spike_streak += 1
            if DEBUG:
                log.debug(f"Spike! {top_label}={top_score:.2f} baseline={baseline_median:.2f} streak={spike_streak}")
        else:
            spike_streak = 0

        confirmed = top_label if spike_streak >= CONFIRM_FRAMES else None
        conf_score = top_score

        if not confirmed or (time.time() - last_capture) < COOLDOWN:
            time.sleep(0.1)
            continue

        # --- Detection confirmed ---
        ts = time.strftime("%Y%m%d-%H%M%S")
        photo_path = f"{SAVE_DIR}/visitor_{ts}.jpg"
        video_path = f"{SAVE_DIR}/visitor_{ts}.mp4"

        log.info(f"VISITOR detected ({top_label}, score={conf_score:.2f}, baseline={baseline_median:.2f})")
        spike_streak = 0
        detection_start = time.time()

        if PHOTO_DELAY > 0:
            time.sleep(PHOTO_DELAY)

        if not capture_photo(photo_path):
            last_capture = time.time()
            continue

        video_path, extra_photos = record_video(video_path, confirmed, ts, baseline_median)
        visit_duration = time.time() - detection_start
        species = identify_best(photo_path, extra_photos, confirmed)

        # If neither local model nor GPT identified anything, discard as false positive
        if not species:
            log.info("No species identified by local model or GPT — discarding false positive.")
            for f in [photo_path, video_path]:
                try:
                    if f and os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass
            last_capture = time.time()
            continue

        send_detection_email("visitor", conf_score, photo_path, video_path, species)
        database.log_detection(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            species=species.get("name") if species else None,
            common_name=species.get("common_name") if species else None,
            gpt_confidence=species.get("score") if species else None,
            spike_score=conf_score,
            baseline_score=baseline_median,
            photo_path=photo_path,
            video_path=video_path,
            visit_duration=visit_duration,
        )
        last_capture = time.time()

except KeyboardInterrupt:
    log.info("Shutting down...")
finally:
    picam2.stop()
