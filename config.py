import os
import sys

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

ESP32_URL = os.getenv("ESP32_URL", "http://192.168.50.2").rstrip("/")
HOST = os.getenv("ROVER_HOST", "0.0.0.0")
PORT = _int("ROVER_PORT", 8080)
REQUEST_TIMEOUT = _float("ESP32_TIMEOUT", 3)
# Rover location shown on the dashboard while the GPS module has no satellite fix,
# as "latitude,longitude" in decimal degrees (north and east positive).
ROVER_LOCATION = os.getenv("ROVER_LOCATION", "13.01425,80.19058")
# ESP32 motion calibration (DISTANCE_MS_PER_CM and TURN_MS_PER_DEGREE in its
# src/config.h), used to translate curved drives for firmware without /api/drive.
ESP32_MS_PER_CM = _float("ESP32_MS_PER_CM", 15.3)
ESP32_MS_PER_DEGREE = _float("ESP32_MS_PER_DEGREE", 11.4)

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
JPEG_QUALITY = _int("JPEG_QUALITY", 60)
# Bytes the Pi may queue per viewer before it starts skipping frames. Small
# values keep the stream live on slow Wi-Fi; large values add seconds of lag.
STREAM_SEND_BUFFER = _int("STREAM_SEND_BUFFER", 16384)

# Vision.
MODELS_DIR = os.getenv("MODELS_DIR", os.path.join(BASE_DIR, "models"))
FACES_DIR = os.getenv("FACES_DIR", os.path.join(BASE_DIR, "faces"))
VISION_MAX_FPS = _float("VISION_MAX_FPS", 8)
# Run face detection and recognition on every Nth frame only (the slowest step);
# body detection, which follow mode uses, still runs on every frame.
FACE_EVERY_N_FRAMES = _int("FACE_EVERY_N_FRAMES", 3)
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
# RFID authorisation (detection mode): when an unknown face appears, an
# authorised card must be tapped within AUTH_TIMEOUT seconds or the intruder
# alarm starts. A tapped card grants access for AUTH_GRANT_SECONDS.
AUTH_TIMEOUT = _float("AUTH_TIMEOUT", 10)
AUTH_GRANT_SECONDS = _float("AUTH_GRANT_SECONDS", 120)
# RC522 reader on the Pi's SPI bus 0, chip select 0 (/dev/spidev0.0).
RFID_ENABLED = _flag("RFID_ENABLED", "1" if sys.platform.startswith("linux") else "0")
RFID_SPI_BUS = _int("RFID_SPI_BUS", 0)
RFID_SPI_DEVICE = _int("RFID_SPI_DEVICE", 0)
CARDS_FILE = os.getenv("CARDS_FILE", os.path.join(BASE_DIR, "cards.json"))
# Show security status on the ESP32's 16x2 LCD.
LCD_ENABLED = _flag("LCD_ENABLED", "1")
# Remembers the alert mode (safe/detection) across restarts.
SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(BASE_DIR, "settings.json"))

# Follow mode. Each command runs for FOLLOW_COMMAND_MS, longer than the
# command interval, so the rover moves continuously instead of stop-start. If
# the Pi stops sending, the rover still halts within one command.
FOLLOW_INTERVAL = _float("FOLLOW_INTERVAL", 0.25)
FOLLOW_COMMAND_MS = _int("FOLLOW_COMMAND_MS", 700)
# Motor power while approaching (0-255): faster when the person is further away. Below
# about 100 a loaded rover barely moves.
FOLLOW_MIN_SPEED = _int("FOLLOW_MIN_SPEED", 120)
FOLLOW_MAX_SPEED = _int("FOLLOW_MAX_SPEED", 190)
# While approaching, the rover curves towards the person: the outer side runs
# up to FOLLOW_STEER_GAIN faster than the inner side when they are at the edge
# of the frame.
FOLLOW_STEER_GAIN = _int("FOLLOW_STEER_GAIN", 110)
# Once close, it turns on the spot to keep facing the person.
FOLLOW_TURN_MIN_SPEED = _int("FOLLOW_TURN_MIN_SPEED", 110)
FOLLOW_TURN_MAX_SPEED = _int("FOLLOW_TURN_MAX_SPEED", 160)
# When close, start turning on the spot once the person is this far off-center
# (fraction of the frame width), and stop once back within FOLLOW_CENTER_EXIT.
# FOLLOW_CENTER_EXIT is also the steering dead zone while approaching.
FOLLOW_CENTER_ENTER = _float("FOLLOW_CENTER_ENTER", 0.15)
FOLLOW_CENTER_EXIT = _float("FOLLOW_CENTER_EXIT", 0.06)
# Stop approaching once the target fills this fraction of the frame height,
# and drive again once it has shrunk by FOLLOW_RESUME_MARGIN of that. With a
# mast-mounted webcam (about 45 degrees vertical view) 0.9 stops about 2 m from
# a standing adult; the status line shows the current size to tune this.
FOLLOW_STOP_BODY_HEIGHT = _float("FOLLOW_STOP_BODY_HEIGHT", 0.9)
FOLLOW_STOP_FACE_HEIGHT = _float("FOLLOW_STOP_FACE_HEIGHT", 0.25)
FOLLOW_RESUME_MARGIN = _float("FOLLOW_RESUME_MARGIN", 0.1)
# Weight of each new detection when smoothing the target position (0..1).
FOLLOW_SMOOTHING = _float("FOLLOW_SMOOTHING", 0.5)
# Largest speed change between consecutive commands, so the rover eases in and out.
FOLLOW_MAX_SPEED_CHANGE = _int("FOLLOW_MAX_SPEED_CHANGE", 20)
# Keep driving on the last steering this long when the person is briefly not detected.
FOLLOW_LOST_GRACE = _float("FOLLOW_LOST_GRACE", 1.5)
# Stop if the newest detection result is older than this (vision running too slowly).
FOLLOW_VISION_TIMEOUT = _float("FOLLOW_VISION_TIMEOUT", 2.5)
# Keep tracking a recognized person's body for this long after their face
# was last recognized (for example when they turn away).
FOLLOW_TRACK_MEMORY = _float("FOLLOW_TRACK_MEMORY", 3)
