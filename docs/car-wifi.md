# Car WiFi (client-only, no Pi hotspot)

- Boot does not wait for WiFi (`wlan0` is `allow-hotplug`).
- `car-wifi.service` starts `ifup wlan0` in the background after boot.
- `wpa_supplicant` keeps scanning and joins when home or phone SSID appears.
- `dietpi-wifi-monitor` reconnects if the gateway drops while driving.

Add your phone hotspot once:

```bash
car-wifi-add-phone-hotspot "YourHotspotSSID" "YourHotspotPassword"
```

SSH from phone: use the IP shown on the Pi for `wlan0` (`ip -br a`), not your home LAN address.
