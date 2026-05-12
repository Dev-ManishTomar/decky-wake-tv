# Wake TV - Decky Loader Plugin

A [Decky Loader](https://decky.xyz) plugin that automatically wakes your TV and switches to the correct HDMI input when your Steam Deck / ROG Ally resumes from sleep or when you press the gamepad Guide button.

Works with any TV that supports Wake-on-LAN (WOL) and the LG webOS SSAP protocol for HDMI switching (LG TVs from 2018+).

## Features

- **Wake-on-LAN** - Send magic packets to wake your TV
- **Auto HDMI switch** - Automatically switch to the correct HDMI input after waking
- **Gamepad Guide button wake** - Press the Guide/Home button on your gamepad to wake the TV
- **Sleep/resume wake** - Automatically wake the TV when your device resumes from sleep
- **External gamepad fix** - Automatically rebinds external USB gamepads after resume (fixes the common issue where wireless gamepads stop working after sleep)
- **Pair & forget** - One-time pairing with your LG TV, credentials persist across reboots

## Installation

1. Install [Decky Loader](https://decky.xyz) on your device
2. Copy the plugin to `~/homebrew/plugins/waketv/`
3. Restart Decky Loader: `sudo systemctl restart plugin_loader`

## Plugin Setup

1. Open the Decky sidebar (... button) and find **Wake TV**
2. Enter your TV's IP address
3. Click **Pair with TV** - accept the pairing prompt on your TV screen
4. Select the correct HDMI input
5. Click **Save**

The MAC address is auto-discovered during pairing.

## System Setup (One-Time)

The following system-level configuration enables advanced features like gamepad wake-from-sleep, external gamepad fix, and Wake-on-LAN to the device itself. These commands require SSH access to your device.

### 1. Sudoers Rules

These allow the plugin to rebind USB devices after resume (fixes external gamepads) and enable remote suspend.

```bash
# USB unbind/bind for external gamepad fix after sleep
ssh -t deck@<DEVICE_IP> "echo 'deck ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/bus/usb/drivers/usb/unbind, /usr/bin/tee /sys/bus/usb/drivers/usb/bind' | sudo tee /etc/sudoers.d/zz-waketv-usb && sudo chmod 440 /etc/sudoers.d/zz-waketv-usb"

# (Optional) Allow remote suspend without password - needed for Homebridge/Apple Home integration
ssh -t deck@<DEVICE_IP> "echo 'deck ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/zz-waketv-suspend && sudo chmod 440 /etc/sudoers.d/zz-waketv-suspend"
```

### 2. USB Wake Service

Enables USB wake sources so the device can wake from sleep when a gamepad connects/disconnects or when a Wake-on-LAN packet arrives over Ethernet.

> **Note:** The USB bus numbers (`usb5`, `usb6`, `5-1`, `6-1`) are specific to the ROG Ally with an external USB dock. You may need to adjust these for your hardware. Run `lsusb` and check `/sys/bus/usb/devices/*/product` to identify your devices.

```bash
ssh -t deck@<DEVICE_IP> "sudo tee /etc/systemd/system/usb-wake.service > /dev/null << 'EOF'
[Unit]
Description=Enable USB wake for gamepad and Ethernet WOL
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c \"echo enabled > /sys/bus/usb/devices/usb5/power/wakeup; echo enabled > /sys/bus/usb/devices/5-1/power/wakeup; echo XHC2 > /proc/acpi/wakeup; echo enabled > /sys/bus/usb/devices/usb6/power/wakeup; echo enabled > /sys/bus/usb/devices/6-1/power/wakeup\"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now usb-wake.service"
```

### 3. InputPlumber Suspend Service

Enables proper input device cleanup/restoration on sleep/resume (fixes the Ally's built-in controller after wake).

```bash
ssh -t deck@<DEVICE_IP> "sudo systemctl enable inputplumber-suspend.service"
```

## How It Works

### On Resume from Sleep

1. System wakes up (power button, gamepad disconnect, or WOL packet)
2. Plugin detects the D-Bus `PrepareForSleep(false)` signal
3. External USB gamepads are rebound (unbind + bind) to fix the xpad driver resume bug
4. Plugin waits for the network to come up (polls every 1s)
5. WOL magic packet is sent to the TV
6. Plugin connects to the TV via WebSocket and switches to the configured HDMI input

### On Gamepad Guide Button Press (While Awake)

1. Plugin watches `/dev/input/event*` devices for `BTN_MODE` (code 316) events
2. On press, sends WOL + HDMI switch to the TV
3. 5-second cooldown prevents duplicate triggers

## Optional: Apple Home / Siri Integration

If you have a [Homebridge](https://homebridge.io) setup, you can control the entire gaming setup from Apple Home or Siri.

### Install the WOL Plugin

In the Homebridge UI, install **Homebridge WoL** (`homebridge-wol`).

### Configuration

| Field | Value |
|-------|-------|
| **Name** | `Gaming Setup` (or whatever you like) |
| **MAC address** | Your device's **Ethernet** MAC (not WiFi) |
| **Broadcast address** | `192.168.1.255` (adjust for your subnet) |
| **Ping command** | `ping -c 1 -W 2 <DEVICE_IP>` |
| **Wake grace time** | `30` |
| **Shutdown command** | see below |

Find your Ethernet MAC:
```bash
ssh deck@<DEVICE_IP> "cat /sys/class/net/enp*/address"
```

### Shutdown Command (with Steam animation)

To suspend with the proper Steam sleep animation, the shutdown command injects a fake power button event:

```
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 deck@<DEVICE_IP> "python3 -c \"import struct,time;fd=open('/dev/input/event0','wb');t=time.time();s=int(t);u=int((t-s)*1e6);p=struct.pack;fd.write(p('llHHi',s,u,1,116,1)+p('llHHi',s,u,0,0,0));fd.flush();time.sleep(0.1);t=time.time();s=int(t);u=int((t-s)*1e6);fd.write(p('llHHi',s,u,1,116,0)+p('llHHi',s,u,0,0,0));fd.close()\""
```

> **Note:** Homebridge must have SSH key-based auth to the device. Run `ssh-keygen && ssh-copy-id deck@<DEVICE_IP>` from the Homebridge terminal.

### Usage

- **"Hey Siri, turn on Gaming Setup"** - Sends WOL to device, device wakes, plugin wakes TV + switches HDMI
- **"Hey Siri, turn off Gaming Setup"** - SSHs to device, triggers Steam sleep animation
- Works with HomeKit scenes and automations

## Wake Methods Summary

| Method | How | Animation |
|--------|-----|-----------|
| Power button | Press the device's power button | Yes |
| Gamepad disconnect | Hold Guide button to power off gamepad, reconnect wakes device | Yes |
| Wake-on-LAN | Send WOL packet to device's Ethernet MAC from any device on the network | Yes |
| Apple Home / Siri | "Hey Siri, turn on Gaming Setup" via Homebridge WOL plugin | Yes |

All methods trigger the plugin's auto-wake flow: WOL to TV + HDMI switch.

## Development

### Prerequisites

- Node.js 18+
- pnpm

### Build

```bash
pnpm install
pnpm run build
```

### Deploy to Device

```bash
# Configure .env.deck with your device's IP
./deploy.sh
```

### Project Structure

```
wake-tv/
├── main.py                    # Backend: lifecycle, watchers, settings, TV control
├── py_modules/
│   ├── tv_client.py           # LG webOS SSAP protocol (WebSocket, WOL, MAC discovery)
│   ├── input_watcher.py       # Gamepad Guide button detection via /dev/input
│   ├── sleep_watcher.py       # D-Bus PrepareForSleep signal monitoring
│   └── usb_rebind.py          # USB device rebind for external gamepad fix
├── src/
│   ├── index.tsx              # Frontend plugin entry point
│   └── components/
│       └── WakeTVPanel.tsx    # Settings UI (IP, HDMI, toggles, pairing)
├── plugin.json                # Plugin metadata
├── deploy.sh                  # Build & deploy script
└── .env.deck                  # Device SSH config (not committed)
```

## Troubleshooting

### TV doesn't wake
- Ensure WOL is enabled in your TV's settings (Network > Wake on LAN)
- Verify the MAC address is correct in the plugin settings
- Check that the TV and device are on the same network/subnet

### HDMI doesn't switch
- Make sure you've paired with the TV (accept the prompt on the TV screen)
- Check the HDMI input number matches the port your device is connected to

### External gamepad doesn't work after sleep
- Verify the sudoers rule for USB unbind/bind is in place
- Check plugin logs: `cat ~/homebrew/logs/waketv/plugin.log`
- The `usb-wake.service` and `inputplumber-suspend.service` should both be enabled

### Plugin logs
```bash
# View latest logs
cat ~/homebrew/logs/waketv/plugin.log

# Follow logs in real-time
tail -f ~/homebrew/logs/waketv/plugin.log
```
