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


def _remaining_length(data: bytes) -> tuple[int, int] | None:
    """Return an MQTT packet's remaining length and encoded field width."""
    if len(data) < 2:
        return None
    value = 0
    multiplier = 1
    for index in range(1, min(len(data), 5)):
        digit = data[index]
        value += (digit & 0x7F) * multiplier
        if digit < 0x80:
            return value, index
        multiplier *= 128
    if len(data) >= 5:
        raise ValueError("invalid MQTT remaining length")
    return None


def _packet_size(data: bytes) -> int | None:
    decoded = _remaining_length(data)
    if decoded is None:
        return None
    remaining_length, width = decoded
    return 1 + width + remaining_length


def _encode_remaining_length(value: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        encoded.append(digit)
        if not value:
            return bytes(encoded)


def _packet_id(data: bytes) -> bytes | None:
    try:
        packet_size = _packet_size(data)
        decoded = _remaining_length(data)
    except ValueError:
        return None
    if packet_size is None or decoded is None or len(data) != packet_size:
        return None
    remaining_length, width = decoded
    offset = 1 + width
    if remaining_length < 2:
        return None
    return data[offset:offset + 2]


def _subscription_count(data: bytes, offset: int) -> int | None:
    count = 0
    while offset < len(data):
        if offset + 2 > len(data):
            return None
        topic_length = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        if topic_length == 0 or offset + topic_length + 1 > len(data):
            return None
        offset += topic_length
        if data[offset] > 2:
            return None
        offset += 1
        count += 1
    return count or None


def _reply(data: bytes) -> bytes | None:
    """Answer MQTT session packets. Do not log or return the body."""
    if not data:
        return None
    try:
        packet_size = _packet_size(data)
        decoded = _remaining_length(data)
    except ValueError:
        return None
    if packet_size is None or decoded is None or len(data) != packet_size:
        return None
    remaining_length, width = decoded
    offset = 1 + width
    kind = data[0] & 0xF0
    if kind == 0x10:
        return _CONNACK
    if kind == 0xC0:
        return _PINGRESP
    if kind == 0x80:
        packet_id = _packet_id(data)
        if packet_id is None or remaining_length < 2:
            return None
        count = _subscription_count(data, offset + 2)
        if count is None:
            return None
        suback_length = 2 + count
        return (
            bytes((0x90,))
            + _encode_remaining_length(suback_length)
            + packet_id
            + bytes(count)
        )
    if kind == 0x30 and data[0] & 0x06:
        qos = (data[0] >> 1) & 0x03
        if qos == 3 or remaining_length < 2:
            return None
        topic_len = int.from_bytes(data[offset:offset + 2], "big")
        packet_id_offset = offset + 2 + topic_len
        if topic_len == 0 or packet_id_offset + 2 > len(data):
            return None
        packet_id = data[packet_id_offset:packet_id_offset + 2]
        ack = 0x40 if qos == 1 else 0x50
        return bytes((ack, 0x02)) + packet_id
    if kind == 0x60 and data[0] & 0x0F == 0x02 and remaining_length == 2:
        packet_id = _packet_id(data)
        if packet_id is not None:
            return bytes((0x70, 0x02)) + packet_id
    return None


async def _session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    ssl_object = writer.get_extra_info("ssl_object")
    cipher = ssl_object.cipher() if ssl_object is not None else None
    _LOGGER.info("tls accepted from %s cipher=%s", peer, cipher)
    try:
        buffer = bytearray()
        while True:
            data = await reader.read(512)
            if not data:
                _LOGGER.info("cloud check %s closed", peer)
                break
            buffer.extend(data)
            while buffer:
                try:
                    packet_size = _packet_size(buffer)
                except ValueError:
                    _LOGGER.info("cloud check %s sent an invalid MQTT packet", peer)
                    return
                if packet_size is None or len(buffer) < packet_size:
                    break
                packet = bytes(buffer[:packet_size])
                del buffer[:packet_size]
                _LOGGER.info(
                    "cloud check %s sent %d bytes type=0x%02x",
                    peer,
                    len(packet),
                    packet[0],
                )
                reply = _reply(packet)
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
