"""Append-only runtime authority derived from user input and exact tool calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .phase3.canonical import sha256_digest
from .phase3.task_contract import ApprovalRequirement, AuthorizedEffect, TaskContract
from .storage import AstraStore


def effective_contract(
    store: AstraStore,
    task_id: str,
    contract: TaskContract,
) -> TaskContract:
    """Overlay governed runtime intents without mutating the frozen contract ref."""

    rows = store.query_all(
        """
        SELECT effect_json, approval_requirement_json
        FROM phase4_dynamic_effect_intents
        WHERE task_id = ? ORDER BY effect_intent_id
        """,
        (task_id,),
    )
    if not rows:
        return contract
    effects = list(contract.authorized_effects)
    requirements = list(contract.approval_requirements)
    known_effects = {item.effect_intent_id for item in effects}
    known_requirements = {item.ref for item in requirements}
    for row in rows:
        effect = AuthorizedEffect.model_validate_json(row["effect_json"])
        requirement = ApprovalRequirement.model_validate_json(
            row["approval_requirement_json"]
        )
        if effect.effect_intent_id not in known_effects:
            effects.append(effect)
            known_effects.add(effect.effect_intent_id)
        if requirement.ref not in known_requirements:
            requirements.append(requirement)
            known_requirements.add(requirement.ref)
    # model_copy deliberately retains the original immutable contract hash/ref.
    # Runtime authority is separately persisted and auditable.
    return contract.model_copy(
        update={
            "authorized_effects": tuple(effects),
            "approval_requirements": tuple(requirements),
        }
    )


def record_subject_grant(
    store: AstraStore,
    *,
    task_id: str,
    authority_domain: str,
    subject_type: str,
    subject_id: str,
    access_scope: str,
    source_type: str,
    source_ref: str,
) -> str:
    payload = {
        "task_id": task_id,
        "authority_domain": authority_domain,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "access_scope": access_scope,
        "source_type": source_type,
        "source_ref": source_ref,
    }
    grant_id = "subject-grant:" + sha256_digest(payload).removeprefix("sha256:")
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO phase4_subject_grants(
                grant_id, task_id, authority_domain, subject_type, subject_id,
                access_scope, source_type, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                grant_id,
                task_id,
                authority_domain,
                subject_type,
                subject_id,
                access_scope,
                source_type,
                source_ref,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return grant_id


def has_subject_grant(
    store: AstraStore,
    *,
    task_id: str,
    authority_domain: str,
    subject_type: str,
    subject_id: str,
    access_scope: str,
) -> bool:
    row = store.query_one(
        """
        SELECT 1 FROM phase4_subject_grants
        WHERE task_id = ? AND authority_domain = ? AND subject_type = ?
          AND subject_id = ? AND revoked_at IS NULL
          AND (
              access_scope = ? OR access_scope = 'effect_candidate'
              OR (? = 'effect_candidate' AND access_scope = 'read')
          )
        LIMIT 1
        """,
        (
            task_id,
            authority_domain,
            subject_type,
            subject_id,
            access_scope,
            access_scope,
        ),
    )
    return row is not None


def subject_validated_in_user_resolution(
    store: AstraStore,
    *,
    task_id: str,
    subject_id: str,
) -> str | None:
    rows = store.query_all(
        """
        SELECT interaction_id, resolution_json FROM interactions
        WHERE task_id = ? AND status = 'resolved' AND kind = 'user_input'
        ORDER BY resolved_at
        """,
        (task_id,),
    )
    for row in rows:
        raw = str(row["resolution_json"] or "")
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        matched = value.get("matched_identifiers")
        if (
            isinstance(matched, list)
            and all(isinstance(item, str) for item in matched)
            and subject_id in matched
        ):
            return str(row["interaction_id"])
    return None


def record_dynamic_effect_intent(
    store: AstraStore,
    *,
    task_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    effect: AuthorizedEffect,
    approval_requirement: ApprovalRequirement,
) -> None:
    arguments_hash = sha256_digest(dict(arguments))
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO phase4_dynamic_effect_intents(
                task_id, effect_intent_id, effect_json,
                approval_requirement_json, source_tool_name,
                source_arguments_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, source_tool_name, source_arguments_hash)
            DO NOTHING
            """,
            (
                task_id,
                effect.effect_intent_id,
                effect.model_dump_json(),
                approval_requirement.model_dump_json(),
                tool_name,
                arguments_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
