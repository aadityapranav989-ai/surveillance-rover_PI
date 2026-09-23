"""Downloads the body detection and face recognition models into models/.

Only needed on the machine that runs vision (the laptop). Works on Windows,
macOS and Linux:  python scripts/download_models.py
"""
import hashlib
import os
import sys
from urllib.request import urlopen

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODELS = [
    ("mobilenet_ssd_deploy.prototxt",
     "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/deploy.prototxt",
     "2d180f723b3109e21f8287f6b3c691390d07b60eed998327cd3259ffa0e50608"),
    ("mobilenet_ssd_iter_73000.caffemodel",
     "https://github.com/chuanqi305/MobileNet-SSD/raw/master/mobilenet_iter_73000.caffemodel",
     "52eed8be80522c152a17fb56740de705b79881bde1a167e0e747310523685fc7"),
    ("face_detection_yunet_2023mar.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    ("face_recognition_sface_2021dec.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    for name, url, expected in MODELS:
        path = os.path.join(MODELS_DIR, name)
        if os.path.exists(path) and sha256(path) == expected:
            print(f"ok        {name}")
            continue
        print(f"download  {name}")
        partial = path + ".part"
        with urlopen(url, timeout=60) as response, open(partial, "wb") as file:
            while block := response.read(1 << 20):
                file.write(block)
        if sha256(partial) != expected:
            os.remove(partial)
            sys.exit(f"checksum mismatch for {name}; not installed")
        os.replace(partial, path)
    print(f"Models ready in {MODELS_DIR}")


if __name__ == "__main__":
    main()
