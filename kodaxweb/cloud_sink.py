"""Answer the camera's leftover cloud checks on TCP 8894.

Firmware still dials mqtt-t01-r1/r2/r3.perimetersafe.com. A silent accept or
an immediate reset (what a stock MQTT broker does, because the camera speaks
TLS) stops the 4.5-minute Wi-Fi drop on some firmware, but older firmware
keeps clicking on every failed handshake. This completes TLS with a
certificate generated here and then holds the connection. The certificate is
ours, stored in the data directory, and is not a PerimeterSafe certificate.
If the firmware pins Kodak's certificate, the handshake still fails.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger("kodaxweb.cloud_sink")

# Names the firmware looks up. DNS for these must point at this host.
CLOUD_HOSTS = (
    "mqtt-t01-r1.perimetersafe.com",
    "mqtt-t01-r2.perimetersafe.com",
    "mqtt-t01-r3.perimetersafe.com",
)
DEFAULT_PORT = 8894
_CONNACK = bytes((0x20, 0x02, 0x00, 0x00))
_PINGRESP = bytes((0xD0, 0x00))


def _ensure_cert(directory: Path) -> tuple[Path, Path]:
    """Write a stable local certificate if this data directory does not have one."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "cloud-sink.pem"
    key_path = directory / "cloud-sink.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CLOUD_HOSTS[0])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in CLOUD_HOSTS]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    _LOGGER.info("wrote local cloud-sink certificate %s", cert_path)
    return cert_path, key_path


def _packet_id(data: bytes) -> bytes | None:
    """Packet id for a short MQTT packet whose remaining length fits in one byte."""
    if len(data) < 4 or data[1] >= 0x80:
        return None
    return data[2:4]


def _reply(data: bytes) -> bytes | None:
    """Answer MQTT session packets. Do not log or return the body."""
    if not data:
        return None
    kind = data[0] & 0xF0
    if kind == 0x10:
        return _CONNACK
    if kind == 0xC0:
        return _PINGRESP
    if kind == 0x80:
        packet_id = _packet_id(data)
        if packet_id is None:
            return None
        return bytes((0x90, 0x03)) + packet_id + bytes((0x00,))
    if kind == 0x30 and data[0] & 0x06:
        if len(data) < 6 or data[1] >= 0x80:
            return None
        topic_len = int.from_bytes(data[2:4], "big")
        at = 4 + topic_len
        if len(data) < at + 2:
            return None
        packet_id = data[at:at + 2]
        ack = 0x40 if (data[0] & 0x06) == 0x02 else 0x50
        return bytes((ack, 0x02)) + packet_id
    return None


async def _session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    ssl_object = writer.get_extra_info("ssl_object")
    cipher = ssl_object.cipher() if ssl_object is not None else None
    _LOGGER.info("tls accepted from %s cipher=%s", peer, cipher)
    try:
        while True:
            data = await reader.read(512)
            if not data:
                _LOGGER.info("cloud check %s closed", peer)
                break
            _LOGGER.info("cloud check %s sent %d bytes type=0x%02x", peer, len(data), data[0])
            reply = _reply(data)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
    except (ConnectionError, TimeoutError, ssl.SSLError) as err:
        _LOGGER.info("cloud check %s ended: %s", peer, type(err).__name__)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


class CloudSink:
    """TLS listener for the firmware's dead cloud check. LAN only."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, data_dir: Path | None = None) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir or Path("data")
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        cert_path, key_path = _ensure_cert(self.data_dir)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1
        try:
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            _LOGGER.debug("OpenSSL refused SECLEVEL=0; using default ciphers")
        context.load_cert_chain(cert_path, key_path)
        self._server = await asyncio.start_server(_session, self.host, self.port, ssl=context)
        bound = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        _LOGGER.info("cloud sink listening on %s (local TLS, not a PerimeterSafe certificate)", bound)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
