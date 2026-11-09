<!--
SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
SPDX-FileCopyrightText: 2025 Ylian Saint-Hilaire @ylianst

SPDX-License-Identifier: MIT
-->

# Home-Assistant Meshtastic Integration

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

[![Marketplace and HACS](https://img.shields.io/badge/Marketplace%20%7C%20HACS-Custom-orange.svg?style=for-the-badge)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshtastic&repository=home-assistant&category=integration)
[![HA Analytics](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.meshtastic.total&style=for-the-badge&label=Active%20Installations&color=red)](https://analytics.home-assistant.io/custom_integrations.json)


_Home Assistant Integration for [Meshtastic](https://www.meshtastic.org)._

Supported Features:
 * Add meshtastic devices as gateways to interact with the mesh
   * Supports TCP, Serial & Bluetooth connection (also works with [Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html))
   * Home Assistant Zeroconf, Bluetooth & USB-Serial Auto-Discovery 
   * Select which meshtastic nodes should be imported to home assistant
 * Meshtastic node metrics
 * Record received messages
 * Send messages (as direct message or on broadcast channel)
 * Record node position (as device tracker)
 * Device triggers & actions for automations
 * Various other service actions (e.g. request metrics, trace route)
 * Bundled meshtastic web client for manual interaction with gateway
 * MQTT client proxy support (forwards messages from radio to MQTT broker, and broker messages for channels with downlink enabled back to the radio)

For more details, see check the [documentation](#documentation).

## Installation

### Recommended: Marketplace or HACS

Home Assistant 2026.11 and newer include the Marketplace (**Settings** > **Marketplace**), which replaces HACS. On 2026.10 and older, use [HACS](https://www.hacs.xyz). The minimum Home Assistant version is 2026.8.0.

1. Add this repository as a custom repository. On 2026.11 and newer, open the Marketplace, select **Add from link** and enter `meshtastic/home-assistant`, or use the Marketplace link: [![Open the repository in the Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=meshtastic&repository=home-assistant&category=integration) On any version, this link opens the Marketplace (2026.11 and newer) or HACS (older): [![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshtastic&repository=home-assistant&category=integration)
2. Install the integration from the Marketplace or HACS.
3. Restart Home Assistant.
4. Set up the integration using the UI: [![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshtastic)

### Alternatives
<details>
<summary>Alternatives</summary>

### Manual
1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `homeassistant-meshtastic`.
4. Download _all_ the files from the `custom_components/meshtastic/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant
7. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Meshtastic"
</details>

### Configuration is done in the UI

To change how a gateway is reached (a new IP address, serial port or Bluetooth address), open the integration, choose the entry's menu and select **Reconfigure**. Devices, entities and automations are kept. The new connection must reach the same radio: it is matched by public key, or by node number when a key is missing, and a different radio is rejected. A radio accepts one API client at a time, so a new address on the same radio can only be validated once the old connection is gone.

<!---->

# Documentation

## Supported devices

A gateway is any Meshtastic radio the integration can reach with the phone API, over one of three connections:

| Connection | Reaches |
| --- | --- |
| TCP | Radios with Wi-Fi or Ethernet (ESP32 and similar), and `meshtasticd` |
| Bluetooth LE | Any radio with Bluetooth, directly or through a [Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) |
| Serial (USB) | Radios on a USB port of the Home Assistant host, including nRF52 boards that have no TCP |

USB auto-discovery matches the common nRF52, ESP32, RP2040 and CH340 based radios. Other radios are added by entering the serial port by hand. Firmware 2.8 renumbering is handled, see [Node numbers on firmware 2.8](#node-numbers-on-firmware-28). The minimum Home Assistant version is 2026.8.0.


## Supported Platforms
### Sensor / Binary Sensor
For nodes selected as part of the configuration, metrics are exposed as corresponding sensors.

The integration is classified as `local_push`: Home Assistant is notified as soon as new state is available.
Polling is only a fallback should the push mechanism stop working.

### [Notification](https://www.home-assistant.io/integrations/notify/)
Using notification platform and the generated `notify.mesh_*` entities is the recommended way to send messages to the mesh when you don't need control over all the details 
(which gateway to use, acknowledgements, etc.)

Depending on your needs, all nodes from the gateway's node database as well as the channels can be made available as notification targets.


Note: Support is based on new [entity notification platform](https://developers.home-assistant.io/blog/2024/04/10/new-notify-entity-platform/), 
for an example on how to use it, see [official documentation](https://www.home-assistant.io/integrations/notify/#example-with-the-entity-platform-notify-action).

### [Automation](https://www.home-assistant.io/docs/automation/)

For nodes selected as part of the configuration, device triggers and device actions are available.
Prefer using device actions and triggers in your automation over using (service) actions.

#### Triggers
There are triggers when messages have been sent or have been received.
Gateway nodes offer more triggers than other nodes. You can further refine the triggers to only fire when a message was 
sent/received on a particular channel or as direct message.

For anything the device triggers don't cover, use the [events](#events) below with a "Manual Events" trigger.

#### Actions
 * Send Direct Message
 * Request Telemetry
 * Request Position

Note: Ensure you have a delay before the action if your automations are triggered by meshtastic device trigger,
otherwise you risk that your action triggered message is being dropped by meshtastic firmware because
it is still busy with receiving / sending other mesh messages.

#### Events

Every gateway fires these events on the Home Assistant event bus. All of them share one envelope:

```yaml
config_entry_id: 01J...   # the gateway's config entry
node: 1127918844          # node number the event is about (the sender for packets)
data: {...}               # event specific, see below
```

Packet based events also carry `message_id`, the mesh packet id. Inside `data`, `to` is
`{node: <number>, channel: null}` for a direct message and `{node: null, channel: <index>}` for a
channel broadcast; the channel index is the gateway's own.

| Event | Fired when | `data` |
| --- | --- | --- |
| `meshtastic_api_text_message` | a text message is received | `from`, `to`, `gateway`, `message`, `hop_start`, `hop_limit`, `rx_snr`, `rx_rssi`, `via_mqtt`, `xeddsa_signed`, `reply_id`, `is_reaction` |
| `meshtastic_api_text_message_out` | the gateway sends a text message | `from` (the gateway), `to`, `gateway`, `message`, `reply_id`, `is_reaction` |
| `meshtastic_api_detection_sensor` | a detection sensor packet is received | `from`, `to`, `gateway`, `message` (the sensor text) |
| `meshtastic_api_mesh_beacon` | a Mesh Beacon is received | `from`, `to`, `gateway`, `beacon`, `hop_start`, `hop_limit`, `rx_snr`, `rx_rssi` |
| `meshtastic_api_waypoint` | a waypoint is received | `from`, `to`, `gateway`, `waypoint` |
| `meshtastic_api_position` | a position is received | the decoded `Position`, plus `latitude` and `longitude` in degrees |
| `meshtastic_api_telemetry` | telemetry is received | the metrics group; the envelope adds `telemetry_type` (`device_metrics`, `local_stats`, `environment_metrics`, `power_metrics` or `host_metrics`) |
| `meshtastic_api_node_updated` | a node announces itself (NodeInfo) | `num` and `user` (`id`, `longName`, `shortName`, `hwModel`, `publicKey` and so on) |
| `meshtastic_api_packet` | any packet reaches the gateway | the whole decoded `MeshPacket` |

`position` and `telemetry` events also put `node_info: {name: <long name>}` in the envelope.
Decoded protobuf messages use the protobuf JSON names (`longName`, `geofenceRadius`), so fields left at
their default are omitted.

On text messages, `hop_start` minus `hop_limit` is the number of hops the message took, when the sender
set `hop_start` (it is 0 otherwise). `rx_snr` and `rx_rssi` are as measured by the gateway, `via_mqtt` is true
when the message came in over MQTT, and `xeddsa_signed` is true only when firmware verified the sender's
signature. `reply_id` is the `message_id` the message replies to, or empty, and `is_reaction` is true when
the message is an emoji reaction to that message. `beacon` holds `message` and, when offered, `offerChannel`, `offerRegion`, `offerPreset` and
`offerFrequencySlot`.

#### Examples
<details>
<summary>Reply back after message received from predefined device on arbitrary channel (including direct message) </summary>

```yaml
- id: '1800000042000'
  alias: Ping Sample
  description: 'Reply back after message from device'
  triggers:
  - device_id: e3376b45b4912c27cffb46c58e4998e4
    domain: meshtastic
    type: message.sent
    trigger: device
  actions:
  - delay:
      seconds: 10
  - device_id: e3376b45b4912c27cffb46c58e4998e4
    domain: meshtastic
    type: send_message
    message: PONG {{ trigger.event.data.message }}
```

</details>

<details>
<summary>Echo incoming channel text messages from any node with gateway device trigger</summary>

```yaml
- id: '1735857524502'
  alias: Echo Channel Message
  description: ''
  triggers:
  - domain: meshtastic
    device_id: 16efde6990a6a09903153abb8624fe38
    type: channel_message.received
    entity_id: meshtastic.gateway_brig_channel_primary
    trigger: device
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.broadcast_channel_message
    metadata: {}
    data:
      ack: true
      channel: meshtastic.gateway_brig_channel_primary
      message: 'ECHO: {{ trigger.event.data.message }}'
  mode: single
```
</details>

<details>
<summary>Advanced: Handling incoming text messages from any node without notification platform and its entities</summary>

```yaml
- id: '1735852176270'
  alias: Echo on Channel Message (without Notify Platform)
  description: 'Only from gateway with node id 3771721320 and channel 0'
  triggers:
  - trigger: event
    event_type: meshtastic_api_text_message
    event_data:
      data:
        to:
          node:
          channel: 0
        gateway: 3771721320
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.send_text
    data:
      ack: true
      text: 'ECHO: {{ trigger.event.data.data.message }}'
      from: '{{ trigger.event.data.data.gateway }}'
      channel: '{{ trigger.event.data.data.to.channel }}'
  mode: single
- id: '1735852176271'
  alias: Echo on Direct Message (without Notify Platform)
  description: 'Only from gateway with node id 3771721320'
  triggers:
  - trigger: event
    event_type: meshtastic_api_text_message
    event_data:
      data:
        to:
          node: 3771721320
          channel:
        gateway: 3771721320
  conditions: []
  actions:
  - delay:
      seconds: 5
  - action: meshtastic.send_text
    data:
      ack: true
      text: 'ECHO: {{ trigger.event.data.data.message }}'
      from: '{{ trigger.event.data.data.gateway }}'
      to: '{{ trigger.event.data.data.from }}'
  mode: single
```

If you don't want to use the recommend notification platform for sending messages (e.g. if you don't want to clutter your Home Assistant instance with potentially hundreds of notify mesh entities), 
you can still handle incoming text messages from any public node and reply to these messages. 
This is useful if to want to reply to incoming direct messages with a standard message, use a LLM or handle various commands with automations.

To do this, create a new Home Assistant automation that triggers on "Manual Events" and put `meshtastic_api_text_message` as the "Event Type". This will cause this automation to get triggerred on all incoming channel and direct messages. You will get events that include this information:

```yaml
trigger:
  event:
    event_type: meshtastic_api_text_message
    event_data:
      data:
        from: 1127918844
        to:
          node: null
          channel: 0
        gateway: 862525748
        message: Sample Message
        hop_start: 3
        hop_limit: 2
        rx_snr: 6.25
        rx_rssi: -71
        via_mqtt: false
        xeddsa_signed: true
        reply_id: null
        is_reaction: false
      message_id: 2834221771
```

From contains the node id of the sender of the message, to will have the node id of the gateway for direct messages, or a gateway channel id if the message is directed at the channel. 
Note that the channel id is dependent on the gateway node, so make sure you are using the proper gateway node when replying using that channel id. 

You can create conditions in the automation to filter out the incoming messages you want or you can directly filter in the trigger.
For example to filter out messages addressed to your gateway node, use this condition with your node id.

```
{{ trigger.event.data.data.to.node == 862525748 }}
```

To filter out messages addresses at the primary channel (Channel 0 is typically LONGFAST), use this condition:

```
{{ trigger.event.data.data.to.channel == 0 }}
```

You can also forward these messages as notifications to your phone, etc. For example:

```
Meshtastic message from ({{ trigger.event.data.data.from }}): {{ trigger.event.data.data.message }}
```

To reply to a text message in this situation, add a 2 second or more delay action and then an action called `Meshtastic 'Send Text'` to your automation. You need to add a short delay to make sure your Meshtastic device is idle before replying. Change the `Meshtastic 'Send Text'` action to edit in yaml and change the `to`, `from` and `text` values to something like his:

```
action: meshtastic.send_text
metadata: {}
data:
  ack: false
  from: "{{ trigger.event.data.data.gateway }}"
  to: "{{ trigger.event.data.data.from }}"
  text: "ECHO: {{ trigger.event.data.data.message }}"
```

In the example above, we echo back an incoming direct message.

</details>

<details>
<summary>Waypoint events (including geofences)</summary>

Waypoints received by your gateway fire a `meshtastic_api_waypoint` event. Meshtastic 2.8 waypoints can carry a geofence: a circle (`geofenceRadius` in meters around the waypoint position), a `boundingBox` (degrees scaled by 1e-7), or both.

```yaml
trigger:
  event:
    event_type: meshtastic_api_waypoint
    event_data:
      data:
        from: 1127918844
        to:
          node: null
          channel: 0
        gateway: 862525748
        waypoint:
          id: 1234
          name: Campsite
          description: ""
          icon: 0
          expire: 0
          lockedTo: 0
          latitudeI: 407128000
          longitudeI: -740060000
          latitude: 40.7128
          longitude: -74.006
          geofenceRadius: 250
          boundingBox:
            longitudeWestI: -740100000
            latitudeSouthI: 407100000
            longitudeEastI: -740000000
            latitudeNorthI: 407200000
          notifyOnEnter: true
          notifyOnExit: false
          notifyFavoritesOnly: false
```

`boundingBox`, `latitudeI` and `longitudeI` are only present when set. The `notify*` flags are carried as sent; a receiving device keeps its own enter/exit preferences and ignores the flags on waypoints it gets from the mesh. Enter and exit are evaluated by the device and are not sent as events, so this integration does not report them.

</details>

### [Logbook](https://www.home-assistant.io/integrations/logbook/)

Direct messages or channel messages are recorded in the log book. 
Each gateway has an entity for direct messages and its channels, you can navigate to the device and select the entitiy
to see an extract of the logbook, or you can navigate to the logbook and filter for the desired entities there. 

Note: When logbook is not enabled, messages are not recorded.

### [Device Tracker](https://www.home-assistant.io/integrations/device_tracker/)

For nodes selected as part of the configuration, their position is exposed as a device tracker.
You can see the nodes on home assistant map accordingly and home assistant will report if 
node is at home or away.

### Node numbers on firmware 2.8

Firmware 2.8 derives a node's number from its public key, so upgrading a node from 2.7 gives it a new
number. On startup the integration finds selected nodes that moved: a peer is followed when the gateway's
node database holds the same public key at the new number, and the gateway is followed through its own
connection. Devices and entities move to the new number in place, so entity ids and the automations that
use them keep working. The integration also stores each selected node's key, so a later move is still
followed after the gateway forgets the old number.

A node whose key changes as well, through a full device reset, a flash erase or a key regenerate or
import, is a new identity. Nothing links it to the old one, so it shows up as a new node to select.

### Services / [Actions](https://developers.home-assistant.io/blog/2024/07/16/service-actions/)

Use the available actions if you need more control compared to other methods to interact with
meshtastic devices. Certain actions need you to understand meshtastic details and are not recommended 
for the average user. 

## [Meshtastic Web Client](https://meshtastic.org/docs/software/web-client/)

In order to enable manual interaction with gateway nodes and because of the limitation of meshtastic firmware
that only allows one single connection, this integration offers a workaround by exposing a meshtastic client http api
for each configured gateway. The bundled meshtastic web client can then be connected
to this integration directly and the integration will act as a proxy.

This also has the added benefit that it allows you to connect to meshtastic devices via the web client
that are not connected via TCP (like serial or bluetooth) or don't support TCP at all (e.g. nRF based nodes).

**Security Note**: As a side effect, enabling this feature results in unauthenticated access to your gateway nodes for 
anyone that can reach your home assistant instance (because meshtastic does not support authentication on the http api).
Make sure to only use this feature if your home assistant instance is running in a trusted environment!

To access the web client, perform the following steps:

In Home Assistant:
1. Enable the feature in the integration configuration
2. Navigate to the "Meshtastic" menu item. If you don't see it, reload home assistant interface
3. Press the "Open" button of the desired gateway to launch the web client

Inside the Meshtastic Web Client:

4. Press "New Connection" - the correct hostname is already populated
5. Press "Connect"

## Known limitations

- A radio serves one phone-API client at a time. The Meshtastic phone apps and other tools can only connect while this integration is not using the radio, unless you enable the [web client](#meshtastic-web-client) or the TCP proxy and connect through it.
- Messages are sent and received through the gateway radio only. Nodes the gateway does not hear are not reachable.
- Waypoint geofence enter and exit are evaluated by the device and are not reported as events.
- The web client has no authentication, see its security note above.

## Troubleshooting

- **Gateway keeps disconnecting or fails to connect:** another client, such as a phone app or script, holds the radio's single API connection. Close it, or share the connection through the TCP proxy or web client.
- **Node moved or shows twice after a firmware upgrade:** see [Node numbers on firmware 2.8](#node-numbers-on-firmware-28). A reset or key change creates a new node.
- **Different address for the same radio:** use **Reconfigure** on the entry, see above.
- **Logs:** Home Assistant writes them to **Settings** > **System** > **Logs**. Turn on debug logging for the integration and its libraries in `configuration.yaml`, then restart:

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.meshtastic: debug
      serialx: debug
      aiomqtt: debug
      bleak: debug
  ```

- **Diagnostics:** download them from the gateway entry, see below.

## Reporting a bug

Attach the [diagnostics](https://www.home-assistant.io/integrations/diagnostics/) file (Settings -> Devices & services -> Meshtastic -> the three dots on the gateway entry -> Download diagnostics) together with debug logs. It holds the gateway firmware and hardware, the node and module config, the channel names and roles, and the data of the selected nodes. Channel keys, node keys, MQTT and Wi-Fi credentials, MAC addresses, hosts and positions are redacted; check the file before you post it.

## Removal

1. Go to **Settings** > **Devices & services** > **Meshtastic**, open the menu of each gateway entry and select **Delete**. This removes its devices and entities and disconnects from the radio.
2. Uninstall the integration: **Settings** > **Marketplace** on 2026.11 and newer, or HACS on older versions. Restart Home Assistant. Without the Marketplace or HACS, delete the `custom_components/meshtastic` folder and restart.
3. Remove any `meshtastic` entries you added to `logger:` in `configuration.yaml`.

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

***

[commits-shield]: https://img.shields.io/github/commit-activity/y/meshtastic/home-assistant.svg?style=for-the-badge
[commits]: https://github.com/meshtastic/home-assistant/commits/main
[license-shield]: https://img.shields.io/github/license/meshtastic/home-assistant.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/meshtastic/home-assistant.svg?style=for-the-badge
[releases]: https://github.com/meshtastic/home-assistant/releases

