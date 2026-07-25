#!/usr/bin/env python3
"""Safely copy a Huizhi Assistant 1.1 data volume into a QwenPaw 2.0.1 volume.

The old service must be stopped before a real migration.  This command never
modifies its source directories and refuses to overwrite existing target
files.  Use ``--dry-run`` first on the production server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


DATA_ITEMS = (
    "config.json",
    "workspaces",
    "users",
    "skill_pool",
)
SECRET_ITEMS = (
    ".master_key",
    "auth.json",
    "providers",
)


@dataclass
class MigrationReport:
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "copied": self.copied,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_item(source: Path, target: Path, report: MigrationReport, dry_run: bool) -> None:
    label = str(source)
    if not source.exists():
        report.skipped.append(f"missing: {label}")
        return
    if target.exists():
        report.skipped.append(f"target exists: {target}")
        return
    if dry_run:
        report.copied.append(f"would copy: {label} -> {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, target)
        if _hash_file(source) != _hash_file(target):
            raise RuntimeError(f"checksum mismatch after copying {source}")
    report.copied.append(f"copied: {label} -> {target}")


def _validate_v1_source(working_dir: Path, secret_dir: Path) -> list[str]:
    errors: list[str] = []
    config_path = working_dir / "config.json"
    if not config_path.is_file():
        errors.append(f"missing source config: {config_path}")
    else:
        try:
            json.loads(config_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid source config: {exc}")
    if not (secret_dir / "auth.json").is_file():
        errors.append(f"missing source authentication data: {secret_dir / 'auth.json'}")
    return errors


def _summarize(working_dir: Path, secret_dir: Path, report: MigrationReport) -> None:
    auth_path = secret_dir / "auth.json"
    if auth_path.is_file():
        try:
            auth_data = json.loads(auth_path.read_text("utf-8"))
            users = auth_data.get("users", {})
            if isinstance(users, dict):
                report.warnings.append(f"detected {len(users)} existing user accounts")
            elif isinstance(auth_data.get("user"), dict):
                report.warnings.append("detected one legacy single-user account")
        except (OSError, json.JSONDecodeError):
            report.warnings.append("unable to inspect encrypted or invalid auth.json")

    workspaces = working_dir / "workspaces"
    if workspaces.is_dir():
        report.warnings.append(
            f"detected {sum(1 for item in workspaces.iterdir() if item.is_dir())} agent workspaces",
        )


def migrate(source_working: Path, source_secret: Path, target_working: Path, target_secret: Path, dry_run: bool) -> MigrationReport:
    report = MigrationReport()
    errors = _validate_v1_source(source_working, source_secret)
    if errors:
        raise RuntimeError("; ".join(errors))

    for item in DATA_ITEMS:
        _copy_item(source_working / item, target_working / item, report, dry_run)
    for item in SECRET_ITEMS:
        _copy_item(source_secret / item, target_secret / item, report, dry_run)

    _summarize(source_working, source_secret, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-working", type=Path, required=True)
    parser.add_argument("--source-secret", type=Path, required=True)
    parser.add_argument("--target-working", type=Path, required=True)
    parser.add_argument("--target-secret", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, help="write JSON report to this path")
    args = parser.parse_args()

    try:
        report = migrate(
            args.source_working.resolve(),
            args.source_secret.resolve(),
            args.target_working.resolve(),
            args.target_secret.resolve(),
            args.dry_run,
        )
    except RuntimeError as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    print(payload)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
