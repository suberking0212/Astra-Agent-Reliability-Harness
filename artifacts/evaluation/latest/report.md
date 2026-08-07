# Hermes × Astra Reliability Evaluation Report

> Observed run metrics; evidence scope is defined by the claim-limit note below.

| Profile | Trials | True success | False success | Duplicate | Unauthorized | Observed interruption continuation | Unresolved | Mean tokens | Median tokens | Mean latency(s) | Human | Tools | Provider calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| astra_controlled | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 90.0 | 90.0 | 23.717 | 0 | 4 | 6 |
| astra_full_hermes | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 90.0 | 90.0 | 25.913 | 0 | 4 | 6 |
| native_full | 1 | 1.000 | 0.000 | 0.000 | 0.000 | N/A | 0.000 | 90.0 | 90.0 | 33.864 | 0 | 4 | 6 |

## Observed Task B deltas (not learning proof)

No paired-learning trials were present.

> Metrics describe observed runs only. Scripted-provider results validate deterministic harness behavior, not Agent autonomy or live-model behavior; scripted-provider runs do not prove learning; native_full is a non-Astra baseline; its waiting-input case verifies same-session Hermes history continuation, not Astra Runtime recovery; its process restart remains unverified as recovery; process-recovery acceptance requires actual OS process termination rather than an injected exception. Comparative superiority requires comparable live-provider data.
