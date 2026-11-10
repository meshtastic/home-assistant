# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

import contextlib

from .connection.tcp import TcpConnection
from .interface import MeshInterface

_interface = [MeshInterface, TcpConnection]

with contextlib.suppress(ImportError):
    from .connection.serial import SerialConnection

    _interface.append(SerialConnection)

with contextlib.suppress(ImportError):
    from .connection.bluetooth import BluetoothConnection

    _interface.append(BluetoothConnection)


__all__ = [cls.__name__ for cls in _interface]
