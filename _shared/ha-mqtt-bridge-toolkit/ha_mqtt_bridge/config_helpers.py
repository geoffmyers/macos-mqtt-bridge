"""YAML + ${ENV_VAR} configuration helpers for MQTT bridges.

Small, framework-free utilities for bridges that use the YAML-with-env
config pattern (others use flat environment variables). Imports ``yaml`` lazily so the toolkit's core publisher /
discovery helpers don't pull in PyYAML for bridges that don't need it.

``substitute_env_vars`` is the only piece that's also useful outside
the YAML loader — bridges that want to substitute env vars in any
text blob (Docker secrets pattern, dotenv-like config strings) can
use it directly.

Pydantic mixins were considered and intentionally NOT included —
forcing a pydantic dependency on every bridge (including the ones that
don't use it) outweighed the small win
for any future bridge that picks up the pattern. The two utilities
here are framework-free; a future bridge can wrap them in whatever
config layer fits its needs (pydantic, dataclasses, plain dict).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def substitute_env_vars(text: str) -> str:
    """Replace ``${VAR}`` placeholders in ``text`` with values from
    ``os.environ``.

    Only matches ``${UPPER_SNAKE}`` — by design, to avoid eating shell
    constructs like ``${1}``, ``${foo:-bar}``, or ``${PWD}/foo`` in
    file paths. Raises ``KeyError`` on the first unset variable, so a
    missing required env value fails loudly at config-load time
    instead of silently producing a corrupt config string.

    ``$VAR`` (no braces) is intentionally NOT substituted — the
    explicit-braces requirement keeps the substitution rule simple
    and removes ambiguity around dollar signs in passwords / regex.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise KeyError(f"environment variable {name} is not set")
        return os.environ[name]

    return _ENV_RE.sub(repl, text)


def load_yaml_with_env(path: str | Path) -> Any:
    """Read a YAML file, expand ``${ENV_VAR}`` placeholders, and parse.

    Returns whatever ``yaml.safe_load`` returns — typically a dict for
    a top-level mapping config, but a list / scalar is also valid YAML.

    Imports ``yaml`` lazily so the toolkit's core helpers stay
    dependency-free for bridges that don't need YAML. Raises
    ``ImportError`` with a clear message if PyYAML isn't installed.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "load_yaml_with_env requires PyYAML — install with "
            "`pip install pyyaml` or `pip install "
            "'ha-mqtt-bridge-toolkit[yaml]'`."
        ) from e

    raw = Path(path).read_text()
    raw = substitute_env_vars(raw)
    return yaml.safe_load(raw)
