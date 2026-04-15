# scripts/compare_event_sequences.py
"""
Compare event sequences from two JSON files to verify additive-only changes.

Usage:
  python scripts/compare_event_sequences.py old_events.json new_events.json

Each file must contain a JSON array of event objects with at least {"event_type": "..."}.

Exit code 0 = compatible (new has all old events + possibly additive ones)
Exit code 1 = incompatible (new path drops standard events)
"""
from __future__ import annotations

import json
import sys
from collections import Counter


ADDITIVE_ONLY_EVENTS = {"task_dag", "coordinator_step", "task_start", "task_complete", "step"}
STANDARD_EVENTS = {"question", "task", "node", "checkpoint", "file", "result", "error"}


def load_events(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected JSON array, got {type(data)}")
    return data


def event_type_counts(events: list[dict]) -> Counter:
    return Counter(e.get("event_type") for e in events if e.get("event_type"))


def compare(old_path: str, new_path: str) -> int:
    old_events = load_events(old_path)
    new_events = load_events(new_path)

    old_counts = event_type_counts(old_events)
    new_counts = event_type_counts(new_events)

    print(f"Old path events: {dict(old_counts)}")
    print(f"New path events: {dict(new_counts)}")

    errors: list[str] = []
    for event_type in STANDARD_EVENTS:
        if event_type in old_counts and event_type not in new_counts:
            errors.append(f"DROPPED: '{event_type}' present in old path but missing in new path")
        elif event_type in old_counts and new_counts[event_type] < old_counts[event_type]:
            errors.append(
                f"REDUCED: '{event_type}' count dropped from {old_counts[event_type]} "
                f"to {new_counts[event_type]} (may indicate suppression)"
            )

    # New additive events
    additive_added = {et for et in new_counts if et not in old_counts and et in ADDITIVE_ONLY_EVENTS}
    if additive_added:
        print(f"OK — additive-only new events: {additive_added}")

    unexpected_new = {et for et in new_counts if et not in old_counts and et not in ADDITIVE_ONLY_EVENTS}
    if unexpected_new:
        print(f"WARNING — unexpected new non-standard events: {unexpected_new}")

    if errors:
        print("\nERRORS:")
        for err in errors:
            print(f"  x {err}")
        return 1
    else:
        print("\nOK - Event sequences are compatible.")
        return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} old_events.json new_events.json")
        sys.exit(2)
    sys.exit(compare(sys.argv[1], sys.argv[2]))
