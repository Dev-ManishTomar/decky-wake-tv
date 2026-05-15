# Wake TV - Decky Loader Plugin

A [Decky Loader](https://decky.xyz) plugin that automatically wakes your TV and switches to the correct HDMI input when your Steam Deck, steamMachine or any device running steamos resumes from sleep or when you press the gamepad Guide button.

Works with any TV that supports Wake-on-LAN (WOL) and the LG webOS SSAP protocol for HDMI switching (LG TVs from 2018+).

## Features

- **Wake-on-LAN** - Send magic packets to wake your TV
- **Auto HDMI switch** - Automatically switch to the correct HDMI input after waking
- **Gamepad Guide button wake** - Press the Guide/Home button on your gamepad to wake the TV
- **Sleep/resume wake** - Automatically wake the TV when your device resumes from sleep
- **Auto-disable built-in controller** - Automatically disables the built-in gamepad (e.g. ROG Ally controller) when an external controller is connected, and re-enables it when disconnected — eliminates the duplicate controller problem. External controllers are emulated as Xbox Elite for back paddle support via InputPlumber.
- **External gamepad fix** - Automatically rebinds external USB gamepads after resume when InputPlumber is not present (defers to InputPlumber's suspend service when available to avoid duplicate notifications)
- **USB wakeup chain repair** - Re-enables USB hub wakeup flags after each resume so the gamepad can always wake the device from the next sleep
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

These allow the plugin to rebind USB devices and enable USB wakeup after resume.

```bash
# USB unbind/bind for external gamepad fix + wakeup sysfs writes
ssh -t deck@<DEVICE_IP> "echo 'deck ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/bus/usb/drivers/usb/unbind, /usr/bin/tee /sys/bus/usb/drivers/usb/bind, /usr/bin/tee /sys/bus/usb/devices/*/power/wakeup' | sudo tee /etc/sudoers.d/zz-waketv-usb && sudo chmod 440 /etc/sudoers.d/zz-waketv-usb"

# (Optional) Allow remote suspend without password - needed for Homebridge/Apple Home integration
ssh -t deck@<DEVICE_IP> "echo 'deck ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/zz-waketv-suspend && sudo chmod 440 /etc/sudoers.d/zz-waketv-suspend"
```

### 2. Polkit Rule for Controller Management

Required for the "Auto-disable built-in controller" feature. Allows the plugin to toggle InputPlumber's virtual gamepad and manage external controllers via D-Bus without interactive authentication. Decky's plugin sandbox drops supplementary groups, so the default InputPlumber polkit rules (which check for the `wheel` group) don't apply.

```bash
ssh -t deck@<DEVICE_IP> "sudo tee /etc/polkit-1/rules.d/zz-waketv-inputplumber.rules > /dev/null << 'RULES'
polkit.addRule(function(action, subject) {
    if ((action.id === \"org.shadowblip.Input.CompositeDevice.SetTargetDevices\" ||
         action.id === \"org.shadowblip.InputPlumber.SetManageAllDevices\") &&
        subject.user === \"deck\") {
        return polkit.Result.YES;
    }
});
RULES
sudo chmod 644 /etc/polkit-1/rules.d/zz-waketv-inputplumber.rules"
```

### 3. USB Wake: Udev Rule + Boot Service

The udev rule enables wakeup on USB dock hubs when they appear (handles hot-plug and boot timing). The boot service enables wakeup on root host controllers and ACPI wakeup sources.

> **Note:** The hub VID:PID (`05e3:0610` and `05e3:0626`) match common Genesys Logic USB hubs used in docks. The ACPI wakeup sources (`XHC0`-`XHC4`) and root hubs (`usb5`, `usb6`) are for the ROG Ally. Adjust for your hardware if needed - run `lsusb` and check `/proc/acpi/wakeup`.

```bash
# Install udev rule for dock USB hubs (enables wakeup on hot-plug)
ssh -t deck@<DEVICE_IP> "sudo tee /etc/udev/rules.d/99-waketv-usb-wake.rules > /dev/null << 'UDEV'
ACTION==\"add\", SUBSYSTEM==\"usb\", ATTR{idVendor}==\"05e3\", ATTR{idProduct}==\"0610\", RUN+=\"/bin/sh -c 'echo enabled > /sys%p/power/wakeup'\"
ACTION==\"add\", SUBSYSTEM==\"usb\", ATTR{idVendor}==\"05e3\", ATTR{idProduct}==\"0626\", RUN+=\"/bin/sh -c 'echo enabled > /sys%p/power/wakeup'\"
UDEV
sudo udevadm control --reload-rules"
```

```bash
# Install boot service for root hubs and ACPI wakeup (idempotent, won't fail on missing paths)
ssh -t deck@<DEVICE_IP> "sudo tee /etc/systemd/system/usb-wake.service > /dev/null << 'EOF'
[Unit]
Description=Enable USB wake for gamepad and Ethernet WOL
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '\
  for hub in usb5 usb6; do \
    [ -f /sys/bus/usb/devices/\$hub/power/wakeup ] && echo enabled > /sys/bus/usb/devices/\$hub/power/wakeup; \
  done; \
  for xhc in XHC0 XHC1 XHC2 XHC3 XHC4; do \
    grep -q \"^\$xhc.*disabled\" /proc/acpi/wakeup 2>/dev/null && echo \$xhc > /proc/acpi/wakeup; \
  done; \
  true'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now usb-wake.service"
```

### 4. InputPlumber Suspend Service

Enables proper input device cleanup/restoration on sleep/resume. When this service is enabled, the plugin automatically skips its own USB rebind (InputPlumber handles it), which avoids duplicate controller notifications on wake.

```bash
ssh -t deck@<DEVICE_IP> "sudo systemctl enable inputplumber-suspend.service"
```

### 5. (Optional) Prioritize Wired LAN over WiFi

If your device is connected to both WiFi and a wired dock/hub, NetworkManager may prefer WiFi. These commands set the wired connection to higher priority so traffic always routes through LAN. Changes persist across reboots and SteamOS updates.

```bash
ssh -t deck@<DEVICE_IP> "\
  sudo nmcli connection modify 'LAN' ipv4.route-metric 100 ipv6.route-metric 100 && \
  sudo nmcli connection modify 'LAN' connection.autoconnect-priority 100"
```

Then lower the priority on each WiFi network you use:

```bash
ssh -t deck@<DEVICE_IP> "\
  sudo nmcli connection modify '<WIFI_NAME>' ipv4.route-metric 700 ipv6.route-metric 700"
```

> **Note:** Replace `LAN` with your wired connection name and `<WIFI_NAME>` with each WiFi network name. Run `nmcli connection show` on the device to see all connection names. Lower metric = higher priority.

## How It Works

### On Resume from Sleep

1. System wakes up (power button, gamepad disconnect, or WOL packet)
2. Plugin detects the D-Bus `PrepareForSleep(false)` signal
3. If `inputplumber-suspend.service` is not enabled, external USB gamepads are rebound (unbind + bind) to fix the xpad driver resume bug. Otherwise InputPlumber handles device restoration.
4. USB hub wakeup flags are re-enabled (some drivers reset them on suspend)
5. Plugin waits for the network to come up (polls every 1s)
6. WOL magic packet is sent to the TV
7. Plugin connects to the TV via WebSocket and switches to the configured HDMI input

### Auto-Disable Built-in Controller

When enabled in the plugin settings, the plugin continuously monitors connected gamepads:

1. Polls `/dev/input/event*` every 3 seconds for gamepad-capable devices
2. Classifies each device as built-in or external by resolving its sysfs path to a USB bus port
3. When an external gamepad is detected, the built-in controller's virtual gamepad is removed via InputPlumber's D-Bus API (`SetTargetDevices` empty on the built-in CompositeDevice)
4. InputPlumber's `ManageAllDevices` is enabled so the external controller is absorbed and emulated as Xbox Elite (`xbox-elite` target type), providing back paddle and full button support
5. When all external gamepads are disconnected, the built-in controller is restored and `ManageAllDevices` is reverted
6. On plugin startup/shutdown, the built-in controller is always re-enabled as a safety net

Requires the polkit rule (section 2 of System Setup). Works with USB dongles, wired controllers, and Bluetooth gamepads.

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
│   ├── usb_rebind.py          # USB rebind + wakeup chain repair after resume
│   └── controller_manager.py  # Auto-disable built-in controller on external connect
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

### Device doesn't wake from sleep via gamepad
- Verify the full USB wakeup chain is enabled:
  ```bash
  # Check hub and root hub wakeup flags
  for d in usb5 5-1 usb6 6-1; do
    echo "$d: $(cat /sys/bus/usb/devices/$d/power/wakeup 2>/dev/null || echo N/A)"
  done
  # Check ACPI wakeup sources (XHC controllers should show *enabled)
  grep XHC /proc/acpi/wakeup
  ```
- Ensure `usb-wake.service` is running: `systemctl status usb-wake.service`
- Ensure the udev rule is installed: `cat /etc/udev/rules.d/99-waketv-usb-wake.rules`
- The `inputplumber-suspend.service` should be enabled

### External gamepad doesn't work after sleep
- If `inputplumber-suspend.service` is enabled, InputPlumber handles device restoration. Check its logs: `journalctl -u inputplumber -n 50`
- If InputPlumber is not installed, verify the sudoers rule is in place: `ls -la /etc/sudoers.d/zz-waketv-usb`
- Check plugin logs for rebind results (look for "Post-resume:" and "inputplumber-suspend")

### Plugin logs
```bash
# View latest log file
ls -t ~/homebrew/logs/waketv/*.log | head -1 | xargs tail -100

# Follow logs in real-time (via deploy script)
./deploy.sh --logs
```
