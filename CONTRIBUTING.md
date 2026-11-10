# Contribution guidelines

Bug reports, fixes, discussion and feature proposals all go through GitHub.

## Pull requests

1. Fork the repo and create your branch from `main`.
2. If you've changed something, update the documentation, including the event table in the README.
3. Make sure your code lints (see below).
4. Test your contribution.
5. Open the pull request. Titles follow [Conventional Commits](https://www.conventionalcommits.org/)
   (`fix: ...`, `feat: ...`, `chore: ...`), and pull requests are squash-merged.

## Bug reports

Report a bug by [opening a new issue](../../issues/new/choose). A useful report has:

- a short summary and background,
- specific steps to reproduce, with sample automation YAML where it helps,
- what you expected and what happened instead,
- the integration, Home Assistant and firmware versions, and debug logs.

## Code style

[Ruff](https://docs.astral.sh/ruff/) is the formatter and linter, pinned in the `dev`
dependency group of `pyproject.toml`, which [uv](https://docs.astral.sh/uv/) manages (`uv.lock`).
`scripts/setup` runs `uv sync --group dev --group test` and `scripts/lint` formats and fixes what it can.
CI runs `ruff check .` and `ruff format --check .` on every pull request, next to the HACS and hassfest
validations. Runtime requirements stay in `custom_components/meshtastic/manifest.json`.

## Tests

`tests/` holds pytest tests for the TCP proxy and the channel handling, using
`pytest-homeassistant-custom-component` from the `test` dependency group. CI runs them on every pull
request; to run them locally:

```sh
uv run python -m pytest tests
```

The repo also ships a VS Code dev container that starts a standalone Home Assistant with
[`config/configuration.yaml`](./config/configuration.yaml) (`scripts/develop`), which is the quickest way
to try a change against a real radio.

## Protobufs

The generated protobuf code under `custom_components/meshtastic/aiomeshtastic/protobuf/` comes from the
`protobufs` submodule. To move to a new version, check out its tag in the submodule and run
`scripts/generate_protobufs`. The script needs:

- `protoc` and `protoc-gen-mypy`, from the `protobuf` dependency group: `grpcio-tools` 1.68.1
  (`python -m grpc_tools.protoc`, with gencode that matches the `protobuf` pin in the `test` group) and
  `mypy-protobuf` 3.6.0 (newer releases need protobuf 6). The group conflicts with `dev` and `test`
  (protobuf 5 against 6), so run it alone:
  `uv run --no-default-groups --group protobuf bash scripts/generate_protobufs`,
- GNU sed, as `sed` or `gsed`.

Commit the submodule pointer together with the regenerated files.

## License

Each source file carries an SPDX header naming its license, mostly MIT, with the texts in `LICENSES/`.
The repository as a whole is distributed under the GPL-3.0 in the root [`LICENSE`](LICENSE), like the
rest of the Meshtastic organization. Pull requests need the Meshtastic
Contributor License Agreement signed; the CLA assistant check on the pull request links to it.
