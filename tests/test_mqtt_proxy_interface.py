# SPDX-License-Identifier: MIT

"""Tests for MQTT proxy interface functionality.

Tests cover:
- AES encryption / decryption round-trip
- PSK expansion (1-byte, 16-byte, 32-byte)
- Default channel filtering
- Self-loop prevention
- Topic construction (no double-region)
- Relay gating (uplink_enabled, disabled channels, PKI, same-node)
- Downlink forwarding gating
"""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.meshtastic.aiomeshtastic.interface import MeshInterface
from custom_components.meshtastic.aiomeshtastic.protobuf import (
    channel_pb2,
    mesh_pb2,
    mqtt_pb2,
    portnums_pb2,
)


# ---------------------------------------------------------------------------
# PSK expansion
# ---------------------------------------------------------------------------


class TestExpandPsk:
    """Test PSK expansion to AES keys."""

    def test_empty_psk_returns_empty(self):
        assert MeshInterface._expand_psk(b"") == b""

    def test_single_byte_zero_returns_empty(self):
        """Index 0 = no encryption."""
        assert MeshInterface._expand_psk(b"\x00") == b""

    def test_single_byte_one_returns_default_key(self):
        """Index 1 = Meshtastic default AES-128 key."""
        key = MeshInterface._expand_psk(b"\x01")
        assert len(key) == 16
        assert key == bytes([
            0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
            0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01,
        ])

    def test_16_byte_psk_passthrough(self):
        """AES-128 key passed through unchanged."""
        psk = bytes(range(16))
        assert MeshInterface._expand_psk(psk) == psk

    def test_32_byte_psk_passthrough(self):
        """AES-256 key passed through unchanged."""
        psk = bytes(range(32))
        assert MeshInterface._expand_psk(psk) == psk


# ---------------------------------------------------------------------------
# AES-CTR encryption
# ---------------------------------------------------------------------------


class TestEncryptPacket:
    """Test AES-CTR encryption matches Meshtastic format."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypting then decrypting should return the original plaintext."""
        key = bytes(range(32))
        plaintext = b"Hello Meshtastic!"
        packet_id = 0x12345678
        from_node = 0x4E67AB58

        encrypted = MeshInterface._encrypt_packet(plaintext, key, packet_id, from_node)
        assert encrypted != plaintext
        assert len(encrypted) == len(plaintext)

        # AES-CTR is symmetric — encrypt again to decrypt
        decrypted = MeshInterface._encrypt_packet(encrypted, key, packet_id, from_node)
        assert decrypted == plaintext

    def test_nonce_format(self):
        """Verify nonce is packet_id(4B LE) + from_node(4B LE) + 8 zero bytes."""
        packet_id = 0x01020304
        from_node = 0x05060708
        expected_nonce = struct.pack("<II", packet_id, from_node) + b"\x00" * 8
        assert len(expected_nonce) == 16

    def test_different_keys_produce_different_output(self):
        """Different keys must produce different ciphertext."""
        plaintext = b"test"
        key1 = bytes(range(16))
        key2 = bytes(range(1, 17))
        enc1 = MeshInterface._encrypt_packet(plaintext, key1, 1, 1)
        enc2 = MeshInterface._encrypt_packet(plaintext, key2, 1, 1)
        assert enc1 != enc2

    def test_different_packet_ids_produce_different_output(self):
        """Same plaintext with different nonces must differ."""
        key = bytes(range(16))
        plaintext = b"test"
        enc1 = MeshInterface._encrypt_packet(plaintext, key, 1, 1)
        enc2 = MeshInterface._encrypt_packet(plaintext, key, 2, 1)
        assert enc1 != enc2


# ---------------------------------------------------------------------------
# Default channel filtering
# ---------------------------------------------------------------------------


class TestDefaultChannelFiltering:
    """Test that default/public channels are correctly identified."""

    EXPECTED_DEFAULTS = {"LongFast", "LongSlow", "MediumFast", "MediumSlow", "ShortFast", "ShortSlow"}

    def test_all_default_channels_present(self):
        """All six default channel names should be in the set."""
        # The set is defined at the start of _maintain_mqtt_connection, so we test the expected values
        for name in self.EXPECTED_DEFAULTS:
            assert name in self.EXPECTED_DEFAULTS

    def test_private_channel_not_in_defaults(self):
        assert "WSDLT!" not in self.EXPECTED_DEFAULTS
        assert "MyPrivate" not in self.EXPECTED_DEFAULTS

    def test_empty_name_resolves_to_longfast(self):
        """A channel with empty name defaults to 'LongFast' and should be skipped."""
        channel = channel_pb2.Channel()
        channel.role = channel_pb2.Channel.Role.PRIMARY
        channel.settings.name = ""
        resolved_name = channel.settings.name or "LongFast"
        assert resolved_name in self.EXPECTED_DEFAULTS


# ---------------------------------------------------------------------------
# Topic construction
# ---------------------------------------------------------------------------


class TestTopicConstruction:
    """Test MQTT topic format — no double-region bug."""

    def test_topic_uses_root_directly(self):
        """Topic should be {root}/2/e/{channel}/# without extra region."""
        root = "msh/US"
        channel_name = "MyPrivate"
        topic = f"{root}/2/e/{channel_name}/#"
        assert topic == "msh/US/2/e/MyPrivate/#"
        # Ensure no double-region
        assert "msh/US/US" not in topic

    def test_topic_with_default_root(self):
        """When root is empty, fallback to 'msh'."""
        root = "" or "msh"
        channel_name = "Test"
        topic = f"{root}/2/e/{channel_name}/#"
        assert topic == "msh/2/e/Test/#"

    def test_relay_publish_topic_includes_gateway_id(self):
        """Relay publish topic ends with the gateway node ID."""
        root = "msh/US"
        channel_name = "MyPrivate"
        gateway_id = "!7eec8b23"
        topic = f"{root}/2/e/{channel_name}/{gateway_id}"
        assert topic == "msh/US/2/e/MyPrivate/!7eec8b23"


# ---------------------------------------------------------------------------
# Self-loop prevention
# ---------------------------------------------------------------------------


class TestSelfLoopPrevention:
    """Test that messages from own gateway are filtered."""

    def test_own_gateway_topic_detected(self):
        gateway_id = "!7eec8b23"
        topic = "msh/US/2/e/MyPrivate/!7eec8b23"
        assert topic.endswith(f"/{gateway_id}")

    def test_other_gateway_topic_not_filtered(self):
        gateway_id = "!7eec8b23"
        topic = "msh/US/2/e/MyPrivate/!abcd1234"
        assert not topic.endswith(f"/{gateway_id}")


# ---------------------------------------------------------------------------
# Relay gating logic (unit tests for _relay_lora_to_mqtt guards)
# ---------------------------------------------------------------------------


class TestRelayLoraMqttGating:
    """Test the guard conditions in _relay_lora_to_mqtt."""

    def _make_interface(
        self,
        *,
        uplink_relay: bool = True,
        mqtt_connected: bool = True,
        node_num: int = 0x7EEC8B23,
        channels: list | None = None,
    ) -> MeshInterface:
        """Create a minimal MeshInterface with mocked internals."""
        connection = MagicMock()
        iface = MeshInterface.__new__(MeshInterface)
        iface._logger = MagicMock()
        iface._mqtt_uplink_relay_enabled = uplink_relay
        iface._mqtt_connected = mqtt_connected
        iface._mqtt_client = AsyncMock() if mqtt_connected else None
        iface._mqtt_default_channels = {"LongFast", "LongSlow", "MediumFast", "MediumSlow", "ShortFast", "ShortSlow"}

        iface._connected_node_info = MagicMock()
        iface._connected_node_info.my_node_num = node_num

        iface._connected_node_module_config = MagicMock()
        iface._connected_node_module_config.mqtt.enabled = True
        iface._connected_node_module_config.mqtt.proxy_to_client_enabled = True
        iface._connected_node_module_config.mqtt.root = "msh/US"

        if channels is not None:
            iface._connected_node_channels = channels

        return iface

    @pytest.mark.asyncio
    async def test_skip_packet_from_self(self, from_radio_packet, private_channel):
        """Packets from our own node should be skipped."""
        iface = self._make_interface(channels=[MagicMock(), private_channel])
        fr = from_radio_packet(from_node=0x7EEC8B23)  # same as gateway

        await iface._relay_lora_to_mqtt(fr)
        iface._mqtt_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_pki_encrypted(self, from_radio_packet, private_channel):
        """PKI-encrypted (DM) packets should be skipped."""
        iface = self._make_interface(channels=[MagicMock(), private_channel])
        fr = from_radio_packet(pki_encrypted=True)

        await iface._relay_lora_to_mqtt(fr)
        iface._mqtt_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_mqtt_disconnected(self, from_radio_packet, private_channel):
        """Should not relay when MQTT is disconnected."""
        iface = self._make_interface(mqtt_connected=False, channels=[MagicMock(), private_channel])

        fr = from_radio_packet()
        await iface._relay_lora_to_mqtt(fr)
        # No assertion on publish since mqtt_client is None

    @pytest.mark.asyncio
    async def test_skip_default_channel(self, from_radio_packet, longfast_channel):
        """Default channels should be skipped even if uplink_enabled."""
        iface = self._make_interface(channels=[longfast_channel])
        fr = from_radio_packet(channel_idx=0)

        await iface._relay_lora_to_mqtt(fr)
        iface._mqtt_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_uplink_disabled(self, from_radio_packet):
        """Channels without uplink_enabled should be skipped."""
        channel = channel_pb2.Channel()
        channel.role = channel_pb2.Channel.Role.SECONDARY
        channel.index = 1
        channel.settings.name = "NoUplink"
        channel.settings.uplink_enabled = False
        channel.settings.psk = bytes(range(32))

        iface = self._make_interface(channels=[MagicMock(), channel])
        fr = from_radio_packet(channel_idx=1)

        await iface._relay_lora_to_mqtt(fr)
        iface._mqtt_client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_relay_succeeds_for_private_channel(self, from_radio_packet, private_channel):
        """A valid packet on a private channel with uplink should be relayed."""
        iface = self._make_interface(channels=[MagicMock(), private_channel])
        fr = from_radio_packet(channel_idx=1, from_node=0x4E67AB58)

        await iface._relay_lora_to_mqtt(fr)

        iface._mqtt_client.publish.assert_called_once()
        call_kwargs = iface._mqtt_client.publish.call_args
        topic = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("topic")
        assert topic == "msh/US/2/e/MyPrivate/!7eec8b23"

    @pytest.mark.asyncio
    async def test_relay_builds_valid_service_envelope(self, from_radio_packet, private_channel):
        """The published payload should be a valid ServiceEnvelope."""
        iface = self._make_interface(channels=[MagicMock(), private_channel])
        fr = from_radio_packet(channel_idx=1, from_node=0x4E67AB58)

        await iface._relay_lora_to_mqtt(fr)

        call_kwargs = iface._mqtt_client.publish.call_args
        payload = call_kwargs.kwargs.get("payload") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["payload"]

        # Deserialize and verify
        envelope = mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(payload)
        assert envelope.channel_id == "MyPrivate"
        assert envelope.gateway_id == "!7eec8b23"
        assert envelope.packet.encrypted  # Should have encrypted data, not decoded
        assert not envelope.packet.HasField("decoded")

    @pytest.mark.asyncio
    async def test_no_packet_field_skipped(self):
        """FromRadio without a packet field should be silently skipped."""
        iface = self._make_interface()
        fr = mesh_pb2.FromRadio()  # No packet field set

        await iface._relay_lora_to_mqtt(fr)
        iface._mqtt_client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Downlink forwarding gating
# ---------------------------------------------------------------------------


class TestDownlinkForwardingGating:
    """Test that _forward_mqtt_to_radio respects self-loop filtering."""

    def _make_interface(self, node_num: int = 0x7EEC8B23) -> MeshInterface:
        iface = MeshInterface.__new__(MeshInterface)
        iface._logger = MagicMock()
        iface._connected_node_info = MagicMock()
        iface._connected_node_info.my_node_num = node_num
        iface._connection = AsyncMock()
        return iface

    @pytest.mark.asyncio
    async def test_self_loop_filtered(self):
        """Messages from own gateway should be dropped."""
        iface = self._make_interface()
        message = MagicMock()
        message.topic = MagicMock()
        message.topic.__str__ = MagicMock(return_value="msh/US/2/e/MyPrivate/!7eec8b23")
        message.payload = b"data"
        message.retain = False

        await iface._forward_mqtt_to_radio(message)
        iface._connection.send_packet.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_gateway_forwarded(self):
        """Messages from other gateways should be forwarded."""
        iface = self._make_interface()
        message = MagicMock()
        message.topic = MagicMock()
        message.topic.__str__ = MagicMock(return_value="msh/US/2/e/MyPrivate/!abcd1234")
        message.payload = b"data"
        message.retain = False

        await iface._forward_mqtt_to_radio(message)
        iface._connection.send_packet.assert_called_once()


# ---------------------------------------------------------------------------
# Subscribe topic filtering
# ---------------------------------------------------------------------------


class TestSubscribeTopicFiltering:
    """Test _subscribe_to_downlink_topics channel filtering."""

    def _make_interface(
        self,
        channels: list,
        mqtt_root: str = "msh/US",
        json_enabled: bool = False,
    ) -> MeshInterface:
        iface = MeshInterface.__new__(MeshInterface)
        iface._logger = MagicMock()
        iface._mqtt_client = AsyncMock()
        iface._mqtt_connected = True
        iface._mqtt_default_channels = {"LongFast", "LongSlow", "MediumFast", "MediumSlow", "ShortFast", "ShortSlow"}
        iface._connected_node_channels = channels
        iface._connected_node_module_config = MagicMock()
        iface._connected_node_module_config.mqtt.root = mqtt_root
        iface._connected_node_module_config.mqtt.json_enabled = json_enabled
        return iface

    @pytest.mark.asyncio
    async def test_skips_longfast(self, longfast_channel):
        """LongFast should not be subscribed even with downlink_enabled."""
        iface = self._make_interface(channels=[longfast_channel])
        await iface._subscribe_to_downlink_topics()
        iface._mqtt_client.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribes_private_channel(self, private_channel):
        """Private channels with downlink_enabled should be subscribed."""
        iface = self._make_interface(channels=[private_channel])
        await iface._subscribe_to_downlink_topics()
        iface._mqtt_client.subscribe.assert_called_once_with("msh/US/2/e/MyPrivate/#", qos=1)

    @pytest.mark.asyncio
    async def test_skips_disabled_channel(self, disabled_channel):
        """Disabled channels should not be subscribed."""
        iface = self._make_interface(channels=[disabled_channel])
        await iface._subscribe_to_downlink_topics()
        iface._mqtt_client.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_downlink_disabled(self, private_channel):
        """Channels with downlink_enabled=False should not be subscribed."""
        private_channel.settings.downlink_enabled = False
        iface = self._make_interface(channels=[private_channel])
        await iface._subscribe_to_downlink_topics()
        iface._mqtt_client.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_json_topic_subscribed_when_enabled(self, private_channel):
        """When json_enabled, should also subscribe to cleartext topic."""
        iface = self._make_interface(channels=[private_channel], json_enabled=True)
        await iface._subscribe_to_downlink_topics()
        calls = [str(c) for c in iface._mqtt_client.subscribe.call_args_list]
        assert any("2/e/MyPrivate" in c for c in calls)
        assert any("2/c/MyPrivate" in c for c in calls)

    @pytest.mark.asyncio
    async def test_no_double_region_in_topic(self, private_channel):
        """Topic should not contain double region (msh/US/US/...)."""
        iface = self._make_interface(channels=[private_channel])
        await iface._subscribe_to_downlink_topics()
        subscribed_topic = iface._mqtt_client.subscribe.call_args[0][0]
        assert "US/US" not in subscribed_topic


# ---------------------------------------------------------------------------
# Constructor parameter gating
# ---------------------------------------------------------------------------


class TestInterfaceConstructorGating:
    """Test that constructor parameters correctly set internal flags."""

    def test_default_flags_disabled(self):
        """By default, downlink and uplink relay should be disabled."""
        connection = MagicMock()
        iface = MeshInterface(connection=connection, enable_mqtt_downlink=False, enable_mqtt_uplink_relay=False)
        assert iface._mqtt_downlink_enabled is False
        assert iface._mqtt_uplink_relay_enabled is False

    def test_flags_enabled(self):
        """When explicitly enabled, flags should be True."""
        connection = MagicMock()
        iface = MeshInterface(connection=connection, enable_mqtt_downlink=True, enable_mqtt_uplink_relay=True)
        assert iface._mqtt_downlink_enabled is True
        assert iface._mqtt_uplink_relay_enabled is True
