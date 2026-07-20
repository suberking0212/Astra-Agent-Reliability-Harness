"""Thin CLI adapter for Phase 3 contract-domain validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .round2 import validate_round2_fixture
from .governance import validate_governance_round2_fixture
from .task_contract import TaskContract


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astra-phase3",
        description=(
            "Validate frozen Phase 3 contracts through domain and governance "
            "components."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    round2 = commands.add_parser(
        "validate-round2",
        help="Validate all scenario/path cases in a Round 2 fixture.",
    )
    round2.add_argument("--fixture", type=Path, required=True)
    governance_round2 = commands.add_parser(
        "validate-governance-round2",
        help="Validate Round 2 decisions through the Runtime Governance Core.",
    )
    governance_round2.add_argument("--fixture", type=Path, required=True)
    contract = commands.add_parser(
        "validate-contract",
        help="Validate one persisted Task Contract, including its content hash.",
    )
    contract.add_argument("contract", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _load_json(
            args.fixture
            if args.command in {"validate-round2", "validate-governance-round2"}
            else args.contract
        )
        if args.command == "validate-round2":
            report = validate_round2_fixture(payload)
            print(report.model_dump_json(indent=2))
            return 0 if report.valid else 1
        if args.command == "validate-governance-round2":
            report = validate_governance_round2_fixture(payload)
            print(report.model_dump_json(indent=2))
            return 0 if report.valid else 1
        contract = TaskContract.model_validate(payload)
        print(
            json.dumps(
                {
                    "valid": True,
                    "contract_id": contract.contract_id,
                    "contract_version": contract.contract_version,
                    "contract_hash": contract.contract_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "valid": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 2
