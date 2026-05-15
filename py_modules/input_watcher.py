"""
Gamepad Guide/Home button watcher using raw /dev/input/event* reading.

Uses only Python stdlib (struct, os, select, asyncio) -- no evdev dependency.
Monitors all gamepad input devices for BTN_MODE (code 316) presses and
fires a callback when detected.
"""

import asyncio
import glob
import os
import select
import struct
import time
import logging

logger = logging.getLogger("input_watcher")

EV_KEY = 0x01
BTN_MODE = 316
RESCAN_INTERVAL = 30
COOLDOWN_SECONDS = 5
MIN_RESCAN_INTERVAL = 3

# struct input_event: struct timeval (long, long), __u16 type, __u16 code, __s32 value
# On 64-bit Linux: timeval is two 8-byte longs
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def _read_sysfs(path: str) -> str:
    """Read a sysfs file, return stripped content or empty string."""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def _device_has_key_capability(event_path: str, key_code: int) -> bool:
    """Check if an input device supports a specific key code via sysfs capabilities."""
    basename = os.path.basename(event_path)
    caps_path = f"/sys/class/input/{basename}/device/capabilities/key"
    caps_hex = _read_sysfs(caps_path)
    if not caps_hex:
        return False

    # capabilities/key is a space-separated hex bitmask, LSB on the right
    bits = 0
    for i, chunk in enumerate(reversed(caps_hex.split())):
        bits |= int(chunk, 16) << (i * 64)
    return bool(bits & (1 << key_code))


def find_gamepad_devices(button_code: int = BTN_MODE) -> list[str]:
    """Find /dev/input/event* devices that can emit the target button code.

    Skips InputPlumber virtual devices (no phys path) to avoid watching
    devices that churn when ManageAllDevices is enabled.
    """
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if _device_has_key_capability(path, button_code):
            basename = os.path.basename(path)
            name = _read_sysfs(f"/sys/class/input/{basename}/device/name")
            phys = _read_sysfs(f"/sys/class/input/{basename}/device/phys")
            if not phys:
                logger.info(f"Skipping virtual device: {name or 'unknown'} ({path})")
                continue
            devices.append(path)
            logger.info(f"Gamepad found: {name or 'unknown'} ({path})")
    return devices


async def watch_guide_button(callback, button_code: int = BTN_MODE) -> None:
    """
    Continuously watch for guide button presses on all gamepads.
    Calls `callback()` (async) on each press with a cooldown.
    Re-scans for new devices every RESCAN_INTERVAL seconds.
    """
    last_trigger = 0.0
    open_fds: dict[str, int] = {}

    def scan_and_open():
        nonlocal open_fds
        new_fds: dict[str, int] = {}
        paths = find_gamepad_devices(button_code)

        for path in paths:
            if path in open_fds:
                new_fds[path] = open_fds.pop(path)
            else:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    new_fds[path] = fd
                    logger.info(f"Watching: {path}")
                except PermissionError:
                    logger.warning(f"No permission to read {path}")
                except Exception as exc:
                    logger.warning(f"Cannot open {path}: {exc}")

        for path, fd in open_fds.items():
            try:
                os.close(fd)
            except Exception:
                pass
            logger.info(f"Stopped watching: {path}")
        open_fds = new_fds

    scan_and_open()
    last_scan = time.monotonic()

    try:
        while True:
            if not open_fds:
                await asyncio.sleep(RESCAN_INTERVAL)
                scan_and_open()
                last_scan = time.monotonic()
                continue

            fd_to_path = {fd: path for path, fd in open_fds.items()}
            try:
                ready, _, _ = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: select.select(list(fd_to_path.keys()), [], [], 2.0),
                )
            except Exception:
                elapsed = time.monotonic() - last_scan
                if elapsed < MIN_RESCAN_INTERVAL:
                    await asyncio.sleep(MIN_RESCAN_INTERVAL - elapsed)
                scan_and_open()
                last_scan = time.monotonic()
                continue

            for fd in ready:
                try:
                    while True:
                        data = os.read(fd, EVENT_SIZE)
                        if len(data) < EVENT_SIZE:
                            break
                        _tv_sec, _tv_usec, ev_type, ev_code, ev_value = struct.unpack(
                            EVENT_FORMAT, data
                        )
                        if ev_type == EV_KEY and ev_code == button_code and ev_value == 1:
                            now = time.monotonic()
                            if now - last_trigger >= COOLDOWN_SECONDS:
                                last_trigger = now
                                path = fd_to_path.get(fd, "?")
                                logger.info(f"Guide button pressed on {path}")
                                try:
                                    await callback()
                                except Exception as exc:
                                    logger.error(f"Wake callback failed: {exc}")
                except BlockingIOError:
                    pass
                except OSError:
                    path = fd_to_path.get(fd, "?")
                    logger.info(f"Device lost: {path}, will rescan")
                    elapsed = time.monotonic() - last_scan
                    if elapsed < MIN_RESCAN_INTERVAL:
                        await asyncio.sleep(MIN_RESCAN_INTERVAL - elapsed)
                    scan_and_open()
                    last_scan = time.monotonic()
                    break

            now = time.monotonic()
            if now - last_scan >= RESCAN_INTERVAL:
                scan_and_open()
                last_scan = now
    finally:
        for fd in open_fds.values():
            try:
                os.close(fd)
            except Exception:
                pass
