"""Inspect saved X page captures without browser access or archive writes."""

import argparse
import json
import sys
from pathlib import Path

from .adapters.x.threads import ThreadGraph


def inspect(paths: list[Path], selected_id: str) -> dict:
    records, observations, issues = [], [], []
    for path in paths:
        capture = json.loads(path.read_text(encoding="utf-8"))
        if capture.get("schema_version") != 1:
            raise ValueError(f"Unsupported capture schema: {path}")
        if not isinstance(capture.get("records"), list):
            raise ValueError(f"Capture has no record list: {path}")
        observations.append(
            {
                key: capture[key]
                for key in ("page_url", "observed_at", "page_state", "coverage")
            }
        )
        records.extend(capture["records"])
        issues.extend(capture["issues"])
    graph = ThreadGraph(records)
    if selected_id not in graph.nodes:
        raise ValueError(
            f"Selected post {selected_id} was not captured; do not treat this as an empty thread"
        )
    return {
        "thread": graph.resolve(selected_id).to_dict(),
        "observed_unique_posts": len(graph.nodes),
        "observations": observations,
        "extraction_issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--selected-id", required=True)
    args = parser.parse_args()
    try:
        report = inspect(args.captures, args.selected_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Capture inspection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
