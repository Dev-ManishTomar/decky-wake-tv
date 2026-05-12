"""
USB wake helpers.

Manages USB device unbind/bind for sleep wake support.
When the system goes to sleep, unbinding the gamepad receiver forces
a USB reconnect event on the next button press, which generates a
wake signal that brings the system out of sleep.

Also enables USB wake sources on the host controller chain.
"""

import glob
import os
import subprocess
import logging

logger = logging.getLogger("usb_wake")

# Known gamepad receiver vendor:product IDs to auto-unbind on sleep
GAMEPAD_RECEIVER_IDS = {
    "32c2:0018",  # HS6209 2.4G Wireless Receiver
    "3537:1098",  # 2.4G XBOX 360 For Windows
    "045e:028e",  # Microsoft Xbox360 Controller
}


def _read_sysfs(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def find_gamepad_usb_ports() -> list[str]:
    """
    Find USB device sysfs paths (e.g. '5-1.3') for known gamepad receivers.
    Returns the port identifiers that can be used with bind/unbind.
    """
    ports = []
    for dev_dir in glob.glob("/sys/bus/usb/devices/[0-9]*"):
        vid = _read_sysfs(os.path.join(dev_dir, "idVendor"))
        pid = _read_sysfs(os.path.join(dev_dir, "idProduct"))
        if not vid or not pid:
            continue
        dev_id = f"{vid}:{pid}"
        if dev_id in GAMEPAD_RECEIVER_IDS:
            port = os.path.basename(dev_dir)
            product = _read_sysfs(os.path.join(dev_dir, "product"))
            ports.append(port)
            logger.info(f"Found gamepad USB device: {product} ({dev_id}) at {port}")
    return ports


def unbind_usb_device(port: str) -> bool:
    """Unbind a USB device by port (e.g. '5-1.3') from its driver."""
    unbind_path = f"/sys/bus/usb/drivers/usb/unbind"
    try:
        with open(unbind_path, "w") as f:
            f.write(port)
        logger.info(f"Unbound USB device {port}")
        return True
    except PermissionError:
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", f"echo {port} > {unbind_path}"],
                timeout=5, check=True, capture_output=True,
            )
            logger.info(f"Unbound USB device {port} (via sudo)")
            return True
        except Exception as exc:
            logger.warning(f"Failed to unbind {port}: {exc}")
            return False
    except Exception as exc:
        logger.warning(f"Failed to unbind {port}: {exc}")
        return False


def bind_usb_device(port: str) -> bool:
    """Re-bind a USB device by port."""
    bind_path = f"/sys/bus/usb/drivers/usb/bind"
    try:
        with open(bind_path, "w") as f:
            f.write(port)
        logger.info(f"Re-bound USB device {port}")
        return True
    except PermissionError:
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", f"echo {port} > {bind_path}"],
                timeout=5, check=True, capture_output=True,
            )
            logger.info(f"Re-bound USB device {port} (via sudo)")
            return True
        except Exception as exc:
            logger.warning(f"Failed to bind {port}: {exc}")
            return False
    except Exception as exc:
        logger.warning(f"Failed to bind {port}: {exc}")
        return False


def enable_usb_wake_chain() -> None:
    """
    Enable USB wake on host controllers and hubs so gamepad reconnect
    can wake the system from sleep.
    """
    # Enable wake on all USB root hubs
    for wakeup_path in glob.glob("/sys/bus/usb/devices/usb*/power/wakeup"):
        current = _read_sysfs(wakeup_path)
        if current != "enabled":
            try:
                with open(wakeup_path, "w") as f:
                    f.write("enabled")
                logger.info(f"Enabled wake on {wakeup_path}")
            except Exception:
                pass

    # Enable wake on intermediate hubs
    for wakeup_path in glob.glob("/sys/bus/usb/devices/*/power/wakeup"):
        if "/usb" in wakeup_path:
            continue
        dev_dir = os.path.dirname(os.path.dirname(wakeup_path))
        # Only enable on hubs (devices with child ports)
        if glob.glob(os.path.join(dev_dir, "[0-9]*-[0-9]*.[0-9]*")):
            current = _read_sysfs(wakeup_path)
            if current != "enabled":
                try:
                    with open(wakeup_path, "w") as f:
                        f.write("enabled")
                    logger.info(f"Enabled wake on hub {wakeup_path}")
                except Exception:
                    pass

    # Enable ACPI wake on all XHC controllers
    try:
        with open("/proc/acpi/wakeup", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[0].startswith("XHC") and "*disabled" in line:
                    device_name = parts[0]
                    try:
                        with open("/proc/acpi/wakeup", "w") as wf:
                            wf.write(device_name)
                        logger.info(f"Enabled ACPI wake on {device_name}")
                    except Exception:
                        pass
    except Exception:
        pass
