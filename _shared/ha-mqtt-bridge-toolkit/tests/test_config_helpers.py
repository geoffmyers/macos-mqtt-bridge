"""Tests for ha_mqtt_bridge.config_helpers.

Pins the contract a bridge's ``load_config`` relied on before the
helper was extracted: only ``${UPPER_SNAKE}`` matches; missing env vars
raise ``KeyError`` at load time; ``$VAR`` (no braces) and lowercase
forms pass through untouched.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from ha_mqtt_bridge.config_helpers import (
    load_yaml_with_env,
    substitute_env_vars,
)


class TestSubstituteEnvVars:
    def test_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOO", "bar")
        assert substitute_env_vars("x=${FOO}") == "x=bar"

    def test_multiple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert substitute_env_vars("${A}-${B}-${A}") == "1-2-1"

    def test_missing_var_raises_keyerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MISSING_THINGY", raising=False)
        with pytest.raises(KeyError, match="MISSING_THINGY"):
            substitute_env_vars("x=${MISSING_THINGY}")

    def test_no_braces_passes_through(self) -> None:
        # ``$FOO`` is NOT substituted — the explicit-braces rule keeps
        # the substitution surface tight (passwords with $ chars, regex
        # patterns, etc. should not be munged).
        assert substitute_env_vars("price = $5") == "price = $5"
        assert substitute_env_vars("hello $FOO world") == "hello $FOO world"

    def test_lowercase_passes_through(self) -> None:
        # ``${foo}`` doesn't match — UPPER_SNAKE only.
        assert substitute_env_vars("${foo}") == "${foo}"

    def test_mixed_case_passes_through(self) -> None:
        assert substitute_env_vars("${Foo}") == "${Foo}"

    def test_var_starting_with_digit_passes_through(self) -> None:
        # ${1} (positional arg syntax) does not match the UPPER_SNAKE
        # rule because the first char must be letter or underscore.
        assert substitute_env_vars("${1}") == "${1}"

    def test_var_with_digit_in_middle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_VAR_2", "ok")
        assert substitute_env_vars("${MY_VAR_2}") == "ok"

    def test_underscore_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_PRIVATE", "x")
        assert substitute_env_vars("${_PRIVATE}") == "x"

    def test_empty_string_passthrough(self) -> None:
        assert substitute_env_vars("") == ""

    def test_no_placeholders_passthrough(self) -> None:
        assert substitute_env_vars("plain text\nno vars") == "plain text\nno vars"


class TestLoadYamlWithEnv:
    def test_loads_and_substitutes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOMENAME", "kitchen")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            dedent(
                """
                mqtt:
                  host: broker.lan
                  topic: sensors/${HOMENAME}/state
                """
            )
        )
        data = load_yaml_with_env(cfg)
        assert data == {
            "mqtt": {
                "host": "broker.lan",
                "topic": "sensors/kitchen/state",
            }
        }

    def test_missing_var_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("x: ${NOPE_NOT_SET}\n")
        with pytest.raises(KeyError, match="NOPE_NOT_SET"):
            load_yaml_with_env(cfg)

    def test_accepts_str_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("V", "ok")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("v: ${V}\n")
        # Str path also works (matches the Pathlib API contract).
        assert load_yaml_with_env(str(cfg)) == {"v": "ok"}
