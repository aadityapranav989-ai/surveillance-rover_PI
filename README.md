# Raspberry Pi Rover Gateway

This repository runs on two machines:

- **Raspberry Pi (on the rover):** streams the USB webcam, serves the manual
  driving dashboard, and forwards drive commands to the ESP32. No video
  processing runs on the Pi.
- **Laptop (vision):** reads the Pi's camera stream, runs human body
  detection and face recognition, and in follow mode steers the rover
  towards a detected person by sending commands through the Pi.

The ESP32 remains responsible for motor control, timed stopping, watchdog
behavior, and GPS parsing.

```text
                 USB webcam
                     |
Laptop  <-- /camera stream --  Raspberry Pi :8080  -->  ESP32 :80  -->  motors + GPS
(vision,  --- follow cmds -->  (gateway, manual
 follow)                        dashboard)
```

Both roles run the same `app.py`. `VISION_ENABLED=1` selects the laptop
role; the Pi runs with vision off (the default).

## Network layout

The ESP32 creates the `ESP32-Robot` access point at `192.168.4.1`. The Pi and
the laptop join that Wi-Fi network directly; no router is needed. The Wi-Fi
password is set in the ESP32 firmware (`src/config.h`) and is not stored in
this repository.

```text
ESP32 access point: 192.168.4.1
Raspberry Pi:       192.168.4.10
Laptop:             DHCP address
```

Configure the Pi with a fixed Wi-Fi address. First find the active
connection name:

```bash
nmcli connection show --active
```

Replace `WIFI_CONNECTION_NAME` below with the Wi-Fi connection name:

```bash
sudo nmcli connection modify "WIFI_CONNECTION_NAME" ipv4.method manual ipv4.addresses 192.168.4.10/24 ipv4.gateway 192.168.4.1 ipv4.dns 192.168.4.1
sudo nmcli connection down "WIFI_CONNECTION_NAME"
sudo nmcli connection up "WIFI_CONNECTION_NAME"
hostname -I
```

The video travels over the ESP32's Wi-Fi, which has limited bandwidth. The
Pi captures from the webcam in its compressed MJPG mode at up to 30 fps and
queues at most about one frame per viewer (`STREAM_SEND_BUFFER`). When the
Wi-Fi can't carry every frame, frames are skipped instead of queued, so the
picture stays live (well under a second behind) rather than drifting
seconds behind. For a smoother picture on a busy network, lower
`JPEG_QUALITY` (for example 50) on the Pi. Watch the annotated video on the
laptop dashboard rather than opening the Pi's stream in extra browsers.

## Raspberry Pi setup

```bash
sudo apt update
sudo apt install -y git python3 python3-venv v4l-utils
sudo mkdir -p /opt
sudo git clone https://github.com/aadityapranav989-ai/surveillance-rover_PI.git /opt/raspberry-pi-rover
sudo chown -R pi:pi /opt/raspberry-pi-rover
cd /opt/raspberry-pi-rover
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The Pi uses OpenCV only to read and JPEG-encode webcam frames. It does not
need the vision models.

Create the environment file:

```bash
sudo tee /etc/default/rover-dashboard >/dev/null <<'EOF'
ESP32_URL=http://192.168.4.1
CAMERA_DEVICE=/dev/video0
CAMERA_WIDTH=640
CAMERA_HEIGHT=480
CAMERA_FPS=30
JPEG_QUALITY=60
ROVER_HOST=0.0.0.0
ROVER_PORT=8080
EOF
```

Install and start the service:

```bash
sudo cp systemd/rover-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rover-dashboard
sudo systemctl status rover-dashboard
```

The Pi dashboard at `http://192.168.4.10:8080/` has the raw camera, GPS,
manual driving, and a line showing whether the laptop is currently driving.

Check the webcam:

```bash
v4l2-ctl --list-devices
v4l2-ctl --list-formats-ext -d /dev/video0
```

## Laptop setup (vision)

Install once, while the laptop has internet access (Windows PowerShell shown;
use `.venv/bin/python` on macOS or Linux):

```powershell
git clone https://github.com/aadityapranav989-ai/surveillance-rover_PI.git
cd surveillance-rover_PI
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\download_models.py
.venv\Scripts\python -m unittest discover -s tests
```

`scripts/download_models.py` downloads and checksum-verifies the models into
`models/` (about 60 MB):

| Model | Purpose |
| --- | --- |
| MobileNet-SSD (Caffe) | Human body detection |
| YuNet | Face detection |
| SFace | Face recognition embeddings |

Without the models, body detection falls back to OpenCV's built-in HOG
detector (much less accurate), and face recognition is disabled.

To run it, join the laptop to `ESP32-Robot` and start the vision app:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_laptop.ps1
```

Open `http://127.0.0.1:8090/` on the laptop. The script points the app at the
Pi (`-PiUrl http://192.168.4.10:8080` by default), reads the camera from the
Pi's `/camera`, and only listens on the laptop itself. On macOS or Linux, the
equivalent is:

```bash
VISION_ENABLED=1 ESP32_URL=http://192.168.4.10:8080 CAMERA_STREAM_URL=http://192.168.4.10:8080/camera \
  ROVER_HOST=127.0.0.1 ROVER_PORT=8090 .venv/bin/python app.py
```

Set `CAMERA_HFOV_DEG` on the laptop to the rover webcam's horizontal field
of view (default 60). Follow mode uses it to turn the right number of degrees
towards a person.

## Face enrollment (laptop)

Face recognition identifies people enrolled on the laptop. To enroll someone:

1. Have only that person in front of the rover camera, facing it.
2. On the laptop dashboard under **Known faces**, type their name and press
   **Enroll face from camera**.
3. Repeat 3-5 times with different angles, distances, and lighting.

Photos are saved in `faces/<name>/` on the laptop. You can also copy photos
there (one clearly visible face per photo) and restart the app. The `faces/`
folder is excluded from Git. Face photos are personal data, so only enroll
people who have agreed to it, and delete them from the dashboard when they
are no longer needed.

A face is accepted as a match when its similarity is at least
`FACE_MATCH_THRESHOLD` (default `0.363`). Raise the threshold if the rover
confuses people; lower it if an enrolled person shows as `unknown`.

## Follow mode (laptop)

On the laptop dashboard under **Follow a person**, choose a target and press
**Start following**:

- **Anyone**: follows the closest (largest) detected person and sticks with
  that person while they stay in view.
- **A named person**: follows only a body whose face was recognized as that
  person. After recognition, the rover keeps tracking that body for up to
  `FOLLOW_TRACK_MEMORY` seconds if the person turns away. If only the face is
  visible, it steers towards the face.

Every `FOLLOW_INTERVAL` seconds the laptop sends one command through the Pi:

- Target off-center: turn `LEFT`/`RIGHT` by the target's angle from the
  center of the image, up to `FOLLOW_MAX_TURN_DEG` degrees.
- Target centered and far away: `FORWARD` by `FOLLOW_STEP_CM`. The rover moves
  faster when the person is farther away (`FOLLOW_MIN_SPEED` to
  `FOLLOW_MAX_SPEED`).
- Target fills `FOLLOW_STOP_BODY_HEIGHT` of the frame height: stop (close
  enough).
- Target lost, or the camera stream freezes: stop and wait.

Stopping follow mode:

- **STOP or any manual drive command on either dashboard** ends follow mode.
  The Pi then refuses further follow commands (HTTP 423) until follow mode is
  started again from the laptop. The laptop dashboard shows "stopped from the
  Pi dashboard".
- If the laptop crashes or leaves Wi-Fi, no new commands arrive. The rover
  finishes its last short step (at most `FOLLOW_STEP_CM` or
  `FOLLOW_MAX_TURN_DEG`) and stops.

Test with the wheels lifted first, then in an open area at a low
`FOLLOW_MAX_SPEED`. The ESP32 watchdog remains the last line of defense.

## Joystick navigation

Drag the stick away from the center to choose direction and speed:

- Up: forward
- Down: backward
- Left/right: turn
- Farther from center: faster movement

While the stick is held, the dashboard sends one short movement command every
150 ms, and never more than one at a time. Release the stick, move it to the
center, switch browser tabs, or press `STOP` to stop the ESP32. The D-pad
uses the same speed box as the joystick.

## Useful commands

On the Pi:

```bash
journalctl -u rover-dashboard -f
curl http://127.0.0.1:8080/api/status
curl http://127.0.0.1:8080/api/vision
curl -X POST "http://127.0.0.1:8080/api/command?direction=FORWARD&speed=120&value=20"
curl -X POST http://127.0.0.1:8080/api/stop
```

If the Pi firewall is enabled, allow the dashboard port:

```bash
sudo ufw allow 8080/tcp
```

All settings and their defaults are in `config.py`.

## API

Both roles serve these:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Dashboard |
| GET | `/camera` | MJPEG stream (raw on the Pi, annotated on the laptop) |
| GET | `/api/status` | ESP32 status (GPS), proxied |
| GET | `/api/vision` | Camera state, detections, follow state, enrolled faces, autopilot latch |
| POST | `/api/command?direction=&speed=&value=` | Manual move; ends follow mode |
| POST | `/api/stop` | Stop the rover; ends follow mode |

Pi gateway only:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/command?...&source=auto` | Follow-mode move from the laptop; `423` after an operator stop |
| POST | `/api/stop?source=auto` | Follow-mode stop; does not block follow mode |
| POST | `/api/autopilot/resume` | Allow follow-mode commands again (sent when follow mode starts) |

Laptop only:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/follow?target=NAME` | Start following `NAME`, or anyone when empty |
| POST | `/api/follow/stop` | End follow mode |
| POST | `/api/faces/enroll?name=NAME` | Enroll the single face in view |
| POST | `/api/faces/delete?name=NAME` | Delete an enrolled person |

For moves, `value` is centimeters for FORWARD/BACKWARD and degrees for
LEFT/RIGHT. When the ESP32 rejects a command, its status code and body are
passed back unchanged. The Pi answers `502` only when it cannot reach the
ESP32.

Anyone on the `ESP32-Robot` Wi-Fi network can use the Pi dashboard, so keep
a strong Wi-Fi password on the ESP32.

## ESP32 prerequisite

The ESP32 repository must already be flashed with its AP-only dashboard
firmware. The Pi gateway expects these endpoints:

- `GET /api/status`
- `POST /api/command?direction=FORWARD&speed=120&value=20`
- `POST /api/stop`
