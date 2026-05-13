# SPDX-License-Identifier: MIT

"""Shared fixtures for Meshtastic integration tests."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Direct-import helpers
# ---------------------------------------------------------------------------
# The custom_components.meshtastic package depends on ``homeassistant`` at
# import time.  Rather than mocking the entire HA runtime we load only the
# specific sub-modules our tests need using importlib so the top-level
# ``__init__.py`` is never executed.
# ---------------------------------------------------------------------------

_COMPONENT_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "meshtastic"
_AIO_ROOT = _COMPONENT_ROOT / "aiomeshtastic"
_PROTO_ROOT = _AIO_ROOT / "protobuf"


def _import_module_from_path(module_name: str, file_path: Path):
    """Import a single Python file as *module_name*."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ensure_package(package_name: str, package_dir: Path):
    """Register a stub package in sys.modules (no __init__.py execution)."""
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(package_dir)]
        pkg.__package__ = package_name
        sys.modules[package_name] = pkg


def _load_package_init(package_name: str, package_dir: Path):
    """Execute a package's __init__.py into the already-registered package module."""
    _ensure_package(package_name, package_dir)
    init_file = package_dir / "__init__.py"
    if init_file.exists():
        spec = importlib.util.spec_from_file_location(package_name, init_file,
                                                       submodule_search_locations=[str(package_dir)])
        spec.loader.exec_module(sys.modules[package_name])


# --- Register stub packages so protobuf imports resolve ---
_ensure_package("custom_components", _COMPONENT_ROOT.parent)
_ensure_package("custom_components.meshtastic", _COMPONENT_ROOT)
_ensure_package("custom_components.meshtastic.aiomeshtastic", _AIO_ROOT)
_ensure_package("custom_components.meshtastic.aiomeshtastic.protobuf", _PROTO_ROOT)

# --- Import only the protobuf modules (pure protobuf, no HA dependencies) ---
# Import every *_pb2.py to ensure cross-references resolve (e.g. mesh_pb2 -> portnums_pb2)
for pb2_file in sorted(_PROTO_ROOT.glob("*_pb2.py")):
    _import_module_from_path(
        f"custom_components.meshtastic.aiomeshtastic.protobuf.{pb2_file.stem}",
        pb2_file,
    )

# Also register the protobuf package __init__ for its __version__
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.protobuf.__init__",
    _PROTO_ROOT / "__init__.py",
)

# Convenience aliases used by fixtures
from custom_components.meshtastic.aiomeshtastic.protobuf import (  # noqa: E402
    channel_pb2,
    localonly_pb2,
    mesh_pb2,
    mqtt_pb2,
    portnums_pb2,
)

# --- Mock homeassistant.util.ssl so we can import interface.py ---
for mod_name in (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.ssl",
    "homeassistant.util.event_type",
    "homeassistant.util.hass_dict",
):
    sys.modules.setdefault(mod_name, MagicMock())

# --- Import const.py directly (only needs homeassistant.util.event_type) ---
const_mod = _import_module_from_path(
    "custom_components.meshtastic.const",
    _COMPONENT_ROOT / "const.py",
)

# --- Import aiomeshtastic sub-modules needed by interface.py ---
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.const",
    _AIO_ROOT / "const.py",
)
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.errors",
    _AIO_ROOT / "errors.py",
)
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.packet",
    _AIO_ROOT / "packet.py",
)

# connection package + its sub-modules
_conn_root = _AIO_ROOT / "connection"
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.connection.errors",
    _conn_root / "errors.py",
)
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.connection.listener",
    _conn_root / "listener.py",
)
_load_package_init("custom_components.meshtastic.aiomeshtastic.connection", _conn_root)
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.connection.streaming",
    _conn_root / "streaming.py",
)

# --- Import the MeshInterface itself ---
_import_module_from_path(
    "custom_components.meshtastic.aiomeshtastic.interface",
    _AIO_ROOT / "interface.py",
)

from custom_components.meshtastic.aiomeshtastic.interface import MeshInterface  # noqa: E402


@pytest.fixture
def mock_mqtt_client():
    """Create a mock aiomqtt client."""
    client = AsyncMock()
    client.subscribe = AsyncMock()
    client.publish = AsyncMock()
    return client


@pytest.fixture
def mqtt_module_config():
    """Create a module config with MQTT enabled."""
    module_config = localonly_pb2.LocalModuleConfig()
    module_config.mqtt.enabled = True
    module_config.mqtt.proxy_to_client_enabled = True
    module_config.mqtt.root = "msh/US"
    module_config.mqtt.json_enabled = False
    return module_config


@pytest.fixture
def private_channel():
    """Create a private channel with uplink/downlink enabled."""
    channel = channel_pb2.Channel()
    channel.role = channel_pb2.Channel.Role.SECONDARY
    channel.index = 1
    channel.settings.name = "MyPrivate"
    channel.settings.downlink_enabled = True
    channel.settings.uplink_enabled = True
    # 32-byte PSK (AES-256)
    channel.settings.psk = bytes(range(32))
    return channel


@pytest.fixture
def longfast_channel():
    """Create a default LongFast channel."""
    channel = channel_pb2.Channel()
    channel.role = channel_pb2.Channel.Role.PRIMARY
    channel.index = 0
    channel.settings.name = "LongFast"
    channel.settings.downlink_enabled = True
    channel.settings.uplink_enabled = True
    channel.settings.psk = b"\x01"  # Default key index 1
    return channel


@pytest.fixture
def disabled_channel():
    """Create a disabled channel."""
    channel = channel_pb2.Channel()
    channel.role = channel_pb2.Channel.Role.DISABLED
    channel.index = 2
    channel.settings.name = "Unused"
    return channel


@pytest.fixture
def node_info():
    """Create mock node info for the gateway."""
    info = mesh_pb2.MyNodeInfo()
    info.my_node_num = 0x7EEC8B23  # 2129431331
    return info


@pytest.fixture
def from_radio_packet():
    """Create a FromRadio with a decoded MeshPacket from another node."""

    def _make(
        from_node: int = 0x4E67AB58,
        to_node: int = 0xFFFFFFFF,
        channel_idx: int = 1,
        portnum: int = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        payload: bytes = b"Hello",
        packet_id: int = 12345,
        pki_encrypted: bool = False,
    ) -> mesh_pb2.FromRadio:
        from_radio = mesh_pb2.FromRadio()
        mp = from_radio.packet
        mp.id = packet_id
        setattr(mp, "from", from_node)
        mp.to = to_node
        mp.channel = channel_idx
        mp.decoded.portnum = portnum
        mp.decoded.payload = payload
        mp.pki_encrypted = pki_encrypted
        return from_radio

    return _make
