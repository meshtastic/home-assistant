# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The sidebar panel loads from the integration alone."""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.meshtastic.ha_frontend import locate_dir


def test_the_panel_references_no_external_host() -> None:
    source = (Path(locate_dir()) / "panel.js").read_text()

    assert re.findall(r"https?://", source) == []


def test_the_panel_has_no_module_imports() -> None:
    source = (Path(locate_dir()) / "panel.js").read_text()

    assert re.findall(r"^\s*(?:import\b|export\b)", source, re.MULTILINE) == []
