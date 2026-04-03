# Submitting to the SREGym Leaderboard

To appear on the leaderboard, drop your results CSV in a folder named after your agent:

```
leaderboard/submissions/<your-agent-name>/results.csv
```

Example: `leaderboard/submissions/resolve/results.csv`

## CSV Format

Your CSV must have at minimum:

| Column | Type | Description |
|--------|------|-------------|
| `problem_id` | string | Must match a SREGym problem ID (see registry) |
| `Diagnosis.success` | True/False | Did your agent correctly diagnose the root cause? |
| `Mitigation.success` | True/False | Did your agent successfully mitigate? (optional) |
| `TTL` | float (seconds) | Time to diagnosis (optional) |
| `TTM` | float (seconds) | Time to mitigation (optional) |

### Minimal example

```csv
problem_id,Diagnosis.success,Mitigation.success,TTL,TTM
namespace_memory_limit,True,True,92.3,187.4
rbac_misconfiguration,True,False,134.1,
readiness_probe_misconfiguration_hotel_reservation,False,False,,
```

### Notes

- One row per problem run. If you ran a problem multiple times, include all rows — the leaderboard takes your best attempt.
- `Mitigation.success` is optional. If omitted, ranking is based on diagnosis only.
- The folder name becomes your display name on the leaderboard (e.g. folder `resolve` → agent "resolve").
- You can include multiple CSV files in your folder — all will be loaded.
- Problem IDs must exactly match the registry. See `sregym/conductor/problems/registry.py` for the full list.

## Regenerating the leaderboard

After adding your results:

```bash
python leaderboard/leaderboard.py
open leaderboard/leaderboard.html
```
