"""VLVL P2P protocol handler for Kodak Smart Home cameras.

Kodak cameras stream H264 video using a proprietary UDP protocol called VLVL
(magic bytes b'VLVL').  When a client requests a session via:

    GET http://<ip>/?req=get_session_key&mode=local&port1=<PORT>&ip=<OUR_IP>&streamname=<MAC>_<random>

…the camera replies with ``error=200,...&key=<32 hex chars>&...`` and pushes
UDP packets to <OUR_IP>:<PORT>.  Each packet has a 20-byte VLVL header
followed by a fragment of an H264 Annex B access unit.

Header layout (big-endian):
  0 - 3 : magic  "VLVL" (0x56 0x4C 0x56 0x4C)
  4 - 5 : packet type  (see _VIDEO_FRAME_TYPES / _AUDIO_FRAME_TYPE)
  6 - 9 : capture timestamp of the frame, milliseconds (camera clock)
 10 -11 : number of packets in the frame
 12 -15 : seqS  — absolute packet index of the START of the current frame
 16 -19 : seqC  — absolute packet index of THIS packet

The camera's Wi-Fi regularly goes quiet for ~0.5 s and then delivers the
held-back frames in a burst; the capture timestamps let players smooth that.

A new H264 IDR frame begins when seqS == seqC.  All packets with the same
seqS value belong to the same frame.  Reassembling the payloads gives one
complete H264 Annex B access unit (SPS + PPS + IDR slice, sent in-band).

The reassembled frame is sparsely encrypted:
one 16-byte AES-128-ECB block every 0x410 bytes, starting at offset 0, using
the hex-decoded per-session ``key`` from the get_session_key response.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import struct
import time
from typing import Any, AsyncIterator, Callable

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# ── H264 codec parameters ──────────────────────────────────────────────────
# The camera sends SPS/PPS in-band at the start of every frame; these copies
# are only used for the RTSP SDP.  1280×720 High profile, Level 4.0, CABAC.
_SPS_BODY = bytes.fromhex("67640028ace805005b90")
_PPS_BODY = bytes.fromhex("68ee3c30")

_ANNEX_B = b"\x00\x00\x00\x01"
SPS_NALU: bytes = _ANNEX_B + _SPS_BODY
PPS_NALU: bytes = _ANNEX_B + _PPS_BODY
CODEC_EXTRADATA: bytes = SPS_NALU + PPS_NALU

# Base64-encoded SPS/PPS without Annex B start code (for RTSP SDP)
SPS_B64: str = "Z2QAKKzoBQBbkA=="
PPS_B64: str = "aO48MA=="
PROFILE_LEVEL_ID: str = "640028"

# ── VLVL protocol constants ────────────────────────────────────────────────
_VLVL_MAGIC = b"VLVL"
_VLVL_HEADER_SIZE = 20
_VLVL_CMD_TYPE = 0x001E    # client -> camera text command
_VLVL_CMD_RESP_TYPE = 0x001F  # camera -> client command response
_VLVL_ACK_TYPE = 0x32      # client -> camera selective ACK
_CONTROL_TYPES = {0x0020, 0x0021}  # stream-control probes (no seq fields)
# Frame types in Full mode: 0x0A keyframe, 0x0C P-frame, 0x19 audio.
# Preview mode sends keyframes only, as 0x0D.
_VIDEO_FRAME_TYPES = {0x000A, 0x000B, 0x000C, 0x000D}
# Audio: G.722 at 64 kbit/s (16 kHz mono), 512 bytes per packet, and fully
# AES-ECB encrypted (every 16-byte block), unlike the sparse video frames.
_AUDIO_FRAME_TYPE = 0x0019
_ACK_INTERVAL_S = 0.05  # faster ACKs let the camera's flow control send more
_ACK_WINDOW = 336
_LOST_FRAME_WAIT_S = 0.3  # give up on a frame with missing packets after this

# p2p_ses_mode_set values
STREAM_MODE_PREVIEW = 0    # ~1 fps keyframes only (camera default)
STREAM_MODE_IFRAME_ONLY = 2
STREAM_MODE_FULL = 3       # ~8 fps keyframes + P-frames, plus audio
_CRYPT_BLOCK = 16
_CRYPT_STRIDE = 0x410      # one encrypted block per 1040 bytes of frame
_KEY_RE = re.compile(r"key=([0-9a-fA-F]{32})")
_PORT_RE = re.compile(r"port1=(\d+)")

_SESSION_TIMEOUT_S = 5.0     # seconds to wait for each UDP packet
_FRAME_TIMEOUT_S = 10.0      # max seconds to wait for one complete frame
_MAX_PACKETS_PER_FRAME = 300  # safety limit


class VLVLFrameReceiver:
    """Manages a VLVL streaming session and reassembles H264 IDR frames.

    Usage::

        async with aiohttp.ClientSession() as http:
            rx = VLVLFrameReceiver("192.168.1.100", "192.168.1.10", "AABBCCDDEEFF")
            if await rx.open(http):
                frame = await rx.receive_one_frame()  # Annex B bytes w/ SPS+PPS
                rx.close()
    """

    def __init__(self, camera_ip: str, local_ip: str, mac: str) -> None:
        self._camera_ip = camera_ip
        self._local_ip = local_ip
        self._mac = mac.replace(":", "").upper()  # normalise
        # Unique per session (MAC_<random>): the camera
        # keeps old sessions alive for a while and mode commands are
        # addressed by stream name.
        self._stream_name = f"{self._mac}_{random.randint(0, 65535)}"
        self._sock: socket.socket | None = None
        self._port: int = 0
        self._key: bytes | None = None
        self._camera_port: int = 0
        self._cmd_seq = 0
        self._pending: dict[int, asyncio.Future[str]] = {}
        self._streaming = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def open(self, http_session: aiohttp.ClientSession) -> bool:
        """Open the UDP socket and request a VLVL session from the camera.

        Returns True if the camera accepted the session.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        self._port = sock.getsockname()[1]
        self._sock = sock

        url = (
            f"http://{self._camera_ip}/?req=get_session_key"
            f"&mode=local"
            f"&port1={self._port}"
            f"&ip={self._local_ip}"
            f"&streamname={self._stream_name}"
        )
        _LOGGER.debug("VLVL session request: %s", url)
        try:
            async with http_session.get(
                url, timeout=aiohttp.ClientTimeout(total=6)
            ) as resp:
                text = await resp.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("VLVL session request failed: %s", err)
            sock.close()
            self._sock = None
            return False

        _LOGGER.debug("VLVL session response: %s", text.strip())
        if "error=200" not in text:
            _LOGGER.warning("Camera rejected VLVL session: %s", text)
            sock.close()
            self._sock = None
            return False

        match = _KEY_RE.search(text)
        if match is None:
            _LOGGER.warning("VLVL session response has no key: %s", text)
            sock.close()
            self._sock = None
            return False
        self._key = bytes.fromhex(match.group(1))
        port_match = _PORT_RE.search(text)
        self._camera_port = int(port_match.group(1)) if port_match else 0

        _LOGGER.debug(
            "VLVL session opened on UDP port %d (camera %s)",
            self._port,
            self._camera_ip,
        )
        return True

    async def receive_one_frame(self) -> bytes | None:
        """Receive one complete keyframe (for snapshots).

        Uses the session's default Preview mode (keyframes only).  Returns as
        soon as the frame's packets — counted in the header — have all
        arrived: Annex B bytes starting with SPS + PPS.  Returns None on
        timeout rather than a partial frame, which would decode as garbage.
        """
        if self._sock is None:
            return None

        loop = asyncio.get_running_loop()
        frames: dict[int, _PendingFrame] = {}  # seqS -> frame being assembled
        deadline = loop.time() + _FRAME_TIMEOUT_S

        for _ in range(_MAX_PACKETS_PER_FRAME * 2):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                raw: bytes = await asyncio.wait_for(
                    loop.sock_recv(self._sock, 65536),
                    timeout=min(remaining, _SESSION_TIMEOUT_S),
                )
            except (asyncio.TimeoutError, OSError):
                break

            if len(raw) < _VLVL_HEADER_SIZE or raw[:4] != _VLVL_MAGIC:
                continue
            if struct.unpack_from(">H", raw, 4)[0] not in _VIDEO_FRAME_TYPES:
                continue  # audio or control packet
            ts_ms, count = struct.unpack_from(">IH", raw, 6)
            seq_s, seq_c = struct.unpack_from(">II", raw, 12)
            frame = frames.setdefault(seq_s, _PendingFrame(ts_ms, count))
            frame.packets[seq_c] = raw[_VLVL_HEADER_SIZE:]
            if frame.complete:
                data = self._assemble(frame.packets)
                if is_keyframe(data):
                    _LOGGER.debug("VLVL keyframe ready: %d bytes", len(data))
                    return data
                del frames[seq_s]  # a P-frame on its own can't be decoded

        _LOGGER.debug("VLVL: no complete keyframe before timeout")
        return None

    async def frame_stream(
        self,
        mode: int = STREAM_MODE_FULL,
        on_audio: Callable[[bytes, int], None] | None = None,
        with_timestamps: bool = False,
    ) -> AsyncIterator[Any]:
        """Continuously yield H264 access units (Annex B).

        In Full mode these are keyframes (with SPS+PPS) and P-frames; in
        Preview mode keyframes only.  With ``with_timestamps`` each item is
        ``(frame, capture_ms)``.  If ``on_audio`` is given it receives each
        decrypted G.722 audio packet and its capture time (Full mode only).

        A frame is released as soon as all its packets have arrived, always in
        order; one whose packets never all arrive is dropped after a short
        wait so it can't hold up the stream.

        Runs indefinitely; the caller should cancel the enclosing task to stop.
        If the session dies (no packets for _SESSION_TIMEOUT_S seconds) the
        stream stops automatically.
        """
        if self._sock is None:
            return

        loop = asyncio.get_running_loop()
        pending: dict[int, _PendingFrame] = {}  # seqS -> frame being assembled
        released_up_to = -1  # seqS of the last frame released or dropped

        self._streaming = True
        mode_task = None
        if mode != STREAM_MODE_PREVIEW:
            mode_task = asyncio.ensure_future(self._set_mode(mode))
        try:
            async for pkt_type, seq_s, seq_c, payload, ts_ms, count in self._stream_packets(loop):
                if pkt_type == _AUDIO_FRAME_TYPE:
                    if on_audio is not None:
                        on_audio(self._decrypt_dense(payload), ts_ms)
                    continue
                if pkt_type not in _VIDEO_FRAME_TYPES or seq_s <= released_up_to:
                    continue  # not video, or a straggler from a released frame
                frame = pending.setdefault(seq_s, _PendingFrame(ts_ms, count))
                frame.packets[seq_c] = payload
                if frame.complete and frame.completed_at is None:
                    frame.completed_at = loop.time()

                # Release from the oldest frame forwards, in order.
                while pending:
                    oldest = min(pending)
                    head = pending[oldest]
                    if head.complete:
                        del pending[oldest]
                        released_up_to = oldest
                        data = self._assemble(head.packets)
                        yield (data, head.ts_ms) if with_timestamps else data
                        continue
                    newer_done = [
                        f.completed_at for s, f in pending.items()
                        if s != oldest and f.completed_at is not None
                    ]
                    if newer_done and loop.time() - min(newer_done) > _LOST_FRAME_WAIT_S:
                        _LOGGER.debug("VLVL dropping incomplete frame %d", oldest)
                        del pending[oldest]  # lost packets: give up on it
                        released_up_to = oldest
                        continue
                    break
        finally:
            self._streaming = False
            if mode_task is not None:
                mode_task.cancel()

    async def _set_mode(self, mode: int) -> None:
        reply = await self.send_command(
            "p2p_ses_mode_set", f"&value={mode}&streamname={self._stream_name}"
        )
        if reply is None or not reply.endswith(": 0"):
            _LOGGER.warning("VLVL stream mode %d not accepted: %s", mode, reply)

    async def _stream_packets(
        self, loop: asyncio.AbstractEventLoop
    ) -> AsyncIterator[tuple[int, int, int, bytes, int, int]]:
        """Yield parsed media packets; send ACKs and route command replies.

        Each item is (type, seqS, seqC, payload, capture_ms, packets_in_frame).

        The camera paces the stream on selective ACKs:
        VLVL 00 32 + first-missing seqC (BE) + one byte
        per packet for the next 336 seqCs.  Without them it stalls after a
        couple of frames.
        """
        base: int | None = None
        seen: set[int] = set()
        last_ack = 0.0
        last_packet = loop.time()
        while self._sock is not None:
            now = loop.time()
            if base is not None and now - last_ack >= _ACK_INTERVAL_S:
                last_ack = now
                ring = bytes(1 if base + i in seen else 0 for i in range(_ACK_WINDOW))
                packet = _VLVL_MAGIC + bytes((0, _VLVL_ACK_TYPE)) + struct.pack(">I", base) + ring
                try:
                    await loop.sock_sendto(self._sock, packet, (self._camera_ip, self._camera_port))
                except OSError:
                    return
            try:
                raw: bytes = await asyncio.wait_for(
                    loop.sock_recv(self._sock, 65536),
                    timeout=_ACK_INTERVAL_S if base is not None else _SESSION_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                if base is not None and loop.time() - last_packet < _SESSION_TIMEOUT_S:
                    continue  # quiet moment; keep ACKing
                _LOGGER.debug("VLVL stream timeout — stopping (port %d)", self._port)
                return
            except (OSError, asyncio.CancelledError):
                return
            last_packet = loop.time()

            if raw[:4] != _VLVL_MAGIC or len(raw) < 6:
                continue
            pkt_type = struct.unpack_from(">H", raw, 4)[0]
            if pkt_type == _VLVL_CMD_RESP_TYPE:
                self._handle_response(raw)
                continue
            if len(raw) < _VLVL_HEADER_SIZE or pkt_type in _CONTROL_TYPES:
                continue
            seq_s, seq_c = struct.unpack_from(">II", raw, 12)

            if base is None:
                base = seq_c
            if seq_c >= base:
                seen.add(seq_c)
                if seq_c >= base + _ACK_WINDOW:  # fell too far behind: slide
                    base = seq_c - _ACK_WINDOW + 1
                    seen = {s for s in seen if s >= base}
                while base in seen:
                    seen.discard(base)
                    base += 1
            ts_ms, count = struct.unpack_from(">IH", raw, 6)
            yield pkt_type, seq_s, seq_c, raw[_VLVL_HEADER_SIZE:], ts_ms, count

    # ── Command channel ─────────────────────────────────────────────────────
    # Same text commands as the HTTP API (``req=<cmd>&k=v``), but carried in
    # the session: VLVL 001E + seq(4, BE) + AES-CBC(zero IV, zero-padded)
    # of seq(4) + text, sent to the camera's port1.  Replies arrive as
    # VLVL 001F + seq(4) + len(2) + AES-CBC(text).  Pan/tilt only works here.

    def _cbc(self, data: bytes, encrypt: bool) -> bytes:
        assert self._key is not None
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(b"\0" * 16))
        ctx = cipher.encryptor() if encrypt else cipher.decryptor()
        return ctx.update(data) + ctx.finalize()

    def _handle_response(self, raw: bytes) -> None:
        if len(raw) < 12 or self._key is None:
            return
        seq = struct.unpack_from(">I", raw, 6)[0]
        length = struct.unpack_from(">H", raw, 10)[0]
        body = raw[12:12 + length]
        body = body[: len(body) // 16 * 16]
        text = self._cbc(body, encrypt=False).rstrip(b"\0").decode(errors="replace")
        future = self._pending.pop(seq, None)
        if future is not None and not future.done():
            future.set_result(text)

    async def send_command(self, req: str, params: str = "", timeout: float = 2.0) -> str | None:
        """Send a command over the session; return the camera's reply text.

        The reply looks like ``get_version: 03.03.92``.  Returns None on
        timeout.  Motor commands are fire-and-forget (pass a short timeout):
        the first of a sequence answers ``-2`` and repeats echo the request,
        yet the camera moves either way.
        """
        if self._sock is None or self._key is None or not self._camera_port:
            return None
        loop = asyncio.get_running_loop()
        seq = self._cmd_seq
        self._cmd_seq += 1
        body = struct.pack(">I", seq) + f"req={req}{params}".encode()
        body += b"\0" * (-len(body) % 16)
        packet = _VLVL_MAGIC + struct.pack(">HI", _VLVL_CMD_TYPE, seq) + self._cbc(body, encrypt=True)
        future: asyncio.Future[str] = loop.create_future()
        self._pending[seq] = future
        deadline = loop.time() + timeout
        try:
            for _ in range(4):  # UDP: resend until a reply arrives
                await loop.sock_sendto(self._sock, packet, (self._camera_ip, self._camera_port))
                wait = min(0.4, deadline - loop.time())
                if wait <= 0:
                    break
                if self._streaming:
                    try:
                        return await asyncio.wait_for(asyncio.shield(future), wait)
                    except asyncio.TimeoutError:
                        continue
                await self._read_responses(loop, wait)
                if future.done():
                    return future.result()
            return None
        finally:
            self._pending.pop(seq, None)

    async def _read_responses(self, loop: asyncio.AbstractEventLoop, wait: float) -> None:
        """Without a stream loop running, read packets until a reply lands."""
        end = loop.time() + wait
        while self._sock is not None and loop.time() < end:
            try:
                raw = await asyncio.wait_for(loop.sock_recv(self._sock, 65536), end - loop.time())
            except (asyncio.TimeoutError, OSError):
                return
            if raw[:4] == _VLVL_MAGIC and len(raw) >= 6 and struct.unpack_from(">H", raw, 4)[0] == _VLVL_CMD_RESP_TYPE:
                self._handle_response(raw)
                if not self._pending or all(f.done() for f in self._pending.values()):
                    return

    def _decrypt_dense(self, payload: bytes) -> bytes:
        """Decrypt every whole 16-byte block (audio packets)."""
        if self._key is None:
            return payload
        whole = len(payload) // _CRYPT_BLOCK * _CRYPT_BLOCK
        decryptor = Cipher(algorithms.AES(self._key), modes.ECB()).decryptor()
        return decryptor.update(payload[:whole]) + payload[whole:]

    def _assemble(self, frame_packets: dict[int, bytes]) -> bytes:
        """Join payloads in seqC order and decrypt the sparse AES blocks."""
        frame = bytearray()
        for _, pkt_payload in sorted(frame_packets.items()):
            frame.extend(pkt_payload)
        if self._key is not None:
            decryptor = Cipher(algorithms.AES(self._key), modes.ECB()).decryptor()
            for off in range(0, len(frame) - _CRYPT_BLOCK + 1, _CRYPT_STRIDE):
                end = off + _CRYPT_BLOCK
                frame[off:end] = decryptor.update(bytes(frame[off:end]))
        return bytes(frame)

    def close(self) -> None:
        """Close the UDP socket and release resources."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            _LOGGER.debug("VLVL session closed (was on port %d)", self._port)


# ── Utilities ──────────────────────────────────────────────────────────────


def is_keyframe(frame: bytes) -> bool:
    """True if an Annex B access unit starts with SPS or an IDR slice."""
    i = frame.find(b"\x00\x00\x01", 0, 64)
    return i >= 0 and i + 3 < len(frame) and (frame[i + 3] & 0x1F) in (5, 7)


class _PendingFrame:
    """A video frame being reassembled (header gives its packet count)."""

    __slots__ = ("ts_ms", "count", "packets", "completed_at")

    def __init__(self, ts_ms: int, count: int) -> None:
        self.ts_ms = ts_ms
        self.count = count
        self.packets: dict[int, bytes] = {}
        self.completed_at: float | None = None

    @property
    def complete(self) -> bool:
        return len(self.packets) >= self.count > 0


async def get_camera_mac(
    camera_ip: str,
    http_session: aiohttp.ClientSession,
) -> str | None:
    """Return the camera's MAC address (e.g. 'AABBCCDDEEFF')."""
    url = f"http://{camera_ip}/?req=get_mac_address"
    try:
        async with http_session.get(
            url, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            text = (await resp.text()).strip()
            # Response: "get_mac_address: AABBCCDDEEFF"
            if ": " in text:
                mac = text.split(": ", 1)[1].strip()
            else:
                mac = text
            mac = mac.replace(":", "").replace("-", "").upper()
            if len(mac) == 12 and all(c in "0123456789ABCDEF" for c in mac):
                return mac
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("get_mac_address failed: %s", err)
    return None


def get_local_ip_for(camera_ip: str) -> str:
    """Return the local IP that the OS would use to reach camera_ip.

    This creates a dummy UDP socket (no packets sent) to determine
    which local interface the OS would route the traffic through.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((camera_ip, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "0.0.0.0"


async def h264_to_jpeg(h264_bytes: bytes) -> bytes | None:
    """Decode an H264 Annex B frame to JPEG using ffmpeg.

    Returns JPEG bytes or None if ffmpeg is unavailable / decoding fails.
    The h264_bytes should start with SPS+PPS NALUs (CODEC_EXTRADATA) for
    reliable decoding.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "h264",  # raw H264 Annex B input
            "-i",
            "pipe:0",
            "-frames:v",
            "1",       # decode only one frame
            "-vcodec",
            "mjpeg",
            "-q:v",
            "2",       # JPEG quality (1=best, 31=worst)
            "-f",
            "image2",
            "pipe:1",  # output to stdout
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(h264_bytes), timeout=10.0
        )
        if stdout and stdout[:2] == b"\xff\xd8":  # JPEG SOI magic
            return stdout
        _LOGGER.debug("ffmpeg output is not JPEG (len=%d)", len(stdout) if stdout else 0)
    except FileNotFoundError:
        _LOGGER.warning(
            "ffmpeg not found — cannot decode H264 frames to JPEG. "
            "Install ffmpeg on the host to enable live snapshots."
        )
    except (asyncio.TimeoutError, OSError) as err:
        _LOGGER.debug("h264_to_jpeg failed: %s", err)
    return None
