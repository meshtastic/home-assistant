# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""What the manifest asks Home Assistant to discover."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = Path(__file__).parent.parent / "custom_components" / "meshtastic" / "manifest.json"

# USB-serial bridges and vendor default IDs that every board on the chip reports, Meshtastic or not
SHARED_USB_VENDORS = {"1A86", "303A", "2E8A", "10C4", "0403"}


def test_usb_discovery_offers_no_id_shared_with_unrelated_devices() -> None:
    matchers = json.loads(MANIFEST.read_text())["usb"]

    assert matchers
    assert not [m for m in matchers if m["vid"].upper() in SHARED_USB_VENDORS]
