#!/usr/bin/env python3
"""Validate the documentation authority registry without changing repository files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REQUIRED_HEADER_FIELDS = (
    "Role",
    "Authority",
    "Topic",
    "Scope",
    "Not Responsible For",
    "Depends On",
    "Status",
)
LIFECYCLE_STATES = {"DRAFT", "REVIEW", "FROZEN", "SUPERSEDED", "HISTORICAL"}
AUTHORITY_RE = re.compile(r"^> - \*\*(.+?):\*\*\s*(.*)$", re.MULTILINE)
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def header_fields(text: str) -> dict[str, str]:
    return {key: value.strip() for key, value in AUTHORITY_RE.findall(text)}


def topic_registry(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 5 or not cells[1].startswith(("GOVERNANCE.", "PRODUCT.", "ARCH.", "DESIGN.", "ACCEPTANCE.")):
            continue
        path_match = re.search(r"`([^`]+\.md)`", cells[3])
        if path_match:
            rows[cells[1]] = (path_match.group(1), cells[3])
    return rows


def document_registry(text: str) -> dict[str, str]:
    return {
        path: authority
        for path, authority in re.findall(
            r"\| `([^`]+\.md)` \|[^\n]+\| (A(?:0|1|2|3|4|5a|5b)) \|", text
        )
    }


def local_links(path: Path, text: str) -> list[Path]:
    targets: list[Path] = []
    for raw_target in LINK_RE.findall(text):
        target = raw_target.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith(("mailto:", "#")):
            continue
        targets.append((path.parent / target).resolve())
    return targets


def check(root: Path) -> list[str]:
    """Return every deterministic governance defect found below ``root``."""
    docs = root / "docs"
    authority = docs / "AUTHORITY.md"
    errors: list[str] = []
    if not authority.is_file():
        return ["docs/AUTHORITY.md: missing documentation authority manifest"]
    authority_text = authority.read_text(encoding="utf-8")
    topics = topic_registry(authority_text)
    derived_documents = document_registry(authority_text)

    if not topics:
        errors.append("docs/AUTHORITY.md: no Topic Registry rows found")
    source_paths = {path for path, _ in topics.values()}
    if source_paths != set(derived_documents):
        errors.append(
            "docs/AUTHORITY.md: Topic Registry and Current normative documents differ: "
            f"topic-only={sorted(source_paths - set(derived_documents))}, "
            f"derived-only={sorted(set(derived_documents) - source_paths)}"
        )

    dependency_graph: dict[str, set[str]] = {}
    for topic, (relative_path, _source) in topics.items():
        path = root / relative_path
        if not path.is_file():
            errors.append(f"{relative_path}: registered authority source is missing")
            continue
        fields = header_fields(path.read_text(encoding="utf-8"))
        missing = [field for field in REQUIRED_HEADER_FIELDS if not fields.get(field)]
        if missing:
            errors.append(f"{relative_path}: incomplete governance header; missing {', '.join(missing)}")
            continue
        authority_level = fields["Authority"].split()[0]
        if authority_level not in {"A0", "A1", "A2", "A3", "A4", "A5a", "A5b"}:
            errors.append(f"{relative_path}: invalid Authority level {fields['Authority']!r}")
        elif derived_documents.get(relative_path) != authority_level:
            errors.append(
                f"{relative_path}: Authority {authority_level!r} does not match "
                f"Document Registry {derived_documents.get(relative_path)!r}"
            )
        if fields["Status"].upper() not in LIFECYCLE_STATES:
            errors.append(f"{relative_path}: Authority status must be a lifecycle state, got {fields['Status']!r}")
        dependencies = {item.strip() for item in fields["Depends On"].split(",") if item.strip()}
        if dependencies == {"NONE"}:
            if topic != "GOVERNANCE.DOCUMENTATION":
                errors.append(f"{relative_path}: only GOVERNANCE.DOCUMENTATION may depend on NONE")
            dependencies = set()
        unknown = dependencies - set(topics)
        if unknown:
            errors.append(f"{relative_path}: unknown Depends On Topic IDs: {', '.join(sorted(unknown))}")
        dependency_graph[topic] = dependencies

    for path in docs.rglob("*.md"):
        relative_path = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        fields = header_fields(text)
        authority_tokens = fields.get("Authority", "").split()
        declared_authority = authority_tokens[0] if authority_tokens else ""
        if declared_authority in {"A0", "A1", "A2", "A3", "A4", "A5a", "A5b"} and relative_path not in source_paths:
            errors.append(f"{relative_path}: declares {declared_authority} but is absent from the Topic Registry")
        for target in local_links(path, text):
            if not target.exists():
                errors.append(f"{relative_path}: local document link is missing: {target.relative_to(root) if target.is_relative_to(root) else target}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(topic: str) -> None:
        if topic in visiting:
            errors.append(f"Topic Registry dependency cycle includes {topic}")
            return
        if topic in visited:
            return
        visiting.add(topic)
        for dependency in dependency_graph.get(topic, set()):
            visit(dependency)
        visiting.remove(topic)
        visited.add(topic)

    for topic in dependency_graph:
        visit(topic)

    return sorted(set(errors))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check(root)
    if errors:
        print("Documentation governance check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    topic_count = len(topic_registry((root / "docs" / "AUTHORITY.md").read_text(encoding="utf-8")))
    print(f"Documentation governance check passed: {topic_count} registered topics checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
