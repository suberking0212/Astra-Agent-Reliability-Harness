"""Contract tests for the Documentation Governance CI checker."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "documentation_governance", ROOT / "scripts" / "check_documentation_governance.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _header(*, authority: str = "A1", topic: str = "PRODUCT.CORE", status: str = "FROZEN") -> str:
    return f"""> **Documentation Governance**
> - **Role:** Test authority.
> - **Authority:** {authority} — Test.
> - **Topic:** {topic}
> - **Scope:** Test scope.
> - **Not Responsible For:** Other behavior.
> - **Depends On:** GOVERNANCE.DOCUMENTATION
> - **Status:** {status}
"""


def _repository(tmp_path: Path, document_header: str | None = None) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "AUTHORITY.md").write_text(
        _header(authority="A0", topic="GOVERNANCE.DOCUMENTATION").replace(
            "**Depends On:** GOVERNANCE.DOCUMENTATION", "**Depends On:** NONE"
        )
        + "\n| Topic ID | Topic | Single authoritative source | Supporting context |\n"
        + "|---|---|---|---|\n"
        + "| GOVERNANCE.DOCUMENTATION | Docs | `docs/AUTHORITY.md` | None |\n"
        + "| PRODUCT.CORE | Product | `docs/PRD.md` | None |\n"
        + "\n| Document | Role | Authority | Status |\n|---|---|---|---|\n"
        + "| `docs/AUTHORITY.md` | Docs | A0 | FROZEN |\n"
        + "| `docs/PRD.md` | Product | A1 | FROZEN |\n",
        encoding="utf-8",
    )
    (docs / "PRD.md").write_text(document_header or _header(), encoding="utf-8")
    return tmp_path


def test_valid_repository_passes(tmp_path: Path):
    assert checker.check(_repository(tmp_path)) == []


def test_registered_source_requires_complete_header(tmp_path: Path):
    root = _repository(tmp_path, "> - **Role:** Incomplete.\n")
    assert any("docs/PRD.md: incomplete governance header" in error for error in checker.check(root))


def test_conflicting_authority_declaration_is_rejected(tmp_path: Path):
    root = _repository(tmp_path, _header(authority="A2"))
    assert any("Authority 'A2' does not match Document Registry 'A1'" in error for error in checker.check(root))


def test_historical_document_cannot_claim_current_authority(tmp_path: Path):
    root = _repository(tmp_path)
    historical = root / "docs" / "history"
    historical.mkdir()
    (historical / "old.md").write_text(
        _header(authority="A1", topic="PRODUCT.OLD", status="CURRENT"), encoding="utf-8"
    )
    errors = checker.check(root)
    assert any("docs/history/old.md: declares A1 but is absent from the Topic Registry" in error for error in errors)
