# Hermes × Astra Reliability Evaluation Report

> Observed run metrics; evidence scope is defined by the claim-limit note below.

| Profile | Trials | True success | False success | Duplicate | Unauthorized | Observed interruption continuation | Unresolved | Mean tokens | Median tokens | Mean latency(s) | Human | Tools | Provider calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| astra_controlled | 11 | 0.818 | 0.091 | 0.000 | 0.000 | 1.000 | 0.000 | 87.3 | 90.0 | 37.444 | 6 | 40 | 65 |
| astra_full_hermes | 11 | 0.818 | 0.091 | 0.000 | 0.000 | 1.000 | 0.000 | 87.3 | 90.0 | 38.179 | 6 | 40 | 65 |
| native_full | 11 | 0.727 | 0.273 | 0.000 | 0.091 | N/A | 0.091 | 79.1 | 90.0 | 33.276 | 3 | 37 | 63 |

## Observed Task B deltas (not learning proof)

| Profile | Rep | Success Δ | False success Δ | Tools Δ | Errors Δ | Tokens Δ | Latency Δ(s) | Skill reused | Artifacts | Unsafe transfer |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| astra_controlled | 1 | 0 | 0 | 0 | 0 | 0 | -0.016 | false | 0 | false |
| astra_full_hermes | 1 | 0 | 0 | 0 | 0 | 0 | 2.075 | false | 0 | false |
| native_full | 1 | 0 | 0 | 0 | 0 | 0 | 7.580 | false | 0 | false |

> Metrics describe observed runs only. Scripted-provider results validate deterministic harness behavior, not Agent autonomy or live-model behavior; scripted-provider runs do not prove learning; native_full is a non-Astra baseline; its waiting-input case verifies same-session Hermes history continuation, not Astra Runtime recovery; its process restart remains unverified as recovery; process-recovery acceptance requires actual OS process termination rather than an injected exception. Comparative superiority requires comparable live-provider data.
