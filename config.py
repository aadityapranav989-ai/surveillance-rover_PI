import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _float(name, default):
    return float(os.getenv(name, str(default)))


def _int(name, default):
    return int(os.getenv(name, str(default)))


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# The same app runs in two roles:
# - Pi gateway (VISION_ENABLED=0, the default): streams the webcam and forwards
#   drive commands to the ESP32. No detection runs on the Pi.
# - Laptop vision (VISION_ENABLED=1): reads the Pi's /camera stream, runs body
#   detection, face recognition and follow mode, and drives through the Pi.
#   Set ESP32_URL to the Pi gateway, e.g. http://192.168.4.10:8080.
VISION_ENABLED = _flag("VISION_ENABLED", "0")

ESP32_URL = os.getenv("ESP32_URL", "http://192.168.4.1").rstrip("/")
HOST = os.getenv("ROVER_HOST", "0.0.0.0")
PORT = _int("ROVER_PORT", 8080)
REQUEST_TIMEOUT = _float("ESP32_TIMEOUT", 3)

# Camera. CAMERA_STREAM_URL (a phone/network MJPEG stream) takes priority over
# the local USB webcam at CAMERA_DEVICE. Either way the Pi reads the frames,
# runs detection on them and serves the annotated stream at /camera.
CAMERA_STREAM_URL = os.getenv("CAMERA_STREAM_URL", "").strip()
CAMERA_DEVICE = os.getenv("CAMERA_DEVICE", "/dev/video0")
CAMERA_SOURCE = CAMERA_STREAM_URL or CAMERA_DEVICE
CAMERA_WIDTH = _int("CAMERA_WIDTH", 640)
CAMERA_HEIGHT = _int("CAMERA_HEIGHT", 480)
# Capture rate. The stream drops frames the Wi-Fi can't carry, so asking the
# webcam for its full rate costs no latency.
CAMERA_FPS = _int("CAMERA_FPS", 30)
# MJPG is the webcam's compressed mode: most USB webcams only reach ~5 fps at
# 640x480 in the uncompressed YUYV mode but 30 fps in MJPG. Set to YUYV if
# your webcam has no MJPG mode.
CAMERA_FOURCC = os.getenv("CAMERA_FOURCC", "MJPG").strip().upper()
# Horizontal field of view of the webcam, used to turn the rover by the right
# number of degrees towards a person. Most USB webcams are 55-70 degrees.
CAMERA_HFOV_DEG = _float("CAMERA_HFOV_DEG", 60)
JPEG_QUALITY = _int("JPEG_QUALITY", 60)
# Bytes the Pi may queue per viewer before it starts skipping frames. Small
# values keep the stream live on slow Wi-Fi; large values add seconds of lag.
STREAM_SEND_BUFFER = _int("STREAM_SEND_BUFFER", 16384)

# Vision.
MODELS_DIR = os.getenv("MODELS_DIR", os.path.join(BASE_DIR, "models"))
FACES_DIR = os.getenv("FACES_DIR", os.path.join(BASE_DIR, "faces"))
VISION_MAX_FPS = _float("VISION_MAX_FPS", 8)
# CPU cores OpenCV may use for detection. Left unset, detection grabs every
# core and the camera thread waits, dropping the stream below 30 fps on a Pi.
VISION_THREADS = _int("VISION_THREADS", 2)
PERSON_CONFIDENCE = _float("PERSON_CONFIDENCE", 0.5)
FACE_CONFIDENCE = _float("FACE_CONFIDENCE", 0.8)
# Cosine similarity needed to accept a face as a known person. 0.363 is the
# threshold published with the SFace model; raise it to reduce false matches.
FACE_MATCH_THRESHOLD = _float("FACE_MATCH_THRESHOLD", 0.363)

# Unknown-person alerts (detection mode): an alert starts on the first
# unrecognized face and clears once none has been seen for this many seconds.
ALERT_CLEAR_AFTER = _float("ALERT_CLEAR_AFTER", 2)
# Remembers the alert mode (safe/detection) across restarts.
SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(BASE_DIR, "settings.json"))

# Follow mode.
FOLLOW_INTERVAL = _float("FOLLOW_INTERVAL", 0.3)
FOLLOW_MIN_SPEED = _int("FOLLOW_MIN_SPEED", 90)
FOLLOW_MAX_SPEED = _int("FOLLOW_MAX_SPEED", 150)
FOLLOW_TURN_SPEED = _int("FOLLOW_TURN_SPEED", 110)
# 20 cm is ~306 ms on the ESP32, just longer than FOLLOW_INTERVAL, so the
# rover drives smoothly instead of stop-start.
FOLLOW_STEP_CM = _float("FOLLOW_STEP_CM", 20)
FOLLOW_MAX_TURN_DEG = _float("FOLLOW_MAX_TURN_DEG", 25)
# Person must be within this fraction of the frame width from center before
# the rover drives forward instead of turning.
FOLLOW_CENTER_TOLERANCE = _float("FOLLOW_CENTER_TOLERANCE", 0.12)
# Stop approaching once the target fills this fraction of the frame height.
FOLLOW_STOP_BODY_HEIGHT = _float("FOLLOW_STOP_BODY_HEIGHT", 0.8)
FOLLOW_STOP_FACE_HEIGHT = _float("FOLLOW_STOP_FACE_HEIGHT", 0.25)
# Keep tracking a recognized person's body for this long after their face
# was last recognized (for example when they turn away).
FOLLOW_TRACK_MEMORY = _float("FOLLOW_TRACK_MEMORY", 3)
