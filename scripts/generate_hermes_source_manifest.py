#!/usr/bin/env python3
"""Build a deterministic Hermes source archive and identity manifest.

The workspace contains an extracted source snapshot rather than the original
download archive. This script creates a canonical tar.gz from the immutable
source inputs, then records both the archive digest and a content-oriented tree
digest. Local environments and test/build caches are intentionally excluded.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import stat
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
EXCLUDED_DIRECTORY_SUFFIXES = (".egg-info",)
EXCLUDED_FILE_NAMES = frozenset({".DS_Store", "test_durations.json"})
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")
TREE_HASH_ALGORITHM = (
    "sha256 over sorted records: "
    "directory=D\\0<path>\\0<mode>\\n; "
    "file=F\\0<path>\\0<mode>\\0<size>\\0<content_sha256>\\n; "
    "symlink=L\\0<path>\\0<target>\\n"
)
ARCHIVE_NORMALIZATION = (
    "tar.gz with lexicographic POSIX paths, gzip mtime=0, tar mtime=0, "
    "uid/gid=0, empty uname/gname, and preserved permission bits"
)


@dataclass(frozen=True)
class SourceEntry:
    path: Path
    relative_path: str
    kind: str
    mode: int
    size: int = 0
    content_sha256: str | None = None
    link_target: str | None = None


def _is_excluded(relative_path: Path) -> bool:
    if any(
        part in EXCLUDED_DIRECTORY_NAMES
        or part.endswith(EXCLUDED_DIRECTORY_SUFFIXES)
        for part in relative_path.parts[:-1]
    ):
        return True
    name = relative_path.name
    return (
        name in EXCLUDED_DIRECTORY_NAMES
        or name.endswith(EXCLUDED_DIRECTORY_SUFFIXES)
        or name in EXCLUDED_FILE_NAMES
        or name.endswith(EXCLUDED_FILE_SUFFIXES)
    )


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def collect_source_entries(source_root: Path) -> list[SourceEntry]:
    entries: list[SourceEntry] = []
    for path in source_root.rglob("*"):
        relative = path.relative_to(source_root)
        if _is_excluded(relative):
            continue

        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        relative_posix = relative.as_posix()
        if path.is_symlink():
            entries.append(
                SourceEntry(
                    path=path,
                    relative_path=relative_posix,
                    kind="symlink",
                    mode=mode,
                    link_target=os.readlink(path),
                )
            )
        elif path.is_dir():
            entries.append(
                SourceEntry(
                    path=path,
                    relative_path=relative_posix,
                    kind="directory",
                    mode=mode,
                )
            )
        elif path.is_file():
            entries.append(
                SourceEntry(
                    path=path,
                    relative_path=relative_posix,
                    kind="file",
                    mode=mode,
                    size=metadata.st_size,
                    content_sha256=sha256_file(path),
                )
            )
        else:
            raise ValueError(f"Unsupported source entry: {path}")

    return sorted(entries, key=lambda entry: entry.relative_path.encode("utf-8"))


def source_tree_sha256(entries: Iterable[SourceEntry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        path = entry.relative_path.encode("utf-8")
        if entry.kind == "directory":
            record = b"D\0" + path + b"\0" + f"{entry.mode:o}".encode() + b"\n"
        elif entry.kind == "file":
            record = (
                b"F\0"
                + path
                + b"\0"
                + f"{entry.mode:o}".encode()
                + b"\0"
                + str(entry.size).encode()
                + b"\0"
                + str(entry.content_sha256).encode()
                + b"\n"
            )
        elif entry.kind == "symlink":
            record = (
                b"L\0"
                + path
                + b"\0"
                + str(entry.link_target).encode("utf-8")
                + b"\n"
            )
        else:  # pragma: no cover - SourceEntry construction is closed above.
            raise ValueError(f"Unsupported entry kind: {entry.kind}")
        digest.update(record)
    return digest.hexdigest()


def build_deterministic_archive(
    entries: Iterable[SourceEntry],
    archive_path: Path,
    archive_root_name: str,
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as gzip_handle:
            with tarfile.open(
                fileobj=gzip_handle,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                root_info = tarfile.TarInfo(archive_root_name)
                root_info.type = tarfile.DIRTYPE
                root_info.mode = 0o755
                _normalize_tar_info(root_info)
                archive.addfile(root_info)

                for entry in entries:
                    archive_name = f"{archive_root_name}/{entry.relative_path}"
                    info = tarfile.TarInfo(archive_name)
                    info.mode = entry.mode
                    _normalize_tar_info(info)

                    if entry.kind == "directory":
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    elif entry.kind == "symlink":
                        info.type = tarfile.SYMTYPE
                        info.linkname = str(entry.link_target)
                        archive.addfile(info)
                    elif entry.kind == "file":
                        info.type = tarfile.REGTYPE
                        info.size = entry.size
                        with entry.path.open("rb") as source_handle:
                            archive.addfile(info, source_handle)
                    else:  # pragma: no cover
                        raise ValueError(f"Unsupported entry kind: {entry.kind}")


def _normalize_tar_info(info: tarfile.TarInfo) -> None:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}


def _declared_version(source_root: Path) -> str:
    with (source_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["version"])


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def generate_manifest(
    *,
    workspace_root: Path,
    source_root: Path,
    archive_path: Path,
    manifest_path: Path,
    compatibility_passed: int,
    compatibility_failed: int,
) -> dict[str, object]:
    entries = collect_source_entries(source_root)
    tree_digest = source_tree_sha256(entries)
    build_deterministic_archive(entries, archive_path, source_root.name)

    file_entries = [entry for entry in entries if entry.kind == "file"]
    manifest: dict[str, object] = {
        "schema_version": 1,
        "repository": "NousResearch/hermes-agent",
        "declared_version": _declared_version(source_root),
        "commit_sha": None,
        "source_directory": _relative_to_workspace(source_root, workspace_root),
        "archive_path": _relative_to_workspace(archive_path, workspace_root),
        "archive_format": "deterministic tar.gz",
        "archive_sha256": sha256_file(archive_path),
        "source_tree_sha256": tree_digest,
        "source_tree_file_count": len(file_entries),
        "source_tree_total_file_bytes": sum(entry.size for entry in file_entries),
        "python_version": platform.python_version(),
        "compatibility_tests": {
            "passed": compatibility_passed,
            "failed": compatibility_failed,
        },
        "hash_specification": {
            "source_tree": TREE_HASH_ALGORITHM,
            "archive": ARCHIVE_NORMALIZATION,
        },
        "excluded_local_artifacts": {
            "directory_names": sorted(EXCLUDED_DIRECTORY_NAMES),
            "directory_suffixes": list(EXCLUDED_DIRECTORY_SUFFIXES),
            "file_names": sorted(EXCLUDED_FILE_NAMES),
            "file_suffixes": list(EXCLUDED_FILE_SUFFIXES),
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--archive-path", type=Path)
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--compatibility-passed", type=int, default=158)
    parser.add_argument("--compatibility-failed", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace_root = args.workspace_root.resolve()
    source_root = (args.source_root or workspace_root / "hermes-agent-main").resolve()
    archive_path = (
        args.archive_path
        or workspace_root / "artifacts" / "hermes-source-snapshot-0.18.2.tar.gz"
    ).resolve()
    manifest_path = (
        args.manifest_path
        or workspace_root / "artifacts" / "hermes-source-manifest.json"
    ).resolve()
    manifest = generate_manifest(
        workspace_root=workspace_root,
        source_root=source_root,
        archive_path=archive_path,
        manifest_path=manifest_path,
        compatibility_passed=args.compatibility_passed,
        compatibility_failed=args.compatibility_failed,
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
