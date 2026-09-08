"""Summarize recorded cgroup curves without relying on a healthy metrics stack."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def summarize(directory):
    curves = defaultdict(list)
    identities = {}
    files = sorted(directory.glob("sample-*.json")) + sorted(directory.glob("cgroup-*.json"))
    for path in files:
        sample = json.loads(path.read_text())
        try:
            pods = json.loads(sample["pods"]["out"])["items"]
        except (KeyError, ValueError):
            continue
        for pod in pods:
            meta = pod["metadata"]
            for container in pod.get("status", {}).get("containerStatuses", []):
                cid = container.get("containerID", "").removeprefix("containerd://")
                identities[cid] = f"{meta['namespace']}/{container['name']}"
        for key, result in sample.items():
            if not key.startswith("cgroup/"):
                continue
            for cid, fields in result.get("containers", {}).items():
                if cid not in identities:
                    continue
                stat = dict(line.split() for line in fields.get("memory.stat", "").splitlines())
                try:
                    curves[identities[cid]].append(
                        {
                            "time": sample["time"],
                            "container_id": cid,
                            "current": int(fields["memory.current"]),
                            "anon": int(stat["anon"]),
                            "file": int(stat["file"]),
                            "limit": fields.get("memory.max", "").strip(),
                        }
                    )
                except (KeyError, ValueError):
                    continue
    output = {}
    for name, points in sorted(curves.items()):
        points.sort(key=lambda p: p["time"])
        end = points[-1]["time"]
        last = [p for p in points if p["time"] >= end - 300]
        previous = [p for p in points if end - 600 <= p["time"] < end - 300]
        output[name] = {
            "samples": len(points),
            "span_seconds": points[-1]["time"] - points[0]["time"],
            "container_ids": sorted({p["container_id"] for p in points}),
            "limits": sorted({p["limit"] for p in points}),
            "max_current_mib": round(max(p["current"] for p in points) / 1024**2, 2),
            "max_anon_mib": round(max(p["anon"] for p in points) / 1024**2, 2),
            "max_file_mib": round(max(p["file"] for p in points) / 1024**2, 2),
            "last_5min_median_mib": round(statistics.median(p["current"] for p in last) / 1024**2, 2),
            "previous_5min_median_mib": round(statistics.median(p["current"] for p in previous) / 1024**2, 2)
            if previous
            else None,
        }
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory), indent=2))
