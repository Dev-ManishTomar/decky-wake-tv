import asyncio
import json
import os
import sys

import decky

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
from tv_client import TVClient, send_wol, discover_mac, is_reachable  # noqa: E402


SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "tv_ip": "",
    "hdmi_input": "HDMI_1",
    "mac_address": "",
    "client_key": "",
    "paired": False,
}


class Plugin:
    _settings: dict = {}
    _settings_path: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        self._settings_path = os.path.join(
            os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "/tmp"),
            SETTINGS_FILE,
        )
        self._load_settings()
        decky.logger.info("Wake TV plugin loaded")

    async def _unload(self) -> None:
        decky.logger.info("Wake TV plugin unloaded")

    async def _uninstall(self) -> None:
        pass

    async def _migration(self) -> None:
        pass

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
        }

    async def save_settings(self, tv_ip: str, hdmi_input: str, mac_address: str) -> dict:
        self._settings["tv_ip"] = tv_ip.strip()
        self._settings["hdmi_input"] = hdmi_input.strip()
        self._settings["mac_address"] = mac_address.strip()
        self._save_settings_to_disk()
        decky.logger.info(f"Settings saved: ip={tv_ip} hdmi={hdmi_input}")
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

            mac = discover_mac(ip)
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
        if ip and key:
            for attempt in range(5):
                await asyncio.sleep(3)
                try:
                    client = TVClient(ip)
                    await client.connect()
                    await client.register(client_key=key)
                    await client.switch_input(hdmi)
                    await client.close()
                    decky.logger.info(f"HDMI switched to {hdmi} (attempt {attempt + 1})")
                    return {"ok": True}
                except Exception:
                    continue

        return {"ok": True, "note": "WOL sent, HDMI switch may not have succeeded"}

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
