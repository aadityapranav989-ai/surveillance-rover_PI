#!/bin/bash
# Undoes setup_hotspot.sh: the Pi stops hosting "TAPIR" and rejoins the ESP32's
# own network "ESP32-Robot" (old setup, or the new firmware's fallback network).
#
#   sudo nohup bash scripts/leave_hotspot.sh > ~/hotspot.log 2>&1 &
set -u
ENV_FILE="${ROVER_ENV_FILE:-/etc/default/rover-dashboard}"
if [ "$(id -u)" != 0 ]; then echo "Run with sudo."; exit 1; fi

sed -i "s#^ESP32_URL=.*#ESP32_URL=http://192.168.4.1#" "$ENV_FILE"
nmcli connection modify "ESP32-Robot" connection.autoconnect yes
nmcli connection delete "TAPIR" >/dev/null 2>&1
nmcli --wait 30 connection up "ESP32-Robot"
systemctl restart rover-dashboard
echo "$(date '+%T') Back on ESP32-Robot; the dashboard is at http://192.168.4.10:8080/"
