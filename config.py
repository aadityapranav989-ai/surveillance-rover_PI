import os

ESP32_URL = os.getenv("ESP32_URL", "http://192.168.4.1").rstrip("/")
HOST = os.getenv("ROVER_HOST", "0.0.0.0")
PORT = int(os.getenv("ROVER_PORT", "8080"))
REQUEST_TIMEOUT = float(os.getenv("ESP32_TIMEOUT", "3"))
CAMERA_STREAM_URL = os.getenv("CAMERA_STREAM_URL", "").strip()
CAMERA_DEVICE = os.getenv("CAMERA_DEVICE", "/dev/video0")
CAMERA_WIDTH = os.getenv("CAMERA_WIDTH", "640")
CAMERA_HEIGHT = os.getenv("CAMERA_HEIGHT", "480")
CAMERA_FPS = os.getenv("CAMERA_FPS", "5")
