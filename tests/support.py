"""Load protocol/client code without requiring a running Home Assistant."""

import asyncio
import importlib.util
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_bmt01_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components" / "sonoff_bmt01")]
sys.modules[PACKAGE] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", Path(package.__path__[0]) / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


protocol = load_module("protocol")
load_module("const")


class FakeBleakError(Exception):
    pass


def load_client():
    """Stub only external imports; execute the actual production client."""
    stubs = {}
    for name in (
        "bleak", "bleak.exc", "bleak_retry_connector", "homeassistant",
        "homeassistant.components", "homeassistant.components.bluetooth",
        "homeassistant.config_entries", "homeassistant.core",
    ):
        stubs[name] = ModuleType(name)
    stubs["bleak.exc"].BleakError = FakeBleakError
    stubs["bleak_retry_connector"].BleakClientWithServiceCache = object
    stubs["bleak_retry_connector"].establish_connection = None
    stubs["homeassistant.components"].bluetooth = stubs[
        "homeassistant.components.bluetooth"
    ]
    stubs["homeassistant.config_entries"].ConfigEntry = object
    stubs["homeassistant.core"].CALLBACK_TYPE = object
    stubs["homeassistant.core"].HomeAssistant = object
    stubs["homeassistant.core"].callback = lambda fn: fn
    with patch.dict(sys.modules, stubs):
        return load_module("client")


client_module = load_client()


def packet(crypto, command, flags, tsn, payload=b""):
    body = crypto.xor(bytes([flags, tsn]) + payload)
    return struct.pack("<HH", command, len(body)) + body


def probe_packet(crypto, tsn=1, flags=protocol.FLAG_STREAM):
    return packet(
        crypto, protocol.CMD_PROBE_TEMPERATURE_RESPONSE, flags, tsn,
        struct.pack("<4H", 26, 0xFFFF, 0xFFFF, 32),
    )


class FakeLink:
    def __init__(self, mtu=247, properties=("write",)):
        self.is_connected = True
        self.mtu_size = mtu
        self.characteristic = SimpleNamespace(
            properties=properties, max_write_without_response_size=20
        )
        self.services = SimpleNamespace(
            get_characteristic=lambda uuid: self.characteristic
        )
        self.writes = []
        self.written = asyncio.Queue()
        self.notifications = {}
        self.on_write = None

    async def write_gatt_char(self, characteristic, data, response):
        assert characteristic is self.characteristic
        self.writes.append((bytes(data), response))
        self.written.put_nowait(bytes(data))
        if self.on_write:
            self.on_write(bytes(data))

    async def start_notify(self, uuid, callback):
        self.notifications[uuid] = callback

    async def disconnect(self):
        self.is_connected = False


def make_client():
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(loop=loop, async_create_task=asyncio.create_task)
    client = client_module.BMT01Client(hass, "AA:BB:CC:DD:EE:FF", "synthetic-test-key")
    client._client = FakeLink()
    client._authenticated = True
    client._synced = True
    return client
