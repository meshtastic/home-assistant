import enum
from logging import Logger, getLogger

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

SERVICE_SEND_TEXT = "send_text"


class ConnectionType(enum.StrEnum):
    TCP = "tcp"
    BLUETOOTH = "bluetooth"
    SERIAL = "serial"
