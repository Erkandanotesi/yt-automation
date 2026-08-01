from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SOURCE_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = SOURCE_ROOT.parent

from file_utils import atomic_write_json  # noqa: E402
from file_locations import MEDIA_BENCHMARK_FILE, MEDIA_INDEX_REPORT_FILE  # noqa: E402
from media_index import MediaIndexSettings, load_media_index, normalize_search_text  # noqa: E402
from media_selection_stage import MediaSelectionSettings, infer_scene_intent, shortlist_assets  # noqa: E402


def evaluate_retrieval(
    *,
    benchmark_path: Path,
    report_path: Path,
    index_settings: MediaIndexSettings | None = None,
    selection_settings: MediaSelectionSettings | None = None,
    rescan: bool = True,
) -> dict:
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    cases = benchmark.get("cases", [])
    if len(cases) < 20:
        raise RuntimeError("The retrieval benchmark must contain at least 20 cases.")
    index_settings = index_settings or MediaIndexSettings.from_environment()
    selection_settings = (
        selection_settings or MediaSelectionSettings.from_environment()
    )
    index = load_media_index(index_settings, rescan=rescan)
    results = []
    previous_sha = ""
    accepted = 0
    wrong_entity = 0
    immediate_duplicates = 0
    crop_failures = 0
    for position, case in enumerate(cases, start=1):
        query_text = str(case["text"])
        window = {
            "window_id": f"benchmark_{position:03d}",
            "text": query_text,
            "entities": list(case.get("entities", [])),
            "scene_intent": infer_scene_intent(
                " ".join(
                    [
                        query_text,
                        str(case.get("visual_query", "")),
                        *case.get("entities", []),
                    ]
                )
            ),
            "visual_queries": [str(case.get("visual_query", ""))],
        }
        shortlist = shortlist_assets(
            index,
            window,
            selection_settings,
            previous_sha=previous_sha,
        )
        selected = shortlist[0]
        selected_path = normalize_search_text(
            selected["workspace_relative_path"]
        )
        accepted_fragments = [
            normalize_search_text(value)
            for value in case.get("accepted_path_fragments", [])
        ]
        is_accepted = any(
            fragment in selected_path for fragment in accepted_fragments
        )
        expected_entity_tokens = {
            token
            for entity in case.get("entities", [])
            for token in normalize_search_text(entity).split()
            if len(token) > 2
        }
        entity_present = any(
            token in selected_path
            for token in expected_entity_tokens
        )
        is_duplicate = bool(
            previous_sha and selected["sha256"] == previous_sha
        )
        is_crop_failure = "crop_risk" in selected.get("quality_flags", [])
        accepted += int(is_accepted)
        wrong_entity += int(not is_accepted and not entity_present)
        immediate_duplicates += int(is_duplicate)
        crop_failures += int(is_crop_failure)
        results.append(
            {
                "id": case["id"],
                "accepted": is_accepted,
                "selected_path": selected["workspace_relative_path"],
                "selected_asset_id": selected["asset_id"],
                "local_score": selected["local_score"],
                "expected_path_fragments": case["accepted_path_fragments"],
                "entity_present": entity_present,
                "immediate_duplicate": is_duplicate,
                "crop_failure": is_crop_failure,
                "shortlist": [
                    {
                        "path": item["workspace_relative_path"],
                        "score": item["local_score"],
                    }
                    for item in shortlist[:3]
                ],
            }
        )
        previous_sha = selected["sha256"]
    total = len(results)
    report = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark_path": str(benchmark_path),
        "media_index_fingerprint": index.get("index_fingerprint", ""),
        "scope": (
            "Current descriptively renamed assets only; rerun as filename tagging "
            "progresses."
        ),
        "case_count": total,
        "metrics": {
            "top1_acceptance_rate": round(accepted / total, 4),
            "top1_accepted": accepted,
            "wrong_entity_rate": round(wrong_entity / total, 4),
            "wrong_entity_count": wrong_entity,
            "immediate_duplicate_rate": round(immediate_duplicates / total, 4),
            "immediate_duplicate_count": immediate_duplicates,
            "crop_failure_rate": round(crop_failures / total, 4),
            "crop_failure_count": crop_failures,
            "milestone_target": 0.8,
            "milestone_target_passed": accepted / total >= 0.8,
        },
        "results": results,
    }
    atomic_write_json(report_path, report)
    return report


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate local media retrieval without making API calls."
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=MEDIA_BENCHMARK_FILE,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=MEDIA_INDEX_REPORT_FILE,
    )
    parser.add_argument("--no-rescan", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    report = evaluate_retrieval(
        benchmark_path=args.benchmark.resolve(),
        report_path=args.report.resolve(),
        rescan=not args.no_rescan,
    )
    if args.json:
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    else:
        metrics = report["metrics"]
        print(
            "Top-1 acceptance: "
            f"{metrics['top1_accepted']}/{report['case_count']} "
            f"({metrics['top1_acceptance_rate']:.0%})"
        )
        print(f"Report: {args.report.resolve()}")
    return 0 if report["metrics"]["milestone_target_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
