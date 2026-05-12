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


def rebind_external_gamepads() -> int:
    """Find and rebind all external gamepad USB devices. Returns count."""
    ports = find_external_gamepad_ports()
    if not ports:
        logger.info("No external gamepad USB devices found to rebind")
        return 0
    success = 0
    for port in ports:
        if rebind_usb_device(port):
            success += 1
    logger.info(f"Rebound {success}/{len(ports)} external gamepad(s)")
    return success
