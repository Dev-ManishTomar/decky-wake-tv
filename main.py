import asyncio
import json
import os
import sys

import decky

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
from tv_client import TVClient, send_wol, discover_mac, is_reachable  # noqa: E402
from input_watcher import watch_guide_button  # noqa: E402
from sleep_watcher import watch_sleep_resume  # noqa: E402
from usb_rebind import rebind_external_gamepads  # noqa: E402


SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "tv_ip": "",
    "hdmi_input": "HDMI_1",
    "mac_address": "",
    "client_key": "",
    "paired": False,
    "wake_on_guide_button": True,
    "wake_on_resume": True,
}


class Plugin:
    _settings: dict = {}
    _settings_path: str = ""
    _guide_task: asyncio.Task | None = None
    _sleep_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        self._settings_path = os.path.join(
            os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "/tmp"),
            SETTINGS_FILE,
        )
        self._load_settings()
        decky.logger.info(
            f"Wake TV plugin loading | ip={self._settings.get('tv_ip', '')} "
            f"mac={self._settings.get('mac_address', '')} "
            f"hdmi={self._settings.get('hdmi_input', '')} "
            f"paired={self._settings.get('paired', False)} "
            f"guide={self._settings.get('wake_on_guide_button', True)} "
            f"resume={self._settings.get('wake_on_resume', True)}"
        )
        self._start_watchers()

        if self._settings.get("wake_on_resume", True) and self._settings.get("mac_address"):
            asyncio.get_event_loop().create_task(self._startup_wake())

        decky.logger.info("Wake TV plugin loaded, all background tasks started")

    async def _unload(self) -> None:
        self._stop_watchers()
        decky.logger.info("Wake TV plugin unloaded")

    async def _uninstall(self) -> None:
        pass

    async def _migration(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Background watchers
    # ------------------------------------------------------------------

    def _start_watchers(self) -> None:
        loop = asyncio.get_event_loop()
        if self._settings.get("wake_on_guide_button", True):
            self._guide_task = loop.create_task(self._watch_guide_button())
            decky.logger.info("Guide button watcher started")
        if self._settings.get("wake_on_resume", True):
            self._sleep_task = loop.create_task(self._watch_sleep_resume())
            decky.logger.info("Sleep resume watcher started")

    def _stop_watchers(self) -> None:
        if self._guide_task and not self._guide_task.done():
            self._guide_task.cancel()
            self._guide_task = None
        if self._sleep_task and not self._sleep_task.done():
            self._sleep_task.cancel()
            self._sleep_task = None

    def _restart_watchers(self) -> None:
        self._stop_watchers()
        self._start_watchers()

    async def _watch_guide_button(self) -> None:
        try:
            await watch_guide_button(self._do_wake)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            decky.logger.error(f"Guide button watcher crashed: {exc}")

    async def _watch_sleep_resume(self) -> None:
        try:
            await watch_sleep_resume(on_resume=self._on_resume)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            decky.logger.error(f"Sleep watcher crashed: {exc}")

    async def _on_resume(self) -> None:
        """Called when system resumes: rebind gamepads and wait for network in parallel, then wake TV."""
        import socket

        async def _rebind():
            try:
                loop = asyncio.get_event_loop()
                count = await loop.run_in_executor(None, rebind_external_gamepads)
                decky.logger.info(f"Post-resume: rebound {count} gamepad(s)")
            except Exception as exc:
                decky.logger.warning(f"Post-resume: gamepad rebind failed: {exc}")

        async def _wait_for_network():
            for i in range(15):
                await asyncio.sleep(1)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.settimeout(0.5)
                    s.connect(("8.8.8.8", 80))
                    decky.logger.info(f"Post-resume: network up after {i+1}s")
                    return
                except OSError:
                    pass
                finally:
                    s.close()
            decky.logger.warning("Post-resume: network not ready after 15s, trying wake anyway")

        decky.logger.info("Post-resume: rebinding gamepads + waiting for network...")
        await asyncio.gather(_rebind(), _wait_for_network())
        await self._do_wake()

    async def _startup_wake(self) -> None:
        """
        Fire a wake on plugin startup. Covers the case where the Deck just
        resumed from sleep but the D-Bus signal was missed because the plugin
        was restarting. Small delay to let the network come up.
        """
        decky.logger.info("Startup wake: waiting 5s for network to stabilize...")
        await asyncio.sleep(5)

        ip = self._settings.get("tv_ip", "")
        if ip:
            reachable = await is_reachable(ip, timeout=3.0)
            decky.logger.info(f"Startup wake: TV reachable check = {reachable}")
            if not reachable:
                decky.logger.info("Startup wake: TV appears off, firing auto-wake")
                await self._do_wake()
            else:
                decky.logger.info("Startup wake: TV already on, skipping")
        else:
            decky.logger.info("Startup wake: no TV IP configured, skipping")

    async def _do_wake(self) -> None:
        """Shared wake logic: send WOL then try to connect and switch HDMI."""
        mac = self._settings.get("mac_address", "")
        ip = self._settings.get("tv_ip", "")
        hdmi = self._settings.get("hdmi_input", "HDMI_1")
        key = self._settings.get("client_key", "")

        decky.logger.info(
            f"Auto-wake triggered | mac={mac} ip={ip} hdmi={hdmi} has_key={bool(key)}"
        )

        if not mac:
            decky.logger.warning("Auto-wake skipped: no MAC address configured")
            return

        try:
            send_wol(mac)
            decky.logger.info(f"Auto-wake: WOL magic packet sent to {mac}")
        except Exception as exc:
            decky.logger.error(f"Auto-wake: WOL send failed: {exc}")
            return

        if not (ip and key):
            decky.logger.info("Auto-wake: no IP/key, skipping HDMI switch")
            return

        for attempt in range(5):
            decky.logger.info(f"Auto-wake: HDMI switch attempt {attempt + 1}/5, waiting 3s...")
            await asyncio.sleep(3)
            client = TVClient(ip)
            try:
                await client.connect()
                decky.logger.info(f"Auto-wake: connected to TV at {ip}")
                await client.register(client_key=key)
                decky.logger.info("Auto-wake: registered with TV")
                await client.switch_input(hdmi)
                decky.logger.info(f"Auto-wake: HDMI switched to {hdmi} (attempt {attempt + 1})")
                return
            except Exception as exc:
                decky.logger.info(f"Auto-wake: attempt {attempt + 1} failed: {exc}")
                continue
            finally:
                await client.close()
        decky.logger.warning("Auto-wake: HDMI switch failed after 5 attempts")

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            if os.path.isfile(self._settings_path):
                with open(self._settings_path, "r") as f:
                    self._settings = {**DEFAULT_SETTINGS, **json.load(f)}
                    return
        except Exception as exc:
            decky.logger.warning(f"Failed to load settings: {exc}")
        self._settings = dict(DEFAULT_SETTINGS)

    def _save_settings_to_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._settings_path), exist_ok=True)
            with open(self._settings_path, "w") as f:
                json.dump(self._settings, f, indent=2)
        except Exception as exc:
            decky.logger.warning(f"Failed to save settings: {exc}")

    # ------------------------------------------------------------------
    # Frontend-callable methods
    # ------------------------------------------------------------------

    async def get_settings(self) -> dict:
        return {
            "tv_ip": self._settings.get("tv_ip", ""),
            "hdmi_input": self._settings.get("hdmi_input", "HDMI_1"),
            "mac_address": self._settings.get("mac_address", ""),
            "paired": self._settings.get("paired", False),
            "wake_on_guide_button": self._settings.get("wake_on_guide_button", True),
            "wake_on_resume": self._settings.get("wake_on_resume", True),
        }

    async def save_settings(
        self,
        tv_ip: str,
        hdmi_input: str,
        mac_address: str,
        wake_on_guide_button: bool = True,
        wake_on_resume: bool = True,
    ) -> dict:
        self._settings["tv_ip"] = tv_ip.strip()
        self._settings["hdmi_input"] = hdmi_input.strip()
        self._settings["mac_address"] = mac_address.strip()
        self._settings["wake_on_guide_button"] = wake_on_guide_button
        self._settings["wake_on_resume"] = wake_on_resume
        self._save_settings_to_disk()
        self._restart_watchers()
        decky.logger.info(
            f"Settings saved: ip={tv_ip} hdmi={hdmi_input} "
            f"guide={wake_on_guide_button} resume={wake_on_resume}"
        )
        return {"ok": True}

    async def pair_tv(self) -> dict:
        ip = self._settings.get("tv_ip", "")
        if not ip:
            return {"ok": False, "error": "TV IP not configured"}

        client = TVClient(ip)
        try:
            await client.connect()
            existing_key = self._settings.get("client_key") or None
            key = await client.register(client_key=existing_key)
            self._settings["client_key"] = key
            self._settings["paired"] = True

            loop = asyncio.get_event_loop()
            mac = await loop.run_in_executor(None, discover_mac, ip)
            if mac:
                self._settings["mac_address"] = mac

            self._save_settings_to_disk()
            decky.logger.info(f"Paired with TV at {ip}")
            return {"ok": True, "mac_address": mac or self._settings.get("mac_address", "")}
        except Exception as exc:
            decky.logger.error(f"Pairing failed: {exc}")
            return {"ok": False, "error": str(exc)}
        finally:
            await client.close()

    async def wake_tv(self) -> dict:
        mac = self._settings.get("mac_address", "")
        if not mac:
            return {"ok": False, "error": "MAC address not configured"}

        try:
            send_wol(mac)
            decky.logger.info(f"WOL sent to {mac}")
        except Exception as exc:
            decky.logger.error(f"WOL failed: {exc}")
            return {"ok": False, "error": str(exc)}

        ip = self._settings.get("tv_ip", "")
        hdmi = self._settings.get("hdmi_input", "HDMI_1")
        key = self._settings.get("client_key", "")
        if not (ip and key):
            return {"ok": True, "note": "WOL sent but no IP/key configured for HDMI switch"}

        for attempt in range(5):
            await asyncio.sleep(3)
            client = TVClient(ip)
            try:
                await client.connect()
                await client.register(client_key=key)
                await client.switch_input(hdmi)
                decky.logger.info(f"HDMI switched to {hdmi} (attempt {attempt + 1})")
                return {"ok": True}
            except Exception:
                continue
            finally:
                await client.close()

        decky.logger.warning("HDMI switch failed after 5 attempts")
        return {"ok": False, "error": "WOL sent but HDMI switch failed after 5 attempts"}

    async def turn_off_tv(self) -> dict:
        ip = self._settings.get("tv_ip", "")
        key = self._settings.get("client_key", "")
        if not ip or not key:
            return {"ok": False, "error": "TV not configured or not paired"}

        client = TVClient(ip)
        try:
            await client.connect()
            await client.register(client_key=key)
            await client.power_off()
            decky.logger.info("TV turned off")
            return {"ok": True}
        except Exception as exc:
            decky.logger.error(f"Turn off failed: {exc}")
            return {"ok": False, "error": str(exc)}
        finally:
            await client.close()

    async def switch_hdmi(self, input_id: str) -> dict:
        ip = self._settings.get("tv_ip", "")
        key = self._settings.get("client_key", "")
        if not ip or not key:
            return {"ok": False, "error": "TV not configured or not paired"}

        client = TVClient(ip)
        try:
            await client.connect()
            await client.register(client_key=key)
            await client.switch_input(input_id)
            decky.logger.info(f"Switched to {input_id}")
            return {"ok": True}
        except Exception as exc:
            decky.logger.error(f"HDMI switch failed: {exc}")
            return {"ok": False, "error": str(exc)}
        finally:
            await client.close()

    async def get_status(self) -> dict:
        ip = self._settings.get("tv_ip", "")
        if not ip:
            return {"reachable": False}
        reachable = await is_reachable(ip)
        return {"reachable": reachable}
