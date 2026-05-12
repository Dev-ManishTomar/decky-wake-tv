"""
USB rebind helper for external gamepads after sleep/resume.

The xpad driver loses its IRQ URB after suspend, leaving external USB
gamepads unresponsive. Rebinding the USB device forces the driver to
reinitialize, restoring functionality.

Requires passwordless sudo for tee to unbind/bind sysfs paths
(configured via /etc/sudoers.d/zz-waketv-usb).
"""

import glob
import os
import subprocess
import logging
import time

logger = logging.getLogger("usb_rebind")

GAMEPAD_RECEIVER_IDS = {
    "32c2:0018",  # HS6209 2.4G Wireless Receiver
    "3537:1098",  # 2.4G XBOX 360 For Windows
    "045e:028e",  # Microsoft Xbox360 Controller
    "045e:0b12",  # Microsoft Xbox Wireless Controller
    "045e:0b13",  # Microsoft Xbox Elite 2 Controller
}

BUILTIN_USB_BUSES = {"1-2", "1-3"}
SETTLE_DELAY = 1.5
MAX_RETRIES = 3
RETRY_DELAY = 1.0


def _read_sysfs(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def find_external_gamepad_ports() -> list[str]:
    """Find USB port IDs of external gamepad devices (not built-in)."""
    ports = []
    for dev_dir in glob.glob("/sys/bus/usb/devices/[0-9]*"):
        vid = _read_sysfs(os.path.join(dev_dir, "idVendor"))
        pid = _read_sysfs(os.path.join(dev_dir, "idProduct"))
        if not vid or not pid:
            continue
        dev_id = f"{vid}:{pid}"
        if dev_id not in GAMEPAD_RECEIVER_IDS:
            continue
        port = os.path.basename(dev_dir)
        if port in BUILTIN_USB_BUSES:
            continue
        product = _read_sysfs(os.path.join(dev_dir, "product"))
        logger.info(f"Found external gamepad: {product} ({dev_id}) at {port}")
        ports.append(port)
    return ports


def rebind_usb_device(port: str) -> bool:
    """Unbind then rebind a USB device to force driver reinitialization."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/unbind"],
            input=port.encode(), timeout=5, capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            logger.warning(f"Unbind {port} failed: {err}")
            return False

        time.sleep(0.3)

        result = subprocess.run(
            ["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/bind"],
            input=port.encode(), timeout=5, capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            if "No such device" in err or "busy" in err.lower():
                logger.info(f"Bind {port} skipped (already bound or gone): {err}")
            else:
                logger.warning(f"Bind {port} failed: {err}")
            return False

        logger.info(f"Rebound USB port {port}")
        return True
    except Exception as exc:
        logger.warning(f"Rebind {port} exception: {exc}")
        return False


USB_HUB_IDS = {
    "05e3:0610",  # USB2.1 Hub (dock)
    "05e3:0626",  # USB3.1 Hub (dock)
}


def enable_usb_wakeup() -> int:
    """Enable wakeup on dock USB hubs and their root host controllers.

    Returns the number of sysfs nodes successfully enabled.
    """
    enabled = 0
    root_hubs: set[str] = set()

    for dev_dir in glob.glob("/sys/bus/usb/devices/[0-9]*"):
        vid = _read_sysfs(os.path.join(dev_dir, "idVendor"))
        pid = _read_sysfs(os.path.join(dev_dir, "idProduct"))
        if not vid or not pid:
            continue
        if f"{vid}:{pid}" not in USB_HUB_IDS:
            continue

        port = os.path.basename(dev_dir)
        wake_path = os.path.join(dev_dir, "power", "wakeup")
        current = _read_sysfs(wake_path)
        if current == "enabled":
            logger.info(f"USB wakeup already enabled on {port}")
            enabled += 1
        else:
            try:
                result = subprocess.run(
                    ["sudo", "-n", "tee", wake_path],
                    input=b"enabled", timeout=5, capture_output=True,
                )
                if result.returncode == 0:
                    logger.info(f"USB wakeup enabled on {port}")
                    enabled += 1
                else:
                    err = result.stderr.decode(errors="replace").strip()
                    logger.warning(f"Failed to enable wakeup on {port}: {err}")
            except Exception as exc:
                logger.warning(f"Exception enabling wakeup on {port}: {exc}")

        bus = port.split("-")[0] if "-" in port else ""
        if bus:
            root_hubs.add(f"usb{bus}")

    for root in sorted(root_hubs):
        wake_path = f"/sys/bus/usb/devices/{root}/power/wakeup"
        current = _read_sysfs(wake_path)
        if current == "enabled":
            enabled += 1
            continue
        try:
            result = subprocess.run(
                ["sudo", "-n", "tee", wake_path],
                input=b"enabled", timeout=5, capture_output=True,
            )
            if result.returncode == 0:
                logger.info(f"USB wakeup enabled on {root}")
                enabled += 1
            else:
                err = result.stderr.decode(errors="replace").strip()
                logger.warning(f"Failed to enable wakeup on {root}: {err}")
        except Exception as exc:
            logger.warning(f"Exception enabling wakeup on {root}: {exc}")

    return enabled


def rebind_external_gamepads() -> int:
    """Find and rebind all external gamepad USB devices. Returns count.

    Waits briefly for USB devices to settle after resume, and retries
    the scan if fewer devices than expected are found (some dongles
    enumerate slower than others).
    """
    time.sleep(SETTLE_DELAY)
    all_ports: set[str] = set()
    for attempt in range(MAX_RETRIES):
        ports = find_external_gamepad_ports()
        all_ports.update(ports)
        if len(all_ports) >= 2 or attempt == MAX_RETRIES - 1:
            break
        logger.info(
            f"Found {len(all_ports)} gamepad(s), retrying scan "
            f"({attempt + 1}/{MAX_RETRIES})..."
        )
        time.sleep(RETRY_DELAY)

    if not all_ports:
        logger.info("No external gamepad USB devices found to rebind")
        return 0
    success = 0
    for port in sorted(all_ports):
        if rebind_usb_device(port):
            success += 1
    logger.info(f"Rebound {success}/{len(all_ports)} external gamepad(s)")
    return success
