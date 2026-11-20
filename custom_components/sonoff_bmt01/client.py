"""Persistent, authenticated BLE connection to a Sonoff BMT01."""

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
    SYNC_TIMEOUT,
    TELEMETRY_TIMEOUT,
    TICKET_MAX_AGE,
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
    FLAG_RESPONSE,
    FLAG_STREAM,
    HEARTBEAT,
    PROBE_COUNT,
    SYNC_REQUEST,
    SYNC_RESPONSE,
    BMT01Crypto,
    FrameBuffer,
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
        self._confirm_task: asyncio.Task | None = None
        self._stopping = False
        self._authenticated = False
        self._synced = False
        self._app_uuid = ""
        self._rx_buffers: dict[int, FrameBuffer] = {}
        self._auth_event = asyncio.Event()
        self._sync_event = asyncio.Event()
        self._auth_error: Exception | None = None
        self._disconnected = asyncio.Event()
        self._last_tsn: int | None = None
        self._ticket_generation = 0
        self._consumed_generation = 0
        self._ticket_time = 0.0
        self._last_telemetry_time = 0.0
        self._tsn_event = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._battery_future: asyncio.Future[int] | None = None
        self._battery_tsn: int | None = None
        self._listeners: list[CALLBACK_TYPE] = []

    @property
    def connected(self) -> bool:
        """True while the current link is authenticated and synchronized."""
        return bool(
            self._authenticated
            and self._synced
            and self._client is not None
            and self._client.is_connected
            and not self._disconnected.is_set()
        )

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register an update callback."""
        self._listeners.append(update_callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    def start(self, entry: ConfigEntry) -> None:
        """Start the background connection loop."""
        self._entry = entry
        self._task = entry.async_create_background_task(
            self.hass, self._run(), f"sonoff_bmt01_{self.address}"
        )

    async def stop(self) -> None:
        """Cancel the connection loop and all session work."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._cleanup_session()

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

    def _reset_session_state(self) -> None:
        self._disconnected = asyncio.Event()
        self._auth_event = asyncio.Event()
        self._sync_event = asyncio.Event()
        self._auth_error = None
        self._authenticated = False
        self._synced = False
        self._app_uuid = ""
        self._rx_buffers.clear()
        self.probes = [None] * PROBE_COUNT
        self._last_tsn = None
        self._ticket_generation = 0
        self._consumed_generation = 0
        self._ticket_time = 0.0
        self._last_telemetry_time = 0.0
        self._tsn_event = asyncio.Event()

    def _check_connection(self) -> None:
        if (
            self._stopping
            or self._disconnected.is_set()
            or self._client is None
            or not self._client.is_connected
        ):
            raise BleakError("thermometer disconnected")

    async def _session(self) -> None:
        self._reset_session_state()
        self._notify_listeners()
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise RuntimeError("not in range of any connectable adapter or proxy")

        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            f"BMT01 {self.address}",
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )
        self._client = client

        # Bind callbacks to this client so delayed notifications cannot affect
        # the next session after a reconnect.
        def notify(characteristic, data: bytearray) -> None:
            if self._client is client and not self._disconnected.is_set():
                self._on_notify(characteristic, data)

        try:
            await client.start_notify(NOTIFY_UUID, notify)
            try:
                await client.start_notify(NOTIFY2_UUID, notify)
            except BleakError as err:
                _LOGGER.debug("%s: secondary notify unavailable: %s", self.address, err)

            self._app_uuid = str(uuid.uuid4())
            await self._send_session(
                AUTH_REQUEST, self.crypto.aes_encrypt_b64(self._app_uuid)
            )
            try:
                await asyncio.wait_for(self._auth_event.wait(), AUTH_TIMEOUT)
            except TimeoutError as err:
                raise RuntimeError(
                    "authentication timed out; check key, link and app connection"
                ) from err
            self._check_connection()
            if self._auth_error:
                raise self._auth_error

            self._authenticated = True
            await self._send_session(SYNC_REQUEST, b"")
            await asyncio.wait_for(self._sync_event.wait(), SYNC_TIMEOUT)
            self._check_connection()
            self._synced = True
            self._last_telemetry_time = self.hass.loop.time()
            _LOGGER.info("%s: connected", self.address)
            self._notify_listeners()

            if self._entry:
                self._battery_task = self._entry.async_create_background_task(
                    self.hass, self._battery_loop(), f"sonoff_bmt01_battery_{self.address}"
                )

            while not self._disconnected.is_set():
                remaining = TELEMETRY_TIMEOUT - (
                    self.hass.loop.time() - self._last_telemetry_time
                )
                if remaining <= 0:
                    raise RuntimeError("probe telemetry timed out; reconnecting")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._disconnected.wait(), remaining)
            _LOGGER.info("%s: disconnected", self.address)
        finally:
            await self._cleanup_session()

    async def _cleanup_session(self) -> None:
        self._authenticated = False
        self._synced = False
        self.probes = [None] * PROBE_COUNT
        self._disconnected.set()
        self._auth_event.set()
        self._sync_event.set()
        self._tsn_event.set()
        if self._battery_future and not self._battery_future.done():
            self._battery_future.cancel()
        self._battery_future = None
        self._battery_tsn = None
        for name in ("_battery_task", "_confirm_task"):
            task = getattr(self, name)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, name, None)
        client, self._client = self._client, None
        if client and client.is_connected:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._rx_buffers.clear()
        self._notify_listeners()

    def _on_disconnect(self, client) -> None:
        if client is not self._client:
            return
        self._authenticated = False
        self._synced = False
        self.probes = [None] * PROBE_COUNT
        self._disconnected.set()
        self._auth_event.set()
        self._sync_event.set()
        self._tsn_event.set()
        if self._battery_future and not self._battery_future.done():
            self._battery_future.cancel()
        self._notify_listeners()

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
        """Consume one stream generation received after this command started."""
        minimum_generation = self._ticket_generation + 1
        deadline = self.hass.loop.time() + TICKET_TIMEOUT
        while True:
            self._check_connection()
            self._tsn_event.clear()
            if (
                self._last_tsn is not None
                and self._ticket_generation >= minimum_generation
                and self._ticket_generation > self._consumed_generation
                and self.hass.loop.time() - self._ticket_time <= TICKET_MAX_AGE
            ):
                self._consumed_generation = self._ticket_generation
                return self._last_tsn
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                raise TimeoutError("no fresh stream ticket")
            await asyncio.wait_for(self._tsn_event.wait(), remaining)

    async def async_request_battery(self, attempts: int = 3) -> int:
        """Read battery with a serialized, ticket-matched transaction."""
        async with self._command_lock:
            if not self.connected:
                raise BleakError("thermometer is not ready")
            for _ in range(attempts):
                tsn = await self._next_ticket()
                self._battery_tsn = tsn
                future = self.hass.loop.create_future()
                self._battery_future = future
                try:
                    await self._write(
                        build_control(self.crypto, CMD_BATTERY_LEVEL_REQUEST, tsn)
                    )
                    return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
                except TimeoutError:
                    continue
                finally:
                    if not future.done():
                        future.cancel()
                    self._battery_future = None
                    self._battery_tsn = None
            raise RuntimeError(f"no battery response after {attempts} attempts")

    async def _write(
        self,
        data: bytes,
        *,
        session_type: int | None = None,
        expected_client: BleakClientWithServiceCache | None = None,
    ) -> None:
        client = expected_client if expected_client is not None else self._client
        async with self._write_lock:
            self._check_connection()
            if client is not self._client:
                raise BleakError("session changed before write")
            characteristic = client.services.get_characteristic(WRITE_UUID)
            if characteristic is None:
                raise BleakError("BMT01 write characteristic is missing")
            properties = characteristic.properties
            response = "write" in properties
            if not response and "write-without-response" not in properties:
                raise BleakError("BMT01 characteristic does not support writes")

            limit = (
                max(20, min(512, client.mtu_size - 3))
                if response
                else characteristic.max_write_without_response_size
            )
            # Preserve the packet boundaries captured from the working app when
            # the adapter supports them; retain small-MTU fragmentation otherwise.
            if session_type == AUTH_REQUEST and len(data) <= limit:
                packets = [data]
            elif session_type == AUTH_CONFIRM and limit >= 70 and len(data) == 112:
                packets = [data[:70], data[70:73], data[73:]]
            else:
                size = min(CHUNK_SIZE, limit)
                if size <= 0:
                    raise BleakError("invalid GATT write size")
                packets = [data[i : i + size] for i in range(0, len(data), size)]
            for packet in packets:
                self._check_connection()
                if self._client is not client:
                    raise BleakError("session changed during write")
                await client.write_gatt_char(characteristic, packet, response=response)
                await asyncio.sleep(0.05)
            _LOGGER.debug("%s: TX type=0x%02X len=%s", self.address, data[0], len(data))

    async def _send_session(
        self, frame_type: int, body: bytes, *, expected_client=None
    ) -> None:
        header = bytes([frame_type, 0x00]) + struct.pack("<H", len(body))
        await self._write(
            header + body, session_type=frame_type, expected_client=expected_client
        )

    def _on_notify(self, characteristic, data: bytearray) -> None:
        handle = characteristic.handle
        buffer = self._rx_buffers.setdefault(handle, FrameBuffer())
        try:
            frames = buffer.feed(bytes(data))
        except ValueError as err:
            _LOGGER.debug("%s: invalid notification on %s: %s", self.address, handle, err)
            return
        for frame in frames:
            if frame[1] == 0x00:
                if frame[0] == AUTH_CHALLENGE:
                    self._handle_challenge(frame)
                else:
                    self._handle_session(frame)
            else:
                self._handle_control(frame)

    def _handle_challenge(self, frame: bytes) -> None:
        if self._confirm_task is not None or not self._app_uuid:
            return
        try:
            device_uuid = self.crypto.aes_decrypt_b64(frame[4:].rstrip(b"\x00"))
            uuid.UUID(device_uuid)
        except (ValueError, UnicodeError):
            self._auth_error = BMT01AuthError(
                "could not decrypt a valid device challenge; check the key"
            )
            self._auth_event.set()
            return
        confirm = self.crypto.aes_encrypt_b64(f"{self._app_uuid}_{device_uuid}")
        self._confirm_task = self.hass.async_create_task(
            self._send_confirmation(confirm, self._client)
        )

    async def _send_confirmation(self, confirm: bytes, client) -> None:
        try:
            await self._send_session(AUTH_CONFIRM, confirm, expected_client=client)
        except Exception as err:  # noqa: BLE001
            self._auth_error = err
            self._auth_event.set()

    def _handle_session(self, data: bytes) -> None:
        if (
            len(data) < 4
            or struct.unpack_from("<H", data, 2)[0] != len(data) - 4
        ):
            return
        if data[0] == AUTH_ACCEPTED:
            if self._confirm_task is None or len(data) != 5:
                return
            if data[4] != 0:
                self._auth_error = BMT01AuthError(
                    "the thermometer rejected authentication"
                )
            self._auth_event.set()
        elif data[0] == SYNC_RESPONSE:
            if self._authenticated and len(data) == 7:
                self._sync_event.set()
        elif data[0] != HEARTBEAT:
            _LOGGER.debug("%s: unhandled session frame 0x%02X", self.address, data[0])

    def _handle_control(self, data: bytes) -> None:
        if not self._authenticated:
            return
        frame = parse_control(self.crypto, data)
        if frame is None:
            _LOGGER.debug("%s: malformed control frame ignored", self.address)
            return
        if frame.command == CMD_PROBE_TEMPERATURE_RESPONSE:
            probes = decode_probe_frame(self.crypto, frame)
            if probes is None:
                _LOGGER.debug(
                    "%s: unsupported probe frame flags=0x%02X",
                    self.address,
                    frame.flags,
                )
                return
            self.probes = probes
            self._last_telemetry_time = self.hass.loop.time()
            if frame.flags == FLAG_STREAM:
                self._last_tsn = frame.tsn
                self._ticket_generation += 1
                self._ticket_time = self._last_telemetry_time
                self._tsn_event.set()
            self._notify_listeners()
        elif (
            frame.command == CMD_BATTERY_LEVEL_RESPONSE
            and frame.flags == FLAG_RESPONSE
            and len(frame.payload) == 1
            and frame.tsn == self._battery_tsn
            and self._battery_future is not None
            and not self._battery_future.done()
        ):
            level = frame.payload[0]
            if 0 <= level <= 100:
                self.battery = level
                self._battery_future.set_result(level)
                self._notify_listeners()
