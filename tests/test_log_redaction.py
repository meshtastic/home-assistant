# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Logged protobufs leave out the fields the schema marks sensitive and admin payloads."""

from __future__ import annotations

from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.protobuf import (
    admin_pb2,
    channel_pb2,
    config_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
)


def logged(message: object) -> str:
    return str(ClientApiConnection._protobuf_log(message))


def test_the_config_exchange_logs_no_secrets() -> None:
    security = mesh_pb2.FromRadio(config=config_pb2.Config(security=config_pb2.Config.SecurityConfig()))
    security.config.security.private_key = b"private-key-bytes"
    security.config.security.public_key = b"public-key-bytes"
    network = mesh_pb2.FromRadio(config=config_pb2.Config(network=config_pb2.Config.NetworkConfig()))
    network.config.network.wifi_ssid = "home"
    network.config.network.wifi_psk = "wifi-password"
    mqtt = mesh_pb2.FromRadio(moduleConfig=module_config_pb2.ModuleConfig())
    mqtt.moduleConfig.mqtt.password = "mqtt-password"
    channel = mesh_pb2.FromRadio(channel=channel_pb2.Channel(index=0))
    channel.channel.settings.psk = b"channel-psk"
    channel.channel.settings.name = "LongFast"

    text = " ".join(logged(m) for m in (security, network, mqtt, channel))

    for secret in ("private-key-bytes", "wifi-password", "mqtt-password", "channel-psk"):
        assert secret not in text
    for kept in ("public-key-bytes", "home", "LongFast"):
        assert kept in text
    assert security.config.security.private_key == b"private-key-bytes", "the message itself is left whole"


def test_an_admin_packet_logs_without_its_payload() -> None:
    admin = admin_pb2.AdminMessage()
    admin.get_device_connection_status_response.wifi.status.ip_address = 1
    admin.get_device_connection_status_response.bluetooth.pin = 987654
    packet = mesh_pb2.FromRadio(packet=mesh_pb2.MeshPacket(id=5))
    packet.packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
    packet.packet.decoded.payload = admin.SerializeToString()

    text = logged(packet)

    assert "payload" not in text
    assert "ADMIN_APP" in text
