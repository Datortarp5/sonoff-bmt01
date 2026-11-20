"""Offline BLE lifecycle and command tests with a deterministic fake link."""

import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from support import (
    FakeBleakError, FakeLink, client_module, make_client, packet,
    probe_packet, protocol,
)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = make_client()

    async def asyncTearDown(self):
        await self.client.stop()

    async def next_write(self):
        return await asyncio.wait_for(self.client._client.written.get(), 1)

    def stream(self, tsn=1, flags=protocol.FLAG_STREAM):
        self.client._handle_control(probe_packet(self.client.crypto, tsn, flags))

    def battery(self, tsn=1, flags=protocol.FLAG_RESPONSE, value=75):
        self.client._handle_control(packet(
            self.client.crypto, protocol.CMD_BATTERY_LEVEL_RESPONSE,
            flags, tsn, bytes([value]),
        ))

    async def test_response_and_history_cannot_supply_tickets(self):
        task = asyncio.create_task(self.client._next_ticket())
        await asyncio.sleep(0)
        self.stream(flags=protocol.FLAG_RESPONSE)
        self.client._handle_control(packet(
            self.client.crypto, protocol.CMD_HISTORY_RESPONSE,
            protocol.FLAG_STREAM, 2, b"\x00" * 4,
        ))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.stream(3)
        self.assertEqual(await task, 3)

    async def test_same_sequence_can_be_consumed_in_a_later_generation(self):
        for _ in range(2):
            task = asyncio.create_task(self.client._next_ticket())
            await asyncio.sleep(0)
            self.stream(255)
            self.assertEqual(await task, 255)
        self.assertEqual(self.client._consumed_generation, 2)

    async def test_cached_ticket_is_not_reused(self):
        self.stream(1)
        task = asyncio.create_task(self.client._next_ticket())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.stream(2)
        self.assertEqual(await task, 2)

    async def test_battery_requires_matching_flags_ticket_and_valid_value(self):
        task = asyncio.create_task(self.client.async_request_battery())
        await asyncio.sleep(0)
        self.stream(7)
        await self.next_write()
        self.battery(8)
        self.battery(7, flags=protocol.FLAG_STREAM)
        self.battery(7, value=101)
        self.assertIsNone(self.client.battery)
        self.assertFalse(task.done())
        self.battery(7, value=75)
        self.assertEqual(await task, 75)

    async def test_concurrent_battery_requests_use_different_streams(self):
        first = asyncio.create_task(self.client.async_request_battery())
        second = asyncio.create_task(self.client.async_request_battery())
        await asyncio.sleep(0)
        self.stream(4)
        await self.next_write()
        self.battery(4)
        self.assertEqual(await first, 75)
        self.assertFalse(second.done())
        self.stream(5)
        await self.next_write()
        self.battery(5, value=74)
        self.assertEqual(await second, 74)

    async def test_timeout_retry_ignores_late_response(self):
        retry_started = asyncio.Event()
        original_next_ticket = self.client._next_ticket
        calls = 0
        async def next_ticket():
            nonlocal calls
            calls += 1
            if calls == 2:
                retry_started.set()
            return await original_next_ticket()
        with (
            patch.object(client_module, "RESPONSE_TIMEOUT", 0.01),
            patch.object(self.client, "_next_ticket", side_effect=next_ticket),
        ):
            task = asyncio.create_task(self.client.async_request_battery())
            await asyncio.sleep(0)
            self.stream(1)
            await self.next_write()
            await asyncio.wait_for(retry_started.wait(), 1)
            self.stream(2)
            await self.next_write()
            self.battery(1)
            self.assertFalse(task.done())
            self.battery(2, value=80)
            self.assertEqual(await task, 80)

    async def test_disconnect_clears_readings_and_wakes_ticket_waiter(self):
        self.stream()
        task = asyncio.create_task(self.client._next_ticket())
        await asyncio.sleep(0)
        self.client._on_disconnect(self.client._client)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.probes, [None] * 4)
        with self.assertRaises(FakeBleakError):
            await task

    async def test_old_disconnect_callback_does_not_break_current_session(self):
        self.client._on_disconnect(FakeLink())
        self.assertTrue(self.client.connected)

    async def test_new_session_does_not_restore_old_temperatures(self):
        self.stream()
        self.client._reset_session_state()
        self.assertEqual(self.client.probes, [None] * 4)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client._ticket_generation, 0)

    async def test_notification_buffers_are_separate(self):
        first = probe_packet(self.client.crypto)
        second = probe_packet(self.client.crypto, tsn=2)
        self.client._on_notify(SimpleNamespace(handle=10), bytearray(first[:3]))
        self.client._on_notify(SimpleNamespace(handle=15), bytearray(second))
        self.assertEqual(self.client._ticket_generation, 1)
        self.client._on_notify(SimpleNamespace(handle=10), bytearray(first[3:]))
        self.assertEqual(self.client._ticket_generation, 2)

    async def test_auth_result_requires_confirmation_and_complete_status(self):
        self.client._auth_event.clear()
        self.client._handle_session(bytes.fromhex("0300010000"))
        self.assertFalse(self.client._auth_event.is_set())
        self.client._confirm_task = asyncio.create_task(asyncio.sleep(10))
        self.client._handle_session(bytes.fromhex("03000100"))
        self.assertFalse(self.client._auth_event.is_set())
        self.client._handle_session(bytes.fromhex("0300010001"))
        self.assertTrue(self.client._auth_event.is_set())
        self.assertIsInstance(self.client._auth_error, client_module.BMT01AuthError)

    async def test_sync_requires_auth_and_complete_response(self):
        self.client._authenticated = False
        self.client._handle_session(bytes.fromhex("22000300010005"))
        self.assertFalse(self.client._sync_event.is_set())
        self.client._authenticated = True
        self.client._handle_session(bytes.fromhex("2200030001"))
        self.assertFalse(self.client._sync_event.is_set())
        self.client._handle_session(bytes.fromhex("22000300010005"))
        self.assertTrue(self.client._sync_event.is_set())

    async def test_auth_packet_boundaries_and_response_write(self):
        await self.client._send_session(protocol.AUTH_REQUEST, b"x" * 64)
        self.assertEqual(
            [(len(data), response) for data, response in self.client._client.writes],
            [(68, True)],
        )
        self.client._client.writes.clear()
        await self.client._send_session(protocol.AUTH_CONFIRM, b"x" * 108)
        self.assertEqual(
            [(len(data), response) for data, response in self.client._client.writes],
            [(70, True), (3, True), (39, True)],
        )

    async def test_small_mtu_and_write_without_response_fallback(self):
        for link in (FakeLink(mtu=23), FakeLink(properties=("write-without-response",))):
            self.client._client = link
            await self.client._send_session(protocol.AUTH_REQUEST, b"x" * 64)
            self.assertEqual([len(data) for data, _ in link.writes], [20, 20, 20, 8])
            self.assertTrue(all(response == ("write" in link.characteristic.properties)
                                for _, response in link.writes))

    async def test_write_rejects_old_client_and_missing_characteristic(self):
        with self.assertRaises(FakeBleakError):
            await self.client._write(b"test", expected_client=FakeLink())
        self.client._client.services.get_characteristic = lambda uuid: None
        with self.assertRaises(FakeBleakError):
            await self.client._write(b"test")

    async def test_cleanup_cancels_confirmation_and_pending_request(self):
        self.client._confirm_task = asyncio.create_task(asyncio.sleep(10))
        confirm = self.client._confirm_task
        future = asyncio.get_running_loop().create_future()
        self.client._battery_future = future
        await self.client._cleanup_session()
        self.assertTrue(confirm.cancelled())
        self.assertTrue(future.cancelled())
        self.assertIsNone(self.client._client)

    async def test_silent_session_times_out_and_disconnects(self):
        link = FakeLink()
        async def send_session(frame_type, body, **kwargs):
            if frame_type == protocol.AUTH_REQUEST:
                self.client._auth_event.set()
            elif frame_type == protocol.SYNC_REQUEST:
                self.client._sync_event.set()
        with (
            patch.object(client_module.bluetooth, "async_ble_device_from_address",
                         return_value=object(), create=True),
            patch.object(client_module, "establish_connection", AsyncMock(return_value=link)),
            patch.object(self.client, "_send_session", side_effect=send_session),
            patch.object(client_module, "TELEMETRY_TIMEOUT", 0.01),
        ):
            with self.assertRaisesRegex(RuntimeError, "telemetry timed out"):
                await self.client._session()
        self.assertFalse(link.is_connected)
        self.assertFalse(self.client.connected)

    async def test_complete_handshake_stream_and_disconnect(self):
        link = FakeLink()
        ready = asyncio.Event()
        frames = protocol.FrameBuffer()
        characteristic = SimpleNamespace(handle=10)
        def notify(data):
            link.notifications[client_module.NOTIFY_UUID](
                characteristic, bytearray(data)
            )
        def device_write(data):
            for frame in frames.feed(data):
                if frame[0] == protocol.AUTH_REQUEST:
                    app_uuid = self.client.crypto.aes_decrypt_b64(frame[4:])
                    self.assertEqual(app_uuid, self.client._app_uuid)
                    body = self.client.crypto.aes_encrypt_b64(
                        "00000000-0000-4000-8000-000000000001"
                    )
                    challenge = b"\x01\x00" + struct.pack("<H", len(body)) + body
                    for byte in challenge:
                        notify(bytes([byte]))
                elif frame[0] == protocol.AUTH_CONFIRM:
                    confirm = self.client.crypto.aes_decrypt_b64(frame[4:])
                    self.assertEqual(
                        confirm,
                        self.client._app_uuid + "_00000000-0000-4000-8000-000000000001",
                    )
                    notify(bytes.fromhex("0300010000"))
                elif frame[0] == protocol.SYNC_REQUEST:
                    notify(bytes.fromhex("22000300010005"))
                    notify(probe_packet(self.client.crypto))
        def on_state():
            if self.client.connected:
                ready.set()
        link.on_write = device_write
        self.client.async_add_listener(on_state)
        with (
            patch.object(client_module.bluetooth, "async_ble_device_from_address",
                         return_value=object(), create=True),
            patch.object(client_module, "establish_connection", AsyncMock(return_value=link)),
        ):
            session = asyncio.create_task(self.client._session())
            await asyncio.wait_for(ready.wait(), 2)
            self.assertEqual(self.client.probes, [26.0, "not_connected", "not_connected", 32.0])
            self.client._on_disconnect(link)
            await asyncio.wait_for(session, 2)
        self.assertEqual(self.client.probes, [None] * 4)
        self.assertIsNone(self.client._confirm_task)
        self.assertFalse(link.is_connected)
