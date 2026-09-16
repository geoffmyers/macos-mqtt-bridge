"""CLI entry point for the merged macos-mqtt-bridge daemon.

Subcommands:

  run                   start the long-lived daemon
  init-state            prime comms source state to current max IDs (so the
                        first poll doesn't replay history)
  dump-once             single dry-run poll, print events to stderr, no MQTT
  bootstrap-allowlist   print top-N user-facing apps from knowledgeC.db as
                        a YAML block to paste under per_app.apps
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from macos_bridge.bootstrap import bootstrap_allowlist, emit_yaml
from macos_bridge.config import load_config, load_config_with_credentials
from macos_bridge.hostname import (
    system_friendly_hostname,
    system_hostname_slug,
    system_interface_mac,
    system_serial_number,
)
from macos_bridge.runtime import Bridge, configure_logging


def _parse_args(argv: list[str]) -> argparse.Namespace:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--config",
        default="config.yaml",
        type=Path,
        help="Path to config.yaml (default: ./config.yaml)",
    )

    parser = argparse.ArgumentParser(prog="macos-mqtt-bridge", parents=[shared])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the daemon", parents=[shared])
    sub.add_parser(
        "init-state",
        help="Prime comms source state to current max IDs",
        parents=[shared],
    )
    sub.add_parser(
        "dump-once",
        help="Single dry-run poll; print events to stderr; no MQTT publish",
        parents=[shared],
    )

    boot = sub.add_parser(
        "bootstrap-allowlist",
        help="Print top-N user-facing apps by usage from knowledgeC.db",
        parents=[shared],
    )
    boot.add_argument("--top", type=int, default=20, help="Top N apps (default 20)")
    boot.add_argument("--days", type=int, default=30, help="Lookback window in days (default 30)")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Production-error reporter: install sys.excepthook + threading.excepthook
    # so every uncaught exception in the daemon flows through GitHub
    # repository_dispatch → the Production Error Intake workflow → Claude
    # auto-fix PR. Silently disabled when GITHUB_ERROR_TOKEN is unset.
    try:
        from python_github_error_reporter import GitHubErrorReporter
        GitHubErrorReporter.register(app="macos-mqtt-bridge", install_async_hook=False)
    except ImportError:
        pass

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config_path = Path(args.config).expanduser()

    if args.command == "bootstrap-allowlist":
        cfg = load_config(config_path)
        configure_logging(cfg.bridge.log_path, cfg.bridge.log_level)
        paths = cfg.expanded_paths()
        knowledge_db = Path(paths["knowledge_db"]) if paths["knowledge_db"] else None
        tmp_dir = Path(paths["tmp_dir"])
        if not knowledge_db or not knowledge_db.exists():
            print(
                f"# knowledgeC.db not found at {knowledge_db} "
                "— verify FDA grant on the venv python.",
                file=sys.stderr,
            )
            return 1
        apps = bootstrap_allowlist(
            knowledge_db, tmp_dir, top_n=args.top, days=args.days
        )
        if not apps:
            print(
                f"# No user-facing apps found in the last {args.days} days. "
                "Verify FDA grant on the venv python.",
                file=sys.stderr,
            )
            return 1
        emit_yaml(apps)
        return 0

    if args.command == "init-state":
        # init-state runs without MQTT credentials.
        cfg = load_config(config_path)
        configure_logging(cfg.bridge.log_path, cfg.bridge.log_level)
        Bridge(
            cfg, creds=None, host_slug=cfg.bridge.hostname or system_hostname_slug(),
            dry_run=True,
        ).init_state()
        return 0

    if args.command == "dump-once":
        cfg = load_config(config_path)
        configure_logging(cfg.bridge.log_path, cfg.bridge.log_level)
        Bridge(
            cfg, creds=None, host_slug=cfg.bridge.hostname or system_hostname_slug(),
            dry_run=True,
        ).run_once_dry()
        return 0

    # run
    cfg, creds = load_config_with_credentials(config_path)
    configure_logging(cfg.bridge.log_path, cfg.bridge.log_level)

    host_slug = cfg.bridge.hostname or system_hostname_slug()
    host_friendly_name = system_friendly_hostname()
    serial_number = system_serial_number()
    mac_address = system_interface_mac("en0")
    logging.getLogger(__name__).info(
        "host identity: slug=%s friendly=%r serial=%s en0_mac=%s",
        host_slug, host_friendly_name, serial_number, mac_address,
    )

    bridge = Bridge(
        cfg,
        creds=creds,
        host_slug=host_slug,
        host_friendly_name=host_friendly_name,
        serial_number=serial_number,
        mac_address=mac_address,
    )
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
