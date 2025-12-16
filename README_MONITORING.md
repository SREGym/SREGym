# Learning Run Monitoring Guide

## Quick Status Check
```bash
./check_learning_progress.sh
```

## Detailed Monitoring
```bash
python3 monitor_learning_run.py
```

## Real-time Monitoring

### Watch Point Identification
```bash
tail -f learning_run_test_fixes_*.log | grep "Identified.*used points"
```

### Watch Validation
```bash
tail -f learning_run_test_fixes_*.log | grep "Validated.*points"
```

### Watch Round Progress
```bash
tail -f learning_run_test_fixes_*.log | grep -E "(Round|ROUND|Starting|Completed)"
```

### Watch Success/Failure
```bash
tail -f learning_run_test_fixes_*.log | grep -E "(SUCCESS|FAILED|success|failed)"
```

## Key Metrics to Monitor

1. **Point Identification**: Should see > 0 for diagnosis/localization/mitigation
2. **Validation**: Should see validation counts after Round 1 completes
3. **Validation Counts in Files**: Should increase after Round 2
4. **Verified Points**: Should appear after 3 validations with 2 successes
5. **Success Rate**: Should improve or at least not decline significantly

## Expected Timeline

- **Round 1**: ~40-60 minutes
  - Points will be generated
  - Point identification should work (with fixes)
  - Validation should occur
  
- **Round 2**: ~40-60 minutes
  - Points from Round 1 should be loaded
  - Point identification should work again
  - Validation counts should update
  - Points may get verified if successful

## Troubleshooting

If point identification shows 0:
- Check if fixes are in place (kubectl mapping, lower threshold, semantic matching)
- Check trace_summary is populated
- Check LLM backend is available

If validation counts don't update:
- Check if point identification is working
- Check if validate_points_from_trace is being called
- Check log for errors
