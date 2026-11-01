"""Live view: one camera session fanned out to every watching browser.

Also owns the pan/tilt motor safety (dead-man stop, hold cap, soft limits),
because motor commands share the camera's single session with the stream.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable

import aiohttp

from . import camera_api, vlvl
from .ptz_limits import DIRECTIONS, SoftLimits

try:  # optional: decodes the camera's G.722 audio for live view
    import av
except ImportError:  # pragma: no cover
    av = None

AUDIO_AVAILABLE = av is not None
_LOGGER = logging.getLogger("kodaxweb")


# Some cameras answer -2 to motor_calibration and never start (others of the
# same model calibrate fine), so say so instead of a bare error code.
CALIBRATION_REFUSED = (
    "This camera refused to calibrate (it answered -2). Other cameras of the same "
    "model do calibrate, so it may depend on this camera's firmware or state. "
    "Pan and tilt still work, and your soft limits still protect the motor."
)

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
            raise camera_api.CameraError(f"unknown direction {direction}")
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._hold_dir is not None and direction != self._hold_dir:
            self._commit_move(now)  # switched direction without a stop
        if direction != self._hold_dir:
            self._hold_dir, self._hold_started = direction, now
        elif now - self._hold_started > MAX_HOLD_S:
            await self.stop_motor()
            raise camera_api.CameraError(
                f"stopped after {MAX_HOLD_S:.0f} s to protect the motor; release and press again"
            )
        # Soft end-stop: stop exactly when the estimate reaches the marked limit.
        pos = self.limits.position(direction, now - self._hold_started)
        remaining = self.limits.remaining(direction, pos)
        if remaining is not None and remaining < MIN_TRAVEL_S:
            await self.stop_motor()
            raise camera_api.CameraError(f"reached the {direction} limit")
        await self.command(*camera_api.ptz_command("move", {"direction": direction}))
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
            await self.command(*camera_api.ptz_command("stop", {}))
        except camera_api.CameraError as err:
            _LOGGER.warning("motor stop failed: %s", err)

    def mark_limit(self, edge: str) -> None:
        if edge not in DIRECTIONS:
            raise camera_api.CameraError(f"unknown edge {edge}")
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
                    raise camera_api.CameraError("camera refused a session (busy streaming elsewhere?)")
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
            raise camera_api.CameraError(f"no reply to {req}")
        value = reply.split(":", 1)[-1].strip()
        if req == "motor_calibration" and value == "-2":
            raise camera_api.CameraError(CALIBRATION_REFUSED)
        if value != "0":
            raise camera_api.CameraError(f"camera rejected {req} ({value})")
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
