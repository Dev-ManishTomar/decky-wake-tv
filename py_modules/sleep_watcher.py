"""
System sleep/resume watcher.

Monitors the D-Bus PrepareForSleep signal from systemd-logind.
- PrepareForSleep(true)  = going to sleep  -> fires on_sleep callback
- PrepareForSleep(false) = resuming        -> fires on_resume callback

Falls back to polling /proc/uptime if dbus-monitor is unavailable.
"""

import asyncio
import time
import logging

logger = logging.getLogger("sleep_watcher")

DBUS_MONITOR_CMD = [
    "dbus-monitor",
    "--system",
    "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
]


async def watch_sleep_resume(on_resume, on_sleep=None) -> None:
    """
    Watch for system sleep/resume events.
    - on_resume(): called when system wakes from sleep
    - on_sleep():  called when system is about to sleep (optional)
    """
    try:
        await _watch_via_dbus(on_resume, on_sleep)
    except Exception as exc:
        logger.warning(f"D-Bus watcher failed ({exc}), falling back to uptime polling")
        await _watch_via_uptime(on_resume)


async def _watch_via_dbus(on_resume, on_sleep=None) -> None:
    """Monitor PrepareForSleep via dbus-monitor subprocess."""
    proc = await asyncio.create_subprocess_exec(
        *DBUS_MONITOR_CMD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("Sleep watcher started (dbus-monitor)")

    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").strip()

            if "boolean true" in decoded:
                logger.info("System going to sleep (D-Bus signal)")
                if on_sleep:
                    try:
                        await on_sleep()
                    except Exception as exc:
                        logger.error(f"Sleep callback failed: {exc}")

            elif "boolean false" in decoded:
                logger.info("System resumed from sleep (D-Bus signal)")
                try:
                    await on_resume()
                except Exception as exc:
                    logger.error(f"Resume wake callback failed: {exc}")
    finally:
        try:
            proc.terminate()
            await proc.wait()
        except Exception:
            pass


def _read_uptime() -> float:
    """Read system uptime in seconds from /proc/uptime."""
    with open("/proc/uptime", "r") as f:
        return float(f.read().split()[0])


async def _watch_via_uptime(on_resume) -> None:
    """
    Fallback: detect resume by noticing a large wall-clock jump
    relative to uptime progression. Cannot detect pre-sleep in this mode.
    """
    logger.info("Sleep watcher started (uptime polling fallback)")
    prev_wall = time.monotonic()
    prev_uptime = _read_uptime()

    while True:
        await asyncio.sleep(5)
        try:
            now_wall = time.monotonic()
            now_uptime = _read_uptime()

            wall_delta = now_wall - prev_wall
            uptime_delta = now_uptime - prev_uptime

            if wall_delta - uptime_delta > 10:
                logger.info(
                    f"System resume detected (wall +{wall_delta:.0f}s, uptime +{uptime_delta:.0f}s)"
                )
                try:
                    await on_resume()
                except Exception as exc:
                    logger.error(f"Resume wake callback failed: {exc}")

            prev_wall = now_wall
            prev_uptime = now_uptime
        except Exception:
            await asyncio.sleep(5)
