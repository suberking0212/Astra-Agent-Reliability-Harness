"""OS-level acceptance checks used by the Stage 1 Hermes launch boundary."""

from __future__ import annotations

import subprocess
import socket
from pathlib import Path

import pytest
from astra.phase3.completion import (
    CompletionContract, CompletionRequirement, EvaluatorRef, RequirementEvaluation,
    RequirementStatus, SubmittedResult, EvidenceSnapshot, aggregate_completion,
    CompletionStatus,
)


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec unavailable")
def test_stage1_profile_denies_authoritative_sqlite_and_raw_sandbox(tmp_path: Path):
    """The same SBPL restrictions used by the launcher reject terminal bypass."""
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    database = authority_dir / "authoritative-business.sqlite3"
    database.write_text("authoritative", encoding="utf-8")
    profile = "\n".join((
        "(version 1)", "(allow default)",
        f'(deny file-read* (subpath "{authority_dir}"))',
        f'(deny file-write* (subpath "{authority_dir}"))',
    ))
    for target in (database, authority_dir / "authoritative-business.sqlite3-wal"):
        target.write_text("authoritative", encoding="utf-8")
        read = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(target)],
                              capture_output=True, text=True, check=False)
        assert read.returncode != 0
    # A TCP connect is an equivalent terminal-side attempt to bypass Runtime.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        # Current macOS SBPL accepts a wildcard host with a fixed port, which
        # covers IPv4, IPv6 and mapped-IPv6 loopback spellings without relying
        # on unsupported literal-IP syntax.
        network_profile = profile + f'\n(deny network-outbound (remote tcp "*:{port}"))'
        profile_file = tmp_path / "stage1-network.sb"
        profile_file.write_text(network_profile, encoding="utf-8")
        for host in ("localhost", "127.0.0.1", "::1"):
            direct = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile_file), "/usr/bin/nc", "-z", host, str(port)],
                                    capture_output=True, text=True, check=False)
            assert direct.returncode != 0
    finally:
        listener.close()


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec unavailable")
def test_stage1_profile_denies_test_probe_from_hermes(tmp_path: Path):
    """Hermes cannot read or launch the test-only probe, even if it sets its env flag."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    probe = scripts_dir / "probe_governed_order.py"
    probe.write_text("print('probe')", encoding="utf-8")
    profile = "\n".join((
        "(version 1)", "(allow default)",
        f'(deny file-read* (subpath "{scripts_dir}"))',
        f'(deny file-write* (subpath "{scripts_dir}"))',
    ))
    read = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(probe)],
                          capture_output=True, text=True, check=False)
    assert read.returncode != 0
    execute = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/python3", str(probe)],
        env={**__import__("os").environ, "ASTRA_ALLOW_TEST_PROBE": "1"},
        capture_output=True, text=True, check=False,
    )
    assert execute.returncode != 0


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec unavailable")
def test_stage1_profile_denies_business_backend_code(tmp_path: Path):
    backend_dir = tmp_path / "business_sandbox"
    backend_dir.mkdir()
    backend = backend_dir / "server.py"
    backend.write_text("print('backend')", encoding="utf-8")
    profile = "\n".join(("(version 1)", "(allow default)",
        f'(deny file-read* (subpath "{backend_dir}"))'))
    read = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(backend)],
                          capture_output=True, text=True, check=False)
    assert read.returncode != 0


def test_completion_contract_rejects_hermes_claim_without_authoritative_evidence():
    contract = CompletionContract(contract_id="stage1", contract_version="1", task_type="business",
        requirements=(CompletionRequirement(requirement_id="evidence", description="authoritative evidence",
            evaluator=EvaluatorRef(evaluator_id="missing", evaluator_version="1")),))
    submitted = SubmittedResult(submitted_result_id="result:hermes-claimed", task_id="task-1",
        attempt_id="attempt-1", execution_id="execution-1", outcome={"assistant_output": "done"})
    snapshot = EvidenceSnapshot.model_construct(evidence_snapshot_id="snapshot:none")
    evaluation = RequirementEvaluation(evaluation_id="evaluation:missing", requirement_id="evidence",
        evaluator_id="missing", evaluator_version="1", submitted_result_id=submitted.submitted_result_id,
        evidence_snapshot_id=snapshot.evidence_snapshot_id, status=RequirementStatus.UNKNOWN)
    result = aggregate_completion(contract, submitted, snapshot, (evaluation,))
    assert result.status == CompletionStatus.INDETERMINATE
    assert result.status != CompletionStatus.SATISFIED
