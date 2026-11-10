# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshtastic.chat import DATA_CHAT
from custom_components.meshtastic.const import CHAT_SENT_BY_MAX_LENGTH, DOMAIN
from custom_components.meshtastic.frontend import CHAT_PANEL_URL_PATH
from custom_components.meshtastic.ha_frontend import locate_dir

from .test_chat import ANNA, BROADCAST, GATEWAY, FakeClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class FakeApiClient(FakeClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__()
        self.metadata = {}

    async def connect(self) -> None:
        return

    async def disconnect(self) -> None:
        # yield like a real connection does
        await asyncio.sleep(0)

    def get_own_node(self) -> dict[str, Any]:
        return {"num": GATEWAY, "user": {"id": "!00004904", "longName": "Home Gateway", "shortName": "4904"}}

    async def async_get_own_node(self) -> dict[str, Any]:
        return self.get_own_node()

    async def async_get_node_local_config(self) -> dict[str, Any]:
        return {}

    async def async_get_node_module_config(self) -> dict[str, Any]:
        return {}


def _entry(options: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home Gateway",
        version=1,
        minor_version=2,
        data={"connection_type": "tcp", "tcp_host": "gateway.local", "tcp_port": 4403},
        options={
            "nodes": [],
            "notify_platform": {"channels": False, "nodes": "none"},
            "web_client": {"enable": False},
            "tcp_proxy": {"enable": False},
            **options,
        },
    )


@pytest.fixture
def fake_api():
    with (
        patch("custom_components.meshtastic.MeshtasticApiClient", FakeApiClient),
        patch("custom_components.meshtastic.fetch_meshtastic_hardware_names", AsyncMock(return_value={})),
        patch("custom_components.meshtastic.frontend._async_register_static_path", AsyncMock()),
    ):
        yield


@pytest.mark.usefixtures("fake_api")
async def test_chat_panel_follows_options(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True, "require_admin": False}})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert entry.entry_id in hass.data[DATA_CHAT]
    panel = hass.data["frontend_panels"][CHAT_PANEL_URL_PATH]
    assert panel.require_admin is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DATA_CHAT, {})
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})


@pytest.mark.usefixtures("fake_api")
async def test_chat_panel_admin_only_while_any_gateway_requires_it(hass: HomeAssistant) -> None:
    open_entry = _entry({"chat": {"enable": True, "require_admin": False}})
    admin_entry = _entry({"chat": {"enable": True, "require_admin": True}})
    open_entry.add_to_hass(hass)
    admin_entry.add_to_hass(hass)

    # setting up the domain sets up both gateways
    assert await hass.config_entries.async_setup(open_entry.entry_id)
    await hass.async_block_till_done()
    assert set(hass.data[DATA_CHAT]) == {open_entry.entry_id, admin_entry.entry_id}
    # the panel is visible to non admins as long as one gateway allows it; the API filters per gateway
    assert hass.data["frontend_panels"][CHAT_PANEL_URL_PATH].require_admin is False

    assert await hass.config_entries.async_unload(open_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data["frontend_panels"][CHAT_PANEL_URL_PATH].require_admin is True

    assert await hass.config_entries.async_unload(admin_entry.entry_id)
    await hass.async_block_till_done()
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})


@pytest.mark.usefixtures("fake_api")
async def test_chat_disabled_by_default(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.data.get(DATA_CHAT)
    assert CHAT_PANEL_URL_PATH not in hass.data.get("frontend_panels", {})

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_options_flow_enables_chat(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert "chat" in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "nodes": [],
            "notify_platform": {"channels": False, "nodes": "none"},
            "web_client": {"enable": False},
            "tcp_proxy": {"enable": False, "port": 4403},
            "chat": {"enable": True, "require_admin": True, "prefix_sender": True, "history_size": 50},
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options["chat"] == {
        "enable": True,
        "require_admin": True,
        "prefix_sender": True,
        "history_size": 50,
        "channel_management": "off",
    }
    assert entry.entry_id in hass.data[DATA_CHAT]
    assert CHAT_PANEL_URL_PATH in hass.data["frontend_panels"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_send_text_action_is_kept_in_chat_history(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client
    chat = hass.data[DATA_CHAT][entry.entry_id]

    await hass.services.async_call(
        DOMAIN, "send_text", {"text": "hello", "channel": "1", "ack": True, "sent_by": "Telegram: Alice"}, blocking=True
    )
    await hass.async_block_till_done()
    message = chat.history("channel:1")[-1]
    assert message["direction"] == "out"
    assert message["text"] == "hello"
    assert message["sent_by"] == "Telegram: Alice"
    assert client.sent[-1] == {"text": "hello", "to": BROADCAST, "channel": 1, "timeout": 60}

    await hass.services.async_call(DOMAIN, "send_text", {"text": "direct", "to": str(ANNA), "ack": True}, blocking=True)
    await hass.async_block_till_done()
    assert chat.history(f"node:{ANNA}")[-1]["sent_by"] is None
    assert client.sent[-1]["to"] == ANNA
    assert client.sent[-1]["channel"] is None

    # a direct message on an explicit channel keeps that channel
    await hass.services.async_call(
        DOMAIN, "send_text", {"text": "direct on 2", "to": str(ANNA), "channel": "2", "ack": True}, blocking=True
    )
    await hass.async_block_till_done()
    assert client.sent[-1] == {"text": "direct on 2", "to": ANNA, "channel": 2, "timeout": 60}
    assert chat.history(f"node:{ANNA}")[-1]["text"] == "direct on 2"

    # without a channel the primary channel is used
    await hass.services.async_call(DOMAIN, "send_text", {"text": "primary", "ack": True}, blocking=True)
    await hass.async_block_till_done()
    assert chat.history("channel:0")[-1]["text"] == "primary"

    # without ack (the default of the action) the message is sent once, without delivery tracking
    await hass.services.async_call(
        DOMAIN, "send_text", {"text": "fire and forget", "to": str(ANNA), "channel": "2", "ack": False}, blocking=True
    )
    await hass.async_block_till_done()
    assert client.sent[-1] == {"text": "fire and forget", "to": ANNA, "channel": 2, "ack": False}
    assert client.sent_ids[-1][0] == chat.history(f"node:{ANNA}")[-1]["packet_id"]
    assert chat.history(f"node:{ANNA}")[-1]["status"] == "sent"
    await hass.services.async_call(DOMAIN, "send_text", {"text": "default"}, blocking=True)
    await hass.async_block_till_done()
    assert client.sent[-1]["ack"] is False
    assert chat.history("channel:0")[-1]["status"] == "sent"

    with pytest.raises(ServiceValidationError, match="longer than"):
        await hass.services.async_call(DOMAIN, "send_text", {"text": "x" * 201}, blocking=True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_send_text_action_without_chat(hass: HomeAssistant) -> None:
    entry = _entry({})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client

    await hass.services.async_call(DOMAIN, "send_text", {"text": "hello", "channel": "1", "ack": True}, blocking=True)
    assert client.sent[-1] == {"text": "hello", "to": "^all", "channel": 1, "ack": True}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_options_form_renders_through_http_api(hass: HomeAssistant, hass_client) -> None:
    """Fetch the options form the way the frontend does: it rejects some selectors that unit tests accept."""
    assert await async_setup_component(hass, "config", {})
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client()
    response = await client.post("/api/config/config_entries/options/flow", json={"handler": entry.entry_id})
    assert response.status == 200
    form = await response.json()
    assert form["type"] == "form"
    chat_section = next(field for field in form["data_schema"] if field["name"] == "chat")
    management = next(field for field in chat_section["schema"] if field["name"] == "channel_management")
    assert management["selector"]["select"]["options"] == ["off", "admin", "all"]
    assert management["default"] == "off"

    # the chosen level ends up in the options and the chat picks it up
    response = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}",
        json={
            "nodes": [],
            "notify_platform": {"channels": False, "nodes": "none"},
            "web_client": {"enable": False},
            "tcp_proxy": {"enable": False, "port": 4403},
            "chat": {
                "enable": True,
                "require_admin": True,
                "prefix_sender": False,
                "history_size": 200,
                "channel_management": "admin",
            },
        },
    )
    assert response.status == 200
    assert (await response.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["chat"]["channel_management"] == "admin"
    assert hass.data[DATA_CHAT][entry.entry_id].channel_management == "admin"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fake_api")
async def test_send_text_action_limits_the_sent_by_label(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "send_text", {"text": "hello", "sent_by": "x" * (CHAT_SENT_BY_MAX_LENGTH + 1)}, blocking=True
        )


def test_panel_ships_the_style_sheets_it_loads() -> None:
    frontend = pathlib.Path(locate_dir())
    script = (frontend / "chat-panel.js").read_text(encoding="utf-8")
    style_sheets = re.findall(r'loadStyleSheet\("([^"]+)"\)', script)
    assert style_sheets
    for name in style_sheets:
        assert (frontend / name).is_file(), name
    # styles live in the style sheets: the script only sets the values it computes (textarea height, tooltip place)
    assert "cssText" not in script
    assert "<style" not in script
    assert re.findall(r"\.style\.(\w+)", script) == ["height", "height", "left", "top"]
    # Home Assistant's frontend patches the form collections: iterating form.elements throws there
    assert ".elements" not in script


@pytest.mark.usefixtures("fake_api")
async def test_options_form_tolerates_nodes_without_a_name(hass: HomeAssistant, hass_client) -> None:
    """A real mesh has nodes the gateway only knows from their position or telemetry, without a user record."""
    assert await async_setup_component(hass, "config", {})
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    original = entry.runtime_data.client.async_get_all_nodes

    async def with_incomplete_nodes() -> dict[int, dict[str, Any]]:
        nodes = await original()
        nodes[0x22222222] = {"num": 0x22222222, "position": {"latitude": 47.0, "longitude": 8.0}}
        nodes[0x33333333] = {"num": 0x33333333, "user": {"id": "!33333333", "shortName": "3333"}}
        return nodes

    entry.runtime_data.client.async_get_all_nodes = with_incomplete_nodes

    client = await hass_client()
    response = await client.post("/api/config/config_entries/options/flow", json={"handler": entry.entry_id})
    assert response.status == 200
    form = await response.json()
    node_field = next(field for field in form["data_schema"] if field["name"] == "node")
    labels = {option["value"]: option["label"] for option in node_field["selector"]["select"]["options"]}
    assert labels[str(0x33333333)] == "!33333333 (!33333333)"  # no long name: shown by its id
    assert str(0x22222222) not in labels  # no user record at all: cannot become a device yet


def _tracker_unique_ids(hass: HomeAssistant) -> set[str]:
    entities = er.async_get(hass).entities.values()
    return {e.unique_id for e in entities if e.platform == DOMAIN and e.domain == "device_tracker"}


@pytest.mark.usefixtures("fake_api")
async def test_selected_node_gets_a_tracker_and_a_device(hass: HomeAssistant) -> None:
    entry = _entry({"chat": {"enable": True}, "nodes": [{"id": ANNA, "name": "Anna Hiking"}]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert f"{entry.entry_id}_device_tracker_{ANNA}_node_position" in _tracker_unique_ids(hass)
    details = await hass.data[DATA_CHAT][entry.entry_id].async_node_details(ANNA)
    assert details["device_id"] is not None


@pytest.mark.usefixtures("fake_api")
async def test_node_added_in_the_options_gets_its_tracker(hass: HomeAssistant) -> None:
    """Selecting a node under Configure creates its device and tracker right away, without a restart."""
    entry = _entry({"chat": {"enable": True}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{entry.entry_id}_device_tracker_{ANNA}_node_position" not in _tracker_unique_ids(hass)

    hass.config_entries.async_update_entry(entry, options={**entry.options, "nodes": [{"id": ANNA, "name": "Anna"}]})
    await hass.async_block_till_done()

    assert f"{entry.entry_id}_device_tracker_{ANNA}_node_position" in _tracker_unique_ids(hass)
    details = await hass.data[DATA_CHAT][entry.entry_id].async_node_details(ANNA)
    assert details["device_id"] is not None
