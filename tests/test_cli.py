"""CLI argparse tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from macos_bridge import cli


def test_parse_args_run():
    args = cli._parse_args(["run", "--config", "/etc/foo.yaml"])
    assert args.command == "run"
    assert args.config == Path("/etc/foo.yaml")


def test_parse_args_init_state():
    args = cli._parse_args(["init-state", "--config", "/etc/foo.yaml"])
    assert args.command == "init-state"


def test_parse_args_dump_once():
    args = cli._parse_args(["dump-once", "--config", "/etc/foo.yaml"])
    assert args.command == "dump-once"


def test_parse_args_bootstrap_allowlist_defaults():
    args = cli._parse_args(["bootstrap-allowlist", "--config", "/etc/foo.yaml"])
    assert args.command == "bootstrap-allowlist"
    assert args.top == 20
    assert args.days == 30


def test_parse_args_bootstrap_allowlist_custom_top_days():
    args = cli._parse_args([
        "bootstrap-allowlist", "--config", "/etc/foo.yaml",
        "--top", "5", "--days", "7",
    ])
    assert args.top == 5
    assert args.days == 7


def test_parse_args_unknown_subcommand_rejected():
    with pytest.raises(SystemExit):
        cli._parse_args(["never-heard-of-it", "--config", "/etc/foo.yaml"])
