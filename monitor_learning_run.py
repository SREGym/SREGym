#!/usr/bin/env python3
"""
Monitor learning run for point identification and validation
"""

import json
import re
import time
from pathlib import Path
from datetime import datetime

def find_latest_log():
    """Find the latest learning run log"""
    # Check both retest and test_fixes logs, prioritize retest
    retest_logs = sorted(Path(".").glob("learning_run_retest_*.log"))
    test_fixes_logs = sorted(Path(".").glob("learning_run_test_fixes_*.log"))
    log_files = retest_logs + test_fixes_logs
    if log_files:
        # Return the most recent one
        return max(log_files, key=lambda p: p.stat().st_mtime)
    return None

def analyze_point_identification(log_file):
    """Analyze point identification from log"""
    if not log_file or not log_file.exists():
        return None
    
    with open(log_file, 'r') as f:
        content = f.read()
    
    # Find all "Identified X used points" lines
    pattern = r'Identified (\d+) used points out of (\d+) active points for (\w+) \((\d+) heuristic, (\d+) LLM\)'
    matches = re.findall(pattern, content)
    
    stats = {}
    for match in matches:
        identified, total, agent, heuristic, llm = match
        if agent not in stats:
            stats[agent] = {
                'total_identifications': 0,
                'total_identified': 0,
                'total_heuristic': 0,
                'total_llm': 0,
                'max_points': 0,
            }
        
        stats[agent]['total_identifications'] += 1
        stats[agent]['total_identified'] += int(identified)
        stats[agent]['total_heuristic'] += int(heuristic)
        stats[agent]['total_llm'] += int(llm)
        stats[agent]['max_points'] = max(stats[agent]['max_points'], int(total))
    
    return stats

def analyze_validation(log_file):
    """Analyze point validation from log"""
    if not log_file or not log_file.exists():
        return None
    
    with open(log_file, 'r') as f:
        content = f.read()
    
    # Find all "Validated X points" lines
    pattern = r'Validated (\d+) points for (\w+): (\d+) successful, (\d+) failed'
    matches = re.findall(pattern, content)
    
    stats = {}
    for match in matches:
        total, agent, successful, failed = match
        if agent not in stats:
            stats[agent] = {
                'total_validations': 0,
                'total_validated': 0,
                'total_successful': 0,
                'total_failed': 0,
            }
        
        stats[agent]['total_validations'] += 1
        stats[agent]['total_validated'] += int(total)
        stats[agent]['total_successful'] += int(successful)
        stats[agent]['total_failed'] += int(failed)
    
    return stats

def check_validation_counts():
    """Check validation counts in point files"""
    points_dir = Path("meta_agent/point_prompts")
    if not points_dir.exists():
        return None
    
    stats = {}
    for agent_file in sorted(points_dir.glob("*_points.json")):
        with open(agent_file, 'r') as f:
            points = json.load(f)
        
        agent_name = agent_file.stem.replace("_points", "")
        
        total_verification = sum(p.get('verification_count', 0) for p in points)
        total_success = sum(p.get('success_count', 0) for p in points)
        total_failure = sum(p.get('failure_count', 0) for p in points)
        verified_count = sum(1 for p in points if p.get('verified', False))
        
        stats[agent_name] = {
            'total_points': len(points),
            'total_verification': total_verification,
            'total_success': total_success,
            'total_failure': total_failure,
            'verified_count': verified_count,
            'points_with_validation': sum(1 for p in points if p.get('verification_count', 0) > 0),
        }
    
    return stats

def check_round_progress(log_file):
    """Check round progress"""
    if not log_file or not log_file.exists():
        return None
    
    with open(log_file, 'r') as f:
        lines = f.readlines()
    
    # Find round information
    rounds = []
    for line in lines:
        if "ROUND" in line.upper() or "Round" in line:
            rounds.append(line.strip())
    
    # Find success/failure counts
    success_count = sum(1 for line in lines if "SUCCESS" in line and "Problem:" in line)
    failed_count = sum(1 for line in lines if "FAILED" in line and "Problem:" in line)
    
    return {
        'rounds_detected': len(rounds),
        'round_info': rounds[-5:] if rounds else [],
        'success_count': success_count,
        'failed_count': failed_count,
    }

def print_status():
    """Print current status"""
    print("="*80)
    print(f"LEARNING RUN MONITORING - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    print()
    
    log_file = find_latest_log()
    if not log_file:
        print("❌ No learning run log found")
        return
    
    print(f"📄 Log file: {log_file}")
    print(f"   Size: {log_file.stat().st_size / 1024 / 1024:.2f} MB")
    print()
    
    # Point identification stats
    id_stats = analyze_point_identification(log_file)
    if id_stats:
        print("🔍 POINT IDENTIFICATION:")
        for agent, stats in sorted(id_stats.items()):
            avg_identified = stats['total_identified'] / stats['total_identifications'] if stats['total_identifications'] > 0 else 0
            heuristic_pct = (stats['total_heuristic'] / stats['total_identified'] * 100) if stats['total_identified'] > 0 else 0
            llm_pct = (stats['total_llm'] / stats['total_identified'] * 100) if stats['total_identified'] > 0 else 0
            
            print(f"   {agent}:")
            print(f"      Calls: {stats['total_identifications']}")
            print(f"      Avg identified: {avg_identified:.1f} / {stats['max_points']} points")
            print(f"      Heuristic: {stats['total_heuristic']} ({heuristic_pct:.1f}%)")
            print(f"      LLM: {stats['total_llm']} ({llm_pct:.1f}%)")
            
            if stats['total_identified'] == 0:
                print(f"      ⚠️  NO POINTS IDENTIFIED - FIXES NOT WORKING")
            elif avg_identified > 0:
                print(f"      ✅ Points being identified")
        print()
    else:
        print("🔍 POINT IDENTIFICATION: No data yet")
        print()
    
    # Validation stats
    val_stats = analyze_validation(log_file)
    if val_stats:
        print("✅ VALIDATION:")
        for agent, stats in sorted(val_stats.items()):
            success_rate = (stats['total_successful'] / stats['total_validated'] * 100) if stats['total_validated'] > 0 else 0
            print(f"   {agent}:")
            print(f"      Validations: {stats['total_validations']}")
            print(f"      Points validated: {stats['total_validated']}")
            print(f"      Successful: {stats['total_successful']} ({success_rate:.1f}%)")
            print(f"      Failed: {stats['total_failed']}")
        print()
    else:
        print("✅ VALIDATION: No data yet")
        print()
    
    # Validation counts in files
    file_stats = check_validation_counts()
    if file_stats:
        print("📊 VALIDATION COUNTS IN FILES:")
        for agent, stats in sorted(file_stats.items()):
            print(f"   {agent}:")
            print(f"      Total points: {stats['total_points']}")
            print(f"      Points with validation: {stats['points_with_validation']}")
            print(f"      Verified points: {stats['verified_count']}")
            print(f"      Total validations: {stats['total_verification']} ({stats['total_success']} success, {stats['total_failure']} failure)")
            
            if stats['points_with_validation'] == 0 and stats['total_points'] > 0:
                print(f"      ⚠️  NO VALIDATION DATA - IDENTIFICATION FAILING")
            elif stats['points_with_validation'] > 0:
                print(f"      ✅ Validation working")
        print()
    else:
        print("📊 VALIDATION COUNTS: No point files found yet")
        print()
    
    # Round progress
    round_info = check_round_progress(log_file)
    if round_info:
        print("📈 ROUND PROGRESS:")
        print(f"   Rounds detected: {round_info['rounds_detected']}")
        if round_info['round_info']:
            print(f"   Recent activity:")
            for info in round_info['round_info']:
                print(f"      {info[:100]}")
        print(f"   Success: {round_info['success_count']}, Failed: {round_info['failed_count']}")
        print()

if __name__ == "__main__":
    print_status()


