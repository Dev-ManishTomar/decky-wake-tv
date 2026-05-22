"""
Self-contained TV control client.

Implements the webOS SSAP WebSocket protocol over stdlib-only asyncio
for pairing, power control, and HDMI input switching.  Also provides
Wake-on-LAN and MAC address discovery helpers.

No external dependencies -- uses only the Python standard library.
"""

import asyncio
import hashlib
import base64
import json
import os
import socket
import ssl
import struct
import subprocess

# ---------------------------------------------------------------------------
# SSAP registration payload (webOS)
# ---------------------------------------------------------------------------

_SIGNATURE = (
    "eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbm"
    "ctY2VydCIsInNpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR"
    "+59aFNwYDyjQgKk3auukd7pcegmE2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRy"
    "aMOv5zWSrthlf7G128qvIlpMT0YNY+n/FaOHE73uLrS/g7swl3/qH/BGFG2Hu4"
    "RlL48eb3lLKqTt2xKHdCs6Cd4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A27Pq1n"
    "50cMchmcaXadJhGrOqH5YmHdOCj5NSHzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM"
    "2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRLW5hA29yeAwaCViZNCP8iC9aO0q9fQoj"
    "oa7NQnAtw=="
)

_REGISTRATION_PAYLOAD: dict = {
    "forcePairing": False,
    "manifest": {
        "appVersion": "1.1",
        "manifestVersion": 1,
        "permissions": [
            "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE",
            "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO",
            "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
            "CONTROL_INPUT_MEDIA_RECORDING",
            "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV",
            "CONTROL_POWER", "READ_APP_STATUS",
            "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
            "READ_NETWORK_STATE", "READ_RUNNING_APPS",
            "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST",
            "READ_POWER_STATE", "READ_COUNTRY_INFO", "READ_SETTINGS",
            "CONTROL_TV_SCREEN", "CONTROL_TV_STANBY",
            "CONTROL_FAVORITE_GROUP", "CONTROL_USER_INFO",
            "CHECK_BLUETOOTH_DEVICE", "CONTROL_BLUETOOTH",
            "CONTROL_TIMER_INFO", "STB_INTERNAL_CONNECTION",
            "CONTROL_RECORDING", "READ_RECORDING_STATE",
            "WRITE_RECORDING_LIST", "READ_RECORDING_LIST",
            "READ_RECORDING_SCHEDULE", "WRITE_RECORDING_SCHEDULE",
            "READ_STORAGE_DEVICE_LIST", "READ_TV_PROGRAM_INFO",
            "CONTROL_BOX_CHANNEL", "READ_TV_ACR_AUTH_TOKEN",
            "READ_TV_CONTENT_STATE", "READ_TV_CURRENT_TIME",
            "ADD_LAUNCHER_CHANNEL", "SET_CHANNEL_SKIP",
            "RELEASE_CHANNEL_SKIP", "CONTROL_CHANNEL_BLOCK",
            "DELETE_SELECT_CHANNEL", "CONTROL_CHANNEL_GROUP",
            "SCAN_TV_CHANNELS", "CONTROL_TV_POWER", "CONTROL_WOL",
        ],
        "signatures": [{"signature": _SIGNATURE, "signatureVersion": 1}],
        "signed": {
            "appId": "com.lge.test",
            "created": "20140509",
            "localizedAppNames": {
                "": "LG Remote App",
                "ko-KR": "\ub9ac\ubaa8\ucee8 \uc571",
                "zxx-XX": "\u041b\u0413 R\u044d\u043c\u043e\u0442\u044d A\u041f\u041f",
            },
            "localizedVendorNames": {"": "LG Electronics"},
            "permissions": [
                "TEST_SECURE", "CONTROL_INPUT_TEXT",
                "CONTROL_MOUSE_AND_KEYBOARD", "READ_INSTALLED_APPS",
                "READ_LGE_SDX", "READ_NOTIFICATIONS", "SEARCH",
                "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
                "CONTROL_POWER", "READ_CURRENT_CHANNEL",
                "READ_RUNNING_APPS", "READ_UPDATE_INFO",
                "UPDATE_FROM_REMOTE_APP", "READ_LGE_TV_INPUT_EVENTS",
                "READ_TV_CURRENT_TIME",
            ],
            "serial": "2f930e2d2cfe083771f68e4fe7bb07",
            "vendorId": "com.lge",
        },
    },
    "pairingType": "PROMPT",
}

# ---------------------------------------------------------------------------
# Minimal RFC-6455 WebSocket helpers (no external deps)
# ---------------------------------------------------------------------------

def _ws_handshake_request(host: str, port: int) -> tuple[bytes, str]:
    """Build an HTTP/1.1 WebSocket upgrade request and return (request_bytes, expected_accept)."""
    raw_key = base64.b64encode(os.urandom(16)).decode()
    magic = "258EAFA5-E914-47DA-95CA-5AB9A10DC8B6"
    accept = base64.b64encode(hashlib.sha1((raw_key + magic).encode()).digest()).decode()
    lines = [
        f"GET / HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {raw_key}",
        "Sec-WebSocket-Version: 13",
        "", "",
    ]
    return "\r\n".join(lines).encode(), accept


def _ws_encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode a masked WebSocket frame (client must mask)."""
    mask_key = os.urandom(4)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    length = len(payload)

    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)

    return header + mask_key + masked


async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one WebSocket frame, return (opcode, payload)."""
    first_two = await reader.readexactly(2)
    opcode = first_two[0] & 0x0F
    masked = bool(first_two[1] & 0x80)
    length = first_two[1] & 0x7F

    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]

    mask_key = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(length)

    if mask_key:
        data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

    return opcode, data

# ---------------------------------------------------------------------------
# TVClient
# ---------------------------------------------------------------------------

class TVClient:
    """Async client for controlling a TV over WebSocket (SSAP protocol)."""

    def __init__(self, host: str, port: int = 3001, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._msg_id = 0

    async def connect(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx),
            timeout=self.timeout,
        )

        req, expected_accept = _ws_handshake_request(self.host, self.port)
        self._writer.write(req)
        await self._writer.drain()

        # Read HTTP response until blank line
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await asyncio.wait_for(self._reader.read(4096), timeout=self.timeout)
            if not chunk:
                raise ConnectionError("Connection closed during WebSocket handshake")
            response += chunk

        if b"101" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.write(_ws_encode_frame(b"", opcode=0x8))
                await self._writer.drain()
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _send_json(self, obj: dict) -> None:
        assert self._writer is not None
        data = json.dumps(obj).encode()
        self._writer.write(_ws_encode_frame(data))
        await self._writer.drain()

    async def _recv_json(self) -> dict:
        assert self._reader is not None
        opcode, data = await asyncio.wait_for(
            _ws_read_frame(self._reader), timeout=self.timeout
        )
        if opcode == 0x8:
            raise ConnectionError("WebSocket closed by TV")
        if opcode == 0x9:  # ping
            assert self._writer is not None
            self._writer.write(_ws_encode_frame(data, opcode=0xA))
            await self._writer.drain()
            return await self._recv_json()
        return json.loads(data.decode())

    def _next_id(self) -> str:
        self._msg_id += 1
        return f"msg_{self._msg_id}"

    async def send_command(self, uri: str, payload: dict | None = None) -> dict:
        msg: dict = {"type": "request", "id": self._next_id(), "uri": uri}
        if payload is not None:
            msg["payload"] = payload
        await self._send_json(msg)
        return await self._recv_json()

    async def register(self, client_key: str | None = None) -> str:
        """
        Send registration request. Returns the client key on success.
        The TV will show a pairing prompt if no valid key is provided.
        """
        payload = dict(_REGISTRATION_PAYLOAD)
        if client_key:
            payload["client-key"] = client_key

        msg = {"type": "register", "id": self._next_id(), "payload": payload}
        await self._send_json(msg)

        unexpected = 0
        while True:
            resp = await asyncio.wait_for(self._recv_json(), timeout=60)
            resp_type = resp.get("type", "")
            resp_payload = resp.get("payload", {})

            if resp_type == "registered":
                return resp_payload.get("client-key", client_key or "")
            if resp_payload.get("pairingType") == "PROMPT":
                continue
            if resp_type == "error":
                raise RuntimeError(f"Registration failed: {resp_payload}")

            unexpected += 1
            if unexpected >= 10:
                raise RuntimeError(
                    f"Registration failed: too many unexpected responses (last: {resp})"
                )

    async def power_off(self) -> dict:
        return await self.send_command("ssap://system/turnOff")

    async def switch_input(self, input_id: str) -> dict:
        return await self.send_command(
            "ssap://tv/switchInput", {"inputId": input_id}
        )

    async def get_inputs(self) -> dict:
        return await self.send_command("ssap://tv/getExternalInputList")

    async def get_system_info(self) -> dict:
        return await self.send_command("ssap://system/getSystemInfo")



# ---------------------------------------------------------------------------
# Wake-on-LAN
# ---------------------------------------------------------------------------

def send_wol(mac_address: str) -> None:
    """Send a Wake-on-LAN magic packet to the given MAC address."""
    mac_clean = mac_address.replace(":", "").replace("-", "").replace(".", "")
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac_address}")

    mac_bytes = bytes.fromhex(mac_clean)
    magic = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, ("255.255.255.255", 9))

# ---------------------------------------------------------------------------
# MAC address discovery
# ---------------------------------------------------------------------------

def discover_mac(ip: str) -> str | None:
    """Try to find a MAC address for the given IP from the ARP table."""
    for cmd in (["ip", "neigh", "show", ip], ["arp", "-n", ip]):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode()
            for token in out.split():
                cleaned = token.replace(":", "").replace("-", "")
                if len(cleaned) == 12:
                    try:
                        int(cleaned, 16)
                        return token.upper()
                    except ValueError:
                        continue
        except Exception:
            continue
    return None

# ---------------------------------------------------------------------------
# TCP reachability check
# ---------------------------------------------------------------------------

async def is_reachable(host: str, port: int = 3001, timeout: float = 3.0) -> bool:
    """Quick TCP connect to check if the TV is reachable."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False
