"""Deterministic post-run timing review without reading book content."""

from __future__ import annotations

from typing import Any, Dict, Mapping


TOTAL_KEYS = frozenset({"total", "total_before_publication"})


def _seconds_map(value: object) -> Dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, float] = {}
    for raw_key, raw_value in value.items():
        try:
            seconds = max(0.0, float(raw_value))
        except (TypeError, ValueError):
            continue
        result[str(raw_key)] = round(seconds, 3)
    return result


def build_performance_review(
    *,
    route: str,
    elapsed_seconds: object,
    workspace: Mapping[str, Any],
    route_details: Mapping[str, Any],
) -> Dict[str, Any]:
    """Rank measured stages and emit conservative, quality-gated experiments."""
    try:
        elapsed = max(0.0, float(elapsed_seconds))
    except (TypeError, ValueError):
        elapsed = 0.0
    time_blocks = _seconds_map(workspace.get("stage_timings_seconds", {}))
    ranked = sorted(
        ((name, seconds) for name, seconds in time_blocks.items() if name not in TOTAL_KEYS),
        key=lambda item: (-item[1], item[0]),
    )
    top = [
        {
            "stage": name,
            "seconds": seconds,
            "share_of_elapsed": round(seconds / elapsed, 4) if elapsed else 0.0,
        }
        for name, seconds in ranked[:3]
    ]
    bottleneck = top[0] if top else {"stage": "unavailable", "seconds": 0.0, "share_of_elapsed": 0.0}
    experiments = []
    if bottleneck["stage"] == "master_materialization":
        if "scanned-pdf" in route:
            experiments.extend(
                [
                    {
                        "id": "page-level-ocr-routing",
                        "priority": 1,
                        "proposal": "仅对纯图、乱码或低置信页运行 OCR，有可靠文字层的页直接提取。",
                    },
                    {
                        "id": "amortize-model-initialization",
                        "priority": 2,
                        "proposal": "批量任务内复用已验证的 Docling/OCR 进程或模型实例，减少重复加载。",
                    },
                    {
                        "id": "ocr-model-benchmark",
                        "priority": 3,
                        "proposal": "只在冻结的中文、表格和图片页样本上比较 OCR 候选，速度与质量同时过门才替换。",
                    },
                ]
            )
        else:
            experiments.append(
                {
                    "id": "source-to-master-profile",
                    "priority": 1,
                    "proposal": "拆分源解析、语义重建和主 EPUB 打包耗时，优先优化占比最高的子阶段。",
                }
            )
    elif bottleneck["stage"] == "final_conversion_and_validation":
        experiments.append(
            {
                "id": "final-artifact-cache",
                "priority": 1,
                "proposal": "优先使用已验对象的 exact final 命中；不得删除 Kindle 回读门来换速度。",
            }
        )
    elif bottleneck["stage"] == "runtime_verification":
        experiments.append(
            {
                "id": "verified-runtime-session-cache",
                "priority": 1,
                "proposal": "只在同一受控批次和不变运行时哈希下复用深度核验证据，不降低完整性门。",
            }
        )
    if not experiments:
        experiments.append(
            {
                "id": "retain-current-route",
                "priority": 1,
                "proposal": "当前没有足够大且安全的自动优化点；保留基线并等待下一个真实样本。",
            }
        )
    return {
        "schema_version": 1,
        "status": "REVIEW_REQUIRED",
        "elapsed_seconds": round(elapsed, 3),
        "time_blocks_seconds": time_blocks,
        "pdf_stage_timings_seconds": _seconds_map(route_details.get("pdf_stage_timings_seconds", {})),
        "pdf_backend_stage_timings_seconds": _seconds_map(route_details.get("pdf_backend_stage_timings_seconds", {})),
        "top_time_blocks": top,
        "primary_bottleneck": bottleneck,
        "candidate_experiments": experiments,
        "adoption_gate": {
            "minimum_total_speedup_ratio": 0.10,
            "or_minimum_stage_saving_seconds": 15.0,
            "quality_regressions_allowed": 0,
            "failed_experiment_action": "revert",
        },
        "agent_closeout_required": [
            "read_this_review",
            "name_primary_bottleneck",
            "run_or_defer_one_safe_experiment_with_reason",
            "compare_timing_and_quality_before_next_book",
        ],
    }


def review_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    details = report.get("details", {})
    if not isinstance(details, Mapping):
        details = {}
    workspace = details.get("workspace", {})
    if not isinstance(workspace, Mapping):
        workspace = {}
    return build_performance_review(
        route=str(report.get("route", "unknown")),
        elapsed_seconds=details.get("elapsed_seconds", 0.0),
        workspace=workspace,
        route_details=details,
    )
