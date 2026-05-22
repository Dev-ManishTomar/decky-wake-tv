"""
Auto-disable built-in controller when an external gamepad is connected.

Uses InputPlumber's D-Bus API (org.shadowblip.InputPlumber) to toggle
the built-in composite device's virtual gamepad output. When an external
gamepad is detected, the built-in device's target is set to empty (removing
the virtual gamepad). When all external gamepads disconnect, the target
is restored to "deck-uhid" (the default Valve Steam Deck Controller).

External controller emulation (e.g. Xbox Elite) is handled by
InputPlumber's device config at /etc/inputplumber/devices/, not by
this module.
"""

import asyncio
import glob
import os
import re
import subprocess
import logging

logger = logging.getLogger("controller_manager")

BTN_SOUTH = 304
BTN_MODE = 316
BUILTIN_USB_BUSES = {"1-2", "1-3"}

POLL_INTERVAL = 2
DEBOUNCE_STABLE_CYCLES = 1

INPUTPLUMBER_BUS = "org.shadowblip.InputPlumber"
COMPOSITE_IFACE = "org.shadowblip.Input.CompositeDevice"
COMPOSITE_BASE = "/org/shadowblip/InputPlumber/CompositeDevice"
DEFAULT_TARGET_TYPE = "deck-uhid"
EXTERNAL_TARGET_TYPE = "xbox-elite"


def _read_sysfs(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def _device_has_key(event_path: str, key_code: int) -> bool:
    basename = os.path.basename(event_path)
    caps_hex = _read_sysfs(
        f"/sys/class/input/{basename}/device/capabilities/key"
    )
    if not caps_hex:
        return False
    bits = 0
    for i, chunk in enumerate(reversed(caps_hex.split())):
        bits |= int(chunk, 16) << (i * 64)
    return bool(bits & (1 << key_code))


_USB_PORT_RE = re.compile(r"/(\d+-\d+(?:\.\d+)*)/")


def _resolve_usb_port(event_path: str) -> str | None:
    """Follow sysfs symlinks to extract the USB bus port (e.g. '1-2')."""
    basename = os.path.basename(event_path)
    real = os.path.realpath(f"/sys/class/input/{basename}")
    m = _USB_PORT_RE.search(real)
    if not m:
        return None
    return m.group(1)


def _is_bluetooth_device(event_path: str) -> bool:
    """Check if an input device is connected via Bluetooth.

    Bluetooth HID devices route through the uhid subsystem, so their
    sysfs path contains '/uhid/'. True virtual devices (e.g. InputPlumber
    output) live under '/devices/virtual/input/' without uhid.
    """
    basename = os.path.basename(event_path)
    real = os.path.realpath(f"/sys/class/input/{basename}")
    return "/uhid/" in real


def _get_root_bus_port(port: str) -> str:
    """Extract root bus-port from a full port path (e.g. '5-1.1' -> '5-1')."""
    return port.split(".")[0]


def find_gamepad_devices() -> list[dict]:
    """Find all gamepad input devices and classify as built-in, external, or virtual.

    Returns list of dicts: event_path, name, usb_port, is_builtin, is_virtual.
    Bluetooth gamepads (no USB port but routed through uhid) are correctly
    classified as external, not virtual.
    """
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if not (_device_has_key(path, BTN_SOUTH) or _device_has_key(path, BTN_MODE)):
            continue

        basename = os.path.basename(path)
        name = _read_sysfs(f"/sys/class/input/{basename}/device/name")
        usb_port = _resolve_usb_port(path)
        bluetooth = _is_bluetooth_device(path)

        is_virtual = usb_port is None and not bluetooth
        is_builtin = False
        if usb_port and _get_root_bus_port(usb_port) in BUILTIN_USB_BUSES:
            is_builtin = True

        devices.append({
            "event_path": path,
            "name": name or "unknown",
            "usb_port": usb_port,
            "is_builtin": is_builtin,
            "is_virtual": is_virtual,
        })
    return devices


# -----------------------------------------------------------------------
# InputPlumber D-Bus helpers
# -----------------------------------------------------------------------

_CLEAN_ENV = {
    "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": os.environ.get("HOME", "/home/deck"),
    "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
}


def _busctl(*args: str, timeout: int = 5) -> tuple[bool, str]:
    """Run a busctl command with a clean environment.

    Decky Loader's sandbox overrides LD_LIBRARY_PATH with bundled libs
    that lack the OpenSSL version systemd needs. Using env={...} bypasses
    the sandbox so busctl can link against system libraries.
    """
    cmd = ["busctl"] + list(args)
    try:
        result = subprocess.run(
            cmd, timeout=timeout, capture_output=True, text=True,
            env=_CLEAN_ENV,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            logger.warning(f"busctl failed: {' '.join(args)} -> {err}")
            return False, err
        return True, result.stdout.strip()
    except Exception as exc:
        logger.warning(f"busctl exception: {exc}")
        return False, str(exc)


def _find_builtin_composite() -> str | None:
    """Find the InputPlumber CompositeDevice path for the built-in controller."""
    for i in range(10):
        path = f"{COMPOSITE_BASE}{i}"
        ok, name = _busctl(
            "get-property", INPUTPLUMBER_BUS, path,
            COMPOSITE_IFACE, "Name",
        )
        if not ok:
            break
        if name.startswith('s "'):
            name = name[3:].rstrip('"')
        if any(kw in name.upper() for kw in ("ASUS", "ROG", "ALLY", "LEGION", "STEAM DECK")):
            logger.info(f"Found built-in composite device: {name} at {path}")
            return path
    return None


def _get_current_target_type(composite_path: str) -> str | None:
    """Read the target device type from the composite device's current target."""
    ok, val = _busctl(
        "get-property", INPUTPLUMBER_BUS, composite_path,
        COMPOSITE_IFACE, "TargetDevices",
    )
    if not ok or not val:
        return None
    if " 0" in val and val.strip().endswith(" 0"):
        return None

    parts = val.split('"')
    if len(parts) < 2:
        return None
    target_path = parts[1]

    ok, dev_type = _busctl(
        "get-property", INPUTPLUMBER_BUS, target_path,
        "org.shadowblip.Input.Target", "DeviceType",
    )
    if not ok:
        return DEFAULT_TARGET_TYPE
    if dev_type.startswith('s "'):
        dev_type = dev_type[3:].rstrip('"')
    return dev_type


def _set_target_devices(composite_path: str, device_types: list[str]) -> bool:
    """Set the target devices for a composite device via D-Bus."""
    count = str(len(device_types))
    ok, _ = _busctl(
        "call", INPUTPLUMBER_BUS, composite_path,
        COMPOSITE_IFACE, "SetTargetDevices",
        "as", count, *device_types,
    )
    if ok:
        if device_types:
            logger.info(f"Set target devices to: {device_types}")
        else:
            logger.info("Cleared target devices (built-in gamepad disabled)")
    return ok


def _has_target_gamepad(composite_path: str) -> bool:
    """Check if the composite device currently has a gamepad target."""
    ok, val = _busctl(
        "get-property", INPUTPLUMBER_BUS, composite_path,
        COMPOSITE_IFACE, "TargetDevices",
    )
    if not ok:
        return True
    return "as 0" not in val or not val.strip().endswith("0")


def _ensure_external_target(builtin_path: str | None) -> None:
    """Set any non-builtin composite devices to xbox-elite.

    InputPlumber's device config creates the composite with the right target,
    but steamos-manager may override it to deck-uhid. This corrects it.
    """
    found = 0
    for i in range(10):
        path = f"{COMPOSITE_BASE}{i}"
        if path == builtin_path:
            continue
        ok, raw = _busctl(
            "get-property", INPUTPLUMBER_BUS, path,
            COMPOSITE_IFACE, "Name",
        )
        if not ok:
            logger.info(f"_ensure_external_target: no device at index {i}, stopping scan")
            break
        name = raw[3:].rstrip('"') if raw.startswith('s "') else raw
        logger.info(f"_ensure_external_target: found {name} at {path}")
        if any(kw in name.upper() for kw in ("ASUS", "ROG", "ALLY", "LEGION", "STEAM DECK")):
            continue
        found += 1
        _set_target_devices(path, [EXTERNAL_TARGET_TYPE])
    if found == 0:
        logger.warning("_ensure_external_target: no external composite devices found")


def disable_builtin_gamepad() -> bool:
    """Disable the built-in controller's virtual gamepad via InputPlumber."""
    composite = _find_builtin_composite()
    if not composite:
        logger.warning("No built-in composite device found in InputPlumber")
        return False
    return _set_target_devices(composite, [])


def enable_builtin_gamepad(target_type: str | None = None) -> bool:
    """Re-enable the built-in controller's virtual gamepad via InputPlumber."""
    composite = _find_builtin_composite()
    if not composite:
        logger.warning("No built-in composite device found in InputPlumber")
        return False
    device_type = target_type or DEFAULT_TARGET_TYPE
    return _set_target_devices(composite, [device_type])


def get_controller_status() -> dict:
    """Return current controller state for the UI."""
    devices = find_gamepad_devices()
    external = [d for d in devices if not d["is_builtin"] and not d["is_virtual"]]

    composite = _find_builtin_composite()
    builtin_active = True
    if composite:
        builtin_active = _has_target_gamepad(composite)

    return {
        "builtin_active": builtin_active,
        "external_count": len(external),
    }


async def watch_controller_toggle(on_change=None) -> None:
    """Poll for external gamepad connect/disconnect and toggle built-in controller.

    on_change(builtin_disabled: bool, external_count: int) is called on state transitions.
    """
    builtin_disabled = False
    original_target_type: str | None = None
    stable_count = 0
    last_external_present = False

    composite_path = _find_builtin_composite()
    if not composite_path:
        logger.warning("No built-in composite device found, controller toggle inactive")
        return

    original_target_type = _get_current_target_type(composite_path) or DEFAULT_TARGET_TYPE
    logger.info(f"Controller toggle active, target type: {original_target_type}")

    while True:
        try:
            devices = find_gamepad_devices()
            external = [d for d in devices if not d["is_builtin"] and not d["is_virtual"]]
            external_present = len(external) > 0

            if external_present != last_external_present:
                stable_count = 0
                last_external_present = external_present
            else:
                stable_count += 1

            if stable_count < DEBOUNCE_STABLE_CYCLES:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if external_present and not builtin_disabled:
                if _set_target_devices(composite_path, []):
                    builtin_disabled = True
                    ext_names = ", ".join(d["name"] for d in external)
                    logger.info(f"Built-in controller disabled (external: {ext_names})")
                    _ensure_external_target(composite_path)
                    if on_change:
                        try:
                            await on_change(True, len(external))
                        except Exception:
                            pass

            elif not external_present and builtin_disabled:
                target = original_target_type or DEFAULT_TARGET_TYPE
                if _set_target_devices(composite_path, [target]):
                    builtin_disabled = False
                    logger.info("Built-in controller re-enabled (no external gamepads)")
                    if on_change:
                        try:
                            await on_change(False, 0)
                        except Exception:
                            pass

        except Exception as exc:
            logger.error(f"Controller watcher error: {exc}")

        await asyncio.sleep(POLL_INTERVAL)
