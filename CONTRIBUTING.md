# Contributing to macOS MQTT Bridge

Thanks for taking an interest. This project is developed inside a private
mono repo and published here as a snapshot, which shapes a couple of the
rules below — please read the last section before opening a PR.

## Getting set up

**Stack:** Python 3.12+, [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
git clone https://github.com/geoffmyers/macos-mqtt-bridge.git
cd macos-mqtt-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -e "_shared/ha-mqtt-bridge-toolkit[yaml]"
pip install -e "_shared/python-github-error-reporter"
pip install -e ".[dev]"
pytest
```

The whole test suite runs on any platform, including the Messages/Calls/
Voicemail/AddressBook source tests — they run against synthetic fixtures
built by `tests/fixtures/build_*_fixture.py` (fictional names, NANP
555-01xx numbers, `example.com` addresses) by default, so you do not need a
Mac, or anyone's real data, to contribute. If you stage a real macOS
Messages/Calls/AddressBook snapshot under `tmp/` (gitignored), those same
tests run against it instead, as an optional extra layer closer to
production data.

## Checks

<!-- CHECKS:START -->
Every push and pull request runs these checks in GitHub Actions
([`.github/workflows/checks.yml`](.github/workflows/checks.yml)), and every release has passed them.
To run one yourself, use the same commands from the directory shown.

**test** (Python 3.12, from the repository root):

```bash
python -m venv /tmp/venv
/tmp/venv/bin/pip install -q -e "./_shared/ha-mqtt-bridge-toolkit[yaml]"
/tmp/venv/bin/pip install -q -e "./_shared/python-github-error-reporter"
/tmp/venv/bin/pip install -q -e ".[dev]"
/tmp/venv/bin/python -m pytest -q
```

<!-- CHECKS:END -->

## Before you open a pull request

- Keep the change focused. One concern per PR is much easier to review.
- Match the surrounding style rather than introducing a new one. There is no
  separate style guide; `ruff` (configured in `pyproject.toml`) is the closest
  thing to one.
- Update the README if you change a published MQTT topic, a Home Assistant
  entity, or `config.example.yaml`.
- Explain **why** in the commit message, not just what. The diff already says
  what changed.

## Reporting a bug

Open an issue with what you did, what you expected, and what happened
instead. macOS version, Python version, and the exact log line (from
`~/Library/Logs/macos-mqtt-bridge*.log`) help more than anything else. If it
is a crash, include the full traceback rather than a summary of it.

## Security

Please do **not** open a public issue for a security problem. Report it
privately through GitHub's *Report a vulnerability* button on the Security tab.

## How this repo is published

This project lives in a private mono repo. Each publish adds **one commit** on
top of the history here, so the history grows with every release, but one
commit here can stand for many upstream changes. Two consequences:

- Pull requests are reviewed here and applied upstream, then arrive back in the
  next published commit, which credits your authorship in its message. The pull
  request is closed with a link to that commit rather than merged, because the
  next publish is built from the upstream tree and would undo a change made
  only here.
- Operator configuration (`*.tpl` and similar) is deliberately excluded from
  the snapshot. If a config file looks missing, look for the matching
  `.example` file instead.

## Licence

By contributing you agree that your contribution is licensed under the same
terms as this project — see [LICENSE.md](LICENSE.md).
