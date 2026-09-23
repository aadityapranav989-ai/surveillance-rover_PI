import threading
import time

import cv2


class Camera(threading.Thread):
    """Owns the only handle to the webcam and shares frames with every consumer.

    The raw frame goes to the vision worker; an annotated JPEG is encoded once
    per frame and shared by all dashboard viewers.
    """

    def __init__(self, source, width, height, fps, jpeg_quality=70, fourcc="MJPG"):
        super().__init__(daemon=True, name="camera")
        self.source = source
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.fourcc = fourcc
        self.annotate = None
        self.error = "starting"
        self._condition = threading.Condition()
        self._frame = None
        self._frame_id = 0
        self._jpeg = None

    @property
    def online(self):
        return self.error is None

    def _open(self):
        source = self.source
        local = source.isdigit() or source.startswith("/dev/")
        if source.isdigit():
            capture = cv2.VideoCapture(int(source), cv2.CAP_V4L2)
        elif local:
            capture = cv2.VideoCapture(source, cv2.CAP_V4L2)
        else:
            capture = cv2.VideoCapture(source)
        if local and self.fourcc:
            # Must be set before the frame size for V4L2 to pick the matching mode.
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def run(self):
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        while True:
            capture = self._open()
            if not capture.isOpened():
                self.error = f"cannot open {self.source}"
                print(f"Camera: {self.error}; retrying")
                capture.release()
                time.sleep(2)
                continue
            self.error = None
            code = int(capture.get(cv2.CAP_PROP_FOURCC))
            fourcc = "".join(chr((code >> shift) & 0xFF) for shift in (0, 8, 16, 24)).strip("\x00") or "?"
            print(f"Camera: streaming from {self.source} "
                  f"({int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))} "
                  f"{fourcc} @ {capture.get(cv2.CAP_PROP_FPS):.0f} fps)")
            while True:
                ok, frame = capture.read()
                if not ok:
                    self.error = f"lost {self.source}"
                    print(f"Camera: {self.error}; reopening")
                    break
                shown = frame
                if self.annotate is not None:
                    shown = self.annotate(frame.copy())
                ok, jpeg = cv2.imencode(".jpg", shown, encode_params)
                with self._condition:
                    self._frame = frame
                    self._jpeg = jpeg.tobytes() if ok else self._jpeg
                    self._frame_id += 1
                    self._condition.notify_all()
            capture.release()
            time.sleep(1)

    def wait_frame(self, last_id, timeout=1.0):
        """Waits for a frame newer than last_id. Returns (frame_id, frame); frame may be None on timeout."""
        with self._condition:
            self._condition.wait_for(lambda: self._frame_id != last_id, timeout)
            if self._frame_id == last_id:
                return last_id, None
            return self._frame_id, self._frame

    def wait_jpeg(self, last_id, timeout=1.0):
        with self._condition:
            self._condition.wait_for(lambda: self._frame_id != last_id, timeout)
            if self._frame_id == last_id:
                return last_id, None
            return self._frame_id, self._jpeg

    def latest_frame(self):
        with self._condition:
            return self._frame
