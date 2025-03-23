import enum
from logging import Logger, getLogger
from typing import Final, TypedDict

from homeassistant.util.event_type import EventType

LOGGER: Logger = getLogger(__package__)

DOMAIN = "meshtastic"

CONF_CONNECTION_TYPE = "connection_type"

CURRENT_CONFIG_VERSION_MAJOR = 1
CURRENT_CONFIG_VERSION_MINOR = 2

CONF_CONNECTION_BLUETOOTH_ADDRESS = "bluetooth_address"
CONF_CONNECTION_SERIAL_PORT = "serial_port"

CONF_CONNECTION_TCP_HOST = "tcp_host"
CONF_CONNECTION_TCP_PORT = "tcp_port"

CONF_OPTION_FILTER_NODES = "nodes"
CONF_OPTION_NODE = "node"
CONF_OPTION_ADD_ANOTHER_NODE = "add_another_node"

CONF_OPTION_NOTIFY_PLATFORM = "notify_platform"
CONF_OPTION_NOTIFY_PLATFORM_CHANNELS = "channels"
CONF_OPTION_NOTIFY_PLATFORM_NODES = "nodes"


class ConfigOptionNotifyPlatformNodes(enum.StrEnum):
    NONE = "none"
    ALL = "all"
    SELECTED = "selected"


CONF_OPTION_NOTIFY_PLATFORM_CHANNELS_DEFAULT = True
CONF_OPTION_NOTIFY_PLATFORM_NODES_DEFAULT = ConfigOptionNotifyPlatformNodes.ALL

CONF_OPTION_WEB_CLIENT = "web_client"
CONF_OPTION_WEB_CLIENT_ENABLE = "enable"
CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT = False


CONF_OPTION_TCP_PROXY = "tcp_proxy"
CONF_OPTION_TCP_PROXY_ENABLE = "enable"
CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT = False

CONF_OPTION_TCP_PROXY_PORT = "port"
CONF_OPTION_TCP_PROXY_PORT_DEFAULT = 4403


SERVICE_SEND_TEXT = "send_text"
SERVICE_SEND_DIRECT_MESSAGE = "send_direct_message"
SERVICE_BROADCAST_CHANNEL_MESSAGE = "broadcast_channel_message"
SERVICE_REQUEST_TELEMETRY = "request_telemetry"
SERVICE_REQUEST_POSITION = "request_position"
SERVICE_REQUEST_TRACEROUTE = "request_traceroute"

ATTR_SERVICE_DATA_TO = "to"
ATTR_SERVICE_DATA_CHANNEL = "channel"
ATTR_SERVICE_DATA_FROM = "from"
ATTR_SERVICE_DATA_ACK = "ack"


ATTR_SERVICE_SEND_TEXT_DATA_TEXT = "text"
ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE = "message"
ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE = "message"
ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL = "channel"

ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE = "type"

STATE_ATTRIBUTE_CHANNEL_INDEX = "index"
STATE_ATTRIBUTE_CHANNEL_NODE = "node"


class ConnectionType(enum.StrEnum):
    TCP = "tcp"
    BLUETOOTH = "bluetooth"
    SERIAL = "serial"


class MeshtasticDomainEventType(enum.StrEnum):
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"


EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE: Final = "message"


class MeshtasticDomainEventData(TypedDict):
    CONF_DEVICE_ID: str
    CONF_ENTITY_ID: str | None
    EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE: str


# Primary user facing event
EVENT_MESHTASTIC_DOMAIN_EVENT: EventType[MeshtasticDomainEventData] = EventType(f"{DOMAIN}_event")


EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: Final = "from_name"
EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: Final = "pki"
EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: Final = "message"


class MeshtasticDomainMessageLogEventData(TypedDict):
    CONF_DEVICE_ID: str
    CONF_ENTITY_ID: str
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: str
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: str
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: bool
    EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE: str


# Event used for logbook
EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG: EventType[MeshtasticDomainMessageLogEventData] = EventType(f"{DOMAIN}_message_log")

URL_BASE = "/meshtastic"
