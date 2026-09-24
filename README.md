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

The Raspberry Pi hosts the rover Wi-Fi, `TAPIR`. The ESP32 and the laptop
join it; no router is needed.

```text
Raspberry Pi (hotspot):  192.168.50.1   dashboard http://192.168.50.1:8080/
ESP32:                   192.168.50.2   (fixed, set in its firmware)
Laptop:                  DHCP address from the Pi
Wi-Fi password:          same as ROVER_WIFI_PASSWORD in the ESP32's src/secrets.h
```

Video goes straight from the Pi to the laptop over the Pi's radio, and the
ESP32 only receives small drive commands (UDP). Earlier, the ESP32 hosted
the network (`ESP32-Robot`) and relayed every video frame through its small
radio, so both the video and the controls lagged.

### Switching the Pi to host the Wi-Fi

1. Flash the ESP32 with the current firmware first. It looks for `TAPIR`;
   until it finds it, it keeps its own `ESP32-Robot` network open after 30
   seconds, so the Pi stays connected meanwhile.
2. On the Pi (from this folder), with the same password as the ESP32's
   `secrets.h`:

   ```bash
   sudo nohup bash scripts/setup_hotspot.sh 'WIFI_PASSWORD' > ~/hotspot.log 2>&1 &
   ```

   An SSH session over Wi-Fi drops at this point. The script creates the
   hotspot (2.4 GHz, channel 6, WPA2), points the dashboard at the ESP32's
   new address, and restarts the service. If the hotspot does not come up
   within 30 seconds, it returns the Pi to the network it was on.
3. Join the laptop to `TAPIR` and open `http://192.168.50.1:8080/`. SSH is
   now `ssh pi@192.168.50.1`. Within about 30 seconds the ESP32 joins too;
   check with `cat ~/hotspot.log` and
   `journalctl -u rover-dashboard -n 20 --no-pager | grep "drive commands"`
   (`UDP port 4210` means the Pi reaches the ESP32).

To go back to the old setup: `sudo nohup bash scripts/leave_hotspot.sh > ~/hotspot.log 2>&1 &`.

While the Pi hosts the Wi-Fi it has no Wi-Fi internet; plug in an Ethernet
cable when it needs to download updates. With Ethernet connected, it also
shares that internet with the laptop.

The Pi queues at most about one video frame per viewer
(`STREAM_SEND_BUFFER`), so on a busy network frames are skipped instead of
piling up, and the picture stays live. For a lighter stream, lower
`JPEG_QUALITY` (for example 50).

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
ESP32_URL=http://192.168.50.2
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

The Pi dashboard at `http://192.168.50.1:8080/` has the raw camera, GPS,
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

To run it, join the laptop to `TAPIR` and start the vision app:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_laptop.ps1
```

Open `http://127.0.0.1:8090/` on the laptop. The script points the app at the
Pi (`-PiUrl http://192.168.50.1:8080` by default), reads the camera from the
Pi's `/camera`, and only listens on the laptop itself. On macOS or Linux, the
equivalent is:

```bash
VISION_ENABLED=1 ESP32_URL=http://192.168.50.1:8080 CAMERA_STREAM_URL=http://192.168.50.1:8080/camera \
  ROVER_HOST=127.0.0.1 ROVER_PORT=8090 .venv/bin/python app.py
```

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

## Security mode, RFID authorisation and the LCD

Under the video, choose a mode:

- **Safe mode (no alerts)**: the default. Faces are still detected and
  labelled, but nothing is checked and nothing alerts.
- **Detection mode**: when a face that is not enrolled appears, the rover
  asks for an RFID card:
  1. The LCD shows `UNKNOWN PERSON` / `Tap card: 10s` counting down, and the
     dashboard shows an amber countdown banner and beeps once.
  2. An authorised card tapped on the reader within `AUTH_TIMEOUT` (10 s)
     shows `ACCESS GRANTED` and a green banner. Unknown faces are then
     allowed for `AUTH_GRANT_SECONDS` (2 minutes).
  3. No authorised card in time: the dashboard raises the red **ACCESS DENIED
     — INTRUDER DETECTED** alarm with repeated beeps, and the LCD shows
     `INTRUDER` / `DETECTED`. Tapping
     an authorised card still cancels it.
  4. An unregistered card shows `ACCESS DENIED` on the LCD and a red
     **ACCESS DENIED** banner on the dashboard for 5 seconds, and changes
     nothing else.

  The check ends by itself if the unknown person leaves (no unknown face for
  `ALERT_CLEAR_AFTER`, 2 s). Enrolled faces are never challenged.

The mode is shared by everyone viewing the dashboard and is saved in
`settings.json`, so it survives restarts. Browsers only play sounds after
you have clicked somewhere on the page once. Checks need a visible face: a
person facing away from the camera is detected as a body but not identified.

### Authorised cards

In the dashboard's **RFID access** panel, type the card holder's name, click
**Add card**, and tap the card on the reader within 20 seconds. Cards are
saved on the Pi in `cards.json` (not in Git); **Delete** removes one. The
panel also shows whether the reader is working and the last card tapped.
The reader handles the 4-byte-ID MIFARE cards and key fobs that come with
RC522 kits.

### RC522 reader wiring (Raspberry Pi)

Power the RC522 from **3.3 V only**; 5 V damages it.

| RC522 pin | Raspberry Pi pin |
| --- | --- |
| SDA (SS) | GPIO8 / CE0 (pin 24) |
| SCK | GPIO11 (pin 23) |
| MOSI | GPIO10 (pin 19) |
| MISO | GPIO9 (pin 21) |
| IRQ | not connected |
| GND | GND (pin 20) |
| RST | 3.3 V (pin 17) |
| 3.3V | 3.3 V (pin 1) |

Turn on SPI once, install the SPI package into the Python environment, and
restart:

```bash
sudo raspi-config nonint do_spi 0
sudo reboot
```

```bash
cd /opt/raspberry-pi-rover && .venv/bin/pip install -r requirements.txt
sudo systemctl restart rover-dashboard
journalctl -u rover-dashboard -n 20 --no-pager | grep RFID
```

The log shows `RFID: RC522 ready`. If it shows `not found`, check the wiring
and that `/dev/spidev0.0` exists. RFID runs on the machine that runs vision;
set `RFID_ENABLED=0` to turn it off.

### 16x2 LCD (on the ESP32)

The LCD with an I2C backpack connects to the ESP32 (wiring in the ESP32
repository's README). The Pi sends it the current status twice a second
when it changes: `SAFE MODE`, `DETECTION MODE`, `UNKNOWN PERSON` with the
countdown, `ACCESS GRANTED`, `INTRUDER DETECTED`, `WELCOME` with the name of a
recognized person, `FOLLOWING`, and so on. If
the Pi stops sending for 10 seconds, the ESP32 shows its own status instead
of a stale message. Set `LCD_ENABLED=0` to stop the Pi sending.

## Follow mode

On the dashboard (the Pi's, or the laptop's when vision runs there) under
**Follow target**, choose a target and press **Start following**:

- **Anyone**: follows the closest (largest) detected person and sticks with
  that person while they stay in view.
- **A named person**: follows only a body whose face was recognized as that
  person. After recognition, the rover keeps tracking that body for up to
  `FOLLOW_TRACK_MEMORY` seconds if the person turns away. If only the face is
  visible, it steers towards the face.

Every `FOLLOW_INTERVAL` (0.25 s) one command is sent. Each command runs for
`FOLLOW_COMMAND_MS` (0.6 s), so the next one arrives before it ends and the
rover moves continuously instead of stop-start:

- Approaching: the rover drives towards the person in a curve. Both sides
  move forward and the side away from the person runs faster (up to
  `FOLLOW_STEER_GAIN` faster at the edge of the frame), so it bends towards
  them in one motion. It is faster when the person is farther away
  (`FOLLOW_MIN_SPEED` to `FOLLOW_MAX_SPEED`).
- Target fills `FOLLOW_STOP_BODY_HEIGHT` of the frame height: stop (close
  enough). It drives again once the person has moved away by
  `FOLLOW_RESUME_MARGIN`, so it does not creep back and forth. While close,
  it turns on the spot (`FOLLOW_TURN_MIN_SPEED` to `FOLLOW_TURN_MAX_SPEED`)
  to keep facing the person once they move beyond `FOLLOW_CENTER_ENTER`.
- Target not detected for a moment: keep going for up to
  `FOLLOW_LOST_GRACE` (0.8 s) instead of stopping on every missed frame.
  Lost for longer, or the camera stream freezes: stop and wait.

To keep this smooth, the target's position is averaged across frames
(`FOLLOW_SMOOTHING`) and the speed changes by at most
`FOLLOW_MAX_SPEED_CHANGE` per command.

Drive commands go from the Pi to the ESP32 as UDP packets (port 4210) when
the ESP32 firmware supports it. Over HTTP, each command opens a new
connection, and on the rover's busy Wi-Fi a lost connection-setup packet
delays a command by a full second, which makes the rover stop and lurch. A
lost UDP packet costs nothing: the next command replaces it. STOP is sent
over both UDP and HTTP. The browser also keeps one connection open to the Pi
for all joystick commands. The Pi logs `ESP32 drive commands: UDP port 4210`
at start-up, or `HTTP` with older firmware.

Curved driving uses the ESP32's `/api/drive` command. With older ESP32
firmware the Pi falls back to straight and spin commands automatically
(and logs a notice), so flash the latest firmware for smooth curves.

Stopping follow mode:

- **STOP or any manual drive command on either dashboard** ends follow mode.
  The Pi then refuses further follow commands (HTTP 423) until follow mode is
  started again from the laptop. The laptop dashboard shows "stopped from the
  Pi dashboard".
- If the laptop crashes or leaves Wi-Fi, no new commands arrive. The rover
  finishes its last step (about 0.6 s) and stops.

Test with the wheels lifted first, then in an open area at a low
`FOLLOW_MAX_SPEED`. The ESP32 watchdog remains the last line of defense.

## Joystick navigation

Drag the stick away from the center to choose direction and speed:

- Up: forward
- Down: backward
- Diagonal: drive in a curve (a slight push sideways gives a gentle curve)
- Fully left/right: turn on the spot
- Farther from center: faster movement

While the stick is held, the dashboard sends one short movement command every
150 ms, and never more than one at a time. Each command sets the left and
right wheel speeds separately (`/api/drive`), and the ESP32 ramps between
speeds, so movement is smooth. Release the stick, move it to the
center, switch browser tabs, or press `STOP` to stop the ESP32. The D-pad
uses the same speed box as the joystick.

## Location

The Telemetry panel shows the rover's location. While the GPS module has no
satellite fix (for example indoors) it shows `ROVER_LOCATION`, which defaults
to 13.01425° N, 80.19058° E; set it in `/etc/default/rover-dashboard` as
`ROVER_LOCATION=latitude,longitude` (decimal degrees, south and west
negative). Once the GPS gets a fix, the live position is shown instead,
labelled "GPS FIX" with the number of satellites.

## GTA driving mode

The third manual mode, **GTA**, drives like a car in GTA Vice City:

| Key | On screen | Action |
| --- | --- | --- |
| W or ↑ | Gas | Speed builds up while held; let go and it coasts down |
| S or ↓ | Brake / Rev | Brakes; once stopped, reverses (up to half speed) |
| A D or ← → | ◀ ▶ | Steer; the path bends while moving, more at higher speed |
| Space | Handbrake | Stops at once |

A game controller (Xbox, PlayStation or generic, by Bluetooth or USB) works
too: right trigger gas (analog, so a light press drives slowly), left
trigger brake / reverse, left stick steer, A / ✕ handbrake. The GTA panel
shows when one is connected, and disconnecting it stops the rover.

**Phone controller:** on a phone, choose GTA mode and turn the phone sideways:
the dashboard becomes a GTA-mobile-style controller over the live camera,
with ◀ ▶ steering under the left thumb, ▲ GAS, ▼ BRAKE and HB (handbrake)
under the right, the power and gear top right, and STOP / EXIT top left.
Buttons can be held together (gas + steer). Turning back to portrait shows
the normal dashboard. The **Full-screen phone controller** button opens it
too, and on Android also hides the browser bars. On a laptop, GTA mode stays
a panel on the dashboard. It uses the
same video stream as the dashboard, so it adds no network load.

Like a car it cannot turn on the spot, and in reverse the nose swings the
other way. The display shows the power (percent of the speed box) and the
gear (D / N / R). Keys only work while GTA mode is selected and you are not
typing in a text box. Switching modes, switching browser tabs, the handbrake
and the emergency STOP all stop the rover.

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
| POST | `/api/drive?left=&right=&ms=` | Manual move with separate wheel speeds (-255..255) for up to 1000 ms; ends follow mode |
| POST | `/api/stop` | Stop the rover; ends follow mode |

Pi gateway only:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/command?...&source=auto` | Follow-mode move from the laptop; `423` after an operator stop |
| POST | `/api/stop?source=auto` | Follow-mode stop; does not block follow mode |
| POST | `/api/autopilot/resume` | Allow follow-mode commands again (sent when follow mode starts) |
| POST | `/api/lcd?line1=&line2=` | Pass LCD text from a laptop running vision to the ESP32 |

Laptop only:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/follow?target=NAME` | Start following `NAME`, or anyone when empty |
| POST | `/api/follow/stop` | End follow mode |
| POST | `/api/faces/enroll?name=NAME` | Enroll the single face in view |
| POST | `/api/faces/delete?name=NAME` | Delete an enrolled person |
| POST | `/api/cards/enroll?name=NAME` | Save the next RFID card tapped (within 20 s) for NAME |
| POST | `/api/cards/cancel` | Stop waiting for a card to add |
| POST | `/api/cards/delete?uid=UID` | Remove an authorised card |
| POST | `/api/alerts/mode?mode=safe\|detection` | Switch unknown-person alerts off or on |

For moves, `value` is centimeters for FORWARD/BACKWARD and degrees for
LEFT/RIGHT. When the ESP32 rejects a command, its status code and body are
passed back unchanged. The Pi answers `502` only when it cannot reach the
ESP32.

Anyone on the `TAPIR` Wi-Fi network can use the Pi dashboard, so keep
a strong Wi-Fi password on the ESP32.

## ESP32 prerequisite

The ESP32 repository must already be flashed with its AP-only dashboard
firmware. The Pi gateway expects these endpoints:

- `GET /api/status`
- `POST /api/command?direction=FORWARD&speed=120&value=20`
- `POST /api/stop`
