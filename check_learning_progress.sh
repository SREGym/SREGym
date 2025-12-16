#!/bin/bash
# Quick check script for learning run progress

echo "=========================================="
echo "LEARNING RUN PROGRESS CHECK"
echo "=========================================="
echo

# Find latest log (check both test_fixes and retest)
LATEST_LOG=$(ls -t learning_run_retest_*.log learning_run_test_fixes_*.log 2>/dev/null | head -1)

if [ -z "$LATEST_LOG" ]; then
    echo "❌ No learning run log found"
    exit 1
fi

echo "📄 Log: $LATEST_LOG"
echo "   Size: $(du -h "$LATEST_LOG" | cut -f1)"
echo

# Check if process is running
if pgrep -f "run_5_rounds_learning.py" > /dev/null; then
    echo "✅ Learning run is ACTIVE"
else
    echo "⚠️  Learning run process not found (may have completed)"
fi
echo

# Point identification
echo "🔍 POINT IDENTIFICATION:"
grep "Identified.*used points" "$LATEST_LOG" | tail -10 | while read line; do
    echo "   $line"
done
IDENTIFIED_COUNT=$(grep -c "Identified.*used points" "$LATEST_LOG" 2>/dev/null | tr -d '\n' || echo "0")
if [ "$IDENTIFIED_COUNT" = "0" ] || [ -z "$IDENTIFIED_COUNT" ]; then
    echo "   ⚠️  No point identifications yet"
else
    echo "   Total identifications: $IDENTIFIED_COUNT"
fi
echo

# Validation
echo "✅ VALIDATION:"
grep "Validated.*points" "$LATEST_LOG" | tail -10 | while read line; do
    echo "   $line"
done
VALIDATED_COUNT=$(grep -c "Validated.*points" "$LATEST_LOG" 2>/dev/null | tr -d '\n' || echo "0")
if [ "$VALIDATED_COUNT" = "0" ] || [ -z "$VALIDATED_COUNT" ]; then
    echo "   ⚠️  No validations yet"
else
    echo "   Total validations: $VALIDATED_COUNT"
fi
echo

# Round progress
echo "📈 ROUND PROGRESS:"
grep -E "(ROUND|Round [0-9])" "$LATEST_LOG" | tail -5 | while read line; do
    echo "   $line"
done
echo

# Success/Failure
SUCCESS=$(grep -c "SUCCESS.*Problem:" "$LATEST_LOG" 2>/dev/null || echo "0")
FAILED=$(grep -c "FAILED.*Problem:" "$LATEST_LOG" 2>/dev/null || echo "0")
echo "📊 EXECUTION: Success=$SUCCESS, Failed=$FAILED"
echo

# Run Python monitor for detailed stats
echo "📊 DETAILED STATS:"
python3 monitor_learning_run.py 2>/dev/null | tail -30

