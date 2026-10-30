"""KodaxWeb: local web app for Kodak Smart Home cameras.

Run:  py server.py            (http://127.0.0.1:8099)
      py server.py --host 0.0.0.0 --port 8099   (expose on the LAN — no login!)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import uuid
from collections.abc import Callable
from pathlib import Path

from aiohttp import WSMsgType, web
import aiohttp

import kodak
import setup_flow
import vlvl
from ptz_limits import DIRECTIONS, SoftLimits

try:  # optional: decodes the camera's G.722 audio for live view
    import av
except ImportError:  # pragma: no cover
    av = None

ROOT = Path(__file__).resolve().parent

CONFIG = ROOT / "cameras.json"
STATIC = ROOT / "static"
_LOGGER = logging.getLogger("kodaxweb")


# ── camera list ────────────────────────────────────────────────────────────


def load_cameras() -> dict[str, dict]:
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cameras(cameras: dict[str, dict]) -> None:
    CONFIG.write_text(json.dumps(cameras, indent=2))


def camera_ip(request: web.Request) -> str:
    cam = request.app["cameras"].get(request.match_info["cam"])
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    return cam["ip"]


# ── live view ──────────────────────────────────────────────────────────────


DEADMAN_S = 0.9   # stop the motor if a held direction isn't repeated in time
MAX_HOLD_S = 3.0  # longest single continuous move
MIN_TRAVEL_S = 0.05  # closer than this to a soft limit counts as "at" it

MSG_VIDEO = b"\x01"  # + capture ms (BE32) + H.264 access unit (Annex B)
MSG_AUDIO = b"\x02"  # + capture ms (BE32) + 16 kHz mono s16le PCM


class G722Decoder:
    """Camera audio (G.722, 64 kbit/s) to 16 kHz s16le PCM."""

    def __init__(self) -> None:
        self._ctx = av.CodecContext.create("g722", "r")
        self._ctx.sample_rate = 16000
        self._ctx.layout = "mono"

    def decode(self, data: bytes) -> bytes:
        return b"".join(
            frame.to_ndarray().astype("<i2").tobytes()
            for frame in self._ctx.decode(av.Packet(data))
        )


class LiveHub:
    """One VLVL session per camera, fanned out to every connected browser."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        ip: str,
        mac: str,
        limits: SoftLimits | None = None,
        on_limits_change: Callable[[], None] | None = None,
    ) -> None:
        self._http = http
        self._ip = ip
        self._mac = mac
        self.limits = limits or SoftLimits({})
        self._on_limits_change = on_limits_change or (lambda: None)
        self._clients: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task | None = None
        self._rx: vlvl.VLVLFrameReceiver | None = None
        self._cmd_rx: vlvl.VLVLFrameReceiver | None = None
        self._cmd_timer: asyncio.TimerHandle | None = None
        self._cmd_lock = asyncio.Lock()
        self._gop: list[bytes] = []  # last keyframe + P-frames since
        self._resync: set[asyncio.Queue[bytes | None]] = set()
        self._audio: set[asyncio.Queue[bytes | None]] = set()  # listeners
        self._decoder: G722Decoder | None = None
        # Motor safety: a held direction must keep being re-sent, and one hold
        # is capped, so the motor can't be left driving into its end-stop.
        self._deadman: asyncio.TimerHandle | None = None
        self._hold_dir: str | None = None
        self._hold_started = 0.0

    async def move(self, direction: str) -> None:
        """One step of a held pan/tilt direction (the browser repeats it)."""
        if direction not in DIRECTIONS:
            raise kodak.CameraError(f"unknown direction {direction}")
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._hold_dir is not None and direction != self._hold_dir:
            self._commit_move(now)  # switched direction without a stop
        if direction != self._hold_dir:
            self._hold_dir, self._hold_started = direction, now
        elif now - self._hold_started > MAX_HOLD_S:
            await self.stop_motor()
            raise kodak.CameraError(
                f"stopped after {MAX_HOLD_S:.0f} s to protect the motor; release and press again"
            )
        # Soft end-stop: stop exactly when the estimate reaches the marked limit.
        pos = self.limits.position(direction, now - self._hold_started)
        remaining = self.limits.remaining(direction, pos)
        if remaining is not None and remaining < MIN_TRAVEL_S:
            await self.stop_motor()
            raise kodak.CameraError(f"reached the {direction} limit")
        await self.command(*kodak.ptz_command("move", {"direction": direction}))
        if self._deadman is not None:
            self._deadman.cancel()
        delay = DEADMAN_S if remaining is None else min(DEADMAN_S, remaining)
        self._deadman = loop.call_later(delay, lambda: asyncio.ensure_future(self.stop_motor()))

    def _commit_move(self, now: float) -> None:
        if self._hold_dir is None:
            return
        self.limits.commit(self._hold_dir, now - self._hold_started)
        self._hold_dir = None
        self._on_limits_change()

    async def stop_motor(self) -> None:
        if self._deadman is not None:
            self._deadman.cancel()
            self._deadman = None
        self._commit_move(asyncio.get_running_loop().time())
        try:
            await self.command(*kodak.ptz_command("stop", {}))
        except kodak.CameraError as err:
            _LOGGER.warning("motor stop failed: %s", err)

    def mark_limit(self, edge: str) -> None:
        if edge not in DIRECTIONS:
            raise kodak.CameraError(f"unknown edge {edge}")
        self.limits.mark(edge)
        self._on_limits_change()

    def clear_limits(self) -> None:
        self.limits.clear()
        self._on_limits_change()

    def set_audio(self, queue: asyncio.Queue[bytes | None], enabled: bool) -> None:
        if enabled and av is not None:
            self._audio.add(queue)
        else:
            self._audio.discard(queue)

    def _on_audio(self, data: bytes, capture_ms: int) -> None:
        if not self._audio:
            self._decoder = None  # restart cleanly when someone listens again
            return
        if self._decoder is None:
            self._decoder = G722Decoder()
        try:
            message = MSG_AUDIO + struct.pack(">I", capture_ms) + self._decoder.decode(data)
        except (av.error.FFmpegError, ValueError):  # type: ignore[union-attr]
            self._decoder = None
            return
        for queue in list(self._audio):
            if not queue.full():  # audio can be dropped; video can't
                queue.put_nowait(message)

    async def command(self, req: str, params: str) -> str | None:
        """Send a session command (pan/tilt), opening a session if needed.

        Motor commands are fire-and-forget: their replies
        (-2 for the first, then echoes) don't indicate success or failure.
        """
        fire_and_forget = req.startswith("motor_") and req != "motor_calibration"
        timeout = 0.3 if fire_and_forget else 2.0
        async with self._cmd_lock:
            if self._rx is None:
                rx = vlvl.VLVLFrameReceiver(self._ip, vlvl.get_local_ip_for(self._ip), self._mac)
                if not await rx.open(self._http):
                    raise kodak.CameraError("camera refused a session (busy streaming elsewhere?)")
                self._rx = self._cmd_rx = rx
            if self._rx is self._cmd_rx:
                # Keep the command session while buttons are being pressed.
                if self._cmd_timer is not None:
                    self._cmd_timer.cancel()
                self._cmd_timer = asyncio.get_running_loop().call_later(
                    5, self._drop_command_session
                )
            reply = await self._rx.send_command(req, params, timeout=timeout)
        if fire_and_forget:
            return reply
        if reply is None:
            raise kodak.CameraError(f"no reply to {req}")
        value = reply.split(":", 1)[-1].strip()
        if value != "0":
            raise kodak.CameraError(f"camera rejected {req} ({value})")
        return reply

    def _drop_command_session(self) -> None:
        self._cmd_timer = None
        rx, self._cmd_rx = self._cmd_rx, None
        if rx is None:
            return
        if self._rx is rx:
            self._rx = None
        rx.close()

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=90)
        for message in self._gop:  # replay from the last keyframe: instant picture
            queue.put_nowait(message)
        self._clients.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._clients.discard(queue)
        self._resync.discard(queue)
        self._audio.discard(queue)
        if not self._clients and self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        local_ip = vlvl.get_local_ip_for(self._ip)
        if self._cmd_timer is not None:
            self._cmd_timer.cancel()
        self._drop_command_session()  # the camera allows one session at a time
        try:
            while self._clients:
                rx = vlvl.VLVLFrameReceiver(self._ip, local_ip, self._mac)
                if not await rx.open(self._http):
                    await asyncio.sleep(3)
                    continue
                self._rx = rx
                self._decoder = None  # fresh G.722 state per session
                try:
                    async for frame, capture_ms in rx.frame_stream(
                        on_audio=self._on_audio, with_timestamps=True
                    ):
                        key = vlvl.is_keyframe(frame)
                        message = MSG_VIDEO + struct.pack(">I", capture_ms) + frame
                        if key:
                            self._gop = []
                        if key or 0 < len(self._gop) < 60:
                            self._gop.append(message)
                        for queue in list(self._clients):
                            if queue in self._resync:
                                if not key:
                                    continue
                                self._resync.discard(queue)
                            if queue.full():
                                # A slow client can't skip P-frames; drop its
                                # backlog and restart it at the next keyframe.
                                while not queue.empty():
                                    queue.get_nowait()
                                if not key:
                                    self._resync.add(queue)
                                    continue
                            queue.put_nowait(message)
                finally:
                    self._rx = None
                    self._gop = []
                    rx.close()
        except asyncio.CancelledError:
            pass
        finally:
            for queue in list(self._clients):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)


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
            on_limits_change=lambda: save_cameras(app["cameras"]),
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
    await ws.send_json({"audio_available": av is not None})

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
        except kodak.CameraError as err:
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
    mac = await kodak.command(http, ip, "get_mac_address")
    if len(mac) != 12:
        raise kodak.CameraError(f"{ip} does not look like a Kodak camera ({mac})")
    cameras = request.app["cameras"]
    cam_id = next((cid for cid, c in cameras.items() if c["mac"] == mac), uuid.uuid4().hex[:8])
    cameras[cam_id] = {"name": str(body.get("name") or f"Camera {mac[-4:]}"), "ip": ip, "mac": mac}
    save_cameras(cameras)
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
    save_cameras(request.app["cameras"])
    return web.json_response({"id": cam_id, **cam})


@api_errors
async def remove_camera(request: web.Request) -> web.Response:
    cam_id = request.match_info["cam"]
    request.app["cameras"].pop(cam_id, None)
    request.app["hubs"].pop(cam_id, None)
    save_cameras(request.app["cameras"])
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
        info = kodak.parse_kv(await kodak.command(http, cam["ip"], "get_caminfo"))
        for flag in ("rtscan", "puscan"):
            if info.get(flag) == "1":
                _LOGGER.info("%s: turning off %s", cam["name"], flag)
                await kodak.command(http, cam["ip"], flag, "&value=0")
        wanted = cam.get("video_bitrate")
        if wanted and int(info.get("brate", wanted)) != wanted:
            _LOGGER.info("%s bitrate was %s, restoring %s", cam["name"], info.get("brate"), wanted)
            await kodak.apply_setting(http, cam["ip"], "video_bitrate", wanted)
    except (kodak.CameraError, ValueError) as err:
        _LOGGER.debug("settings check failed for %s: %s", cam["name"], err)


@api_errors
async def status(request: web.Request) -> web.Response:
    cam = request.app["cameras"].get(request.match_info["cam"])
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    await enforce_bitrate(request.app["http"], cam)
    result = await kodak.get_status(request.app["http"], cam["ip"])
    result["soft_limits"] = get_hub(request.app, request.match_info["cam"]).limits.summary()
    return web.json_response(result)


@api_errors
async def settings(request: web.Request) -> web.Response:
    ip = camera_ip(request)
    body = await request.json()
    for key, value in body.items():
        await kodak.apply_setting(request.app["http"], ip, key, value)
        if key == "video_bitrate":
            request.app["cameras"][request.match_info["cam"]]["video_bitrate"] = int(value)
            save_cameras(request.app["cameras"])
    return web.json_response({"ok": True})


@api_errors
async def wifi(request: web.Request) -> web.Response:
    body = await request.json()
    await kodak.change_wifi(request.app["http"], camera_ip(request), body["ssid"], body["password"])
    return web.json_response({"ok": True})


@api_errors
async def zones_get(request: web.Request) -> web.Response:
    grid = await kodak.get_zones(request.app["http"], camera_ip(request))
    return web.json_response({"rows": kodak.ZONE_ROWS, "cols": kodak.ZONE_COLS, "grid": grid})


@api_errors
async def zones_put(request: web.Request) -> web.Response:
    body = await request.json()
    await kodak.set_zones(request.app["http"], camera_ip(request), body["grid"])
    return web.json_response({"ok": True})


@api_errors
async def action(request: web.Request) -> web.Response:
    ip = camera_ip(request)
    body = await request.json()
    http = request.app["http"]
    name = body["action"]
    if name == "restart":
        await kodak.restart(http, ip)
    elif name == "melody":
        await kodak.play_melody(http, ip, int(body.get("track", 1)))
    elif name == "melody_stop":
        await kodak.stop_melody(http, ip)
    elif name in ("limit_mark", "limit_clear"):
        hub = get_hub(request.app, request.match_info["cam"])
        if name == "limit_mark":
            hub.mark_limit(str(body.get("edge")))
        else:
            hub.clear_limits()
        return web.json_response({"ok": True, "soft_limits": hub.limits.summary()})
    elif name in kodak.PTZ_ACTIONS:
        hub = get_hub(request.app, request.match_info["cam"])
        if name == "move":
            await hub.move(str(body.get("direction")))
        elif name == "stop":
            await hub.stop_motor()
        else:
            await hub.command(*kodak.ptz_command(name, body))
    elif name == "setup_log":
        return web.json_response({"log": await kodak.get_setup_log(http, ip)})
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


def make_app() -> web.Application:
    app = web.Application()
    app["cameras"] = load_cameras()
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
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    web.run_app(make_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
