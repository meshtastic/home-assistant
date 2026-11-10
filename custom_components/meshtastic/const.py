# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
# SPDX-FileCopyrightText: 2025 Ovidiu D. Nițan @ov1d1u
#
# SPDX-License-Identifier: MIT

import enum
from logging import Logger, getLogger
from typing import Final, NotRequired, TypedDict

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


CONF_OPTION_CHAT = "chat"
CONF_OPTION_CHAT_ENABLE = "enable"
CONF_OPTION_CHAT_ENABLE_DEFAULT = False
CONF_OPTION_CHAT_REQUIRE_ADMIN = "require_admin"
CONF_OPTION_CHAT_REQUIRE_ADMIN_DEFAULT = True
CONF_OPTION_CHAT_PREFIX_SENDER = "prefix_sender"
CONF_OPTION_CHAT_PREFIX_SENDER_DEFAULT = False
CONF_OPTION_CHAT_HISTORY_SIZE = "history_size"
CONF_OPTION_CHAT_HISTORY_SIZE_DEFAULT = 200
CONF_OPTION_CHAT_CHANNEL_MANAGEMENT = "channel_management"
CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_OFF = "off"
CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ADMIN = "admin"
CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_ALL = "all"
CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_DEFAULT = CONF_OPTION_CHAT_CHANNEL_MANAGEMENT_OFF
CONF_OPTION_CHAT_HISTORY_SIZE_MIN = 10
CONF_OPTION_CHAT_HISTORY_SIZE_MAX = 2000

CHAT_STORAGE_VERSION = 1
CHAT_STORAGE_SAVE_DELAY = 10  # seconds the history is held back before it is written to disk
CHAT_MAX_TEXT_BYTES = 200  # a Meshtastic text message, sender prefix included
CHAT_ACK_TIMEOUT = 60  # seconds to wait for the mesh to acknowledge a message
CHAT_DEDUPLICATE_WINDOW = 25  # the gateway can report the same packet more than once
CHAT_HISTORY_PAGE_SIZE = 100
CHAT_SENT_BY_MAX_LENGTH = 64  # label of who sent a message on behalf of someone else
CHAT_HISTORY_PAGE_SIZE_MAX = 1000
CHAT_REPLY_PREVIEW_CHARS = 80  # how much of the quoted message is kept with a reply

CHAT_CHANNEL_COUNT = 8  # channel slots of a Meshtastic node
CHAT_CHANNEL_NAME_MAX_BYTES = 11
CHAT_CHANNEL_NAME_INPUT_MAX_LENGTH = 64  # accepted from the websocket before the byte limit is checked
CHAT_CHANNEL_ROLE_PRIMARY = "PRIMARY"
CHAT_CHANNEL_ROLE_SECONDARY = "SECONDARY"
CHAT_CHANNEL_ROLE_DISABLED = "DISABLED"
CHAT_CHANNEL_KEY_KEEP = "keep"
CHAT_CHANNEL_KEY_NONE = "none"
CHAT_CHANNEL_KEY_DEFAULT = "default"
CHAT_CHANNEL_KEY_RANDOM = "random"  # new 256 bit key
CHAT_CHANNEL_KEY_RANDOM_128 = "random128"
CHAT_CHANNEL_KEY_INPUT_MAX_LENGTH = 64  # a base64 encoded 32 byte key is 44 characters
CHAT_CHANNEL_KEY_LENGTHS = (0, 1, 16, 32)  # bytes: none, a well known default key, AES-128, AES-256
CHAT_POSITION_PRECISION_FULL = 32


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
ATTR_SERVICE_DATA_REPLY_ID = "reply_id"
ATTR_SERVICE_DATA_IS_REACTION = "is_reaction"


ATTR_SERVICE_SEND_TEXT_DATA_TEXT = "text"
ATTR_SERVICE_SEND_TEXT_DATA_SENT_BY = "sent_by"
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
    device_id: str
    type: MeshtasticDomainEventType
    entity_id: NotRequired[str]
    message: NotRequired[str]


# Primary user facing event
EVENT_MESHTASTIC_DOMAIN_EVENT: EventType[MeshtasticDomainEventData] = EventType(f"{DOMAIN}_event")


EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: Final = "from_name"
EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: Final = "pki"
EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: Final = "message"


class MeshtasticDomainMessageLogEventData(TypedDict):
    device_id: str | None
    entity_id: str
    message: str
    from_name: str
    pki: bool


# Event used for logbook
EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG: EventType[MeshtasticDomainMessageLogEventData] = EventType(f"{DOMAIN}_message_log")

# fired by Mesh Chat for every message sent from Home Assistant once its delivery is settled
EVENT_MESHTASTIC_CHAT_MESSAGE_SENT: Final = f"{DOMAIN}_chat_message_sent"

URL_BASE = "/meshtastic"
