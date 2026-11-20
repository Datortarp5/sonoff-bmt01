"""Protocol regressions using synthetic keys, never account credentials."""

import struct
import unittest

from support import packet, probe_packet, protocol


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.crypto = protocol.BMT01Crypto("synthetic-test-key")

    def test_aes_roundtrip_and_strict_base64(self):
        encrypted = self.crypto.aes_encrypt_b64("test challenge")
        self.assertEqual(self.crypto.aes_decrypt_b64(encrypted), "test challenge")
        with self.assertRaises(ValueError):
            self.crypto.aes_decrypt_b64(b"!" + encrypted)

    def test_stream_and_response_decode_identically_for_different_keys(self):
        for key in ("synthetic-test-key", "another-synthetic-key"):
            crypto = protocol.BMT01Crypto(key)
            for flags in (protocol.FLAG_STREAM, protocol.FLAG_RESPONSE):
                frame = protocol.parse_control(crypto, probe_packet(crypto, flags=flags))
                self.assertEqual(
                    protocol.decode_probe_frame(crypto, frame),
                    [26.0, "not_connected", "not_connected", 32.0],
                )

    def test_status_values_and_exact_probe_length(self):
        payload = struct.pack("<4H", 0xFFFF, 0xFFFE, 0xFFFD, 0x0FFF)
        frame = protocol.parse_control(
            self.crypto,
            packet(self.crypto, 0x0104, protocol.FLAG_STREAM, 1, payload),
        )
        self.assertEqual(
            protocol.decode_probe_frame(self.crypto, frame),
            ["not_connected", "too_high", "too_low", "invalid"],
        )
        frame.payload += b"\x00"
        self.assertIsNone(protocol.decode_probe_frame(self.crypto, frame))

    def test_no_secondary_mask_guess_or_unknown_flags(self):
        frame = protocol.parse_control(self.crypto, probe_packet(self.crypto))
        frame.payload = struct.pack("<4H", 500, 500, 500, 500)
        self.assertIsNone(protocol.decode_probe_frame(self.crypto, frame))
        frame.payload = struct.pack("<4H", 25, 25, 25, 25)
        frame.flags = 0x01
        self.assertIsNone(protocol.decode_probe_frame(self.crypto, frame))

    def test_length_mismatch_is_rejected(self):
        frame = probe_packet(self.crypto)
        self.assertIsNone(protocol.parse_control(self.crypto, frame[:-1]))
        self.assertIsNone(protocol.parse_control(self.crypto, frame + b"\x00"))

    def test_all_frame_split_points_and_one_byte_notifications(self):
        frame = probe_packet(self.crypto)
        for split in range(1, len(frame)):
            buffer = protocol.FrameBuffer()
            self.assertEqual(buffer.feed(frame[:split]), [])
            self.assertEqual(buffer.feed(frame[split:]), [frame])
        buffer = protocol.FrameBuffer()
        output = []
        for byte in frame:
            output.extend(buffer.feed(bytes([byte])))
        self.assertEqual(output, [frame])

    def test_coalesced_frames_keep_partial_tail(self):
        frame = probe_packet(self.crypto)
        buffer = protocol.FrameBuffer()
        self.assertEqual(buffer.feed(frame + frame[:3]), [frame])
        self.assertEqual(buffer.feed(frame[3:] + frame), [frame, frame])

    def test_oversized_header_resets_buffer(self):
        buffer = protocol.FrameBuffer()
        with self.assertRaises(ValueError):
            buffer.feed(b"\x04\x01\xff\xff")
        frame = probe_packet(self.crypto)
        self.assertEqual(buffer.feed(frame), [frame])
