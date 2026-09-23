# Raspberry Pi Rover Gateway

This repository runs the Raspberry Pi side of the rover system. It hosts the browser dashboard and forwards navigation requests to the ESP32 over HTTP. The ESP32 remains responsible for motor control, timed stopping, watchdog behavior, and GPS parsing.

## Network layout

Recommended layout:

```text
Laptop browser -> Raspberry Pi:8080 -> ESP32:80
                                      -> motors + GPS
```

Both the Pi and ESP32 must be reachable from the same network. If the ESP32 is using its fallback access point, join the Pi to `ESP32-Robot` with password `robot123`; the ESP32 address is `192.168.4.1`. The laptop can join that same access point and browse to the Pi's address.

For normal operation, set Wi-Fi credentials in the ESP32 `src/config.h` and connect the Pi and laptop to that shared router. Use `http://raspberrypi.local:8080/` for the Pi dashboard instead of depending on its changing numeric IP.

## Keep the Pi address stable

The Pi's mDNS hostname is:

```text
raspberrypi.local
```

Open the dashboard from the laptop at:

```text
http://raspberrypi.local:8080/
```

For a stable numeric address as well, create a DHCP reservation in the
router. Find the Pi MAC address:

```bash
cat /sys/class/net/wlan0/address
hostname -I
ip route
```

In the router's DHCP/LAN settings, reserve the current Pi MAC address at
`192.168.192.105`. The router currently appears to be `192.168.192.156`.
Do not configure a random static address that might already belong to another
device. After saving the reservation, renew the Pi lease:

```bash
sudo nmcli connection show --active
sudo nmcli device reapply wlan0
hostname -I
```

If the router does not support reservations, keep using
`http://raspberrypi.local:8080/`.

## Move this repository to the Pi

From the development computer, create a Git repository and push it to a private Git host:

```powershell
cd "D:\Desktop\Hackathon\ESP32 human detection\RaspberryPiRover"
git init
git add .
git commit -m "Add Raspberry Pi rover gateway"
git branch -M main
git remote add origin <your-private-repository-url>
git push -u origin main
```

On the headless Pi:

```bash
sudo apt update
sudo apt install -y git python3 avahi-daemon libnss-mdns
sudo mkdir -p /opt
sudo git clone <your-private-repository-url> /opt/raspberry-pi-rover
sudo chown -R pi:pi /opt/raspberry-pi-rover
```

Do not put passwords or private Wi-Fi credentials in Git. The repository uses only Python's standard library, so no pip package installation is required.

## Configure and run manually

```bash
cd /opt/raspberry-pi-rover
ESP32_URL=http://esp32-rover.local python3 app.py
```

From the laptop, open `http://<PI_IP>:8080/`.

To configure a different ESP32 address:

```bash
ESP32_URL=http://192.168.1.50 python3 app.py
```

## Phone camera preview

Install a phone camera app that provides an MJPEG stream, then set its stream
URL in `/etc/default/rover-dashboard`. Common apps expose URLs similar to
`http://PHONE_IP:8080/video` or `http://PHONE_IP:8080/stream`.

```ini
CAMERA_STREAM_URL=http://PHONE_IP:8080/video
```

The phone, Pi, and ESP32 must be on the same Wi-Fi network. Restart the
service and reload the Pi dashboard:

```bash
sudo systemctl restart rover-dashboard
```

This first phase only displays the stream. It does not yet detect people or
move the rover automatically.

## Joystick navigation

The dashboard uses a virtual joystick instead of directional buttons. Drag
the stick away from the center to choose direction and speed:

- Up: forward
- Down: backward
- Left/right: turn
- Farther from center: faster movement

The Pi sends short repeated movement pulses while the stick is held. Release
the stick, move it to center, switch browser tabs, or press `STOP` to stop the
ESP32. Test with the wheels lifted first.

## Run automatically with systemd

Create the environment file:

```bash
sudo tee /etc/default/rover-dashboard >/dev/null <<'EOF'
ESP32_URL=http://esp32-rover.local
CAMERA_STREAM_URL=
ROVER_HOST=0.0.0.0
ROVER_PORT=8080
EOF
```

For a stable ESP32 address, use the mDNS name `esp32-rover.local`:

```ini
ESP32_URL=http://esp32-rover.local
```

After flashing the updated ESP32 firmware, verify name resolution from the Pi:

```bash
getent hosts esp32-rover.local
curl --max-time 5 http://esp32-rover.local/api/status
```

If the name does not resolve, reserve the ESP32's MAC address in the router's
DHCP settings and use the reserved `192.168.192.x` address instead. The mDNS
name is convenient, but a DHCP reservation is the most predictable option.

Install and start the service:

```bash
sudo cp systemd/rover-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rover-dashboard
sudo systemctl status rover-dashboard
```

Useful headless commands:

```bash
journalctl -u rover-dashboard -f
curl http://127.0.0.1:8080/api/status
curl -X POST "http://127.0.0.1:8080/api/command?direction=FORWARD&speed=120&value=20"
curl -X POST http://127.0.0.1:8080/api/stop
```

If the Pi firewall is enabled, allow the dashboard port:

```bash
sudo ufw allow 8080/tcp
```

## ESP32 prerequisite

The ESP32 repository must already be flashed with its Wi-Fi dashboard firmware. Set `WIFI_SSID` and `WIFI_PASSWORD` in its `src/config.h`, or use its fallback access point. The Pi gateway expects these endpoints:

- `GET /api/status`
- `POST /api/command?direction=FORWARD&speed=120&value=20`
- `POST /api/stop`
