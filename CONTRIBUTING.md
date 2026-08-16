# Contribution and Branching

## Branch model

This repository uses a lightweight MVP branch model:

```text
main
└── codex/astra-mainline
    ├── codex/feat/<topic>
    ├── codex/fix/<topic>
    ├── codex/refactor/<topic>
    ├── codex/test/<topic>
    └── codex/docs/<topic>
```

`main` is the stable, demonstrable baseline. Only reviewed and verified work
is merged into it. `origin/main` is its remote counterpart.

`codex/astra-mainline` is the active integration branch for the current Astra
MVP. It is not a replacement for `main`; it is the branch on which short-lived
topic branches are integrated before a verified merge to `main`.

Do not create permanent `dev`, `fix`, or `refactor` branches. These are work
types, not long-lived integration environments.

## Topic branches

Create a topic branch from `codex/astra-mainline` when the change is isolated,
needs review, or may be developed in parallel. Use lowercase kebab-case names:

| Change type | Pattern | Example |
| --- | --- | --- |
| New behavior | `codex/feat/<topic>` | `codex/feat-capability-broker` |
| Defect correction | `codex/fix/<topic>` | `codex/fix-approval-timeout` |
| Behavior-preserving restructuring | `codex/refactor/<topic>` | `codex/refactor-runtime-boundary` |
| Test or evidence work | `codex/test/<topic>` | `codex/test-e2e-evidence` |
| Documentation only | `codex/docs/<topic>` | `codex/docs-runtime-guide` |

Each topic branch should have one purpose. A refactor must not introduce new
product behavior unless that behavior is separately documented and tested.

## Merge and cleanup

1. Run the relevant tests and evidence checks on the topic branch.
2. Merge the verified topic branch into `codex/astra-mainline`.
3. Merge `codex/astra-mainline` into `main` only when the integrated result is
   stable and demonstrable.
4. Delete the topic branch after merging. Preserve its history through the
   merge commit or pull request, not through a permanent branch.

For an urgent production correction, a `codex/fix/<topic>` branch may start
from `main`. Merge it into `main` first, then bring the same change back to
`codex/astra-mainline`.

## MVP escalation rule

Introduce a long-lived development or release branch only when the team needs
to support parallel release trains, multiple maintained versions, or a release
cadence that makes `main` and `codex/astra-mainline` insufficient.
