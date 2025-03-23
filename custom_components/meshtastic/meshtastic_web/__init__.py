import asyncio  # noqa: D104
import contextlib
import datetime
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

import homeassistant.helpers.entity_registry as er
from aiohttp import web
from google.protobuf import message
from homeassistant.components.http import HomeAssistantRequest, HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

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


class MeshtasticWebApiContext:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._clients: dict[str, MeshtasticApiClient] = {}
        self._meshtastic_queues: dict[str, dict[tuple[str, str], asyncio.Queue[mesh_pb2.FromRadio]]] = defaultdict(dict)
        self._meshtastic_radio_stream_consumer: dict[tuple[str, str], asyncio.Task] = {}
        self._session_last_active: dict[tuple[str, str], datetime.datetime] = {}

        async def expire_sessions() -> None:
            while True:
                await asyncio.sleep(30)
                now = datetime.datetime.now(tz=datetime.UTC)
                expired_session_keys = [
                    session
                    for session, last_active in self._session_last_active.items()
                    if (now - last_active > datetime.timedelta(minutes=1))
                ]
                for session_key in expired_session_keys:
                    _LOGGER.debug("Removing session %s", session_key)
                    self._remove_session(session_key)

        self._expire_sessions_task = asyncio.create_task(expire_sessions(), name="meshtastic_web_expire_sessions")

    def close(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            self._expire_sessions_task.cancel()
        self._expire_sessions_task = None

    def add_client(self, config_entry_id: str, client: MeshtasticApiClient) -> None:
        self._clients[config_entry_id] = client

    def _get_config_entry_id(self, request: HomeAssistantRequest) -> str:
        return request.match_info.get("config_entry_id")

    def _get_config_entry(self, request: HomeAssistantRequest) -> ConfigEntry | None:
        config_entry_id = self._get_config_entry_id(request)
        if config_entry_id is None:
            return None
        try:
            return self._hass.config_entries.async_get_entry(config_entry_id)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to get config entry", exc_info=True)

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
        session_key = (request.remote, meshtastic_config_id)

        session_queue = asyncio.Queue()
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
            except Exception:  # noqa: BLE001
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
        session_key = (request.remote, meshtastic_config_id)

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
    ) -> asyncio.Queue[mesh_pb2.FromRadio]:
        config_entry_id = self._get_config_entry_id(request)
        session_key = self._get_existing_session_key(request, meshtastic_config_id)

        self._session_last_active[session_key] = datetime.datetime.now(tz=datetime.UTC)
        return self._meshtastic_queues.get(config_entry_id, {}).get(session_key, None)

    def get_config_entry_session_queues(self, request: HomeAssistantRequest) -> list[asyncio.Queue[mesh_pb2.FromRadio]]:
        config_entry_id = self._get_config_entry_id(request)
        return list(self._meshtastic_queues[config_entry_id].values())


async def async_setup(hass: HomeAssistant) -> bool:
    try:
        _api_context = MeshtasticWebApiContext(hass)

        hass.http.register_view(MeshtasticWebConfigEntryView(hass))
        hass.http.register_view(MeshtasticWebApiHotspot())
        hass.http.register_view(MeshtasticWebApiV1FromRadioView(hass, _api_context))
        hass.http.register_view(MeshtasticWebApiV1ToRadioView(hass, _api_context))
        hass.http.register_view(MeshtasticWebJsonReportView(hass, _api_context))
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{URL_BASE}/web", str(Path(__file__).parent / "static"))]
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Failed to setup meshtastic web")
        return False
    else:
        return True


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
    ) -> web.Response:
        if not entity_id.startswith("gateway_"):
            return web.FileResponse(Path(__file__).parent / "static" / entity_id, headers={"Cache-Control": "no-cache"})

        entity_registry = er.async_get(self._hass)
        entity_id = f"{DOMAIN}.{entity_id}"
        entity = entity_registry.async_get(entity_id)
        if entity is None:
            return web.HTTPNotFound()
        path = f"{URL_BASE}/web/{entity.config_entry_id}"

        config_entry = self._hass.config_entries.async_get_entry(entity.config_entry_id)
        if config_entry.state != ConfigEntryState.LOADED:
            return web.HTTPBadGateway(
                body=f"Gateway is not ready (config entry state {config_entry.state.value})",
                content_type="text/plain",
                headers={"Cache-Control": "no-cache"},
            )

        if not config_entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
            CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
        ):
            return web.HTTPForbidden(body="Web client not enabled for gateway", headers={"Cache-Control": "no-cache"})

        return web.HTTPTemporaryRedirect(
            location=f"{URL_BASE}/web/index.html?" + urlencode({"path": path}), headers={"Cache-Control": "no-cache"}
        )


class MeshtasticWebApiHotspot(HomeAssistantView):
    url = URL_BASE + "/web/{config_entry_id}/hotspot-detect.html"
    name = "meshtastic:web_api_hotspot"
    requires_auth = False

    async def get(
        self,
        request: HomeAssistantRequest,  # noqa: ARG002
        config_entry_id: str,  # noqa: ARG002
    ) -> web.Response:
        return web.HTTPOk()


class MeshtasticWebApiV1View(HomeAssistantView):
    requires_auth = False
    cors_allowed = True

    def __init__(self, hass: HomeAssistant, context: MeshtasticWebApiContext) -> None:
        self._clients: dict[str, MeshtasticApiClient] = {}
        self._hass = hass
        self._context = context

    def get_connection(self, request: HomeAssistantRequest) -> ClientApiConnection:
        return self._context.get_connection(request)

    @staticmethod
    def _add_protobuf_headers(response: web.Response) -> None:
        response.headers.add("Content-Type", "application/x-protobuf")
        response.headers.add(
            "X-Protobuf-Schema", "https://raw.githubusercontent.com/meshtastic/protobufs/master/meshtastic/mesh.proto"
        )

    def _check_webclient_enabled(self, config_entry_id: str) -> None:
        config_entry = self._hass.config_entries.async_get_entry(config_entry_id)
        if config_entry.state != ConfigEntryState.LOADED:
            raise web.HTTPBadGateway(
                body=f"Gateway is not ready (config entry state {config_entry.state.value})",
                content_type="text/plain",
                headers={"Cache-Control": "no-cache"},
            )

        if not config_entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
            CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
        ):
            raise web.HTTPForbidden(body="Web client not enabled for gateway", headers={"Cache-Control": "no-cache"})


class MeshtasticWebApiV1ToRadioView(MeshtasticWebApiV1View):
    url = URL_BASE + "/web/{config_entry_id}/api/v1/toradio"
    name = "meshtastic:web_v1_to_radio"

    async def put(self, request: HomeAssistantRequest, config_entry_id: str) -> web.Response:
        self._check_webclient_enabled(config_entry_id)
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

        await connection._send_packet(body)  # noqa: SLF001
        self._add_protobuf_headers(response)
        return response


class MeshtasticWebApiV1FromRadioView(MeshtasticWebApiV1View):
    url = URL_BASE + "/web/{config_entry_id}/api/v1/fromradio"
    name = "meshtastic:web_v1_from_radio"

    async def get(self, request: HomeAssistantRequest, config_entry_id: str) -> web.Response:
        self._check_webclient_enabled(config_entry_id)
        config_id = request.cookies.get("config_id")
        if config_id is None:
            await asyncio.sleep(1)
            response = web.HTTPBadRequest()
            response.headers.add("Cache-Control", "no-cache")
            return response

        queue = self._context.get_session_queue(request, config_id)
        if queue is None:
            await asyncio.sleep(1)
            response = web.HTTPGone()
            response.headers.add("Cache-Control", "no-cache")
            return response

        try:
            from_radio = await asyncio.wait_for(queue.get(), timeout=10.0)
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
    url = URL_BASE + "/web/{config_entry_id}/json/report"
    name = "meshtastic:web_json_report"

    async def get(self, request: HomeAssistantRequest, config_entry_id: str) -> web.Response:
        self._check_webclient_enabled(config_entry_id)
        config_id = request.cookies.get("config_id")
        if config_id is None:
            response = web.HTTPBadRequest()
            response.headers.add("Cache-Control", "no-cache")
            return response

        # mock response device probe by frontend
        return web.json_response(data={"status": "ok", "data": {}})
