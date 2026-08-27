#!/usr/bin/env python3
"""Evaluate reproducible AER retrieval baselines and state-feature ablations."""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS = ROOT / "experiments/retrieval/generated/query_sessions.jsonl"
TOKEN = re.compile(r"[a-z0-9]+")
BM25_K1 = 1.2
BM25_B = 0.75
LINKED_CHART_IDS = {
    1106: [1108],
}
METHODS = (
    "query_only_bm25",
    "history_bm25",
    "state_serialized_bm25",
    "structured_state_compatibility",
    "state_hybrid",
    "state_hybrid_no_active_tab",
    "state_hybrid_no_filters",
    "state_hybrid_no_link",
)


def tokens(value: object) -> list[str]:
    return TOKEN.findall(str(value).lower().replace("_", " ").replace("-", " "))


def text_for_values(values: object) -> str:
    if isinstance(values, dict):
        return " ".join(f"{key} {text_for_values(value)}" for key, value in values.items())
    if isinstance(values, list):
        return " ".join(text_for_values(value) for value in values)
    return str(values)


def chart_catalog(catalog_report: dict) -> tuple[dict[int, dict], dict]:
    metadata = catalog_report["chart_catalog"]
    catalog = {}
    for chart in metadata["charts"]:
        chart_id = int(chart["chart_id"])
        catalog[chart_id] = {
            "document": chart["document"],
            "tabs": {chart["tab"]} if chart.get("tab") else set(),
            "filter_terms": set(
                tokens(
                    text_for_values(
                        [*chart.get("dimensions", []), *chart.get("filter_fields", [])]
                    )
                )
            ),
        }
    return catalog, metadata


def bm25_scores(query: str, docs: dict[int, str]) -> dict[int, float]:
    doc_tokens = {chart_id: tokens(text) for chart_id, text in docs.items()}
    n_docs = len(doc_tokens)
    lengths = [len(value) for value in doc_tokens.values()]
    avg_len = sum(lengths) / len(lengths)
    df = Counter(token for value in doc_tokens.values() for token in set(value))
    scores = {chart_id: 0.0 for chart_id in doc_tokens}
    for term in tokens(query):
        idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
        for chart_id, document in doc_tokens.items():
            frequency = document.count(term)
            if not frequency:
                continue
            denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * len(document) / avg_len)
            scores[chart_id] += idf * frequency * (BM25_K1 + 1) / denominator
    return scores


def normalized_bm25_scores(query: str, docs: dict[int, str]) -> dict[int, float]:
    scores = bm25_scores(query, docs)
    maximum = max(scores.values(), default=0.0)
    if maximum <= 0:
        return scores
    return {chart_id: score / maximum for chart_id, score in scores.items()}


def state_text(state: dict) -> str:
    return text_for_values(
        {
            "active_tab": state.get("active_tab", ""),
            "filters": state.get("filters", {}),
        }
    )


def compatibility_scores(state: dict, catalog: dict[int, dict], enabled: set[str]) -> dict[str, dict[int, float]]:
    scores = {feature: {chart_id: 0.0 for chart_id in catalog} for feature in enabled}
    filter_terms = set(tokens(text_for_values(state.get("filters", {}))))
    for chart_id, metadata in catalog.items():
        if "active_tab" in enabled and state.get("active_tab") in metadata["tabs"]:
            scores["active_tab"][chart_id] = 1.0
        if "filters" in enabled and filter_terms:
            overlap = filter_terms & metadata["filter_terms"]
            if overlap:
                scores["filters"][chart_id] = len(overlap) / len(filter_terms)
        if "link" in enabled:
            interaction_source = state.get("focused_chart_id")
            linked_charts = LINKED_CHART_IDS.get(interaction_source, [])
            if chart_id in linked_charts:
                scores["link"][chart_id] = 1.0
    return scores


def method_features(method: str) -> set[str]:
    if method == "structured_state_compatibility":
        return {"active_tab", "filters"}
    if method.startswith("state_hybrid"):
        features = {"active_tab", "filters", "link"}
        if method == "state_hybrid_no_active_tab":
            features.remove("active_tab")
        elif method == "state_hybrid_no_filters":
            features.remove("filters")
        elif method == "state_hybrid_no_link":
            features.remove("link")
        return features
    return set()


def rank_for(row: dict, catalog: dict[int, dict], method: str) -> dict:
    docs = {chart_id: metadata["document"] for chart_id, metadata in catalog.items()}
    query = row["query"]
    if method != "query_only_bm25" and row.get("dialogue_history"):
        query = f"{row['dialogue_history']} {query}"
    if method == "state_serialized_bm25":
        query = f"{query} {state_text(row['state'])}"
    contributions: dict[str, dict[int, float]] = {
        "text_bm25": normalized_bm25_scores(query, docs)
    }
    enabled = method_features(method)
    for feature, values in compatibility_scores(row["state"], catalog, enabled).items():
        contributions[feature] = values

    text_scores = contributions["text_bm25"]
    if method in {"query_only_bm25", "history_bm25", "state_serialized_bm25"}:
        ranking = [
            chart_id
            for chart_id, score in sorted(
                text_scores.items(), key=lambda item: (-item[1], item[0])
            )
            if score > 0
        ]
        priority_keys = {
            chart_id: [round(text_scores[chart_id], 6)]
            for chart_id in catalog
        }
        return {
            "ranking": ranking,
            "scores": text_scores,
            "priority_keys": priority_keys,
            "contributions": contributions,
        }

    # State-aware methods use an explicit priority protocol rather than a
    # weighted sum. The active tab restricts the eligible AER set, an
    # interaction link creates the highest-priority tier, BM25 orders AERs
    # within a tier, and filter compatibility is a deterministic tie-break.
    eligible = set(catalog)
    if "active_tab" in enabled:
        tab_candidates = {
            chart_id
            for chart_id, compatible in contributions["active_tab"].items()
            if compatible > 0
        }
        if tab_candidates:
            eligible = tab_candidates

    link_scores = contributions.get("link", {chart_id: 0.0 for chart_id in catalog})
    filter_scores = contributions.get(
        "filters", {chart_id: 0.0 for chart_id in catalog}
    )
    candidates = [
        chart_id
        for chart_id in eligible
        if (
            text_scores[chart_id] > 0
            or link_scores[chart_id] > 0
            or filter_scores[chart_id] > 0
            or "active_tab" in enabled
        )
    ]
    priority_keys = {
        chart_id: [
            link_scores[chart_id],
            text_scores[chart_id],
            filter_scores[chart_id],
        ]
        for chart_id in catalog
    }
    ranking = sorted(
        candidates,
        key=lambda chart_id: (
            -link_scores[chart_id],
            -text_scores[chart_id],
            -filter_scores[chart_id],
            chart_id,
        ),
    )
    return {
        "ranking": ranking,
        "scores": text_scores,
        "priority_keys": priority_keys,
        "contributions": contributions,
    }


def rank_outcomes(rows: list[dict], catalog: dict[int, dict], method: str) -> list[dict]:
    outcomes = []
    for row in rows:
        started = perf_counter()
        result = rank_for(row, catalog, method)
        latency_ms = (perf_counter() - started) * 1000
        accepted = set(row["gold"]["acceptable_chart_ids"])
        position = next(
            (index + 1 for index, chart_id in enumerate(result["ranking"]) if chart_id in accepted),
            None,
        )
        outcomes.append(
            {
                "query_id": row["query_id"],
                "session_id": row["session_id"],
                "variant": row["variant"],
                "state_condition": row.get("state_condition", "oracle"),
                "accepted_chart_ids": sorted(accepted),
                "rank": position,
                "ranking": result["ranking"],
                "scores": {str(chart_id): round(score, 6) for chart_id, score in result["scores"].items()},
                "priority_keys": {
                    str(chart_id): [round(value, 6) for value in values]
                    for chart_id, values in result["priority_keys"].items()
                },
                "feature_contributions": {
                    feature: {str(chart_id): round(score, 6) for chart_id, score in values.items()}
                    for feature, values in result["contributions"].items()
                },
                "latency_ms": latency_ms,
            }
        )
    return outcomes


def quantile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = max(0, math.ceil(len(values) * percentile) - 1)
    return sorted(values)[index]


def metric_values(outcomes: list[dict]) -> dict[str, float]:
    count = len(outcomes)
    recall_1 = sum(outcome["rank"] == 1 for outcome in outcomes) / count
    recall_3 = sum(outcome["rank"] is not None and outcome["rank"] <= 3 for outcome in outcomes) / count
    mrr = sum(1 / outcome["rank"] if outcome["rank"] else 0 for outcome in outcomes) / count
    return {"recall_at_1": recall_1, "recall_at_3": recall_3, "mrr": mrr}


def bootstrap_confidence_intervals(outcomes: list[dict], samples: int, seed: int) -> dict[str, list[float]]:
    by_query: dict[str, list[dict]] = defaultdict(list)
    for outcome in outcomes:
        by_query[outcome["query_id"]].append(outcome)
    query_ids = sorted(by_query)
    rng = random.Random(seed)
    draws: dict[str, list[float]] = defaultdict(list)
    for _ in range(samples):
        resampled = [outcome for _ in query_ids for outcome in by_query[rng.choice(query_ids)]]
        for name, value in metric_values(resampled).items():
            draws[name].append(value)
    return {
        name: [round(quantile(values, 0.025), 4), round(quantile(values, 0.975), 4)]
        for name, values in draws.items()
    }


def paired_bootstrap_difference(reference: list[dict], comparison: list[dict], samples: int, seed: int) -> dict[str, dict]:
    reference_by_query: dict[str, list[dict]] = defaultdict(list)
    comparison_by_query: dict[str, list[dict]] = defaultdict(list)
    for outcome in reference:
        reference_by_query[outcome["query_id"]].append(outcome)
    for outcome in comparison:
        comparison_by_query[outcome["query_id"]].append(outcome)
    query_ids = sorted(reference_by_query)
    if query_ids != sorted(comparison_by_query):
        raise ValueError("Paired comparison requires identical query ids.")
    point_reference = metric_values(reference)
    point_comparison = metric_values(comparison)
    rng = random.Random(seed)
    draws: dict[str, list[float]] = defaultdict(list)
    for _ in range(samples):
        selected = [rng.choice(query_ids) for _ in query_ids]
        ref_rows = [outcome for query_id in selected for outcome in reference_by_query[query_id]]
        cmp_rows = [outcome for query_id in selected for outcome in comparison_by_query[query_id]]
        ref_metrics = metric_values(ref_rows)
        cmp_metrics = metric_values(cmp_rows)
        for name in ref_metrics:
            draws[name].append(ref_metrics[name] - cmp_metrics[name])
    return {
        name: {
            "difference": round(point_reference[name] - point_comparison[name], 4),
            "ci95": [round(quantile(values, 0.025), 4), round(quantile(values, 0.975), 4)],
            "two_sided_bootstrap_p": round(
                min(1.0, 2 * min(sum(value <= 0 for value in values) / samples, sum(value >= 0 for value in values) / samples)),
                4,
            ),
        }
        for name, values in draws.items()
    }


def summarize(outcomes: list[dict], samples: int, seed: int) -> dict:
    result = {name: round(value, 4) for name, value in metric_values(outcomes).items()}
    latency = [outcome["latency_ms"] for outcome in outcomes]
    result["latency_ms"] = {
        "mean": round(sum(latency) / len(latency), 3),
        "p50": round(quantile(latency, 0.5), 3),
        "p95": round(quantile(latency, 0.95), 3),
    }
    result["confidence_intervals"] = bootstrap_confidence_intervals(outcomes, samples, seed)
    groups = {"per_variant": "variant", "per_state_condition": "state_condition"}
    for result_key, group_key in groups.items():
        grouped: dict[str, list[dict]] = defaultdict(list)
        for outcome in outcomes:
            grouped[outcome[group_key]].append(outcome)
        result[result_key] = {
            key: {**{name: round(value, 4) for name, value in metric_values(values).items()}, "count": len(values)}
            for key, values in sorted(grouped.items())
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="Validation report containing the live-metadata chart_catalog.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rankings-output", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260721)
    args = parser.parse_args()

    if args.bootstrap_samples < 1:
        raise ValueError("--bootstrap-samples must be positive")
    rows = [json.loads(line) for line in args.sessions.read_text(encoding="utf-8").splitlines() if line]
    catalog_report = json.loads(args.catalog.read_text(encoding="utf-8"))
    catalog, catalog_metadata = chart_catalog(catalog_report)
    outcomes_by_method = {method: rank_outcomes(rows, catalog, method) for method in METHODS}
    rankings_output = args.rankings_output or args.output.with_name("rankings.jsonl")
    rankings_output.parent.mkdir(parents=True, exist_ok=True)
    rankings_output.write_text(
        "".join(
            json.dumps({"method": method, **outcome}, sort_keys=True) + "\n"
            for method, outcomes in outcomes_by_method.items()
            for outcome in outcomes
        ),
        encoding="utf-8",
    )
    report = {
        "benchmark": "AER offline retrieval audit",
        "evaluation_target": (
            "Context Manager ranking signals over an already constructed "
            "existing-chart AER collection"
        ),
        "sessions": len(rows),
        "queries": len({row["query_id"] for row in rows}),
        "evaluated_query_ids": sorted({row["query_id"] for row in rows}),
        "candidate_aers": len(catalog),
        "benchmark_protocol": {
            "state_condition": "oracle state reconstructed from annotated interactions",
            "interaction_link_policy": {
                "cross_filter": "interaction source chart routed through the control-to-result link",
                "hover": "retained in reference state but receives no direct ranking bonus",
                "all_other_interactions": "no interaction link supplied",
            },
            "query_variants": {
                "explicit": "full task query without dialogue history",
                "context_dependent": "generic current-view query and target-omitted history",
                "elliptical_followup": "generic follow-up and target-omitted history",
            },
        },
        "document_construction": {
            "fields": ["chart name", "tab", "visualization type", "measures", "dimensions", "filter fields"],
            "source": catalog_metadata["source"],
            "excluded_fields": catalog_metadata["excluded_fields"],
        },
        "ranking_configuration": {
            "bm25": {
                "k1": BM25_K1,
                "b": BM25_B,
                "score_normalization": "divide by maximum candidate score per query",
            },
            "state_serialized_bm25": (
                "dialogue-conditioned query concatenated with active-tab and "
                "filter text before BM25; no manual feature weights"
            ),
            "full_state_priority_protocol": {
                "candidate_scope": "AERs on the active tab when the tab signal is enabled",
                "priority_order": [
                    "interaction-linked tier",
                    "BM25 text similarity within tier",
                    "filter compatibility as tie-break",
                    "ascending chart id as final deterministic tie-break",
                ],
                "manual_feature_weights": "none",
            },
            "linked_chart_ids": LINKED_CHART_IDS,
            "full_state_features": [
                "dialogue-conditioned query text",
                "active tab",
                "filter compatibility",
                "interaction link",
            ],
            "direct_focus_bonus": "disabled",
            "candidate_inclusion": (
                "all active-tab AERs when tab scope is enabled; otherwise an "
                "AER requires positive text, link, or filter compatibility"
            ),
            "final_tie_break": "ascending chart id",
        },
        "method_definitions": {
            "state_hybrid": "tab scope, then linked tier, text rank, and filter tie-break",
            "state_hybrid_no_active_tab": "linked tier, text rank, and filter tie-break over all AERs",
            "state_hybrid_no_filters": "tab scope, then linked tier and text rank",
            "state_hybrid_no_link": "tab scope, then text rank and filter tie-break",
        },
        "methods": {
            method: summarize(outcomes, args.bootstrap_samples, args.seed)
            for method, outcomes in outcomes_by_method.items()
        },
        "paired_bootstrap_vs_state_hybrid": {
            method: paired_bootstrap_difference(outcomes_by_method["state_hybrid"], outcomes, args.bootstrap_samples, args.seed)
            for method, outcomes in outcomes_by_method.items()
            if method != "state_hybrid"
        },
        "rankings_output": str(rankings_output),
        "statistical_protocol": {
            "unit": "query id; all variants and state conditions for a sampled query are retained together",
            "bootstrap_samples": args.bootstrap_samples,
            "seed": args.seed,
        },
        "limitations": [
            "The current sessions remain deterministic transformations of curated tasks, not independently collected user conversations.",
            "The AER catalog is extracted from live chart metadata; the oracle state and control-to-result links remain curated.",
            "Noisy state conditions are synthetic stress tests, not observed interaction traces.",
            "Results measure source-chart retrieval, not end-to-end answer accuracy or causal user benefit.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
