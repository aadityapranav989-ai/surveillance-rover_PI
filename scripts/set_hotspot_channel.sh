#!/bin/bash
# Moves the TAPIR hotspot to another 2.4 GHz channel, e.g. away from a crowded one.
#
#   sudo nohup bash scripts/set_hotspot_channel.sh 13 > ~/hotspot.log 2>&1 &
#
# Wi-Fi SSH drops for a few seconds; the ESP32 and laptop follow automatically.
# If the hotspot does not come up on the new channel (for example channel 13 is
# not allowed in the Pi's Wi-Fi country), it goes back to the previous channel.
set -u
CHANNEL="${1:-}"
if [ "$(id -u)" != 0 ]; then echo "Run with sudo."; exit 1; fi
case "$CHANNEL" in
  [1-9]|1[0-3]) ;;
  *) echo "Give a 2.4 GHz channel from 1 to 13."; exit 1 ;;
esac
previous=$(nmcli -g 802-11-wireless.channel connection show TAPIR) || { echo "No TAPIR hotspot; run setup_hotspot.sh first."; exit 1; }
echo "$(date '+%T') Moving TAPIR from channel ${previous:-auto} to $CHANNEL (Wi-Fi country: $(iw reg get | awk '/country/ {sub(":", "", $2); print $2; exit}'))"
nmcli connection modify TAPIR 802-11-wireless.channel "$CHANNEL"
if nmcli --wait 30 connection up TAPIR; then
  echo "$(date '+%T') TAPIR is on channel $CHANNEL"
  exit 0
fi
echo "$(date '+%T') Channel $CHANNEL did not work; going back to ${previous:-6}"
nmcli connection modify TAPIR 802-11-wireless.channel "${previous:-6}"
nmcli --wait 30 connection up TAPIR
exit 1
