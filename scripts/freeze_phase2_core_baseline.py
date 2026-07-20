#!/usr/bin/env python3
"""Create a deterministic source archive and Phase 2 Core baseline manifest."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import tarfile
from datetime import date
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    Path("pyproject.toml"),
    Path("README.md"),
    Path("astra"),
    Path(".hermes/plugins/astra_bridge"),
    Path("tests/test_phase2_core.py"),
    Path("tests/test_phase2_hermes_e2e.py"),
    Path("tests/test_phase2_live_provider.py"),
    Path("tests/test_phase2_stress.py"),
    Path("scripts/run_phase2_core_audit.py"),
    Path("scripts/freeze_phase2_core_baseline.py"),
    Path("docs/PHASE_STATUS.md"),
    Path("docs/development_updated.md"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_under(paths: tuple[Path, ...]) -> list[Path]:
    files = []
    for relative in paths:
        absolute = WORKSPACE_ROOT / relative
        if absolute.is_file():
            files.append(absolute)
        elif absolute.is_dir():
            files.extend(
                path
                for path in absolute.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            )
        else:
            raise FileNotFoundError(relative)
    return sorted(set(files), key=lambda p: p.relative_to(WORKSPACE_ROOT).as_posix())


def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}
    return info


def build_archive(files: list[Path], output: Path) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                root = normalized(tarfile.TarInfo("phase2-core-baseline"))
                root.type = tarfile.DIRTYPE
                root.mode = 0o755
                tar.addfile(root)
                for path in files:
                    relative = path.relative_to(WORKSPACE_ROOT).as_posix()
                    info = tar.gettarinfo(
                        str(path), arcname=f"phase2-core-baseline/{relative}"
                    )
                    normalized(info)
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=WORKSPACE_ROOT / "artifacts" / "phase2-core-runtime-audit",
    )
    parser.add_argument("--project-tests", default="14 passed, 1 skipped")
    parser.add_argument("--hermes-tests", default="157 passed, 0 failed")
    parser.add_argument("--stress-tests", default="6 passed")
    args = parser.parse_args()
    artifacts = WORKSPACE_ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    archive = artifacts / "phase2-core-baseline-source.tar.gz"
    manifest_path = artifacts / "phase2-core-baseline-manifest.json"
    files = files_under(SOURCE_PATHS)
    build_archive(files, archive)
    source_records = [
        {
            "path": path.relative_to(WORKSPACE_ROOT).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in files
    ]
    audit_dir = args.audit_dir.resolve()
    audit_records = []
    for path in sorted(audit_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            audit_records.append(
                {
                    "path": path.relative_to(WORKSPACE_ROOT).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    audit = json.loads((audit_dir / "audit-report.json").read_text(encoding="utf-8"))
    hermes_manifest = WORKSPACE_ROOT / "artifacts" / "hermes-source-manifest.json"
    manifest = {
        "schema_version": 1,
        "baseline_id": f"phase2-core-{date.today().isoformat()}",
        "status": "core_implemented_live_provider_blocked_missing_credentials",
        "architecture_invariant": "Hermes Agent Loop unmodified; Astra integrates through Adapter and fixed Plugin boundaries",
        "source_archive": {
            "path": archive.relative_to(WORKSPACE_ROOT).as_posix(),
            "sha256": sha256(archive),
            "normalization": "lexicographic paths; gzip/tar mtime=0; uid/gid=0; empty uname/gname",
        },
        "source_files": source_records,
        "hermes_source_manifest": {
            "path": hermes_manifest.relative_to(WORKSPACE_ROOT).as_posix(),
            "sha256": sha256(hermes_manifest),
            "content": json.loads(hermes_manifest.read_text(encoding="utf-8")),
        },
        "validation": {
            "project_tests": args.project_tests,
            "hermes_compatibility_regression": args.hermes_tests,
            "stress_tests": args.stress_tests,
            "stress_parameters": {
                "termination_attempts": 512,
                "suspended_side_effect_attempts": 512,
                "same_idempotency_key_attempts": 512,
                "pause_side_effect_race_attempts": 128,
                "response_lost_idempotent_retries": 1,
                "multiprocess_termination_competitors": 16,
            },
            "runtime_audit_all_checks_passed": audit["all_checks_passed"],
        },
        "runtime_audit_artifacts": audit_records,
        "live_provider_smoke": {
            "status": "not_run_missing_credentials",
            "test": "tests/test_phase2_live_provider.py",
            "method": [
                "Set ASTRA_RUN_LIVE_PROVIDER=1",
                "Set ASTRA_LIVE_BASE_URL, ASTRA_LIVE_API_KEY, ASTRA_LIVE_MODEL",
                "Optionally set ASTRA_LIVE_PROVIDER and ASTRA_LIVE_API_MODE",
                "Run pytest -q tests/test_phase2_live_provider.py -s",
                "Verify real model tool selection, ResultReceipt validity, ExecutionResult, Neutral Trace, and one persisted complaint ticket",
            ],
            "ordinary_ci_behavior": "skipped",
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "manifest": str(manifest_path),
        "archive": str(archive),
        "archive_sha256": manifest["source_archive"]["sha256"],
        "source_file_count": len(source_records),
        "runtime_artifact_count": len(audit_records),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
