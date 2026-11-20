"""Sonoff BMT01 BLE protocol: crypto, frame building and parsing.

Reverse engineered by the Home Assistant community:
https://community.home-assistant.io/t/sonoff-bmt01/799673

Session frames:  [type, 0x00, len_lo, len_hi, body...]        (auth, sync)
Control frames:  [cmd_lo, cmd_hi, len_lo, len_hi, XOR(body)]  (data, commands)
  body = [flags, tsn, payload...], XOR'd with MD5(key) per 16-byte block.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_IV = b"0000000000000000"

# Session frame types
AUTH_REQUEST = 0x00
AUTH_CHALLENGE = 0x01
AUTH_CONFIRM = 0x02
AUTH_ACCEPTED = 0x03
HEARTBEAT = 0x1D
SYNC_REQUEST = 0x21
SYNC_RESPONSE = 0x22

# Control commands
CMD_BATTERY_LEVEL_REQUEST = 0x0101
CMD_BATTERY_LEVEL_RESPONSE = 0x0102
CMD_PROBE_TEMPERATURE_RESPONSE = 0x0104
CMD_HISTORY_RESPONSE = 0x0108
CMD_COMMON_RESPONSE = 0xFFFF

COMMAND_NAMES = {
    CMD_BATTERY_LEVEL_RESPONSE: "BATTERY_LEVEL_RESPONSE",
    CMD_PROBE_TEMPERATURE_RESPONSE: "PROBE_TEMPERATURE_RESPONSE",
    CMD_HISTORY_RESPONSE: "HISTORY_RESPONSE",
    CMD_COMMON_RESPONSE: "COMMON_RESPONSE",
}

FLAG_NEED_RESPONSE = 0x02
FLAG_RESPONSE = 0x05
FLAG_STREAM = 0x09

PROBE_COUNT = 4

# Probe status values, exactly as the device reports them
STATUS_OK = "ok"
PROBE_SENTINELS = {
    0xFFFF: "not_connected",
    0xFFFE: "too_high",
    0xFFFD: "too_low",
    0x0FFF: "invalid",
}
PROBE_STATUSES = [STATUS_OK, *PROBE_SENTINELS.values()]

ProbeValue = float | str | None


class BMT01Crypto:
    """Key material for one thermometer."""

    def __init__(self, key: str) -> None:
        """Derive the AES / XOR key (MD5 of the eWeLink key)."""
        self.key = hashlib.md5(key.strip().encode()).digest()

    def aes_encrypt_b64(self, text: str) -> bytes:
        """AES-128-CBC encrypt with PKCS7 padding, return base64."""
        data = text.encode()
        pad = 16 - (len(data) % 16)
        data += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(AES_IV)).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize())

    def aes_decrypt_b64(self, b64: bytes) -> str:
        """Decrypt base64 AES-128-CBC data. Raises ValueError on a wrong key."""
        ciphertext = base64.b64decode(b64, validate=True)
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError("ciphertext length is not a multiple of 16")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(AES_IV)).decryptor()
        raw = decryptor.update(ciphertext) + decryptor.finalize()
        pad = raw[-1]
        if not 1 <= pad <= 16 or raw[-pad:] != bytes([pad]) * pad:
            raise ValueError("bad padding")
        return raw[:-pad].decode()

    def xor(self, data: bytes) -> bytes:
        """XOR with the MD5 key, restarting every 16-byte block."""
        return bytes(b ^ self.key[i % 16] for i, b in enumerate(data))


@dataclass
class ControlFrame:
    """A decrypted control frame."""

    command: int
    flags: int
    source: int  # Sender bit; both stream (0x09) and response (0x05) set it.
    tsn: int
    payload: bytes

    @property
    def name(self) -> str:
        """Readable command name for logs."""
        return COMMAND_NAMES.get(self.command, f"0x{self.command:04X}")


def parse_control(crypto: BMT01Crypto, data: bytes) -> ControlFrame | None:
    """Decrypt a control frame, or return None if the length doesn't match."""
    if len(data) < 6:
        return None
    command, length = struct.unpack_from("<HH", data, 0)
    body = data[4:]
    if length != len(body):
        return None
    dec = crypto.xor(body)
    return ControlFrame(
        command=command,
        flags=dec[0],
        source=dec[0] & 0x01,
        tsn=dec[1],
        payload=bytes(dec[2:]),
    )


def build_control(
    crypto: BMT01Crypto, command: int, tsn: int, payload: bytes = b""
) -> bytes:
    """Build an encrypted command.

    tsn must be the sequence number of the latest live probe frame
    (single-use "ticket"); the device ignores stale ones.
    """
    body = crypto.xor(bytes([FLAG_NEED_RESPONSE, tsn & 0xFF]) + payload)
    return struct.pack("<HH", command, len(body)) + body


def decode_probes(payload: bytes) -> list[ProbeValue]:
    """Decode four uint16-LE values: °C, or a status string."""
    probes: list[ProbeValue] = []
    for i in range(PROBE_COUNT):
        ofs = i * 2
        if ofs + 1 >= len(payload):
            probes.append(None)
            continue
        raw = struct.unpack_from("<H", payload, ofs)[0]
        probes.append(PROBE_SENTINELS.get(raw, float(raw)))
    return probes


def _plausible(probes: list[ProbeValue]) -> bool:
    return all(
        value is not None and not (isinstance(value, float) and value > 400)
        for value in probes
    )


def decode_probe_frame(
    crypto: BMT01Crypto, frame: ControlFrame
) -> list[ProbeValue] | None:
    """Decode the already-decrypted probe body without guessing another mask."""
    if frame.flags not in (FLAG_STREAM, FLAG_RESPONSE) or len(frame.payload) != 8:
        return None
    probes = decode_probes(frame.payload)
    return probes if _plausible(probes) else None


class FrameBuffer:
    """Reassemble length-prefixed frames for one notification characteristic."""

    def __init__(self, max_payload: int = 1024) -> None:
        self._buffer = bytearray()
        self._max_payload = max_payload

    def feed(self, data: bytes) -> list[bytes]:
        """Retain partial headers/bodies and return every complete frame."""
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= 4:
            length = struct.unpack_from("<H", self._buffer, 2)[0]
            if length > self._max_payload:
                self._buffer.clear()
                raise ValueError("notification payload exceeds frame limit")
            end = 4 + length
            if len(self._buffer) < end:
                break
            frames.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return frames
