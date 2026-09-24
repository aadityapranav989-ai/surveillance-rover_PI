import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

PERSON_PROTOTXT = "mobilenet_ssd_deploy.prototxt"
PERSON_WEIGHTS = "mobilenet_ssd_iter_73000.caffemodel"
FACE_DETECTOR = "face_detection_yunet_2023mar.onnx"
FACE_RECOGNIZER = "face_recognition_sface_2021dec.onnx"
PERSON_CLASS_ID = 15  # "person" in the PASCAL VOC classes used by MobileNet-SSD
NAME_PATTERN = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

Box = Tuple[int, int, int, int]  # x, y, width, height in frame pixels


@dataclass
class Detection:
    box: Box
    score: float
    name: Optional[str] = None
    similarity: float = 0.0

    @property
    def area(self):
        return self.box[2] * self.box[3]

    def to_json(self):
        return {"box": list(self.box), "score": round(self.score, 3), "name": self.name,
                "similarity": round(self.similarity, 3)}


@dataclass
class VisionResult:
    frame_id: int
    timestamp: float
    width: int
    height: int
    persons: List[Detection] = field(default_factory=list)
    faces: List[Detection] = field(default_factory=list)


def _clip_box(x1, y1, x2, y2, width, height):
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(width, int(x2)), min(height, int(y2))
    return x1, y1, max(0, x2 - x1), max(0, y2 - y1)


class PersonDetector:
    """MobileNet-SSD when its model files are present, otherwise OpenCV's built-in HOG detector."""

    def __init__(self, models_dir, confidence):
        self.confidence = confidence
        prototxt = os.path.join(models_dir, PERSON_PROTOTXT)
        weights = os.path.join(models_dir, PERSON_WEIGHTS)
        self.net = None
        self.hog = None
        if not (os.path.exists(prototxt) and os.path.exists(weights)):
            self.backend = "hog (run scripts/download_models.sh for better accuracy)"
        elif not hasattr(cv2.dnn, "readNetFromCaffe"):
            self.backend = "hog (this OpenCV has no Caffe loader; install opencv-python-headless<5)"
        else:
            self.net = cv2.dnn.readNetFromCaffe(prototxt, weights)
            self.backend = "mobilenet-ssd"
        if self.net is None:
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame):
        height, width = frame.shape[:2]
        if self.net is not None:
            blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5)
            self.net.setInput(blob)
            output = self.net.forward()
            persons = []
            for _, class_id, score, x1, y1, x2, y2 in output[0, 0]:
                if int(class_id) != PERSON_CLASS_ID or score < self.confidence:
                    continue
                box = _clip_box(x1 * width, y1 * height, x2 * width, y2 * height, width, height)
                if box[2] > 0 and box[3] > 0:
                    persons.append(Detection(box, float(score)))
            return persons
        scale = min(1.0, 400 / width)
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
        rects, weights = self.hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.05)
        persons = []
        for (x, y, w, h), weight in zip(rects, np.ravel(weights)):
            if weight < self.confidence:
                continue
            box = _clip_box(x / scale, y / scale, (x + w) / scale, (y + h) / scale, width, height)
            persons.append(Detection(box, float(weight)))
        return persons


class FaceEngine:
    """YuNet face detection + SFace face embeddings."""

    def __init__(self, models_dir, confidence):
        self.detector = cv2.FaceDetectorYN.create(
            os.path.join(models_dir, FACE_DETECTOR), "", (320, 320), confidence, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(os.path.join(models_dir, FACE_RECOGNIZER), "")
        self._lock = threading.Lock()

    @staticmethod
    def available(models_dir):
        return all(os.path.exists(os.path.join(models_dir, name)) for name in (FACE_DETECTOR, FACE_RECOGNIZER))

    def detect(self, frame):
        """Returns an Nx15 array of YuNet rows: x, y, w, h, 5 landmark pairs, score."""
        height, width = frame.shape[:2]
        with self._lock:
            self.detector.setInputSize((width, height))
            _, faces = self.detector.detect(frame)
        return faces if faces is not None else np.empty((0, 15), dtype=np.float32)

    def embed(self, frame, face_row):
        with self._lock:
            aligned = self.recognizer.alignCrop(frame, face_row)
            feature = self.recognizer.feature(aligned)
        feature = np.asarray(feature, dtype=np.float32).flatten()
        return feature / (np.linalg.norm(feature) + 1e-9)


class FaceDatabase:
    """Known people stored as faces/<name>/<photo>.jpg on the Pi."""

    def __init__(self, faces_dir, engine, threshold):
        self.faces_dir = faces_dir
        self.engine = engine
        self.threshold = threshold
        self._people = {}
        self._lock = threading.Lock()

    def load(self):
        os.makedirs(self.faces_dir, exist_ok=True)
        people = {}
        for name in sorted(os.listdir(self.faces_dir)):
            folder = os.path.join(self.faces_dir, name)
            if not os.path.isdir(folder) or not NAME_PATTERN.match(name):
                continue
            embeddings = []
            for filename in sorted(os.listdir(folder)):
                if not filename.lower().endswith(IMAGE_EXTENSIONS):
                    continue
                image = cv2.imread(os.path.join(folder, filename))
                if image is None:
                    continue
                faces = self.engine.detect(image)
                if len(faces) == 0:
                    print(f"Faces: no face found in {name}/{filename}; skipping")
                    continue
                largest = max(faces, key=lambda row: row[2] * row[3])
                embeddings.append(self.engine.embed(image, largest))
            if embeddings:
                people[name] = embeddings
        with self._lock:
            self._people = people
        print(f"Faces: loaded {sum(len(e) for e in people.values())} photos of {len(people)} people")

    def summary(self):
        with self._lock:
            return [{"name": name, "samples": len(embeddings)} for name, embeddings in sorted(self._people.items())]

    def identify(self, embedding):
        best_name, best_similarity = None, 0.0
        with self._lock:
            for name, embeddings in self._people.items():
                similarity = float(max(np.dot(known, embedding) for known in embeddings))
                if similarity > best_similarity:
                    best_name, best_similarity = name, similarity
        if best_similarity < self.threshold:
            return None, best_similarity
        return best_name, best_similarity

    def enroll(self, name, frame, face_row):
        """Saves a padded crop of the face and adds its embedding. Returns the sample count for name."""
        embedding = self.engine.embed(frame, face_row)
        x, y, w, h = (int(v) for v in face_row[:4])
        height, width = frame.shape[:2]
        crop = frame[max(0, y - h // 2):min(height, y + h + h // 2), max(0, x - w // 2):min(width, x + w + w // 2)]
        folder = os.path.join(self.faces_dir, name)
        os.makedirs(folder, exist_ok=True)
        cv2.imwrite(os.path.join(folder, time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.jpg"), crop)
        with self._lock:
            self._people.setdefault(name, []).append(embedding)
            return len(self._people[name])

    def delete(self, name):
        with self._lock:
            found = self._people.pop(name, None) is not None
        folder = os.path.join(self.faces_dir, name)
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            found = True
        return found


class Vision(threading.Thread):
    """Runs person detection and face recognition on the newest camera frame."""

    def __init__(self, camera, models_dir, faces_dir, person_confidence, face_confidence,
                 face_threshold, max_fps, face_every=3):
        super().__init__(daemon=True, name="vision")
        self.camera = camera
        self.min_period = 1.0 / max_fps if max_fps > 0 else 0
        self.persons = PersonDetector(models_dir, person_confidence)
        self.faces = None
        self.database = None
        if FaceEngine.available(models_dir):
            self.faces = FaceEngine(models_dir, face_confidence)
            self.database = FaceDatabase(faces_dir, self.faces, face_threshold)
            self.database.load()
        else:
            print("Vision: face models missing; run scripts/download_models.sh to enable face recognition")
        self.fps = 0.0
        # Face recognition is the slowest step, so it runs on every Nth frame and the other
        # frames reuse its result; body detection (what follow mode steers by) runs every frame.
        self.face_every = max(1, face_every)
        self._frames = 0
        self._recent_faces = []
        self.timing = {"person_ms": 0.0, "faces_ms": 0.0}
        self.target_box = None  # set by the follow controller, drawn on the stream
        self.on_result = None  # called with each VisionResult, e.g. by the alert monitor
        self._latest = None
        self._lock = threading.Lock()

    @property
    def latest(self):
        with self._lock:
            return self._latest

    def run(self):
        last_id = 0
        while True:
            started = time.monotonic()
            frame_id, frame = self.camera.wait_frame(last_id, timeout=1.0)
            if frame is None:
                continue
            last_id = frame_id
            try:
                result = self.process(frame_id, frame)
            except cv2.error as error:
                print(f"Vision: {error}")
                time.sleep(1)
                continue
            with self._lock:
                self._latest = result
            if self.on_result is not None:
                self.on_result(result)
            elapsed = time.monotonic() - started
            if elapsed < self.min_period:
                time.sleep(self.min_period - elapsed)
            period = time.monotonic() - started
            self.fps = 0.8 * self.fps + 0.2 * (1.0 / period) if self.fps else 1.0 / period

    def process(self, frame_id, frame):
        height, width = frame.shape[:2]
        result = VisionResult(frame_id, time.monotonic(), width, height)
        started = time.monotonic()
        result.persons = self.persons.detect(frame)
        after_persons = time.monotonic()
        self._measure("person_ms", after_persons - started)
        if self.faces is not None:
            if self._frames % self.face_every == 0:
                faces = []
                for row in self.faces.detect(frame):
                    name, similarity = self.database.identify(self.faces.embed(frame, row))
                    box = _clip_box(row[0], row[1], row[0] + row[2], row[1] + row[3], width, height)
                    faces.append(Detection(box, float(row[14]), name, similarity))
                self._recent_faces = faces
                self._measure("faces_ms", time.monotonic() - after_persons)
            result.faces = list(self._recent_faces)
        self._frames += 1
        # A recognized face labels the body it sits in, so the body can be followed.
        # (Faces may be from a frame or two ago, which is close enough for this.)
        for face in result.faces:
            if face.name is None:
                continue
            fx, fy = face.box[0] + face.box[2] / 2, face.box[1] + face.box[3] / 2
            for person in result.persons:
                x, y, w, h = person.box
                if x <= fx <= x + w and y <= fy <= y + h * 0.6:
                    person.name = face.name
                    person.similarity = face.similarity
        return result

    def enroll(self, name):
        """Enrolls the single face currently in view. Returns (sample_count, error)."""
        if self.database is None:
            return 0, "face recognition models are not installed"
        if not NAME_PATTERN.match(name):
            return 0, "name must be 1-32 letters, digits, spaces, '-' or '_'"
        frame = self.camera.latest_frame()
        if frame is None:
            return 0, "camera is offline"
        faces = self.faces.detect(frame)
        if len(faces) == 0:
            return 0, "no face in view"
        if len(faces) > 1:
            return 0, "more than one face in view"
        return self.database.enroll(name, frame, faces[0]), None

    def _measure(self, key, seconds):
        milliseconds = seconds * 1000
        previous = self.timing[key]
        self.timing[key] = milliseconds if previous == 0 else 0.8 * previous + 0.2 * milliseconds

    def status(self):
        result = self.latest
        fresh = result is not None and time.monotonic() - result.timestamp < 2
        return {
            "fps": round(self.fps, 1) if fresh else 0,
            "timing": {key: round(value) for key, value in self.timing.items()},
            "person_backend": self.persons.backend,
            "face_recognition": self.database is not None,
            "persons": [p.to_json() for p in result.persons] if fresh else [],
            "faces": [f.to_json() for f in result.faces] if fresh else [],
        }

    def annotate(self, frame):
        result = self.latest
        if result is None or time.monotonic() - result.timestamp > 1.5:
            return frame
        for person in result.persons:
            x, y, w, h = person.box
            cv2.rectangle(frame, (x, y), (x + w, y + h), (80, 200, 80), 2)
            label = person.name or "person"
            cv2.putText(frame, f"{label} {person.score:.2f}", (x + 4, y + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 200, 80), 2)
        for face in result.faces:
            x, y, w, h = face.box
            color = (230, 180, 60) if face.name else (60, 60, 230)
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            label = f"{face.name} {face.similarity:.2f}" if face.name else "unknown"
            cv2.putText(frame, label, (x, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        target = self.target_box
        if target is not None:
            x, y, w, h = target
            cv2.rectangle(frame, (x - 3, y - 3), (x + w + 3, y + h + 3), (0, 215, 255), 3)
            cv2.drawMarker(frame, (x + w // 2, y + h // 2), (0, 215, 255), cv2.MARKER_CROSS, 24, 2)
        cv2.putText(frame, f"{self.fps:.1f} fps", (8, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return frame
