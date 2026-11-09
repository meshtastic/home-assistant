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

[Ruff](https://docs.astral.sh/ruff/) is the formatter and linter, pinned to 0.15.15 in `requirements.txt`.
`scripts/setup` installs the requirements and `scripts/lint` formats and fixes what it can. CI runs
`ruff check .` and `ruff format --check .` on every pull request, next to the HACS and hassfest
validations.

## Tests

`tests/` holds pytest tests for the TCP proxy and the channel handling, using
`pytest-homeassistant-custom-component` from `requirements.test.txt`. CI does not run them, so run them
before you open a pull request that touches that code:

```sh
python3 -m pip install -r requirements.txt -r requirements.test.txt
python3 -m pytest tests
```

The repo also ships a VS Code dev container that starts a standalone Home Assistant with
[`config/configuration.yaml`](./config/configuration.yaml) (`scripts/develop`), which is the quickest way
to try a change against a real radio.

## Protobufs

The generated protobuf code under `custom_components/meshtastic/aiomeshtastic/protobuf/` comes from the
`protobufs` submodule. To move to a new version, check out its tag in the submodule and run
`scripts/generate_protobufs`. The script needs:

- `protoc` with Python gencode that matches the `protobuf` pin in `requirements.test.txt`, for example
  from `grpcio-tools` 1.68 (`python -m grpc_tools.protoc`),
- `protoc-gen-mypy` from `mypy-protobuf` 3.6 (newer releases need protobuf 6),
- GNU sed, as `sed` or `gsed`.

Commit the submodule pointer together with the regenerated files.

## License

Each source file carries an SPDX header naming its license, mostly MIT, with the texts in `LICENSES/`.
The repository as a whole is distributed under the GPL-3.0 in the root [`LICENSE`](LICENSE), like the
rest of the Meshtastic organization. Pull requests need the Meshtastic
Contributor License Agreement signed; the CLA assistant check on the pull request links to it.
