"""Persistent BLE connection to a Sonoff BMT01.

Works with local adapters and ESPHome Bluetooth proxies (active mode).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import struct
import uuid

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    AUTH_TIMEOUT,
    BATTERY_INTERVAL,
    BATTERY_RETRY,
    NOTIFY2_UUID,
    NOTIFY_UUID,
    RECONNECT_DELAYS,
    RESPONSE_TIMEOUT,
    TICKET_TIMEOUT,
    WRITE_UUID,
)
from .protocol import (
    AUTH_ACCEPTED,
    AUTH_CHALLENGE,
    AUTH_CONFIRM,
    AUTH_REQUEST,
    CMD_BATTERY_LEVEL_REQUEST,
    CMD_BATTERY_LEVEL_RESPONSE,
    CMD_PROBE_TEMPERATURE_RESPONSE,
    HEARTBEAT,
    PROBE_COUNT,
    SYNC_REQUEST,
    SYNC_RESPONSE,
    BMT01Crypto,
    ProbeValue,
    build_control,
    decode_probe_frame,
    parse_control,
)

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 20


class BMT01AuthError(Exception):
    """The thermometer rejected the key."""


class BMT01Client:
    """Connect, authenticate, receive probe data and read the battery."""

    def __init__(self, hass: HomeAssistant, address: str, key: str) -> None:
        """Initialise."""
        self.hass = hass
        self.address = address.upper()
        self.crypto = BMT01Crypto(key)
        self.probes: list[ProbeValue] = [None] * PROBE_COUNT
        self.battery: int | None = None
        self._entry: ConfigEntry | None = None
        self._client: BleakClientWithServiceCache | None = None
        self._task: asyncio.Task | None = None
        self._battery_task: asyncio.Task | None = None
        self._stopping = False
        self._authenticated = False
        self._app_uuid = ""
        self._rx_buf = bytearray()
        self._auth_event = asyncio.Event()
        self._auth_error: str | None = None
        self._disconnected = asyncio.Event()
        self._last_tsn: int | None = None
        self._tsn_event = asyncio.Event()
        self._battery_future: asyncio.Future[int] | None = None
        self._listeners: list[CALLBACK_TYPE] = []

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        """True while connected and authenticated."""
        return self._authenticated and self._client is not None

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register an update callback; returns a function that removes it."""
        self._listeners.append(update_callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------------ lifecycle

    def start(self, entry: ConfigEntry) -> None:
        """Start the background connection loop."""
        self._entry = entry
        self._task = entry.async_create_background_task(
            self.hass, self._run(), f"sonoff_bmt01_{self.address}"
        )

    async def stop(self) -> None:
        """Stop the loop and disconnect."""
        self._stopping = True
        for task in (self._battery_task, self._task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._disconnect()

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except BMT01AuthError as err:
                _LOGGER.error("%s: %s", self.address, err)
                attempt = len(RECONNECT_DELAYS) - 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: %s", self.address, err)
            if self._stopping:
                break
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise RuntimeError("not in range of any connectable adapter or proxy")

        self._disconnected = asyncio.Event()
        self._auth_event = asyncio.Event()
        self._auth_error = None
        self._rx_buf.clear()
        self._last_tsn = None
        self._tsn_event = asyncio.Event()

        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"BMT01 {self.address}",
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )
        self._client = client
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
            try:
                await client.start_notify(NOTIFY2_UUID, self._on_notify)
            except BleakError as err:
                _LOGGER.debug("%s: secondary notify unavailable: %s", self.address, err)

            self._app_uuid = str(uuid.uuid4())
            await self._send_session(AUTH_REQUEST, self.crypto.aes_encrypt_b64(self._app_uuid))

            try:
                await asyncio.wait_for(self._auth_event.wait(), AUTH_TIMEOUT)
            except TimeoutError as err:
                raise RuntimeError(
                    "authentication timed out (is the eWeLink app connected to it?)"
                ) from err
            if self._auth_error:
                raise BMT01AuthError(self._auth_error)

            _LOGGER.info("%s: connected", self.address)
            self._authenticated = True
            self._notify_listeners()
            await self._write(bytes([SYNC_REQUEST, 0x00, 0x00, 0x00]))

            if self._entry:
                self._battery_task = self._entry.async_create_background_task(
                    self.hass, self._battery_loop(), f"sonoff_bmt01_battery_{self.address}"
                )

            await self._disconnected.wait()
            _LOGGER.info("%s: disconnected", self.address)
        finally:
            if self._battery_task:
                self._battery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._battery_task
                self._battery_task = None
            self._authenticated = False
            await self._disconnect()
            self._notify_listeners()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client and client.is_connected:
            with contextlib.suppress(Exception):
                await client.disconnect()

    def _on_disconnect(self, _client) -> None:
        self._disconnected.set()

    # ------------------------------------------------------------------ battery

    async def _battery_loop(self) -> None:
        while True:
            try:
                await self.async_request_battery()
                await asyncio.sleep(BATTERY_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: battery read failed: %s", self.address, err)
                await asyncio.sleep(BATTERY_RETRY)

    async def _next_ticket(self) -> int:
        """Wait for the next live probe frame and return its sequence number."""
        self._tsn_event.clear()
        await asyncio.wait_for(self._tsn_event.wait(), TICKET_TIMEOUT)
        if self._last_tsn is None:
            raise RuntimeError("no ticket")
        return self._last_tsn

    async def async_request_battery(self, attempts: int = 3) -> int:
        """Ask the thermometer for its battery level (%)."""
        for _ in range(attempts):
            tsn = await self._next_ticket()
            self._battery_future = self.hass.loop.create_future()
            try:
                await self._write(build_control(self.crypto, CMD_BATTERY_LEVEL_REQUEST, tsn))
                return await asyncio.wait_for(self._battery_future, RESPONSE_TIMEOUT)
            except TimeoutError:
                continue
            finally:
                self._battery_future = None
        raise RuntimeError(f"no battery response after {attempts} attempts")

    # ------------------------------------------------------------------ TX

    async def _write(self, data: bytes) -> None:
        if not self._client:
            raise RuntimeError("not connected")
        for i in range(0, len(data), CHUNK_SIZE):
            await self._client.write_gatt_char(
                WRITE_UUID, data[i : i + CHUNK_SIZE], response=False
            )
            await asyncio.sleep(0.05)
        _LOGGER.debug("%s: TX %s", self.address, data.hex(" "))

    async def _send_session(self, frame_type: int, body: bytes) -> None:
        header = bytes([frame_type, 0x00]) + struct.pack("<H", len(body))
        await self._write(header + body)

    # ------------------------------------------------------------------ RX

    def _on_notify(self, _char, data: bytearray) -> None:
        data = bytes(data)
        _LOGGER.debug("%s: RX %s", self.address, data.hex(" "))
        if len(data) < 2:
            return

        # The auth challenge can span several notifications: reassemble it
        if self._rx_buf or (len(data) >= 4 and data[0] == AUTH_CHALLENGE and data[1] == 0x00):
            self._rx_buf += data
            if len(self._rx_buf) > 1024:
                self._rx_buf.clear()
                return
            needed = 4 + struct.unpack_from("<H", self._rx_buf, 2)[0]
            if len(self._rx_buf) < needed:
                return
            frame = bytes(self._rx_buf[:needed])
            self._rx_buf.clear()
            self._handle_challenge(frame)
            return

        if data[1] == 0x00:
            self._handle_session(data)
        else:
            self._handle_control(data)

    def _handle_challenge(self, frame: bytes) -> None:
        try:
            device_uuid = self.crypto.aes_decrypt_b64(frame[4:].rstrip(b"\x00"))
        except Exception:  # noqa: BLE001
            self._auth_error = (
                "the key is wrong (could not decrypt the device challenge)"
            )
            self._auth_event.set()
            return
        confirm = self.crypto.aes_encrypt_b64(f"{self._app_uuid}_{device_uuid}")
        self.hass.async_create_task(self._send_session(AUTH_CONFIRM, confirm))

    def _handle_session(self, data: bytes) -> None:
        if data[0] == AUTH_ACCEPTED:
            self._auth_event.set()
        elif data[0] not in (HEARTBEAT, SYNC_RESPONSE):
            _LOGGER.debug("%s: unhandled session frame 0x%02X", self.address, data[0])

    def _handle_control(self, data: bytes) -> None:
        frame = parse_control(self.crypto, data)
        if frame is None:
            _LOGGER.debug("%s: undecoded frame %s", self.address, data.hex(" "))
            return

        _LOGGER.debug(
            "%s: %s flags=0x%02X tsn=%s payload=%s",
            self.address,
            frame.name,
            frame.flags,
            frame.tsn,
            frame.payload.hex(" "),
        )

        if frame.command == CMD_PROBE_TEMPERATURE_RESPONSE:
            probes = decode_probe_frame(self.crypto, frame)
            if probes is None:
                _LOGGER.debug("%s: implausible probe frame ignored", self.address)
                return
            self.probes = probes
            # Each live probe frame carries a fresh ticket for commands
            self._last_tsn = frame.tsn
            self._tsn_event.set()
            self._notify_listeners()

        elif frame.command == CMD_BATTERY_LEVEL_RESPONSE and frame.payload:
            level = frame.payload[0]
            if not 0 <= level <= 100:
                _LOGGER.debug("%s: battery value %s out of range", self.address, level)
                return
            self.battery = level
            if self._battery_future and not self._battery_future.done():
                self._battery_future.set_result(level)
            self._notify_listeners()
