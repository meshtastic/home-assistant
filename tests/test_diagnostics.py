# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Diagnostics: secrets are redacted, the radio section survives."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from homeassistant.components.diagnostics import REDACTED

from custom_components.meshtastic.const import CONF_CONNECTION_TCP_HOST, CONF_OPTION_FILTER_NODES
from custom_components.meshtastic.diagnostics import async_get_config_entry_diagnostics

from .conftest import GATEWAY_KEY, GATEWAY_NUM, HOST, PEER_KEY, PEER_NUM, FakeClient, make_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

MAC = "q83vASNF"
SECRETS = (HOST, GATEWAY_KEY, PEER_KEY, "c2VjcmV0", MAC, "mqtt.example", "home")


async def test_diagnostics_redact_secrets_and_keep_the_radio_section(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = make_entry(
        {
            CONF_OPTION_FILTER_NODES: [
                {"id": GATEWAY_NUM, "name": "Gateway", "public_key": GATEWAY_KEY},
                {"id": PEER_NUM, "name": "Peer", "public_key": PEER_KEY},
            ]
        }
    )
    entry.add_to_hass(hass)
    fake_client.nodes[GATEWAY_NUM]["user"]["macaddr"] = MAC
    fake_client.nodes[PEER_NUM]["position"] = {"latitudeI": 473769000, "longitudeI": 85417000, "altitude": 408}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    rendered = json.dumps(diagnostics)
    for secret in (*SECRETS, "473769000", "85417000", "408"):
        assert secret not in rendered

    assert diagnostics["config_entry"]["data"][CONF_CONNECTION_TCP_HOST] == REDACTED
    assert diagnostics["config_entry"]["title"] == REDACTED
    assert all(node["public_key"] == REDACTED for node in diagnostics["config_entry"]["options"]["nodes"])

    radio = diagnostics["radio"]
    assert set(radio) == {"my_info", "metadata", "node", "local_config", "module_config", "channels"}
    assert radio["metadata"] == {"firmwareVersion": "2.8.0"}
    assert radio["node"]["user"]["longName"] == "Gateway"
    assert radio["node"]["user"]["publicKey"] == REDACTED
    assert radio["node"]["user"]["macaddr"] == REDACTED
    assert radio["local_config"]["security"] == {"privateKey": REDACTED, "adminKey": REDACTED}
    assert radio["module_config"]["mqtt"]["address"] == REDACTED
    assert radio["channels"] == [{"index": 0, "role": "PRIMARY", "name": "LongFast"}]

    peer = diagnostics["coordinator"]["data"][str(PEER_NUM)]
    assert peer["user"]["longName"] == "Peer"
    assert peer["position"] == {"latitudeI": REDACTED, "longitudeI": REDACTED, "altitude": REDACTED}
    assert diagnostics["node_db"] == {"total_nodes": 2, "selected_nodes": [GATEWAY_NUM, PEER_NUM]}


async def test_diagnostics_without_a_loaded_entry_have_no_radio_section(hass: HomeAssistant) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "radio" not in diagnostics
    assert diagnostics["config_entry"]["data"][CONF_CONNECTION_TCP_HOST] == REDACTED
    assert HOST not in json.dumps(diagnostics)
