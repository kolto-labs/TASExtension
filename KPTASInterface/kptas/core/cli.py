"""CLI for managing Telos Core package enrollment.

This command edits the same Core-owned profile state used by an application
host. It never imports package code from a manifest and it does not pretend to
activate a package in a running GUI process. Package activation belongs to the
Core application that owns the live session.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from .package_host import ExtensionHost, HostError, JsonPackageStateStore
from .tas_package import TAS_MANIFEST


class _EnrollmentOnlyLifecycle:
    """Satisfy the trusted-registration contract without loading TAS code.

    The CLI intentionally never calls start(). This lifecycle exists only
    because the host registry requires an explicit lifecycle object; it fails
    closed if a caller accidentally tries to activate it.
    """

    def activate(self, context: Any):
        raise HostError("CORE_RUNTIME_ADAPTER_UNAVAILABLE",
                        "TAS enrollment is recorded, but the native Core runtime adapter is not wired")

    def deactivate(self, context: Any) -> None:
        return None


def _state_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return config_home / "telos" / "core-packages.json"


def manage(action: str, package_id: str | None = None, state_file: str | None = None) -> int:
    """List or change first-party package enrollment for the selected profile."""
    path = _state_path(state_file)
    try:
        host = ExtensionHost(JsonPackageStateStore(path))
        host.register(TAS_MANIFEST, _EnrollmentOnlyLifecycle())
        if action == "list":
            print(f"Core package profile: {path}")
            print("ID         STATE                         RUNTIME")
            for manifest in sorted(host.manifests(), key=lambda item: item.id):
                state = host.state(manifest.id)
                if not state.installed:
                    enrollment = "not installed"
                elif state.enabled:
                    enrollment = "enabled (requested)"
                else:
                    enrollment = "installed, disabled"
                runtime = "inactive; managed by the Core application"
                print(f"{manifest.id:<10} {enrollment:<29} {runtime}")
            if not host.storage_available:
                print("Warning: profile state could not be saved; changes are visit-only.")
            return 0

        receipt = host.change(package_id or "", action)
        affected = ", ".join(receipt.package_ids)
        persistence = "saved" if receipt.persisted else "not saved; change is session-only"
        print(f"{action} {affected}: {persistence}")
        if not receipt.persisted:
            print(f"Profile: {path}")
            return 1
        if action == "enable":
            print("Activation occurs when the owning Telos Core application starts this registered package.")
        elif action == "install":
            print("This records package enrollment; it does not download or remove executable package files.")
        elif action == "remove":
            print("Package code and user data are retained; this removes the package from this profile.")
        return 0
    except HostError as exc:
        print(f"telos core: {exc.code}" + (f": {exc.detail}" if exc.detail else ""), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"telos core: profile storage unavailable: {exc}", file=sys.stderr)
        return 2


def add_core_subcommands(parent: Any) -> None:
    """Add package lifecycle commands to the main `telos` parser."""
    core = parent.add_parser("core", help="manage optional Telos Core packages")
    core.add_argument("--state", metavar="FILE",
                      help="package profile file (default: $XDG_CONFIG_HOME/telos/core-packages.json)")
    actions = core.add_subparsers(dest="core_action", required=True, metavar="action")
    for action, help_text in (
        ("list", "show package enrollment and runtime ownership"),
        ("install", "enroll a registered first-party package in this profile"),
        ("enable", "request package activation for the Core application"),
        ("disable", "disable a package in this profile"),
        ("remove", "remove package enrollment from this profile"),
    ):
        command = actions.add_parser(action, help=help_text, description=help_text)
        if action != "list":
            command.add_argument("package_id", choices=("telos.tas",))
        command.set_defaults(fn=_dispatch)


def _dispatch(ctx: Any, args: Any) -> int:
    return manage(args.core_action, getattr(args, "package_id", None), args.state)

