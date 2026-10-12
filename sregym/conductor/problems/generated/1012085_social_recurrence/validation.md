Live validation used the existing social-network deployment and the conductor SLO endpoint with a 120-second window. Every recorded verdict below is preserved in the session journal.

| Check | UTC | Violating rounds | Error ratio |
| --- | --- | --- | --- |
| initial-impact-check-1 | 13:48:31 | 11/11 | 37.7–41.6% |
| initial-impact-check-2 | 13:49:56 | 11/11 | 37.7–41.4% |
| runtime-policy-repair-full-window | 13:55:23 | 0/11 | 0.0–0.0% |
| runtime-policy-repair-still-healthy | 13:57:04 | 0/11 | 0.0–0.0% |
| recurring-publication-full-impact | 14:00:57 | 11/11 | 36.9–39.6% |
| reference-mitigation-full-window | 14:03:59 | 0/11 | 0.0–0.0% |
| reference-mitigation-past-refresh | 14:04:55 | 0/11 | 0.0–0.0% |
| cleanup-full-window | 14:05:39 | 0/11 | 0.0–0.0% |

The initial two violation checks were more than 60 seconds apart. Frontend and user-timeline error logs rose from zero to more than 4,000 lines per two-minute observation, satisfying the observability requirement. All pods stayed Running and Ready during steady impact.

Replacing both Redis pods restored their stock runtime policy and temporarily cleared errors. The remote publisher subsequently restored the write requirement through Service DNS. A further settings-only repair passed two full healthy SLO windows and then regressed at the next six-minute publication.

The reference fix was executed from a directory containing only mitigate.sh and redis-config.sh, with no original-state.json. It was run twice successfully. Health remained clean beyond the next scheduled publication time. recover.sh was also run twice successfully; Deployment and ConfigMap content matched the original cluster state and all SLO rounds stayed healthy.

The final scripts were replayed twice successfully. Exactly one publisher remained active, with both runtime policies set to one. Final sustained-impact checks are recorded below.

| Final check | UTC | Violating rounds | Error ratio |
| --- | --- | --- | --- |
| final-replay-impact-check-1 | 14:08:10 | 11/11 | 35.4–40.4% |
| final-replay-impact-check-2 | 14:09:31 | 11/11 | 35.2–40.4% |

Final checks were 80.2 seconds apart. The fault is left active.
