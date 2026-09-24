#!/bin/bash
# Makes this Raspberry Pi host the rover Wi-Fi "TAPIR". The ESP32 (new firmware)
# and the laptop join it: the Pi is 192.168.50.1, the ESP32 192.168.50.2.
#
#   sudo nohup bash scripts/setup_hotspot.sh 'WIFI_PASSWORD' > ~/hotspot.log 2>&1 &
#
# Use the same password as ROVER_WIFI_PASSWORD in the ESP32's src/secrets.h.
# Run it with nohup as shown: an SSH session over Wi-Fi drops when the Pi
# switches networks. If the hotspot does not come up within 30 s, the Pi goes
# back to the Wi-Fi it was on before, so it stays reachable.
set -u
PASSWORD="${1:-}"
SSID="TAPIR"
PI_ADDRESS="192.168.50.1/24"
ESP32_URL="http://192.168.50.2"
ENV_FILE="${ROVER_ENV_FILE:-/etc/default/rover-dashboard}"

if [ "$(id -u)" != 0 ]; then echo "Run with sudo."; exit 1; fi
if [ ${#PASSWORD} -lt 8 ]; then
  echo "Give the Wi-Fi password (8+ characters), the same as ROVER_WIFI_PASSWORD in the ESP32's secrets.h."
  exit 1
fi

previous=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0" {print $1; exit}')
previous_url=$(grep -s '^ESP32_URL=' "$ENV_FILE" | cut -d= -f2-)
echo "$(date '+%T') Current Wi-Fi: ${previous:-none}; current ESP32_URL: ${previous_url:-default}"

nmcli connection delete "$SSID" >/dev/null 2>&1
nmcli connection add type wifi ifname wlan0 con-name "$SSID" ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  802-11-wireless.powersave 2 \
  ipv4.method shared ipv4.addresses "$PI_ADDRESS" ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.psk "$PASSWORD" \
  connection.autoconnect yes connection.autoconnect-priority 100 || exit 1

# Point the dashboard at the ESP32's new address.
if grep -q '^ESP32_URL=' "$ENV_FILE" 2>/dev/null; then
  sed -i "s#^ESP32_URL=.*#ESP32_URL=$ESP32_URL#" "$ENV_FILE"
else
  echo "ESP32_URL=$ESP32_URL" >> "$ENV_FILE"
fi

echo "$(date '+%T') Starting hotspot $SSID (Wi-Fi SSH sessions will drop now)"
if nmcli --wait 30 connection up "$SSID"; then
  # Keep other saved Wi-Fi networks from taking wlan0 back after a reboot.
  nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless" {print $1}' | while IFS= read -r name; do
    [ "$name" != "$SSID" ] && nmcli connection modify "$name" connection.autoconnect no
  done
  systemctl restart rover-dashboard
  echo "$(date '+%T') Hotspot $SSID is up. Join it and open http://192.168.50.1:8080/"
  exit 0
fi

echo "$(date '+%T') Hotspot did not start; going back to ${previous:-the previous Wi-Fi}"
nmcli connection delete "$SSID" >/dev/null 2>&1
if [ -n "$previous_url" ]; then sed -i "s#^ESP32_URL=.*#ESP32_URL=$previous_url#" "$ENV_FILE"; fi
if [ -n "$previous" ]; then nmcli connection up "$previous"; fi
systemctl restart rover-dashboard
exit 1
