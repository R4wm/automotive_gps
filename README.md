# automotive_gps

Raspberry Pi Zero W (DietPi) in-car display: USB GPS speed on a Pimoroni **Scroll HAT Mini**, optional scrolling text, and WiFi that does not block boot (phone hotspot + home SSID).

**Target hardware**

- Raspberry Pi Zero W
- [Enviro / Enviro+](https://shop.pimoroni.com/products/enviro-plus) (BME280 + LTR559 + **160×80 LCD**), **or**
- [Scroll HAT Mini](https://shop.pimoroni.com/products/scroll-hat-mini) (I2C `0x74`, button A = GPIO 5)
- USB GPS on `/dev/ttyUSB0` (NMEA, typically **4800 baud**)

## Features

| Component | Behavior |
|-----------|----------|
| **enviro-gps-speed** | **Enviro LCD:** large integer speed; **LTR559** auto-dims backlight (system service) |
| **scroll-hat-manager** | Scroll HAT: GPS speed; **button A** cycles GPS ↔ ESTHER scroll |
| **gps-speed-hat** / **scroll-esther** | Standalone scripts (same logic as manager modes) |
| **car-wifi** | `allow-hotplug wlan0`; WiFi comes up in background after boot; wpa_supplicant keeps scanning |
| **Phone hotspot** | `car-wifi-add-phone-hotspot` adds SSID at **priority 10** (home usually **5**) |

Scroll HAT Mini LEDs are **white only** (brightness, not RGB).

## Quick install (on the Pi)

```bash
git clone https://github.com/R4wm/automotive_gps.git
cd automotive_gps
sudo ./install.sh
```

Then add your phone hotspot (once):

```bash
sudo car-wifi-add-phone-hotspot "YourHotspotSSID" "YourPassword"
```

Configure home WiFi with `dietpi-config` or edit `/etc/wpa_supplicant/wpa_supplicant.conf` (do not commit real keys).

## Dependencies (DietPi / Raspberry Pi OS)

```bash
sudo apt update
sudo apt install -y python3-pip python3-serial python3-gpiozero python3-lgpio i2c-tools
sudo pip3 install scrollphathd --break-system-packages
```

Enable I2C in `/boot/firmware/config.txt`:

```text
dtparam=i2c_arm=on
```

Add `i2c-dev` to `/etc/modules` if `/dev/i2c-1` is missing after reboot.

## Services

```bash
systemctl status scroll-hat-manager   # display + GPS
systemctl status car-wifi             # background wlan0
```

Logs: `/var/log/car-wifi.log`

## SSH from your phone

1. Enable the phone hotspot.
2. Wait for the Pi to associate (often 10–30 s).
3. Use the Pi’s **hotspot** address: `ip -br a show wlan0` (not your home LAN IP).
4. `ssh root@<that-ip>`

The phone is the DHCP gateway; SSH from the phone to the Pi on the same hotspot LAN is normal.

## Manual scripts

```bash
scroll-hat-manager    # production (systemd)
gps-speed-hat         # speed only
scroll-esther         # scroll only
```

## Repository layout

```text
bin/                  Programs installed to /usr/local/bin
systemd/              Unit files and drop-ins
config/               Example network snippets (no secrets)
install.sh            Deploy to a live system
```

## License

MIT (see LICENSE if present).
