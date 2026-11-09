# AGENTS.md

Working notes for the Meshtastic Home Assistant integration, a HACS custom integration that turns
Meshtastic radios into Home Assistant gateways. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, the
gates and the license; [README.md](README.md) covers what users see, including every bus event. This
file is about the code.

## The shape of it

One config entry is one gateway: a radio reached over TCP, BLE or serial. Everything else hangs off the
client that entry owns.

```
__init__.py   async_setup_entry ──▶ MeshtasticApiClient.connect ──▶ follow renumbered nodes
                                     ──▶ platforms ──▶ devices ──▶ services ──▶ web client / TCP proxy
api.py        MeshtasticApiClient: packet listeners ──▶ HA bus events, send_text, node DB access
aiomeshtastic/ MeshInterface (interface.py) ◀── ClientApiConnection (connection/: tcp, bluetooth, serial)
              Packet (packet.py) decodes app payloads by port number
coordinator.py the node database as coordinator data, keyed by node number
```

| module | holds |
| --- | --- |
| `aiomeshtastic/` | the integration's own asyncio Meshtastic client: transports, config handshake, node DB, packet decode. It does not use the `meshtastic` PyPI package |
| `aiomeshtastic/protobuf/` | generated from the `protobufs` submodule by `scripts/generate_protobufs`; never edit by hand |
| `api.py` | the bridge from the client to Home Assistant: one listener per port number, each firing a `meshtastic_api_*` event |
| `coordinator.py` | `DataUpdateCoordinator` over the node DB; mostly pushed, the 1 hour poll is a fallback |
| `entity.py` | base entities and the unique id builders |
| `sensor.py`, `binary_sensor.py`, `device_tracker.py`, `notify.py`, `text.py` | the platforms |
| `device_trigger.py`, `device_action.py`, `logbook.py`, `services.py` | automation surface |
| `renumber.py` | following nodes whose number changed (firmware 2.8) |
| `meshtastic_tcp/` | TCP proxy so other clients share the gateway's single API connection |
| `meshtastic_web/`, `frontend.py`, `ha_frontend/` | the bundled Meshtastic web client and its sidebar panel |
| `config_flow.py` | connection, node selection, notify, web client and proxy steps, plus the options flow |

## Invariants worth knowing before you change anything

- **A radio serves one phone-API client at a time.** meshtasticd and most firmware drop the previous TCP
  client when a new one connects. The TCP proxy and web client exist so other tools can share the
  gateway's connection, and a client left connected after a failed setup fights the next one in a
  reconnect loop, which is why `async_setup_entry` disconnects on any failure after `connect`.
- **Node numbers are the keys.** Device identifiers are `(DOMAIN, str(node_num))`, entity unique ids embed
  the number as an `_`-separated token (`{entry_id}_{platform}_{node}_{key}`,
  `{entry_id}_gateway_{node}`, `{entry_id}_channel_{node}_{index}`, `meshtastic_node_{node}`), and the
  options store selected nodes as `{"id", "name", "public_key"}`.
- **Firmware 2.8 renumbers nodes** to `crc32(public_key)`. `_async_follow_renumbered_nodes` runs before
  the platforms on every setup: a peer moves only when the node DB holds its key at that canonical
  number, the gateway moves by connection continuity, and identifiers and unique ids are rewritten in
  place so entity ids survive. Key it on the public key for peers only, never for the gateway.
- **Node data is `MessageToDict` output**, so keys are protobuf JSON names (`longName`, `hwModel`,
  `environmentMetrics`) and fields at their default are absent. `hwModel` is missing for `UNSET` nodes,
  `0` readings vanish, and a wrong key creates no sensor and raises no error. Check new keys against the
  generated descriptors.
- **Events share one envelope** built by `_build_event_data`: `config_entry_id`, `node`, `data`, plus
  `message_id` on packet events. The README event table is the contract; update it with any change.
- **Generated protobufs are vendored.** After a submodule bump, regenerate, then compare old and new
  descriptors for removed or renamed fields before merging; v2.8 protos qualify custom options with the
  package, which the generator rewrites.

## Commands

```sh
scripts/setup                 # uv sync --group dev --group test (Home Assistant 2026.8, ruff 0.15.15)
scripts/lint                  # ruff format . && ruff check . --fix
uv run ruff check . && uv run ruff format --check .   # what CI runs
uv run python -m pytest tests # the tests
uv run python -m pytest tests/test_tcp_proxy.py -k disconnect   # one test
scripts/develop               # standalone Home Assistant on config/configuration.yaml
uv run --no-default-groups --group protobuf bash scripts/generate_protobufs   # see CONTRIBUTING.md for GNU sed
scripts/build_web_client vX.Y.Z   # rebuild the bundled web client from a meshtastic/web tag with scripts/web-client/patches
```

`hacs.json` sets the minimum Home Assistant version (2026.8.0), which the `dev` group in `pyproject.toml` pins for
development; the code also runs on current releases, so check deprecations against both. Devices are
per config entry from 2026.8, so look them up with `async_get_device_by_identifier(identifier, entry_id)`.

## CI and merging

- CI is `lint.yml` (ruff), `tests.yml` (pytest on Python 3.14, both through uv), `validate.yml` (HACS and hassfest,
  also nightly) and `release.yml` (zip on release).
- Fork pull requests from first-time contributors wait for a maintainer to approve their workflow runs.
- The CLA assistant check must pass. Pull requests are squash-merged with Conventional titles.
- Renovate opens the dependency bumps (`.github/renovate.json`). A `homeassistant` bump waits for approval on
  the dependency dashboard issue; the pins taken from that release (`pytest-homeassistant-custom-component`,
  `protobuf`, `josepy`, `bleak`) are never bumped by Renovate and move by hand with it.
