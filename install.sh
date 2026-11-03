#!/bin/bash
# Install automotive_gps onto a DietPi / Raspberry Pi OS system (run as root).
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"

install -m 755 "$ROOT/bin/scroll-esther" /usr/local/bin/scroll-esther
install -m 755 "$ROOT/bin/gps-speed-hat" /usr/local/bin/gps-speed-hat
install -m 755 "$ROOT/bin/scroll-hat-manager" /usr/local/bin/scroll-hat-manager
install -m 755 "$ROOT/bin/car-wifi-start.sh" /usr/local/bin/car-wifi-start.sh
install -m 755 "$ROOT/bin/car-wifi-add-phone-hotspot" /usr/local/bin/car-wifi-add-phone-hotspot
if [[ -f "$ROOT/bin/enviro-gps-speed" ]]; then
  install -m 755 "$ROOT/bin/enviro-gps-speed" /usr/local/bin/enviro-gps-speed
fi

install -m 644 "$ROOT/systemd/scroll-hat-manager.service" /etc/systemd/system/scroll-hat-manager.service
if [[ -f "$ROOT/systemd/enviro-gps-speed.service" ]]; then
  install -m 644 "$ROOT/systemd/enviro-gps-speed.service" /etc/systemd/system/enviro-gps-speed.service
fi
install -m 644 "$ROOT/systemd/car-wifi.service" /etc/systemd/system/car-wifi.service
install -d /etc/systemd/system/dietpi-wifi-monitor.service.d
install -m 644 "$ROOT/systemd/dietpi-wifi-monitor.service.d/car-wifi.conf" \
  /etc/systemd/system/dietpi-wifi-monitor.service.d/car-wifi.conf

install -d /etc/car-wifi
install -m 644 "$ROOT/docs/car-wifi.md" /etc/car-wifi/README

if [[ -f /etc/network/interfaces.d/wlan0.conf ]]; then
  if grep -q '^auto wlan0' /etc/network/interfaces.d/wlan0.conf; then
    sed -i 's/^auto wlan0/allow-hotplug wlan0/' /etc/network/interfaces.d/wlan0.conf
    grep -q 'car-wifi' /etc/network/interfaces.d/wlan0.conf || \
      sed -i '1a# car-wifi: wlan0 brought up after boot by car-wifi.service' /etc/network/interfaces.d/wlan0.conf
    echo "Updated /etc/network/interfaces.d/wlan0.conf (auto -> allow-hotplug)."
  fi
else
  echo "No wlan0.conf found; copy config/interfaces.d/wlan0.conf.example manually." >&2
fi

touch /var/log/car-wifi.log
systemctl daemon-reload
systemctl enable car-wifi.service
systemctl restart car-wifi.service || true
if [[ -f /usr/local/bin/enviro-gps-speed ]]; then
  systemctl enable enviro-gps-speed.service
  systemctl restart enviro-gps-speed.service || true
elif [[ -f /usr/local/bin/scroll-hat-manager ]]; then
  systemctl enable scroll-hat-manager.service
  systemctl restart scroll-hat-manager.service || true
fi

echo "Install complete."
