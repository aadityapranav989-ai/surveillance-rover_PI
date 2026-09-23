import html
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse

import config
import esp32
from alerts import AlertMonitor
from camera import Camera
from follow import OPERATOR_STOP, FollowController, FollowSettings

with open(os.path.join(config.BASE_DIR, "static", "dashboard.html"), encoding="utf-8") as dashboard_file:
    DASHBOARD = dashboard_file.read().replace("{{ESP32_URL}}", html.escape(config.ESP32_URL)).encode()


class Autopilot:
    """Operator-stop latch for follow-mode commands (source=auto) arriving from the laptop.

    A STOP or manual drive command from any dashboard blocks follow-mode
    commands until follow mode is started again, so the laptop cannot keep
    driving after someone at the Pi dashboard has taken over.
    """

    def __init__(self):
        self.blocked = False
        self.last_command = 0.0
        self._lock = threading.Lock()

    def block(self):
        with self._lock:
            self.blocked = True

    def resume(self):
        with self._lock:
            self.blocked = False

    def allow_command(self):
        with self._lock:
            if self.blocked:
                return False
            self.last_command = time.monotonic()
            return True

    def status(self):
        return {"blocked": self.blocked, "active": time.monotonic() - self.last_command < 2}


class LCDStatusPublisher(threading.Thread):
    def __init__(self, vision, follow, alerts):
        super().__init__(daemon=True, name="lcd-status")
        self.vision = vision
        self.follow = follow
        self.alerts = alerts

    def status(self):
        alert = self.alerts.status()
        if alert["active"]:
            return "INTRUDER", f'{alert["faces"]} UNKNOWN'

        follow = self.follow.status()
        target = follow["target"] or "ANYONE"
        if follow["enabled"]:
            state = follow["state"]
            if state == "searching":
                return "SEARCHING", target
            if state.startswith("tracking"):
                return "CHASING", target
            if state == "reached target":
                return "TARGET FOUND", target
            return "FOLLOWING", target

        result = self.vision.latest
        if result is not None:
            names = [person.name for person in result.persons if person.name]
            if names:
                return "IDENTIFIED", names[0]
            if result.persons:
                return "PERSON DETECTED", f'{len(result.persons)} IN VIEW'
        return "WATCHING", "NO PERSON"

    def run(self):
        while True:
            try:
                esp32.send_lcd_status(*self.status())
            except (URLError, OSError):
                pass
            time.sleep(0.5)


class RoverHandler(BaseHTTPRequestHandler):
    camera = None
    vision = None  # None on the Pi gateway; vision runs on the laptop
    follow = None
    alerts = None
    autopilot = Autopilot()

    def send_payload(self, status, payload, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, data):
        self.send_payload(status, json.dumps(data).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_payload(200, DASHBOARD, "text/html; charset=utf-8")
        elif path == "/api/status":
            self.proxy("/api/status", "GET")
        elif path == "/api/vision":
            vision = self.vision
            self.send_json(200, {
                "camera": {"online": self.camera.online, "error": self.camera.error},
                "vision": vision.status() if vision else None,
                "follow": self.follow.status() if self.follow else None,
                "known_faces": vision.database.summary() if vision and vision.database else [],
                "autopilot": self.autopilot.status(),
                "alerts": self.alerts.status() if self.alerts else None,
            })
        elif path == "/camera":
            self.stream_camera()
        else:
            self.send_json(404, {"error": "not found"})

    def stream_camera(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # Keep at most about one frame queued for this viewer. When the Wi-Fi is
        # slower than the camera, the write below blocks and the next loop sends
        # the newest frame, so the picture stays live instead of falling behind.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.STREAM_SEND_BUFFER)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        last_id = 0
        try:
            while True:
                last_id, jpeg = self.camera.wait_jpeg(last_id, timeout=5)
                if jpeg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def stop_following(self, reason):
        if self.follow is not None:
            self.follow.disable(reason)

    def do_POST(self):
        parsed = urlparse(self.path)
        values = parse_qs(parsed.query)

        def param(name):
            return values.get(name, [""])[0].strip()

        automatic = param("source") == esp32.AUTO
        if parsed.path == "/api/lcd/status":
            state, detail = param("state"), param("detail")
            if not state or not detail:
                self.send_json(400, {"error": "state and detail are required"})
                return
            self.proxy("/api/lcd/status?" + urlencode({"state": state, "detail": detail}), "POST")
        elif parsed.path == "/api/stop":
            if not automatic:
                self.autopilot.block()
                self.stop_following("stopped")
            self.proxy("/api/stop", "POST")
        elif parsed.path == "/api/command":
            direction = param("direction").upper()
            try:
                speed = int(param("speed"))
                value = float(param("value"))
            except ValueError:
                self.send_json(400, {"error": "invalid speed or value"})
                return
            if direction not in esp32.DIRECTIONS or not 1 <= speed <= 255 or value <= 0:
                self.send_json(400, {"error": "invalid navigation command"})
                return
            if automatic:
                if not self.autopilot.allow_command():
                    self.send_json(OPERATOR_STOP, {"error": "follow mode stopped by an operator"})
                    return
            else:
                self.autopilot.block()
                self.stop_following("manual control")
            self.proxy("/api/command?" + urlencode({"direction": direction, "speed": speed, "value": value}), "POST")
        elif parsed.path == "/api/drive":
            try:
                left, right, ms = int(param("left")), int(param("right")), int(param("ms"))
            except ValueError:
                self.send_json(400, {"error": "left, right and ms must be whole numbers"})
                return
            if not (-255 <= left <= 255 and -255 <= right <= 255 and 1 <= ms <= 1000):
                self.send_json(400, {"error": "left and right must be -255..255 and ms 1..1000"})
                return
            if automatic:
                if not self.autopilot.allow_command():
                    self.send_json(OPERATOR_STOP, {"error": "follow mode stopped by an operator"})
                    return
            else:
                self.autopilot.block()
                self.stop_following("manual control")
            self.relay(esp32.drive, left, right, ms)
        elif parsed.path == "/api/autopilot/resume":
            self.autopilot.resume()
            self.send_json(200, self.autopilot.status())
        elif parsed.path.startswith(("/api/follow", "/api/faces", "/api/alerts")) and self.vision is None:
            self.send_json(404, {"error": "vision runs on the laptop, not on this gateway"})
        elif parsed.path == "/api/alerts/mode":
            try:
                self.alerts.set_mode(param("mode").lower())
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, self.alerts.status())
        elif parsed.path == "/api/follow":
            target = param("target")
            known = {person["name"] for person in self.vision.database.summary()} if self.vision.database else set()
            if target and target not in known:
                self.send_json(400, {"error": f"unknown person '{target}'"})
                return
            self.follow.enable(target)
            self.send_json(200, self.follow.status())
        elif parsed.path == "/api/follow/stop":
            self.follow.disable("off")
            self.send_json(200, self.follow.status())
        elif parsed.path == "/api/faces/enroll":
            samples, error = self.vision.enroll(param("name"))
            if error:
                self.send_json(400, {"error": error})
            else:
                self.send_json(200, {"name": param("name"), "samples": samples})
        elif parsed.path == "/api/faces/delete":
            name = param("name")
            if self.vision.database is None or not self.vision.database.delete(name):
                self.send_json(404, {"error": f"unknown person '{name}'"})
                return
            if self.follow.target_name == name:
                self.follow.disable("target deleted")
            self.send_json(200, {"deleted": name})
        else:
            self.send_json(404, {"error": "not found"})

    def proxy(self, path, method):
        self.relay(esp32.esp32_request, path, method)

    def relay(self, function, *args):
        """Sends a request to the rover and passes its answer back to the browser."""
        try:
            status, payload, content_type = function(*args)
            self.send_payload(status, payload, content_type)
        except (URLError, TimeoutError, OSError) as error:
            self.send_json(502, {"error": "rover unavailable", "detail": str(error)})

    def log_message(self, format, *args):
        # Dashboard polling, the camera stream and follow-mode pulses would flood the journal.
        if self.path.startswith(("/api/status", "/api/vision", "/camera")) or "source=auto" in self.path:
            return
        print("%s - %s" % (self.address_string(), format % args))


def start_vision(camera):
    # OpenCV models are only loaded where vision runs.
    import cv2
    from vision import Vision

    cv2.setNumThreads(config.VISION_THREADS)
    vision = Vision(camera, config.MODELS_DIR, config.FACES_DIR, config.PERSON_CONFIDENCE,
                    config.FACE_CONFIDENCE, config.FACE_MATCH_THRESHOLD, config.VISION_MAX_FPS)
    camera.annotate = vision.annotate
    follow = FollowController(vision, FollowSettings(
        min_speed=config.FOLLOW_MIN_SPEED,
        max_speed=config.FOLLOW_MAX_SPEED,
        turn_min_speed=config.FOLLOW_TURN_MIN_SPEED,
        turn_max_speed=config.FOLLOW_TURN_MAX_SPEED,
        steer_gain=config.FOLLOW_STEER_GAIN,
        command_ms=config.FOLLOW_COMMAND_MS,
        center_enter=config.FOLLOW_CENTER_ENTER,
        center_exit=config.FOLLOW_CENTER_EXIT,
        stop_body_height=config.FOLLOW_STOP_BODY_HEIGHT,
        stop_face_height=config.FOLLOW_STOP_FACE_HEIGHT,
        resume_margin=config.FOLLOW_RESUME_MARGIN,
        smoothing=config.FOLLOW_SMOOTHING,
        max_speed_change=config.FOLLOW_MAX_SPEED_CHANGE,
        lost_grace=config.FOLLOW_LOST_GRACE,
        track_memory=config.FOLLOW_TRACK_MEMORY,
        interval=config.FOLLOW_INTERVAL,
    ))
    alerts = AlertMonitor(config.SETTINGS_FILE, config.ALERT_CLEAR_AFTER)
    vision.on_result = alerts.update
    vision.start()
    follow.start()
    print(f"Person detection: {vision.persons.backend}; face recognition: {'on' if vision.database else 'off'}")
    print(f"Unknown-person alerts: {alerts.mode} mode")
    return vision, follow, alerts


def main():
    camera = Camera(config.CAMERA_SOURCE, config.CAMERA_WIDTH, config.CAMERA_HEIGHT,
                    config.CAMERA_FPS, config.JPEG_QUALITY, config.CAMERA_FOURCC)
    RoverHandler.camera = camera
    if config.VISION_ENABLED:
        RoverHandler.vision, RoverHandler.follow, RoverHandler.alerts = start_vision(camera)
        LCDStatusPublisher(RoverHandler.vision, RoverHandler.follow, RoverHandler.alerts).start()
    camera.start()

    server = ThreadingHTTPServer((config.HOST, config.PORT), RoverHandler)
    server.daemon_threads = True
    role = "vision on" if config.VISION_ENABLED else "gateway, vision off"
    print(f"Rover dashboard [{role}] listening on http://{config.HOST}:{config.PORT}")
    print(f"Camera: {config.CAMERA_SOURCE}; forwarding commands to {config.ESP32_URL}")

    def on_sigterm(*_):
        raise KeyboardInterrupt

    # systemd stops the service with SIGTERM; unwind so a following rover gets a stop command.
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if RoverHandler.follow is not None:
            RoverHandler.follow.disable("shutdown")
        # Skip interpreter teardown: the camera thread is still inside OpenCV,
        # and destroying it underneath that thread aborts the process (SIGABRT).
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
