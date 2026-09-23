# Raspberry Pi Rover Gateway

This repository runs the Raspberry Pi side of the rover system. It hosts the browser dashboard and forwards navigation requests to the ESP32 over HTTP. The ESP32 remains responsible for motor control, timed stopping, watchdog behavior, and GPS parsing.

## Network layout

Recommended layout:

```text
Laptop browser -> Raspberry Pi:8080 -> ESP32:80
                                      -> motors + GPS
```

Both the Pi and ESP32 must be reachable from the same network. If the ESP32 is using its fallback access point, join the Pi to `ESP32-Robot` with password `robot123`; the ESP32 address is `192.168.4.1`. The laptop can join that same access point and browse to the Pi's address.

For normal operation, set Wi-Fi credentials in the ESP32 `src/config.h` and connect the Pi and laptop to that shared router. Find the Pi address with `hostname -I`.

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
sudo apt install -y git python3
sudo mkdir -p /opt
sudo git clone <your-private-repository-url> /opt/raspberry-pi-rover
sudo chown -R pi:pi /opt/raspberry-pi-rover
```

Do not put passwords or private Wi-Fi credentials in Git. The repository uses only Python's standard library, so no pip package installation is required.

## Configure and run manually

```bash
cd /opt/raspberry-pi-rover
ESP32_URL=http://192.168.4.1 python3 app.py
```

From the laptop, open `http://<PI_IP>:8080/`.

To configure a different ESP32 address:

```bash
ESP32_URL=http://192.168.1.50 python3 app.py
```

## Run automatically with systemd

Create the environment file:

```bash
sudo tee /etc/default/rover-dashboard >/dev/null <<'EOF'
ESP32_URL=http://192.168.4.1
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
