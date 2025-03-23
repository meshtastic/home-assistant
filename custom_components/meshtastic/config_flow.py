from __future__ import annotations

import socket
from copy import deepcopy
from typing import TYPE_CHECKING

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, IntegrationError
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from . import CONF_OPTION_WEB_CLIENT, CURRENT_CONFIG_VERSION_MINOR
from .aiomeshtastic import TcpConnection
from .api import (
    MeshtasticApiClient,
)
from .const import (
    CONF_CONNECTION_BLUETOOTH_ADDRESS,
    CONF_CONNECTION_SERIAL_PORT,
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_ADD_ANOTHER_NODE,
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_NODE,
    CONF_OPTION_NOTIFY_PLATFORM,
    CONF_OPTION_NOTIFY_PLATFORM_CHANNELS,
    CONF_OPTION_NOTIFY_PLATFORM_CHANNELS_DEFAULT,
    CONF_OPTION_NOTIFY_PLATFORM_NODES,
    CONF_OPTION_NOTIFY_PLATFORM_NODES_DEFAULT,
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_TCP_PROXY_ENABLE,
    CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT,
    CONF_OPTION_TCP_PROXY_PORT,
    CONF_OPTION_TCP_PROXY_PORT_DEFAULT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT,
    CURRENT_CONFIG_VERSION_MAJOR,
    DOMAIN,
    LOGGER,
    ConfigOptionNotifyPlatformNodes,
    ConnectionType,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping
    from typing import Any

    from habluetooth import BluetoothServiceInfo
    from homeassistant.components import zeroconf
    from homeassistant.components.usb import UsbServiceInfo
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
    from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = LOGGER.getChild(__name__)


def _step_user_data_connection_tcp_schema_factory(host: str = "", port: int | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONNECTION_TCP_HOST, default=host): cv.string,
            vol.Required(CONF_CONNECTION_TCP_PORT, default=port or TcpConnection.DEFAULT_TCP_PORT): cv.positive_int,
        }
    )


def _step_user_data_connection_bluetooth_schema_factory(address: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONNECTION_BLUETOOTH_ADDRESS, default=address): cv.string,
        }
    )


def _step_user_data_connection_serial_schema_factory(device: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONNECTION_SERIAL_PORT, default=device): cv.string,
        }
    )


NODE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OPTION_NODE): cv.string,
        vol.Optional(CONF_OPTION_ADD_ANOTHER_NODE): cv.boolean,
    }
)


def _build_add_node_schema(
    options: dict[str, Any],
    nodes: dict[int, Any],
    node_selection_required: bool = True,  # noqa: FBT001, FBT002
) -> vol.Schema:
    already_selected_node_nums = [el["id"] for el in options[CONF_OPTION_FILTER_NODES]]
    selectable_nodes = {
        node_id: node_info for node_id, node_info in nodes.items() if node_id not in already_selected_node_nums
    }
    if not selectable_nodes:
        return vol.Schema({})

    selector_options = [
        SelectOptionDict(
            value=str(node_id),
            label=f"{node_info['user']['longName']} ({node_info['user']['id']})",
        )
        for node_id, node_info in sorted(
            selectable_nodes.items(),
            key=lambda el: el[1].get("isFavorite", False),
            reverse=True,
        )
    ]

    return vol.Schema(
        {
            vol.Required(CONF_OPTION_NODE)
            if node_selection_required
            else vol.Optional(CONF_OPTION_NODE): SelectSelector(SelectSelectorConfig(options=selector_options)),
            vol.Optional(CONF_OPTION_ADD_ANOTHER_NODE): cv.boolean,
        }
    )


def _build_notify_platform_schema(
    options: dict[str, Any],
) -> vol.Schema:
    node_selector = SelectSelector(
        SelectSelectorConfig(
            options=[str(v) for v in ConfigOptionNotifyPlatformNodes],
            translation_key="option_notify_platform_node_selector",
        )
    )

    return vol.Schema(
        {
            vol.Required(
                CONF_OPTION_NOTIFY_PLATFORM_CHANNELS,
                default=options.get(CONF_OPTION_NOTIFY_PLATFORM_CHANNELS, CONF_OPTION_NOTIFY_PLATFORM_CHANNELS_DEFAULT),
            ): cv.boolean,
            vol.Required(
                CONF_OPTION_NOTIFY_PLATFORM_NODES,
                default=options.get(CONF_OPTION_NOTIFY_PLATFORM_NODES, CONF_OPTION_NOTIFY_PLATFORM_NODES_DEFAULT),
            ): node_selector,
        }
    )


def _build_meshtastic_web_schema(
    options: dict[str, Any],
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_OPTION_WEB_CLIENT_ENABLE,
                default=options.get(CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT),
            ): cv.boolean
        }
    )


def _build_meshtastic_tcp_schema(
    options: dict[str, Any],
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_OPTION_TCP_PROXY_ENABLE,
                default=options.get(CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT),
            ): cv.boolean,
            vol.Required(
                CONF_OPTION_TCP_PROXY_PORT,
                default=options.get(CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT),
            ): cv.positive_int,
        }
    )


async def validate_input_for_connection(
    hass: HomeAssistant, data: dict[str, Any], *, no_nodes: bool = False
) -> tuple[Mapping[str, Any], Mapping[int, Mapping[str, Any]]]:
    try:
        async with MeshtasticApiClient(
            data,
            hass=hass,
            config_entry_id=None,
            no_nodes=no_nodes,
        ) as client:
            gateway_node = await client.async_get_own_node()
            nodes = await client.async_get_all_nodes()
            return gateway_node, nodes
    except IntegrationError as e:
        _LOGGER.warning("Failed to connect to meshtastic device", exc_info=True)
        raise CannotConnectError from e


async def validate_tcp_proxy_port(hass: HomeAssistant, config_entry: ConfigEntry | None, data: dict[str, Any]) -> bool:
    if not data.get(CONF_OPTION_TCP_PROXY, {}).get(CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT):
        return True

    if config_entry is None:
        other_active_entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False, include_disabled=False)
    else:
        other_active_entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN, include_ignore=False, include_disabled=False)
            if e.entry_id != config_entry.entry_id
        ]
    other_tcp_proxies = [e.options.get(CONF_OPTION_TCP_PROXY, {}) for e in other_active_entries]

    other_ports = {
        c.get(CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT)
        for c in other_tcp_proxies
        if c.get(CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT)
    }

    port = data[CONF_OPTION_TCP_PROXY].get(CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT)
    if port in other_ports:
        return False

    port_changed = config_entry is None or port != config_entry.options.get(CONF_OPTION_TCP_PROXY, {}).get(
        CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT
    )
    if port_changed:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", port)) == 0:
                    return False
        except:  # noqa: E722
            _LOGGER.debug("Failed to validate tcp proxy port", exc_info=True)
            return False

    return True


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CURRENT_CONFIG_VERSION_MAJOR
    MINOR_VERSION = CURRENT_CONFIG_VERSION_MINOR

    def __init__(self) -> None:
        self._bluetooth_discovery_info: BluetoothServiceInfo | None = None
        self._zeroconf_discovery_info: ZeroconfServiceInfo | None = None
        self._usb_discovery_info: UsbServiceInfo | None = None
        self.user_input_from_step_user: dict = None
        self.data = {}
        self.options = {}

        self._load_nodes_task: asyncio.Task | None = None

    async def _handle_connection_user_input(
        self, errors: dict[str, str], user_input: dict[str, Any] | None = None, *, no_full_load: bool = False
    ) -> ConfigFlowResult | None:
        try:
            data = dict(self.data)
            data.update(user_input)
            gateway_node, nodes = await validate_input_for_connection(self.hass, data, no_nodes=no_full_load)
        except CannotConnectError:
            errors["base"] = "cannot_connect"
        except:  # noqa: E722
            _LOGGER.warning("Unexpected exception", exc_info=True)
            errors["base"] = "unknown"
        else:
            # Checks that the device is actually unique, otherwise abort
            await self.async_set_unique_id(str(gateway_node["num"]))
            self._abort_if_unique_id_configured(updates=user_input)

            self.gateway_node = gateway_node
            self.nodes = nodes
            self.data.update(user_input)
            self.options = {CONF_OPTION_FILTER_NODES: []}

            # Now call the second step but set user_input to None for the first time to force data entry in step 2
            return await self.async_step_node(user_input=None, load_nodes=no_full_load)

        return None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        return self.async_show_menu(step_id="user", menu_options=["manual_tcp", "manual_bluetooth", "manual_serial"])

    async def async_step_manual_tcp(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self.data[CONF_CONNECTION_TYPE] = ConnectionType.TCP.value
        errors: dict[str, str] = {}
        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        # Show the form for step 1 with the user/host/pass as defined in STEP_USER_DATA_SCHEMA
        return self.async_show_form(
            step_id="manual_tcp",
            data_schema=_step_user_data_connection_tcp_schema_factory(
                self.data.get(CONF_CONNECTION_TCP_HOST), self.data.get(CONF_CONNECTION_TCP_PORT)
            ),
            errors=errors,
        )

    async def async_step_manual_bluetooth(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self.data[CONF_CONNECTION_TYPE] = ConnectionType.BLUETOOTH.value
        errors: dict[str, str] = {}
        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        return self.async_show_form(
            step_id="manual_bluetooth",
            data_schema=_step_user_data_connection_bluetooth_schema_factory(
                self.data.get(CONF_CONNECTION_BLUETOOTH_ADDRESS)
            ),
            errors=errors,
        )

    async def async_step_manual_serial(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self.data[CONF_CONNECTION_TYPE] = ConnectionType.SERIAL.value
        errors: dict[str, str] = {}
        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        return self.async_show_form(
            step_id="manual_serial",
            data_schema=_step_user_data_connection_serial_schema_factory(self.data.get(CONF_CONNECTION_SERIAL_PORT)),
            errors=errors,
        )

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfo) -> ConfigFlowResult:
        self._bluetooth_discovery_info = discovery_info

        self.data = {
            CONF_CONNECTION_TYPE: ConnectionType.BLUETOOTH.value,
            CONF_CONNECTION_BLUETOOTH_ADDRESS: self._bluetooth_discovery_info.address,
        }

        all_entries = self.hass.config_entries.async_entries(DOMAIN, include_disabled=True, include_ignore=True)
        matching_entry = next(
            (
                c
                for c in all_entries
                if c.data.get(CONF_CONNECTION_TYPE) == self.data[CONF_CONNECTION_TYPE]
                and c.data.get(CONF_CONNECTION_BLUETOOTH_ADDRESS) == self.data[CONF_CONNECTION_BLUETOOTH_ADDRESS]
            ),
            None,
        )
        if matching_entry:
            # extract unique id from existing config entry (so that we don't need to connect to node to get node id
            # and might be interrupting the connection)
            await self.async_set_unique_id(matching_entry.unique_id)
        else:
            await self.async_set_unique_id(discovery_info.address)

        self._abort_if_unique_id_configured()

        return await self.async_step_discovery_bluetooth_confirm()

    async def async_step_discovery_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        title = f"{self._bluetooth_discovery_info.name} ({self._bluetooth_discovery_info.address})"
        errors: dict[str, str] = {}

        self.context["title_placeholders"] = {
            "name": title,
        }

        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        return self.async_show_form(
            step_id="discovery_bluetooth_confirm",
            data_schema=_step_user_data_connection_bluetooth_schema_factory(
                self.data.get(CONF_CONNECTION_BLUETOOTH_ADDRESS)
            ),
            description_placeholders=self.context["title_placeholders"],
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: zeroconf.ZeroconfServiceInfo) -> ConfigFlowResult:
        self._zeroconf_discovery_info = discovery_info
        if discovery_info.type == "_http._tcp.local." and discovery_info.name != "Meshtastic._http._tcp.local.":
            return self.async_abort(reason="not_meshtastic_device")

        if discovery_info.type == "_meshtastic._tcp.local.":
            port = discovery_info.port or TcpConnection.DEFAULT_TCP_PORT
        else:
            port = TcpConnection.DEFAULT_TCP_PORT

        self.data = {
            CONF_CONNECTION_TYPE: ConnectionType.TCP.value,
            CONF_CONNECTION_TCP_HOST: discovery_info.host,
            CONF_CONNECTION_TCP_PORT: port,
        }

        all_entries = self.hass.config_entries.async_entries(DOMAIN, include_disabled=True, include_ignore=True)
        matching_entry = next(
            (
                c
                for c in all_entries
                if c.data.get(CONF_CONNECTION_TYPE) == self.data[CONF_CONNECTION_TYPE]
                and c.data.get(CONF_CONNECTION_TCP_HOST) == self.data[CONF_CONNECTION_TCP_HOST]
            ),
            None,
        )
        if matching_entry:
            # extract unique id from existing config entry (so that we don't need to connect to node to get node id
            # and might be interrupting the connection)
            await self.async_set_unique_id(matching_entry.unique_id)
        else:
            await self.async_set_unique_id(discovery_info.host)

        self._abort_if_unique_id_configured()

        return await self.async_step_discovery_zeroconf_confirm()

    async def async_step_discovery_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        title = self._zeroconf_discovery_info.host
        errors: dict[str, str] = {}

        self.context["title_placeholders"] = {
            "name": title,
        }

        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        return self.async_show_form(
            step_id="discovery_zeroconf_confirm",
            data_schema=_step_user_data_connection_tcp_schema_factory(
                host=self.data.get(CONF_CONNECTION_TCP_HOST, ""),
                port=self.data.get(CONF_CONNECTION_TCP_PORT, None),
            ),
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        self._usb_discovery_info = discovery_info

        self.data = {
            CONF_CONNECTION_TYPE: ConnectionType.SERIAL.value,
            CONF_CONNECTION_SERIAL_PORT: discovery_info.device,
        }

        all_entries = self.hass.config_entries.async_entries(DOMAIN, include_disabled=True, include_ignore=True)
        matching_entry = next(
            (
                c
                for c in all_entries
                if c.data.get(CONF_CONNECTION_TYPE) == self.data[CONF_CONNECTION_TYPE]
                and (c.data.get(CONF_CONNECTION_SERIAL_PORT) == self.data[CONF_CONNECTION_SERIAL_PORT])
            ),
            None,
        )
        if matching_entry:
            # extract unique id from existing config entry (so that we don't need to connect to node to get node id
            # and might be interrupting the connection)
            await self.async_set_unique_id(matching_entry.unique_id)
        else:
            await self.async_set_unique_id(discovery_info.device)

        self._abort_if_unique_id_configured()

        return await self.async_step_discovery_usb_confirm()

    async def async_step_discovery_usb_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        device_name = self._usb_discovery_info.manufacturer
        if self._usb_discovery_info.description:
            device_name += " " + self._usb_discovery_info.description
        title = f"{device_name} ({self._usb_discovery_info.device})"
        errors: dict[str, str] = {}

        self.context["title_placeholders"] = {
            "name": title,
        }

        if user_input is not None:
            res = await self._handle_connection_user_input(errors, user_input)
            if res is not None:
                return res

        return self.async_show_form(
            step_id="discovery_usb_confirm",
            data_schema=_step_user_data_connection_serial_schema_factory(
                device=self.data.get(CONF_CONNECTION_SERIAL_PORT, ""),
            ),
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_node(
        self, user_input: dict[str, Any] | None = None, *, load_nodes: bool = False
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            node_id = int(user_input[CONF_OPTION_NODE])
            if node_id not in self.nodes:
                errors["base"] = "invalid_node"

            if not errors:
                self.options[CONF_OPTION_FILTER_NODES].append(
                    {
                        "id": node_id,
                        "name": self.nodes[node_id]["user"]["longName"],
                    }
                )
                if user_input.get(CONF_OPTION_ADD_ANOTHER_NODE, False):
                    return await self.async_step_node()

                return await self.async_step_notify_platform()
        elif load_nodes:
            _, self.nodes = await validate_input_for_connection(self.hass, self.data, no_nodes=False)

        schema = _build_add_node_schema(self.options, self.nodes)
        return self.async_show_form(step_id="node", data_schema=schema, errors=errors)

    async def async_step_notify_platform(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.options[CONF_OPTION_NOTIFY_PLATFORM] = user_input
            return await self.async_step_web_client()

        schema = _build_notify_platform_schema(user_input or {})
        return self.async_show_form(step_id="notify_platform", data_schema=schema, errors=errors)

    async def async_step_web_client(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.options[CONF_OPTION_WEB_CLIENT] = user_input
            return await self.async_step_tcp_proxy()

        schema = _build_meshtastic_web_schema(user_input or {})
        return self.async_show_form(step_id="web_client", data_schema=schema, errors=errors)

    async def async_step_tcp_proxy(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not await validate_tcp_proxy_port(self.hass, None, {CONF_OPTION_TCP_PROXY: user_input}):
                errors[CONF_OPTION_TCP_PROXY_PORT] = "port_in_use"

            if not errors:
                self.options[CONF_OPTION_TCP_PROXY] = user_input
                return await self._finish_steps()

        schema = _build_meshtastic_tcp_schema(user_input or {})
        return self.async_show_form(step_id="tcp_proxy", data_schema=schema, errors=errors)

    async def _finish_steps(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self.gateway_node["user"]["longName"], data=self.data, options=self.options
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:  # noqa: ARG002
        config_entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))

        self.data.update(config_entry.data)
        connection_type = config_entry.data[CONF_CONNECTION_TYPE]
        if connection_type == ConnectionType.TCP.value:
            return await self.async_step_manual_tcp()
        if connection_type == ConnectionType.BLUETOOTH.value:
            return await self.async_step_manual_bluetooth()
        if connection_type == ConnectionType.SERIAL.value:
            return await self.async_step_manual_serial()
        return self.async_abort(reason="invalid_connection_type")


class CannotConnectError(HomeAssistantError):
    pass


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:  # noqa: ARG002
        self.options = {}
        self.nodes = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: PLR0912
        errors: dict[str, str] = {}

        if (
            hasattr(self.config_entry, "runtime_data")
            and self.config_entry.runtime_data
            and self.config_entry.runtime_data.client
        ):
            self.nodes = await self.config_entry.runtime_data.client.async_get_all_nodes()

        if self.nodes is None:
            try:
                _, self.nodes = await validate_input_for_connection(self.hass, self.config_entry.data)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except:  # noqa: E722
                _LOGGER.warning("Unexpected exception", exc_info=True)
                errors["base"] = "unknown"

        if errors:
            return self.async_show_form(step_id="init", errors=errors)

        current_filter_node_option = (
            self.options[CONF_OPTION_FILTER_NODES]
            if CONF_OPTION_FILTER_NODES in self.options
            else self.config_entry.options[CONF_OPTION_FILTER_NODES]
        )
        all_nodes = {str(k): v["user"]["longName"] for k, v in self.nodes.items()}
        already_selected_node_ids = [el["id"] for el in current_filter_node_option]

        if user_input is not None:
            new_data = {}
            updated_filter_node_option = deepcopy(current_filter_node_option)

            removed_node_ids = [
                node_id
                for node_id in already_selected_node_ids
                if str(node_id) not in user_input[CONF_OPTION_FILTER_NODES]
            ]
            for node_id in removed_node_ids:
                updated_filter_node_option = [e for e in updated_filter_node_option if e["id"] != node_id]

            if user_input.get(CONF_OPTION_NODE):
                # Add the new node
                updated_filter_node_option.append(
                    {
                        "id": int(user_input[CONF_OPTION_NODE]),
                        "name": self.nodes[int(user_input[CONF_OPTION_NODE])]["user"]["longName"],
                    }
                )

            if user_input.get(CONF_OPTION_ADD_ANOTHER_NODE, False):
                self.options[CONF_OPTION_FILTER_NODES] = updated_filter_node_option
                return await self.async_step_init()
            new_data[CONF_OPTION_FILTER_NODES] = updated_filter_node_option

            if CONF_OPTION_NOTIFY_PLATFORM in user_input:
                new_data[CONF_OPTION_NOTIFY_PLATFORM] = user_input[CONF_OPTION_NOTIFY_PLATFORM]

            if CONF_OPTION_WEB_CLIENT in user_input:
                new_data[CONF_OPTION_WEB_CLIENT] = user_input[CONF_OPTION_WEB_CLIENT]

            if CONF_OPTION_TCP_PROXY in user_input:
                if not await validate_tcp_proxy_port(self.hass, self.config_entry, user_input):
                    errors["base"] = "option_invalid"
                    errors["tcp_proxy"] = "port_in_use"
                else:
                    new_data[CONF_OPTION_TCP_PROXY] = user_input[CONF_OPTION_TCP_PROXY]

            if not errors:
                return self.async_create_entry(
                    title="",
                    data=new_data,
                )

        selected_nodes = {
            str(node_id): all_nodes.get(str(node_id), f"Unknown (id: {node_id})")
            for node_id in already_selected_node_ids
        }

        notify_options = (
            self.options[CONF_OPTION_NOTIFY_PLATFORM]
            if CONF_OPTION_NOTIFY_PLATFORM in self.options
            else self.config_entry.options.get(CONF_OPTION_NOTIFY_PLATFORM, {})
        )
        webclient_options = (
            self.options[CONF_OPTION_WEB_CLIENT]
            if CONF_OPTION_WEB_CLIENT in self.options
            else self.config_entry.options.get(CONF_OPTION_WEB_CLIENT, {})
        )
        tcp_proxy_options = (
            self.options[CONF_OPTION_TCP_PROXY]
            if CONF_OPTION_TCP_PROXY in self.options
            else self.config_entry.options.get(CONF_OPTION_TCP_PROXY, {})
        )
        options_schema = vol.Schema(
            {
                vol.Required(CONF_OPTION_FILTER_NODES, default=list(selected_nodes.keys())): cv.multi_select(
                    selected_nodes
                ),
                **_build_add_node_schema(
                    self.options if CONF_OPTION_FILTER_NODES in self.options else self.config_entry.options,
                    self.nodes,
                    node_selection_required=False,
                ).schema,
                vol.Required(CONF_OPTION_NOTIFY_PLATFORM): data_entry_flow.section(
                    _build_notify_platform_schema(notify_options), {"collapsed": True}
                ),
                vol.Required(CONF_OPTION_WEB_CLIENT): data_entry_flow.section(
                    _build_meshtastic_web_schema(webclient_options), {"collapsed": True}
                ),
                vol.Required(CONF_OPTION_TCP_PROXY): data_entry_flow.section(
                    _build_meshtastic_tcp_schema(tcp_proxy_options), {"collapsed": "tcp_proxy" in errors}
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=options_schema, errors=errors)
