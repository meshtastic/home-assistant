# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

import logging
from typing import Any


class Undefined:
    def __nonzero__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "..."


UNDEFINED: Any = Undefined()  # Any: it is the default of float-typed parameters
LOGGER = logging.getLogger(__package__)
