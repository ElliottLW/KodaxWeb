"""KodaxWeb: local web app for Kodak Smart Home cameras.

Run:  py -m kodaxweb            (http://127.0.0.1:8099)
      py -m kodaxweb --host 0.0.0.0 --port 8099   (expose on the LAN — no login!)
      py -m kodaxweb --data-dir /var/lib/kodaxweb  (keep your data elsewhere)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web
import aiohttp

from . import camera_api, setup_flow
from .live import AUDIO_AVAILABLE, LiveHub
from .ptz_limits import SoftLimits

STATIC = Path(__file__).resolve().parent / "static"
# Your files (cameras.json, public.pem): git-ignored, kept out of the code.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_LOGGER = logging.getLogger("kodaxweb")


# ── camera list ────────────────────────────────────────────────────────────


def load_cameras(data_dir: Path) -> dict[str, dict]:
    try:
        return json.loads((data_dir / "cameras.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cameras(app: web.Application) -> None:
    data_dir: Path = app["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "cameras.json").write_text(json.dumps(app["cameras"], indent=2))


def camera_ip(request: web.Request) -> str:
    cam = request.app["cameras"].get(request.match_info["cam"])
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    return cam["ip"]


# ── live view ──────────────────────────────────────────────────────────────


def get_hub(app: web.Application, cam_id: str) -> LiveHub:
    """The camera's hub, created with its stored soft limits."""
    hubs: dict[str, LiveHub] = app["hubs"]
    if cam_id not in hubs:
        cam = app["cameras"][cam_id]
        hubs[cam_id] = LiveHub(
            app["http"],
            cam["ip"],
            cam["mac"],
            limits=SoftLimits(cam.setdefault("ptz", {})),
            on_limits_change=lambda: save_cameras(app),
        )
    return hubs[cam_id]


async def live_ws(request: web.Request) -> web.WebSocketResponse:
    cam_id = request.match_info["cam"]
    cam = request.app["cameras"].get(cam_id)
    if cam is None:
        raise web.HTTPNotFound()
    await enforce_bitrate(request.app["http"], cam)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub = get_hub(request.app, cam_id)
    queue = hub.subscribe()
    await ws.send_json({"audio_available": AUDIO_AVAILABLE})

    async def drain_client() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    hub.set_audio(queue, bool(json.loads(msg.data).get("audio")))
                except (ValueError, AttributeError):
                    pass
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    reader = asyncio.create_task(drain_client())
    try:
        while not ws.closed:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                getter.cancel()
                break
            frame = getter.result()
            if frame is None:
                break
            await ws.send_bytes(frame)
    finally:
        reader.cancel()
        hub.unsubscribe(queue)
        await ws.close()
    return ws


# ── REST API ───────────────────────────────────────────────────────────────


def api_errors(handler):
    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await handler(request)
        except camera_api.CameraError as err:
            return web.json_response({"error": str(err)}, status=502)
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"bad request: {err}"}, status=400)
    return wrapper


@api_errors
async def list_cameras(request: web.Request) -> web.Response:
    return web.json_response(
        [{"id": cid, **cam} for cid, cam in request.app["cameras"].items()]
    )


@api_errors
async def add_camera(request: web.Request) -> web.Response:
    body = await request.json()
    ip = str(body["ip"]).strip()
    http = request.app["http"]
    mac = await camera_api.command(http, ip, "get_mac_address")
    if len(mac) != 12:
        raise camera_api.CameraError(f"{ip} does not look like a Kodak camera ({mac})")
    cameras = request.app["cameras"]
    cam_id = next((cid for cid, c in cameras.items() if c["mac"] == mac), uuid.uuid4().hex[:8])
    cameras[cam_id] = {"name": str(body.get("name") or f"Camera {mac[-4:]}"), "ip": ip, "mac": mac}
    save_cameras(request.app)
    return web.json_response({"id": cam_id, **cameras[cam_id]})


@api_errors
async def update_camera(request: web.Request) -> web.Response:
    cam_id = request.match_info["cam"]
    camera_ip(request)
    body = await request.json()
    cam = request.app["cameras"][cam_id]
    if "name" in body:
        cam["name"] = str(body["name"])[:60]
    if "ip" in body:
        cam["ip"] = str(body["ip"]).strip()
        request.app["hubs"].pop(cam_id, None)
    save_cameras(request.app)
    return web.json_response({"id": cam_id, **cam})


@api_errors
async def remove_camera(request: web.Request) -> web.Response:
    cam_id = request.match_info["cam"]
    request.app["cameras"].pop(cam_id, None)
    request.app["hubs"].pop(cam_id, None)
    save_cameras(request.app)
    return web.json_response({"ok": True})


async def enforce_bitrate(http: aiohttp.ClientSession, cam: dict) -> None:
    """Keep the camera on the settings that stream best.

    - Re-apply the user's chosen bitrate: the camera falls back to 550 kbps
      by itself (after power cuts, and when its flow control backs off).
    - Turn off background Wi-Fi scanning (range extender / parent unit,
      neither of which works without Kodak's cloud). The scans take the radio
      off-channel and roughly double the half-second stalls in the stream.
    """
    try:
        info = camera_api.parse_kv(await camera_api.command(http, cam["ip"], "get_caminfo"))
        for flag in ("rtscan", "puscan"):
            if info.get(flag) == "1":
                _LOGGER.info("%s: turning off %s", cam["name"], flag)
                await camera_api.command(http, cam["ip"], flag, "&value=0")
        wanted = cam.get("video_bitrate")
        if wanted and int(info.get("brate", wanted)) != wanted:
            _LOGGER.info("%s bitrate was %s, restoring %s", cam["name"], info.get("brate"), wanted)
            await camera_api.apply_setting(http, cam["ip"], "video_bitrate", wanted)
    except (camera_api.CameraError, ValueError) as err:
        _LOGGER.debug("settings check failed for %s: %s", cam["name"], err)


@api_errors
async def status(request: web.Request) -> web.Response:
    cam = request.app["cameras"].get(request.match_info["cam"])
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    await enforce_bitrate(request.app["http"], cam)
    result = await camera_api.get_status(request.app["http"], cam["ip"])
    result["soft_limits"] = get_hub(request.app, request.match_info["cam"]).limits.summary()
    return web.json_response(result)


@api_errors
async def settings(request: web.Request) -> web.Response:
    ip = camera_ip(request)
    body = await request.json()
    for key, value in body.items():
        await camera_api.apply_setting(request.app["http"], ip, key, value)
        if key == "video_bitrate":
            request.app["cameras"][request.match_info["cam"]]["video_bitrate"] = int(value)
            save_cameras(request.app)
    return web.json_response({"ok": True})


@api_errors
async def wifi(request: web.Request) -> web.Response:
    body = await request.json()
    await camera_api.change_wifi(request.app["http"], camera_ip(request), body["ssid"], body["password"])
    return web.json_response({"ok": True})


@api_errors
async def zones_get(request: web.Request) -> web.Response:
    grid = await camera_api.get_zones(request.app["http"], camera_ip(request))
    return web.json_response({"rows": camera_api.ZONE_ROWS, "cols": camera_api.ZONE_COLS, "grid": grid})


@api_errors
async def zones_put(request: web.Request) -> web.Response:
    body = await request.json()
    await camera_api.set_zones(request.app["http"], camera_ip(request), body["grid"])
    return web.json_response({"ok": True})


@api_errors
async def action(request: web.Request) -> web.Response:
    ip = camera_ip(request)
    body = await request.json()
    http = request.app["http"]
    name = body["action"]
    if name == "restart":
        await camera_api.restart(http, ip)
    elif name == "melody":
        await camera_api.play_melody(http, ip, int(body.get("track", 1)))
    elif name == "melody_stop":
        await camera_api.stop_melody(http, ip)
    elif name in ("limit_mark", "limit_clear"):
        hub = get_hub(request.app, request.match_info["cam"])
        if name == "limit_mark":
            hub.mark_limit(str(body.get("edge")))
        else:
            hub.clear_limits()
        return web.json_response({"ok": True, "soft_limits": hub.limits.summary()})
    elif name in camera_api.PTZ_ACTIONS:
        hub = get_hub(request.app, request.match_info["cam"])
        if name == "move":
            await hub.move(str(body.get("direction")))
        elif name == "stop":
            await hub.stop_motor()
        else:
            await hub.command(*camera_api.ptz_command(name, body))
    elif name == "setup_log":
        return web.json_response({"log": await camera_api.get_setup_log(http, ip)})
    else:
        raise ValueError(f"unknown action {name}")
    return web.json_response({"ok": True})


# ── new camera setup ───────────────────────────────────────────────────────


@api_errors
async def setup_find(request: web.Request) -> web.Response:
    found = await setup_flow.find_camera(request.app["http"])
    return web.json_response({"camera": found})


async def setup_run(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await resp.prepare(request)
    async for step in setup_flow.run_setup(
        request.app["http"],
        public_key=request.app["data_dir"] / "public.pem",
        ip=str(body["ip"]),
        ssid=str(body["ssid"]),
        password=str(body.get("password", "")),
        security=str(body.get("security", "WPA2")),
        timezone_name=str(body.get("timezone", "UTC")),
        utc_offset_minutes=int(body.get("utc_offset_minutes", 0)),
        flicker=int(body.get("flicker", 50)),
    ):
        await resp.write((json.dumps(step) + "\n").encode())
    await resp.write_eof()
    return resp


# ── app ────────────────────────────────────────────────────────────────────


async def index(request: web.Request) -> web.Response:
    """The page, with script/style links versioned by file time (cache-busting)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        version = int((STATIC / name).stat().st_mtime)
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={version}\"")
    return web.Response(text=html, content_type="text/html")


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()


async def revalidate_static(request: web.Request, response: web.StreamResponse) -> None:
    """Make browsers check for a newer page/script on every load.

    Otherwise a cached app.js can outlive a server update and misread the
    live-view messages. Unchanged files still come from cache (304).
    """
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def make_app(data_dir: Path = DEFAULT_DATA_DIR) -> web.Application:
    app = web.Application()
    app["data_dir"] = data_dir
    app["cameras"] = load_cameras(data_dir)
    app["hubs"] = {}
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.on_response_prepare.append(revalidate_static)
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/api/cameras", list_cameras)
    app.router.add_post("/api/cameras", add_camera)
    app.router.add_patch("/api/cameras/{cam}", update_camera)
    app.router.add_delete("/api/cameras/{cam}", remove_camera)
    app.router.add_get("/api/cameras/{cam}/status", status)
    app.router.add_post("/api/cameras/{cam}/settings", settings)
    app.router.add_post("/api/cameras/{cam}/wifi", wifi)
    app.router.add_get("/api/cameras/{cam}/zones", zones_get)
    app.router.add_put("/api/cameras/{cam}/zones", zones_put)
    app.router.add_post("/api/cameras/{cam}/action", action)
    app.router.add_get("/api/cameras/{cam}/live", live_ws)
    app.router.add_get("/api/setup/find", setup_find)
    app.router.add_post("/api/setup/run", setup_run)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help="folder for cameras.json and public.pem (default: data/ next to the code)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    web.run_app(make_app(args.data_dir.resolve()), host=args.host, port=args.port)
