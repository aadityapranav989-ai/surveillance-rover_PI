import os

ESP32_URL = os.getenv("ESP32_URL", "http://esp32-rover.local").rstrip("/")
HOST = os.getenv("ROVER_HOST", "0.0.0.0")
PORT = int(os.getenv("ROVER_PORT", "8080"))
REQUEST_TIMEOUT = float(os.getenv("ESP32_TIMEOUT", "3"))
CAMERA_STREAM_URL = os.getenv("CAMERA_STREAM_URL", "").strip()
