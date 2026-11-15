# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

import asyncio
import contextlib
import datetime as dt
import secrets
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from aiohttp import web
from google.protobuf import message
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantRequest, HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..aiomeshtastic.connection import ClientApiConnection  # noqa: TID252
from ..aiomeshtastic.protobuf import mesh_pb2  # noqa: TID252
from ..api import MeshtasticApiClient  # noqa: TID252
from ..const import (  # noqa: TID252
    CONF_OPTION_WEB_CLIENT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT,
    DOMAIN,
    LOGGER,
    URL_BASE,
)

if TYPE_CHECKING:
    from ..data import MeshtasticConfigEntry  # noqa: TID252

_LOGGER = LOGGER.getChild(__name__)

# The radio API carries admin authority over the gateway, so it is reachable only under a token an
# admin obtained over the authenticated websocket. The web client cannot send HA credentials itself.
TOKEN_IDLE_TIMEOUT = dt.timedelta(hours=1)
TOKEN_MAX_AGE = dt.timedelta(hours=24)
DATA_WEB_CLIENT_TOKENS = f"{DOMAIN}_web_client_tokens"


@dataclass
class _Grant:
    config_entry_id: str
    user_id: str
    issued: dt.datetime
    last_used: dt.datetime


class WebClientTokens:
    def __init__(self) -> None:
        self._grants: dict[str, _Grant] = {}

    def issue(self, config_entry_id: str, user_id: str) -> str:
        now = dt.datetime.now(tz=dt.UTC)
        self._grants = {token: grant for token, grant in self._grants.items() if not self._expired(grant, now)}
        token = secrets.token_urlsafe(32)
        self._grants[token] = _Grant(config_entry_id, user_id, now, now)
        return token

    def check(self, config_entry_id: str, token: str) -> bool:
        now = dt.datetime.now(tz=dt.UTC)
        grant = self._grants.get(token)
        if grant is None or not secrets.compare_digest(grant.config_entry_id, config_entry_id):
            return False
        if self._expired(grant, now):
            self._grants.pop(token, None)
            return False
        grant.last_used = now
        return True

    @staticmethod
    def _expired(grant: _Grant, now: dt.datetime) -> bool:
        return now - grant.last_used > TOKEN_IDLE_TIMEOUT or now - grant.issued > TOKEN_MAX_AGE


def _web_client_enabled(config_entry: ConfigEntry) -> bool:
    return config_entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
        CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
    )


@websocket_api.websocket_command({vol.Required("type"): "meshtastic/web_client/open", vol.Required("entity_id"): str})
@websocket_api.require_admin
@callback
def websocket_open_web_client(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return a web client URL for a gateway entity, under a token bound to its config entry."""
    entity = er.async_get(hass).async_get(msg["entity_id"])
    config_entry = (
        hass.config_entries.async_get_entry(entity.config_entry_id)
        if entity is not None and entity.platform == DOMAIN and entity.config_entry_id
        else None
    )
    if config_entry is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Not a Meshtastic gateway")
        return
    if config_entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], "not_ready", f"Gateway is not ready ({config_entry.state.value})")
        return
    if not _web_client_enabled(config_entry):
        connection.send_error(msg["id"], "not_enabled", "The web client is not enabled for this gateway")
        return

    token = hass.data[DATA_WEB_CLIENT_TOKENS].issue(config_entry.entry_id, connection.user.id)
    path = f"{URL_BASE}/web/{config_entry.entry_id}/{token}"
    connection.send_result(msg["id"], {"url": f"{URL_BASE}/web/index.html?" + urlencode({"path": path})})


class MeshtasticWebApiContext:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._clients: dict[str, MeshtasticApiClient] = {}
        self._meshtastic_queues: dict[str, dict[tuple[str, str], asyncio.Queue[mesh_pb2.FromRadio]]] = defaultdict(dict)
        self._meshtastic_radio_stream_consumer: dict[tuple[str, str], asyncio.Task] = {}
        self._session_last_active: dict[tuple[str, str], dt.datetime] = {}

        async def expire_sessions() -> None:
            while True:
                await asyncio.sleep(30)
                now = dt.datetime.now(tz=dt.UTC)
                expired_session_keys = [
                    session
                    for session, last_active in self._session_last_active.items()
                    if (now - last_active > dt.timedelta(minutes=1))
                ]
                for session_key in expired_session_keys:
                    _LOGGER.debug("Removing session %s", session_key)
                    self._remove_session(session_key)

        self._expire_sessions_task: asyncio.Task[None] | None = hass.async_create_background_task(
            expire_sessions(), name="meshtastic_web_expire_sessions"
        )

    def close(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            cast("asyncio.Task[None]", self._expire_sessions_task).cancel()
        self._expire_sessions_task = None

    def add_client(self, config_entry_id: str, client: MeshtasticApiClient) -> None:
        self._clients[config_entry_id] = client

    def _get_config_entry_id(self, request: HomeAssistantRequest) -> str:
        return cast("str", request.match_info.get("config_entry_id"))

    def _get_config_entry(self, request: HomeAssistantRequest) -> ConfigEntry | None:
        config_entry_id = self._get_config_entry_id(request)
        if config_entry_id is None:
            return None
        try:
            return self._hass.config_entries.async_get_entry(config_entry_id)
        except Exception:
            _LOGGER.warning("Failed to get config entry", exc_info=True)
        return None

    def get_meshtastic_client(self, request: HomeAssistantRequest) -> MeshtasticApiClient | None:
        config_entry = self._get_config_entry(request)
        if config_entry is None:
            return None
        entry = cast("MeshtasticConfigEntry", config_entry)

        if entry.state != ConfigEntryState.LOADED:
            return None

        if entry.runtime_data is None:
            return None
        return entry.runtime_data.client

    def get_connection(self, request: HomeAssistantRequest) -> ClientApiConnection | None:
        client = self.get_meshtastic_client(request)
        if client is None:
            return None

        return client._interface._connection  # noqa: SLF001

    def add_session(
        self, request: HomeAssistantRequest, meshtastic_config_id: str
    ) -> asyncio.Queue[mesh_pb2.FromRadio]:
        config_entry_id = self._get_config_entry_id(request)
        session_key = (cast("str", request.remote), meshtastic_config_id)

        session_queue: asyncio.Queue[mesh_pb2.FromRadio] = asyncio.Queue()
        self._meshtastic_queues[config_entry_id][session_key] = session_queue

        async def consume(queue: asyncio.Queue[mesh_pb2.FromRadio]) -> None:
            try:
                while True:
                    client = self.get_meshtastic_client(request)
                    if client is None:
                        await asyncio.sleep(10)
                        continue

                    try:
                        async for packet in client._interface.from_radio_stream():  # noqa: SLF001
                            await queue.put(packet)
                    except asyncio.CancelledError:
                        break
            except Exception:
                _LOGGER.info("Consume from_radio for session %s stopped unexpectedly", session_key, exc_info=True)
            else:
                _LOGGER.info("Consume from_radio for session %s stopped", session_key)

        self._meshtastic_radio_stream_consumer[session_key] = asyncio.create_task(
            consume(session_queue), name="meshtasic-web-consume"
        )

        return self._meshtastic_queues[config_entry_id][session_key]

    def _get_existing_session_key(
        self, request: HomeAssistantRequest, meshtastic_config_id: str
    ) -> tuple[str, str] | None:
        config_entry_id = self._get_config_entry_id(request)
        session_key: tuple[str, str] | None = (cast("str", request.remote), meshtastic_config_id)

        config_entry_queues = self._meshtastic_queues.get(config_entry_id, {})
        if session_key not in config_entry_queues:
            session_key = None

        if session_key is None:
            session_key = next(
                (
                    (remote, config_id)
                    for (remote, config_id), q in config_entry_queues.items()
                    if config_id == meshtastic_config_id
                ),
                None,
            )

        if session_key is not None and session_key not in config_entry_queues:
            session_key = None

        if session_key is None:
            session_key = next(
                (
                    (remote, config_id)
                    for (remote, config_id), q in config_entry_queues.items()
                    if remote == request.remote
                ),
                None,
            )

        if session_key is not None and session_key not in config_entry_queues:
            session_key = None

        return session_key

    def remove_session(self, request: HomeAssistantRequest, meshtastic_config_id: str) -> bool:
        config_entry_id = self._get_config_entry_id(request)
        session_key = self._get_existing_session_key(request, meshtastic_config_id)
        if session_key is None:
            return False
        return self._remove_session(session_key, config_entry_id)

    def _remove_session(self, session_key: tuple[str, str], config_entry_id: str | None = None) -> bool:
        if config_entry_id is not None:
            sessions = self._meshtastic_queues.get(config_entry_id, {})
            sessions.pop(session_key, None)
        else:
            for sessions in self._meshtastic_queues.values():
                sessions.pop(session_key, None)
        self._session_last_active.pop(session_key, None)
        consumer = self._meshtastic_radio_stream_consumer.pop(session_key, None)
        if consumer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                consumer.cancel()

        return True

    def get_session_queue(
        self, request: HomeAssistantRequest, meshtastic_config_id: str
    ) -> asyncio.Queue[mesh_pb2.FromRadio] | None:
        config_entry_id = self._get_config_entry_id(request)
        # may be None at runtime; None is a valid (if useless) dict key and the lookup then yields no queue
        session_key = cast("tuple[str, str]", self._get_existing_session_key(request, meshtastic_config_id))

        self._session_last_active[session_key] = dt.datetime.now(tz=dt.UTC)
        return self._meshtastic_queues.get(config_entry_id, {}).get(session_key, None)

    def get_config_entry_session_queues(self, request: HomeAssistantRequest) -> list[asyncio.Queue[mesh_pb2.FromRadio]]:
        config_entry_id = self._get_config_entry_id(request)
        return list(self._meshtastic_queues[config_entry_id].values())


class MeshtasticWebSpaView(HomeAssistantView):
    """
    Serve the bundled client for its own multi-segment routes.

    Registered after the API views so they keep precedence, and before the
    static path, which raises rather than falling through on a miss.
    """

    url = URL_BASE + "/web/{path:.+/.+}"
    name = "meshtastic:web_spa"
    requires_auth = False

    async def get(
        self,
        request: HomeAssistantRequest,  # noqa: ARG002
        path: str,
    ) -> web.StreamResponse:
        return _serve_web_client_path(path)


async def async_setup(hass: HomeAssistant) -> bool:
    try:
        _api_context = MeshtasticWebApiContext(hass)
        tokens = hass.data.setdefault(DATA_WEB_CLIENT_TOKENS, WebClientTokens())
        websocket_api.async_register_command(hass, websocket_open_web_client)

        hass.http.register_view(MeshtasticWebConfigEntryView(hass))
        hass.http.register_view(MeshtasticWebApiV1FromRadioView(hass, _api_context, tokens))
        hass.http.register_view(MeshtasticWebApiV1ToRadioView(hass, _api_context, tokens))
        hass.http.register_view(MeshtasticWebJsonReportView(hass, _api_context, tokens))
        hass.http.register_view(MeshtasticWebSpaView())
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{URL_BASE}/web", str(Path(__file__).parent / "static"))]
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Failed to setup meshtastic web")
        return False
    else:
        return True


# The fromradio long poll must answer before the client aborts at 7 s (READ_TIMEOUT_MS in
# @meshtastic/transport-http), or the client reports the connection lost.
FROM_RADIO_HOLD_SECONDS = 5.0

# The client persists its database in OPFS, which needs a cross-origin isolated document.
# credentialless, not require-corp, because the third-party map style sends no CORP header.
CROSS_ORIGIN_ISOLATION_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "credentialless",
}


def _serve_web_client_path(relative_path: str) -> web.StreamResponse:
    """
    Serve a bundled file, or index.html for a client-side route.

    The client is a single page app: navigating to /nodes or
    /messages/broadcast/0 puts a real path in the URL, but there is no such
    file, so a reload or a service worker fetch would 404. Anything that is
    not a file and does not look like one is handed to index.html for the
    router to resolve. Something that does look like a file still 404s, so a
    genuinely missing asset is not masked by a page of HTML.
    """
    static_root = (Path(__file__).parent / "static").resolve()
    headers = {"Cache-Control": "no-cache", **CROSS_ORIGIN_ISOLATION_HEADERS}

    candidate = (static_root / relative_path).resolve()
    if static_root in candidate.parents and candidate.is_file():
        return web.FileResponse(candidate, headers=headers)

    if "." in PurePosixPath(relative_path).name:
        return web.HTTPNotFound()

    return web.FileResponse(static_root / "index.html", headers=headers)


class MeshtasticWebConfigEntryView(HomeAssistantView):
    url = URL_BASE + "/web/{entity_id}"
    name = "meshtastic:web_api_index"
    requires_auth = False

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        self._hass = hass

    async def get(
        self,
        request: HomeAssistantRequest,  # noqa: ARG002
        entity_id: str,
    ) -> web.StreamResponse:
        if not entity_id.startswith("gateway_"):
            return _serve_web_client_path(entity_id)

        # Old gateway links: the panel's Open button is the way in, since it can authenticate.
        return web.HTTPTemporaryRedirect(location=f"/{DOMAIN}", headers={"Cache-Control": "no-cache"})


class MeshtasticWebApiV1View(HomeAssistantView):
    requires_auth = False

    def __init__(self, hass: HomeAssistant, context: MeshtasticWebApiContext, tokens: WebClientTokens) -> None:
        self._clients: dict[str, MeshtasticApiClient] = {}
        self._hass = hass
        self._context = context
        self._tokens = tokens

    def get_connection(self, request: HomeAssistantRequest) -> ClientApiConnection | None:
        return self._context.get_connection(request)

    @staticmethod
    def _add_protobuf_headers(response: web.Response) -> None:
        response.headers.add("Content-Type", "application/x-protobuf")
        response.headers.add(
            "X-Protobuf-Schema", "https://raw.githubusercontent.com/meshtastic/protobufs/master/meshtastic/mesh.proto"
        )

    def _check_access(self, config_entry_id: str, token: str) -> None:
        if not self._tokens.check(config_entry_id, token):
            # 403, not 401: HA's ban middleware counts every 401 as a failed login, and a tab left open
            # across a restart polls with its lost token every few seconds
            raise web.HTTPForbidden(
                body="Web client session expired, open it again from the Meshtastic sidebar",
                headers={"Cache-Control": "no-cache"},
            )

        config_entry = self._hass.config_entries.async_get_entry(config_entry_id)
        if config_entry is None:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-cache"})
        if config_entry.state != ConfigEntryState.LOADED:
            raise web.HTTPBadGateway(
                body=f"Gateway is not ready (config entry state {config_entry.state.value})",
                content_type="text/plain",
                headers={"Cache-Control": "no-cache"},
            )
        if not _web_client_enabled(config_entry):
            raise web.HTTPForbidden(body="Web client not enabled for gateway", headers={"Cache-Control": "no-cache"})


class MeshtasticWebApiV1ToRadioView(MeshtasticWebApiV1View):
    url = URL_BASE + "/web/{config_entry_id}/{token}/api/v1/toradio"
    name = "meshtastic:web_v1_to_radio"

    async def put(self, request: HomeAssistantRequest, config_entry_id: str, token: str) -> web.Response:
        self._check_access(config_entry_id, token)
        connection = self.get_connection(request)
        if connection is None:
            await asyncio.sleep(1)
            raise web.HTTPNotFound(reason="No connection")

        if request.content_type != "application/x-protobuf":
            await asyncio.sleep(1)
            return web.HTTPNotAcceptable()

        body = await request.read()
        to_radio = mesh_pb2.ToRadio()
        try:
            to_radio.ParseFromString(body)
        except message.DecodeError:
            return web.HTTPBadRequest()

        response = web.Response()
        if to_radio.HasField("want_config_id"):
            config_id = str(to_radio.want_config_id)
            self._context.add_session(request, config_id)
            response.set_cookie("config_id", config_id)

        await connection.forward(body, to_radio)
        self._add_protobuf_headers(response)
        return response


class MeshtasticWebApiV1FromRadioView(MeshtasticWebApiV1View):
    url = URL_BASE + "/web/{config_entry_id}/{token}/api/v1/fromradio"
    name = "meshtastic:web_v1_from_radio"

    async def get(self, request: HomeAssistantRequest, config_entry_id: str, token: str) -> web.Response:
        self._check_access(config_entry_id, token)
        config_id = request.cookies.get("config_id")
        if config_id is None:
            await asyncio.sleep(1)
            bad_request = web.HTTPBadRequest()
            bad_request.headers.add("Cache-Control", "no-cache")
            return bad_request

        queue = self._context.get_session_queue(request, config_id)
        if queue is None:
            await asyncio.sleep(1)
            gone = web.HTTPGone()
            gone.headers.add("Cache-Control", "no-cache")
            return gone

        try:
            from_radio = await asyncio.wait_for(queue.get(), timeout=FROM_RADIO_HOLD_SECONDS)
            _LOGGER.debug(
                "Forwarding: %s to %s",
                (config_entry_id, request.remote, config_id),
                ClientApiConnection._protobuf_log(from_radio),  # noqa: SLF001
            )
            binary = from_radio.SerializeToString()
        except TimeoutError:
            binary = b""

        response = web.Response(body=binary)
        response.headers.add("Cache-Control", "no-cache")
        self._add_protobuf_headers(response)
        return response


class MeshtasticWebJsonReportView(MeshtasticWebApiV1View):
    url = URL_BASE + "/web/{config_entry_id}/{token}/json/report"
    name = "meshtastic:web_json_report"

    async def get(self, request: HomeAssistantRequest, config_entry_id: str, token: str) -> web.Response:
        self._check_access(config_entry_id, token)
        config_id = request.cookies.get("config_id")
        if config_id is None:
            response = web.HTTPBadRequest()
            response.headers.add("Cache-Control", "no-cache")
            return response

        # mock response device probe by frontend
        return web.json_response(data={"status": "ok", "data": {}})
