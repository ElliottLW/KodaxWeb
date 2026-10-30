"""First-time Wi-Fi setup for a Kodak camera in pairing (hotspot) mode.

The local part of pairing; the steps that registered the camera with Kodak's
(now discontinued) cloud are no longer needed:

1. The PC joins the camera's own Wi-Fi (``KodakCam-...``); the camera answers
   on 192.168.193.1 (or 192.168.0.254 / 192.168.222.1 on some models).
2. A random AES-128 key is RSA-PKCS1 encrypted with the camera maker's RSA
   public key (``data/public.pem``, supplied by the user, see README) and sent
   with ``set_sec_type``.
3. Timezone is sent with ``set_server_auth`` / ``set_city_timezone``.
4. The home Wi-Fi details are packed into a fixed-width string,
   AES-CBC encrypted (IV ``cinatic_2018`` NUL-padded) and sent with
   ``set_nwk_info_v2&setup=<hex>``.
5. ``restart_system``. The camera stores the network but stays on its own
   hotspot until this; the link usually drops before it answers.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, quote_plus

import aiohttp
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOTSPOT_IPS = ("192.168.193.1", "192.168.0.254", "192.168.222.1")
SSID_PREFIXES = ("KodakCam-", "Camera-")
_IV = b"cinatic_2018".ljust(16, b"\0")
_TIMEOUT = aiohttp.ClientTimeout(total=8)


async def _send(
    http: aiohttp.ClientSession,
    ip: str,
    cmd: str,
    params: str = "",
    timeout: aiohttp.ClientTimeout = _TIMEOUT,
) -> str:
    """Send a setup-mode command, falling back to the older URL form."""
    last = ""
    for url in (
        f"http://{ip}/?req={cmd}{params}",
        f"http://{ip}/?action=command&command={cmd}{params}",
    ):
        try:
            async with http.get(url, timeout=timeout) as resp:
                last = (await resp.text(errors="replace")).strip()
        except (aiohttp.ClientError, TimeoutError) as err:
            last = f"error: {err}"
            continue
        if last.startswith(f"{cmd}:"):
            return last[len(cmd) + 1:].strip()
    return last


def _ok(result: str) -> bool:
    return result == "0"


def _left_pairing(result: str) -> bool:
    """The hotspot dropped. That is the camera rebooting, not a rejected command."""
    return result.startswith("error:")


def _is_udid(reply: str) -> bool:
    """A camera ID, not an error message or a web page."""
    return len(reply) >= 12 and reply.isalnum()


def _aes_hex(key: bytes, text: str) -> str:
    """Zero-pad to 16, then PKCS5, AES-CBC, hex."""
    data = text.encode()
    if len(data) % 16:
        data += b"\0" * (16 - len(data) % 16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(_IV)).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


def _rsa_hex(key: bytes, public_key_path: Path) -> str:
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    return public_key.encrypt(key, rsa_padding.PKCS1v15()).hex()  # type: ignore[union-attr]


def build_network_request(ssid: str, password: str, security: str) -> str:
    """The Wi-Fi details string the camera expects (WPA or open networks)."""
    if security.upper() == "OPEN":
        mode, key_index, password = "0", "0", ""
    else:
        mode, key_index = "2", "0"
    b64_ssid = base64.b64encode(ssid.encode()).decode()
    b64_pass = base64.b64encode(password.encode()).decode()
    core = (
        "1" + "00" + mode + key_index + "0"
        + f"{len(b64_ssid):03d}" + f"{len(b64_pass):02d}"
        + "00" + "00" + "00" + "0"
        + "00" + "00"
        + b64_ssid + b64_pass
    )
    return quote_plus(core)


def timezone_offset(minutes: int) -> str:
    """Format a UTC offset the way the app does (``+01.00``)."""
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}.{minutes % 60:02d}"


async def find_camera(
    http: aiohttp.ClientSession, ips: tuple[str, ...] = HOTSPOT_IPS
) -> dict[str, Any] | None:
    """Look for a camera in setup mode on the hotspot addresses."""
    quick = aiohttp.ClientTimeout(total=3)
    udids = await asyncio.gather(
        *(_send(http, ip, "get_udid", timeout=quick) for ip in ips)
    )
    for ip, udid in zip(ips, udids):
        if _is_udid(udid):
            return {
                "ip": ip,
                "udid": udid,
                "firmware": await _send(http, ip, "get_version"),
                "mac": await _send(http, ip, "get_mac_address"),
            }
    return None


async def run_setup(
    http: aiohttp.ClientSession,
    public_key: Path,
    ip: str,
    ssid: str,
    password: str,
    security: str,
    timezone_name: str,
    utc_offset_minutes: int,
    flicker: int,
) -> AsyncIterator[dict[str, Any]]:
    """Run the pairing steps, yielding a progress record for each."""
    if not public_key.is_file():
        yield {"step": "validate", "ok": False,
               "detail": f"{public_key} is missing; see 'Setting up a new camera' in the README"}
        return
    if len(ssid.encode()) > 32 or not ssid:
        yield {"step": "validate", "ok": False, "detail": "SSID must be 1-32 bytes"}
        return
    if security.upper() != "OPEN" and not 8 <= len(password) <= 63:
        yield {"step": "validate", "ok": False, "detail": "WPA password must be 8-63 characters"}
        return

    udid = await _send(http, ip, "get_udid")
    yield {"step": "Contact camera", "ok": _is_udid(udid), "detail": udid}
    if not _is_udid(udid):
        return

    result = await _send(http, ip, "set_flicker", f"&value={flicker}")
    yield {"step": "Set mains frequency", "ok": _ok(result), "detail": result}

    secret = os.urandom(16)
    result = await _send(http, ip, "set_sec_type", f"&value={_rsa_hex(secret, public_key)}")
    yield {"step": "Exchange encryption key", "ok": _ok(result), "detail": result}
    if not _ok(result):
        return

    offset = timezone_offset(utc_offset_minutes)
    result = await _send(
        http, ip, "set_server_auth", f"&value=local&timezone={quote(offset)}"
    )
    # The cloud token is meaningless now; only the timezone part matters.
    yield {"step": "Set UTC offset", "ok": _ok(result), "detail": result, "optional": True}

    result = await _send(
        http, ip, "set_city_timezone", f"&value={quote(timezone_name, safe='/')}"
    )
    yield {"step": "Set timezone", "ok": _ok(result), "detail": result, "optional": True}

    payload = _aes_hex(secret, build_network_request(ssid, password, security))
    for attempt in range(3):
        result = await _send(http, ip, "set_nwk_info_v2", f"&setup={payload}")
        if _ok(result) or _left_pairing(result):
            break
    stored = _ok(result)
    left = _left_pairing(result)
    # A dropped link here means the camera already started leaving pairing mode.
    # An explicit non-zero reply, while it is still answering, is a real rejection.
    yield {
        "step": "Send Wi-Fi details",
        "ok": stored or left,
        "detail": "left pairing mode" if left else result,
        "finished": left,
    }
    if not stored:
        return

    # The network is stored. restart_system is what makes it leave the hotspot;
    # the reply often never arrives, and a missing reply is still a success.
    # Joining the new network takes about a minute after this.
    result = await _send(
        http, ip, "restart_system", timeout=aiohttp.ClientTimeout(total=12)
    )
    yield {
        "step": "Restart camera",
        "ok": True,
        "detail": "left pairing mode" if _left_pairing(result) or _ok(result) else result,
        "finished": True,
    }
