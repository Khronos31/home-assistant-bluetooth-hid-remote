#!/usr/bin/env python3
"""Synchronize and validate repository release versions."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
README_VERSION = re.compile(
    r"^Current version: \*\*(?P<version>[^*]+)\*\*\.$", re.MULTILINE
)


class VersionError(ValueError):
    """Raised when version representations are invalid or inconsistent."""


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse a stable semantic version without a v prefix."""
    match = SEMVER.fullmatch(value)
    if match is None:
        raise VersionError(f"invalid stable version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "VERSION",
        root / "custom_components/bluetooth_hid_remote/manifest.json",
        root / "README.md",
    )


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise VersionError(f"unable to read {path}: {err}") from err
    if not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str):
        raise VersionError(f"{path} must contain a string version")
    return manifest


def check_versions(root: Path) -> str:
    """Return the canonical version after checking every representation."""
    version_path, manifest_path, readme_path = _paths(root)
    version = version_path.read_text(encoding="utf-8").strip()
    parse_version(version)
    manifest_version = _load_manifest(manifest_path)["version"]
    readme = readme_path.read_text(encoding="utf-8")
    matches = list(README_VERSION.finditer(readme))
    if len(matches) != 1:
        raise VersionError("README.md must contain exactly one current-version line")
    versions = {
        "VERSION": version,
        "manifest.json": manifest_version,
        "README.md": matches[0].group("version"),
    }
    if any(candidate != version for candidate in versions.values()):
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise VersionError(f"version mismatch: {details}")
    return version


def sync_version(root: Path, version: str) -> None:
    """Write one version to every repository representation."""
    parse_version(version)
    version_path, manifest_path, readme_path = _paths(root)
    manifest = _load_manifest(manifest_path)
    readme = readme_path.read_text(encoding="utf-8")
    if len(list(README_VERSION.finditer(readme))) != 1:
        raise VersionError("README.md must contain exactly one current-version line")
    manifest["version"] = version
    version_path.write_text(f"{version}\n", encoding="utf-8")
    manifest_path.write_text(
        f"{json.dumps(manifest, indent=2, ensure_ascii=False)}\n", encoding="utf-8"
    )
    readme_path.write_text(
        README_VERSION.sub(f"Current version: **{version}**.", readme, count=1),
        encoding="utf-8",
    )


def require_newer(candidate: str, current: str) -> None:
    """Require candidate to be strictly newer than current."""
    if parse_version(candidate) <= parse_version(current):
        raise VersionError(f"{candidate} must be newer than {current}")


def main() -> int:
    """Run the version command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("version")
    commands.add_parser("check")
    newer = commands.add_parser("newer")
    newer.add_argument("candidate")
    newer.add_argument("current")
    args = parser.parse_args()

    try:
        if args.command == "sync":
            sync_version(args.root, args.version)
        elif args.command == "check":
            print(check_versions(args.root))
        else:
            require_newer(args.candidate, args.current)
    except (OSError, VersionError) as err:
        print(f"version error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
