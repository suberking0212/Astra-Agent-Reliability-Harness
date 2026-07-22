"""Formal command-line entry points for the Astra production Runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .phase3.completion import CompletionContract
from .phase3.task_contract import TaskContract
from .production import ProductionConfig, ProductionRuntime


def _json_mapping(value: str, *, source: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must contain a JSON object")
    return parsed


def _read_json(path: str | Path) -> Mapping[str, Any]:
    target = Path(path)
    return _json_mapping(target.read_text(), source=str(target))


def _provider_config(value: str | None) -> Mapping[str, Any]:
    raw = value or os.environ.get("ASTRA_PROVIDER_CONFIG", "{}")
    return _json_mapping(raw, source="provider config")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astra-runtime")
    parser.add_argument(
        "--database",
        default=os.environ.get("ASTRA_DATABASE", "astra.sqlite3"),
    )
    parser.add_argument(
        "--hermes-root",
        default=os.environ.get(
            "ASTRA_HERMES_ROOT",
            str(Path(__file__).resolve().parents[1] / "hermes-agent-main"),
        ),
    )
    parser.add_argument("--provider-config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="submit a durable Task")
    submit.add_argument("--command-id", required=True)
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--contract", required=True)
    submit.add_argument("--completion-contract")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--ready-at")

    resolve = subparsers.add_parser(
        "resolve", help="resolve a pending Interaction"
    )
    resolve.add_argument("--command-id", required=True)
    resolve.add_argument("--interaction-id", required=True)
    resolve.add_argument("--expected-version", type=int, required=True)
    resolution = resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--resolution-json")
    resolution.add_argument("--resolution-file")
    resolve.add_argument("--priority", type=int, default=0)
    resolve.add_argument("--ready-at")

    cancel = subparsers.add_parser("cancel", help="cancel a durable Task")
    cancel.add_argument("--command-id", required=True)
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--expected-task-version", type=int, required=True)
    cancel.add_argument("--reason", default="operator_cancelled")

    status = subparsers.add_parser(
        "status", help="show one Task and its authoritative business evidence"
    )
    status.add_argument("--task-id", required=True)
    status.add_argument("--pretty", action="store_true")

    worker = subparsers.add_parser(
        "worker", help="continuously consume durable Run Requests"
    )
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-interval", type=float, default=0.25)
    return parser


def _config(args: argparse.Namespace) -> ProductionConfig:
    return ProductionConfig(
        database_path=args.database,
        hermes_root=args.hermes_root,
        provider_config=_provider_config(args.provider_config),
        worker_poll_interval=getattr(args, "poll_interval", 0.25),
    )


def _submit(app: ProductionRuntime, args: argparse.Namespace) -> int:
    contract = TaskContract.model_validate(_read_json(args.contract))
    completion = (
        CompletionContract.model_validate(_read_json(args.completion_contract))
        if args.completion_contract
        else None
    )
    result = app.submit_task(
        command_id=args.command_id,
        task_id=args.task_id,
        contract=contract,
        completion_contract=completion,
        priority=args.priority,
        ready_at=args.ready_at,
    )
    print(result.model_dump_json())
    return 0


def _resolve(app: ProductionRuntime, args: argparse.Namespace) -> int:
    resolution = (
        _json_mapping(args.resolution_json, source="--resolution-json")
        if args.resolution_json is not None
        else _read_json(args.resolution_file)
    )
    result = app.resolve_interaction(
        command_id=args.command_id,
        interaction_id=args.interaction_id,
        expected_version=args.expected_version,
        resolution=resolution,
        priority=args.priority,
        ready_at=args.ready_at,
    )
    print(result.model_dump_json())
    return 0


async def _run_worker(app: ProductionRuntime, *, once: bool) -> int:
    if once:
        result = await app.run_worker_once()
        print(result.model_dump_json() if result is not None else '{"idle":true}')
        return 0

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            pass
    await app.run_worker(
        stop_event=stop_event,
        on_result=lambda result: print(result.model_dump_json(), flush=True),
    )
    return 0


async def _cancel(app: ProductionRuntime, args: argparse.Namespace) -> int:
    result = await app.cancel_task(
        command_id=args.command_id,
        task_id=args.task_id,
        expected_task_version=args.expected_task_version,
        reason=args.reason,
    )
    print(result.model_dump_json())
    return 0


def _status(app: ProductionRuntime, args: argparse.Namespace) -> int:
    print(
        json.dumps(
            app.task_status(args.task_id),
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with ProductionRuntime(_config(args)) as app:
        if args.command == "submit":
            return _submit(app, args)
        if args.command == "resolve":
            return _resolve(app, args)
        if args.command == "cancel":
            return asyncio.run(_cancel(app, args))
        if args.command == "status":
            return _status(app, args)
        if args.command == "worker":
            return asyncio.run(_run_worker(app, once=args.once))
    raise AssertionError(f"Unhandled command: {args.command}")
