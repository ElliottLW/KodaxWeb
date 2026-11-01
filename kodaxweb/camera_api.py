"""Local HTTP client for Kodak Smart Home (Hubble/CIN platform) cameras.

Every command is ``GET http://<ip>/?req=<command>[&k=v...]`` and the camera
answers ``<command>: <value>``.  Setters answer ``0`` on success and ``-1``
when rejected or unsupported by the model/firmware.
"""
from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import quote

import aiohttp

ZONE_COLS = 40
ZONE_ROWS = 23

_TIMEOUT = aiohttp.ClientTimeout(total=6)


class CameraError(Exception):
    """Raised when the camera is unreachable or rejects a command."""


async def command(
    http: aiohttp.ClientSession, ip: str, req: str, params: str = ""
) -> str:
    """Send one command and return the value after ``<req>:``."""
    url = f"http://{ip}/?req={req}{params}"
    try:
        async with http.get(url, timeout=_TIMEOUT) as resp:
            text = (await resp.text(errors="replace")).strip()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CameraError(f"{ip} unreachable: {err}") from err
    prefix = f"{req}:"
    return text[len(prefix):].strip() if text.startswith(prefix) else text


async def set_command(
    http: aiohttp.ClientSession, ip: str, req: str, params: str = ""
) -> None:
    """Send a setter and raise CameraError unless the camera answers ``0``."""
    result = await command(http, ip, req, params)
    if result != "0" and not (req == "set_night_vision" and result.lstrip("-").isdigit()):
        raise CameraError(f"camera rejected {req} ({result})")


def parse_kv(text: str) -> dict[str, str]:
    """Parse ``a=1&b=2`` into a dict."""
    out: dict[str, str] = {}
    for part in text.split("&"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key] = value
    return out


def _int(value: str | None, default: int = -1) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _triplet(value: str | None) -> list[int]:
    """Split ``enabled:x:sensitivity:y`` detection strings."""
    parts = [_int(p, 0) for p in (value or "").split(":")]
    return (parts + [0, 0, 0, 0])[:4]


# The camera's `bat` isn't a percentage. It's a two-digit code: the units digit
# is a 0-4 charge level (0/25/50/75/100 %) and a tens digit of 1 means plugged
# in, so 14 is "full, charging" and 4 is "full, on battery".
_BATTERY_LEVEL_PERCENT = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}


def battery_percent(raw: int | None) -> int | None:
    """Charge level in percent (steps of 25) from the camera's `bat` code."""
    if raw is None or raw < 0:
        return None
    return _BATTERY_LEVEL_PERCENT[min(raw % 10, 4)]


async def get_status(http: aiohttp.ClientSession, ip: str) -> dict[str, Any]:
    """Read everything the camera exposes and return a structured dict."""
    info = parse_kv(await command(http, ip, "get_caminfo"))
    if not info:
        raise CameraError(f"{ip} returned no camera info")
    versions = parse_kv((await command(http, ip, "get_all_versions")).replace(",", "&"))
    md, sd, td, lbd = (_triplet(info.get(k)) for k in ("md", "sd", "td", "lbd"))
    temp = float(info.get("tem_float", "-273") or -273)
    udid = await command(http, ip, "get_udid")
    return {
        "pan_tilt": model_from_udid(udid) in PAN_TILT_MODELS,
        "battery": battery_percent(_int(info.get("bat"), -1)),
        "charging": info.get("charge") == "1",
        "wifi_strength": _int(info.get("wifi")),
        "ssid": info.get("ssid1", ""),
        "local_ip": info.get("localip", ip),
        "dns": [d for d in (info.get("dnsm"), info.get("dnss")) if d],
        "streaming": info.get("ca") == "1",
        "firmware": await command(http, ip, "get_version"),
        "versions": versions,
        "udid": udid,
        "mac": await command(http, ip, "get_mac_address"),
        "hardware_id": info.get("hw_id", ""),
        "soc_version": info.get("soc_ver", ""),
        "repeater": info.get("rp_conn", ""),
        "temperature": None if temp <= -273 else temp,
        "humidity": None if _int(info.get("hum")) < 0 else float(info.get("hum_float", 0)),
        "sd_card": {
            "capacity_mb": _int(info.get("sdcap")),
            "free_mb": _int(info.get("sdfree")),
        },
        "low_battery_alert": lbd[0] == 1,
        "presets": (presets := await get_presets(http, ip)),
        # get_all_pos answers -5 until the motor has been calibrated; until
        # then the camera doesn't know its end-stops.
        "pan_tilt_calibrated": presets is not None,
        "settings": {
            "night_vision": _int(info.get("ir"), 0),
            "resolution": _int(info.get("res"), 720),
            "flicker": _int(info.get("flicker"), 50),
            "video_bitrate": _int(info.get("brate")),
            "ceiling_mount": info.get("flipup") == "1",
            "image_profile": _int(info.get("isp_idx"), 0),
            "motion_enabled": md[0] == 1,
            "motion_sensitivity": md[2],
            "sound_enabled": sd[0] == 1,
            "sound_sensitivity": sd[2],
            "blue_led": info.get("blue_led_en") == "1",
            "melody_volume": _int(info.get("lulvol"), 0),
            "auto_remove_clips": info.get("sdatrm") == "1",
            "clips_to_keep": _int(info.get("sdnoclips"), 10),
            "store_motion_clips": info.get("storage") == "1",
            "store_motion_snapshots": info.get("snapshot_storage") == "1",
        },
        "_raw": {"md": md, "sd": sd},
    }


def _choice(value: Any, allowed: tuple[int, ...]) -> int:
    number = int(value)
    if number not in allowed:
        raise CameraError(f"value must be one of {allowed}")
    return number


def _range(value: Any, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise CameraError(f"value must be between {low} and {high}")
    return number


async def apply_setting(
    http: aiohttp.ClientSession, ip: str, key: str, value: Any
) -> None:
    """Translate one UI setting into the camera command(s) that change it."""
    if key == "night_vision":
        await set_command(http, ip, "set_night_vision", f"&value={_choice(value, (0, 1, 2))}")
    elif key == "resolution":
        await set_command(http, ip, "set_resolution", f"&value={_choice(value, (480, 720))}")
    elif key == "flicker":
        await set_command(http, ip, "set_flicker", f"&value={_choice(value, (50, 60))}")
    elif key == "video_bitrate":
        await set_command(http, ip, "set_video_bitrate", f"&value={_range(value, 100, 2000)}")
    elif key == "ceiling_mount":
        await set_command(http, ip, "set_flipup", f"&value={int(bool(value))}")
    elif key == "image_profile":
        await set_command(http, ip, "set_isp_idx", f"&value={_range(value, 0, 3)}")
    elif key in ("motion_enabled", "motion_sensitivity", "sound_enabled", "sound_sensitivity"):
        await _apply_detection(http, ip, key, value)
    elif key == "blue_led":
        await set_command(
            http, ip, "set_blue_led", f"&enable={int(bool(value))}&on_time=180&red_led_affect=0"
        )
    elif key == "melody_volume":
        await set_command(http, ip, "melody_vol", f"&value={_range(value, 0, 5)}")
    elif key in ("auto_remove_clips", "clips_to_keep"):
        info = parse_kv(await command(http, ip, "get_caminfo"))
        enabled = int(bool(value)) if key == "auto_remove_clips" else _int(info.get("sdatrm"), 0)
        clips = _range(value, 1, 100) if key == "clips_to_keep" else _int(info.get("sdnoclips"), 10)
        await set_command(http, ip, "auto_rm_clip", f"&value={enabled}&clips={clips}")
    elif key == "store_motion_clips":
        await set_command(http, ip, "set_motion_storage", f"&value={int(bool(value))}")
    elif key == "store_motion_snapshots":
        await set_command(http, ip, "set_motion_snapshot_storage", f"&value={int(bool(value))}")
    elif key == "timezone":
        await set_command(http, ip, "set_city_timezone", f"&value={quote(str(value), safe='/')}")
    else:
        raise CameraError(f"unknown setting {key}")


async def _apply_detection(
    http: aiohttp.ClientSession, ip: str, key: str, value: Any
) -> None:
    """Motion/sound setters take several fields; keep the ones not being changed."""
    info = parse_kv(await command(http, ip, "get_caminfo"))
    if key.startswith("motion"):
        md = _triplet(info.get("md"))
        if key == "motion_enabled":
            await set_command(
                http, ip, "set_motion_source", f"&value={int(bool(value))}&schedule={md[1]}"
            )
        else:
            await set_command(
                http, ip, "set_motion_sensitivity", f"&value={_range(value, 1, 5)}"
            )
        return
    sd = _triplet(info.get("sd"))
    enabled = int(bool(value)) if key == "sound_enabled" else sd[0]
    sensitivity = _range(value, 1, 5) if key == "sound_sensitivity" else sd[2]
    await set_command(
        http,
        ip,
        "set_sound_detection",
        f"&value={enabled}&sensitivity={sensitivity}&schedule={sd[1]}",
    )


async def change_wifi(
    http: aiohttp.ClientSession, ip: str, ssid: str, password: str
) -> None:
    """Move an already-configured camera to another Wi-Fi network."""
    b64 = lambda s: quote(base64.b64encode(s.encode()).decode(), safe="")  # noqa: E731
    await set_command(http, ip, "change_router_info", f"&ssid={b64(ssid)}&password={b64(password)}")


async def get_zones(http: aiohttp.ClientSession, ip: str) -> list[list[int]]:
    """Return the motion-zone grid as ZONE_ROWS x ZONE_COLS of 0/1."""
    fields = parse_kv("status=" + await command(http, ip, "get_zone_detection"))
    raw = base64.b64decode(fields.get("value", ""))
    bits = "".join(f"{byte:08b}" for byte in raw).ljust(ZONE_ROWS * ZONE_COLS, "0")
    return [
        [int(bits[r * ZONE_COLS + c]) for c in range(ZONE_COLS)] for r in range(ZONE_ROWS)
    ]


async def set_zones(
    http: aiohttp.ClientSession, ip: str, grid: list[list[int]]
) -> None:
    """Write the motion-zone grid back to the camera."""
    if len(grid) != ZONE_ROWS or any(len(row) != ZONE_COLS for row in grid):
        raise CameraError(f"zone grid must be {ZONE_ROWS}x{ZONE_COLS}")
    bits = "".join("1" if cell else "0" for row in grid for cell in row)
    raw = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    value = quote(base64.b64encode(raw).decode(), safe="")
    await set_command(http, ip, "set_zone_detection", f"&value={value}")


async def get_setup_log(http: aiohttp.ClientSession, ip: str) -> str:
    return await command(http, ip, "get_setup_log")


async def play_melody(http: aiohttp.ClientSession, ip: str, track: int) -> None:
    await command(http, ip, f"melody{_range(track, 1, 5)}", "&duration=2")


async def stop_melody(http: aiohttp.ClientSession, ip: str) -> None:
    await command(http, ip, "melodystop")


# ── pan/tilt ───────────────────────────────────────────────────────────────
# Motor commands only work over the VLVL session channel (see
# vlvl.VLVLFrameReceiver.send_command); repeat a direction every
# ~0.5 s while held, then sends motor_stop.

PRESET_SLOTS = 5
PAN_TILT_MODELS = ("006", "008", "018")  # UDID model codes with pan/tilt
PTZ_ACTIONS = ("move", "stop", "preset_go", "preset_save", "preset_remove", "calibrate")
_DIRECTIONS = ("up", "down", "left", "right")


def model_from_udid(udid: str) -> str:
    return udid[3:6]


async def get_presets(http: aiohttp.ClientSession, ip: str) -> list[int] | None:
    """Return preset slot states, or None if the camera won't report them."""
    result = await command(http, ip, "get_all_pos")
    code, _, rest = result.partition("&")
    if code.startswith("-"):
        return None
    values = parse_kv(rest).get("value", "")
    return [_int(v, 0) for v in values.split(",")][:PRESET_SLOTS]


def ptz_command(action: str, body: dict[str, Any]) -> tuple[str, str]:
    """Build the (command, params) pair for a pan/tilt action.

    The preset_* actions have no UI at the moment: they didn't work on the
    test camera (see docs/parked-ptz-presets.md).
    """
    now_ms = int(time.time() * 1000)  # the camera expects a fresh timestamp
    if action == "move":
        direction = str(body.get("direction"))
        if direction not in _DIRECTIONS:
            raise CameraError(f"direction must be one of {_DIRECTIONS}")
        return f"motor_{direction}", f"&time={now_ms}"
    if action == "stop":
        return "motor_stop", f"&time={now_ms}"
    if action == "calibrate":
        return "motor_calibration", "&mode=1"
    slot = _range(body.get("slot"), 1, PRESET_SLOTS)
    if action == "preset_go":
        return "go_pos", f"&pos_id={slot}&moving_mode=0"
    if action == "preset_save":
        return "set_pos", f"&pos_id={slot}&stop_time=500"
    if action == "preset_remove":
        return "remove_pos", f"&pos_id={slot}"
    raise CameraError(f"unknown pan/tilt action {action}")


async def restart(http: aiohttp.ClientSession, ip: str) -> None:
    try:
        await command(http, ip, "restart_system")
    except CameraError:
        pass  # the camera often drops the connection while rebooting
