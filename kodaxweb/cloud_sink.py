"""Accept the camera's leftover cloud TCP checks.

Firmware still dials mqtt-t01-r1/r2/r3.perimetersafe.com on TCP 8894. When
nothing answers, some cameras click, flash, and later leave Wi-Fi
(``1_FailToConnServer``). Completing the TCP handshake is enough to stop
that. The camera then fails its own TLS handshake. This is not an MQTT
broker and it does not present a PerimeterSafe certificate.
"""
from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger("kodaxweb.cloud_sink")

# Names the firmware looks up. DNS for these must point at this host.
CLOUD_HOSTS = (
    "mqtt-t01-r1.perimetersafe.com",
    "mqtt-t01-r2.perimetersafe.com",
    "mqtt-t01-r3.perimetersafe.com",
)
DEFAULT_PORT = 8894


async def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    _LOGGER.info("cloud check from %s", peer)
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=8)
            if not chunk:
                break
    except (TimeoutError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


class CloudSink:
    """TCP acceptor. Holds each connection only long enough to read a hello."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(_accept, self.host, self.port)
        bound = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        _LOGGER.info("cloud sink listening on %s (TCP accept only)", bound)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
