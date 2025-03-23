from homeassistant.core import HomeAssistant  # noqa: D104

from ..const import (  # noqa: TID252
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_TCP_PROXY_ENABLE,
    CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT,
    CONF_OPTION_TCP_PROXY_PORT,
    CONF_OPTION_TCP_PROXY_PORT_DEFAULT,
    LOGGER,
)
from ..data import MeshtasticConfigEntry  # noqa: TID252
from .server import MeshtasticTcpProxy

_LOGGER = LOGGER.getChild(__name__)

_servers: dict[str, MeshtasticTcpProxy] = {}


async def async_setup_tcp_proxy(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MeshtasticConfigEntry,
) -> bool:
    tcp_proxy_config = entry.options.get(CONF_OPTION_TCP_PROXY, {})
    if tcp_proxy_config.get(CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT) is False:
        return False

    tcp_proxy_port = tcp_proxy_config.get(CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT)
    server = MeshtasticTcpProxy(entry.runtime_data.client._interface, port=tcp_proxy_port)  # noqa: SLF001
    try:
        await server.start()
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Failed to start tcp proxy server: %s", e)
        return False
    _servers[entry.entry_id] = server

    return True


async def async_unload_tcp_proxy(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MeshtasticConfigEntry,
) -> bool:
    server = _servers.pop(entry.entry_id, None)
    if server:
        await server.stop()
        return True

    return False
