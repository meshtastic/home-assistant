import contextlib  # noqa: D104

from .connection.tcp import TcpConnection
from .interface import MeshInterface

_interface = [MeshInterface, TcpConnection]

with contextlib.suppress(ImportError):
    from .connection.serial import SerialConnection

    _interface.append(SerialConnection)

with contextlib.suppress(ImportError):
    from .connection.bluetooth import BluetoothConnection

    _interface.append(BluetoothConnection)


__all__ = list(_interface)
