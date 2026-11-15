# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""A web client session belongs to one token, and does not outlive its use."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from custom_components.meshtastic.meshtastic_web import MeshtasticWebApiContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.components.http import HomeAssistantRequest
    from homeassistant.core import HomeAssistant

ENTRY = "entry"
PROXY = "10.0.0.1"


def request(token: str, remote: str = PROXY) -> HomeAssistantRequest:
    fake = SimpleNamespace(match_info={"config_entry_id": ENTRY, "token": token}, remote=remote)
    return cast("HomeAssistantRequest", fake)


@pytest.fixture
async def context(hass: HomeAssistant) -> AsyncIterator[MeshtasticWebApiContext]:
    context = MeshtasticWebApiContext(hass)
    yield context
    consumers = list(context._meshtastic_radio_stream_consumer.values())
    for session_key in list(context._meshtastic_radio_stream_consumer):
        context._remove_session(session_key)
    context.close()
    await asyncio.gather(*consumers, return_exceptions=True)


async def test_clients_behind_one_address_keep_their_own_queues(context: MeshtasticWebApiContext) -> None:
    first = context.add_session(request("token-a"), "1")
    second = context.add_session(request("token-b"), "1")

    assert first is not second
    assert context.get_session_queue(request("token-a"), "1") is first
    assert context.get_session_queue(request("token-b"), "1") is second


async def test_a_session_is_found_only_by_its_own_token_and_config_id(context: MeshtasticWebApiContext) -> None:
    context.add_session(request("token-a"), "1")

    assert context.get_session_queue(request("token-b"), "1") is None
    assert context.get_session_queue(request("token-a"), "2") is None
    assert not context.remove_session(request("token-b"), "1")


async def test_a_session_follows_its_token_to_a_new_address(context: MeshtasticWebApiContext) -> None:
    queue = context.add_session(request("token-a"), "1")

    assert context.get_session_queue(request("token-a", remote="10.0.0.2"), "1") is queue


async def test_reconnecting_a_session_stops_its_old_consumer(context: MeshtasticWebApiContext) -> None:
    context.add_session(request("token-a"), "1")
    old = next(iter(context._meshtastic_radio_stream_consumer.values()))

    context.add_session(request("token-a"), "1")
    await asyncio.gather(old, return_exceptions=True)

    assert old.done()
    assert len(context._meshtastic_radio_stream_consumer) == 1
    assert next(iter(context._meshtastic_radio_stream_consumer.values())) is not old


async def test_a_session_that_is_never_polled_is_still_tracked_for_expiry(context: MeshtasticWebApiContext) -> None:
    context.add_session(request("token-a"), "1")

    tracked: dict[Any, Any] = context._session_last_active
    assert ("token-a", "1") in tracked
