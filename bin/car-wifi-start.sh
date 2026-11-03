#!/bin/bash
# Bring up WiFi in background so boot does not wait for SSID/DHCP.
if ip link show wlan0 &>/dev/null; then
  ( /sbin/ifup wlan0 >>/var/log/car-wifi.log 2>&1 ) &
fi
exit 0
