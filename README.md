# Home-Assistant Meshtastic Integration

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)


_Home Assistant Integration for [Meshtastic](https://www.meshtastic.org)._

**Work in Progress**

Supported Features:
 * Add meshtastic devices as gateways to interact with the mesh
   * Supports TCP, Serial & Bluetooth connection
   * Home Assistant Zeroconf & Bluetooth Auto-Discovery (also works with [Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html))
 * Select which meshtastic nodes should be made available in home assistant
 * Basic meshtastic node metrics as sensors
 * Send and receive messages as device trigger and action


## Installation


### Recommended: [HACS](https://www.hacs.xyz)

1. Add this repository as a custom repository to HACS: [![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=broglep&repository=homeassistant-meshtastic&category=integration)
2. Use HACS to install the integration.
3. Restart Home Assistant.
4. Set up the integration using the UI: [![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshtastic)

### Alternative: Manual

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `homeassistant-meshtastic`.
1. Download _all_ the files from the `custom_components/meshtastic/` directory (folder) in this repository.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant
1. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Meshtastic"

## Configuration is done in the UI

<!---->

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

***

[commits-shield]: https://img.shields.io/github/commit-activity/y/broglep/homeassistant-meshtastic.svg?style=for-the-badge
[commits]: https://github.com/broglep/homeassistant-meshtastic/commits/main
[license-shield]: https://img.shields.io/github/license/broglep/homeassistant-meshtastic.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/broglep/homeassistant-meshtastic.svg?style=for-the-badge
[releases]: https://github.com/broglep/homeassistant-meshtastic/releases
