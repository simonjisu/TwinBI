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
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"
TOKEN = re.compile(r"[a-z0-9]+")
BM25_K1 = 1.2
BM25_B = 0.75
LINKED_CHART_IDS = {
    1106: [1108],
    1110: [1111],
    1111: [1110],
    1114: [1115],
    1115: [1114],
}
RANKING_WEIGHTS = {
    "state_text_bm25": 1.5,
    "active_tab": 1.0,
    "filter_compatibility": 1.0,
    "interaction": 0.25,
    "focused_chart": 5.0,
    "linked_chart": 5.5,
}
METHODS = (
    "query_only_bm25",
    "history_bm25",
    "state_serialized_bm25",
    "structured_state_compatibility",
    "state_hybrid",
    "state_hybrid_no_active_tab",
    "state_hybrid_no_filters",
    "state_hybrid_no_focus_link",
    "state_hybrid_no_interaction",
)


def tokens(value: object) -> list[str]:
    return TOKEN.findall(str(value).lower().replace("_", " ").replace("-", " "))


def text_for_values(values: object) -> str:
    if isinstance(values, dict):
        return " ".join(f"{key} {text_for_values(value)}" for key, value in values.items())
    if isinstance(values, list):
        return " ".join(text_for_values(value) for value in values)
    return str(values)


def chart_catalog(gold: dict) -> dict[int, dict]:
    names = gold["chart_inventory"]
    catalog = {
        int(chart_id): {
            "document_parts": [name],
            "tabs": set(),
            "filter_terms": set(),
            "interactions": set(),
        }
        for chart_id, name in names.items()
    }
    for entry in gold["entries"]:
        chart = catalog[entry["primary_chart_id"]]
        chart["document_parts"].extend(
            [
                entry["tab"],
                entry["measure"],
                entry["target"],
                text_for_values(entry["dimensions"]),
                text_for_values(entry["filters"]),
            ]
        )
        chart["tabs"].add(entry["tab"])
        chart["filter_terms"].update(tokens(text_for_values(entry["filters"])))
        chart["interactions"].add(entry["interaction"])
    for chart in catalog.values():
        chart["document"] = " ".join(chart.pop("document_parts"))
    return catalog


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


def state_text(state: dict) -> str:
    return text_for_values(
        {
            "active_tab": state.get("active_tab", ""),
            "interaction": state.get("interaction", ""),
            "filters": state.get("filters", {}),
        }
    )


def compatibility_scores(state: dict, catalog: dict[int, dict], enabled: set[str]) -> dict[str, dict[int, float]]:
    scores = {feature: {chart_id: 0.0 for chart_id in catalog} for feature in enabled}
    filter_terms = set(tokens(text_for_values(state.get("filters", {}))))
    for chart_id, metadata in catalog.items():
        if "active_tab" in enabled and state.get("active_tab") in metadata["tabs"]:
            scores["active_tab"][chart_id] = RANKING_WEIGHTS["active_tab"]
        if "filters" in enabled and filter_terms:
            overlap = filter_terms & metadata["filter_terms"]
            if overlap:
                scores["filters"][chart_id] = RANKING_WEIGHTS["filter_compatibility"] * len(overlap) / len(filter_terms)
        if "interaction" in enabled and state.get("interaction") in metadata["interactions"]:
            scores["interaction"][chart_id] = RANKING_WEIGHTS["interaction"]
        if "focus_link" in enabled:
            focused_chart = state.get("focused_chart_id")
            if chart_id == focused_chart:
                scores["focus_link"][chart_id] = RANKING_WEIGHTS["focused_chart"]
            if chart_id in LINKED_CHART_IDS.get(focused_chart, []):
                scores["focus_link"][chart_id] = RANKING_WEIGHTS["linked_chart"]
    return scores


def method_features(method: str) -> set[str]:
    if method == "structured_state_compatibility":
        return {"active_tab", "filters", "interaction"}
    if method.startswith("state_hybrid"):
        features = {"active_tab", "filters", "interaction", "focus_link"}
        if method == "state_hybrid_no_active_tab":
            features.remove("active_tab")
        elif method == "state_hybrid_no_filters":
            features.remove("filters")
        elif method == "state_hybrid_no_focus_link":
            features.remove("focus_link")
        elif method == "state_hybrid_no_interaction":
            features.remove("interaction")
        return features
    return set()


def rank_for(row: dict, catalog: dict[int, dict], method: str) -> dict:
    docs = {chart_id: metadata["document"] for chart_id, metadata in catalog.items()}
    query = row["query"]
    if method != "query_only_bm25" and row.get("dialogue_history"):
        query = f"{row['dialogue_history']} {query}"
    contributions: dict[str, dict[int, float]] = {"text_bm25": bm25_scores(query, docs)}
    if method in {"state_serialized_bm25", "state_hybrid"} or method.startswith("state_hybrid_no_"):
        contributions["state_text_bm25"] = {
            chart_id: RANKING_WEIGHTS["state_text_bm25"] * score
            for chart_id, score in bm25_scores(state_text(row["state"]), docs).items()
        }
    for feature, values in compatibility_scores(row["state"], catalog, method_features(method)).items():
        contributions[feature] = values
    scores = {
        chart_id: sum(values[chart_id] for values in contributions.values())
        for chart_id in catalog
    }
    ranking = [chart_id for chart_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
    return {"ranking": ranking, "scores": scores, "contributions": contributions}


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
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rankings-output", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260721)
    args = parser.parse_args()

    if args.bootstrap_samples < 1:
        raise ValueError("--bootstrap-samples must be positive")
    rows = [json.loads(line) for line in args.sessions.read_text(encoding="utf-8").splitlines() if line]
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    catalog = chart_catalog(gold)
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
        "sessions": len(rows),
        "queries": len({row["query_id"] for row in rows}),
        "evaluated_query_ids": sorted({row["query_id"] for row in rows}),
        "candidate_aers": len(catalog),
        "document_construction": {
            "fields": ["chart name", "tab", "measure", "target", "dimensions", "filters"],
            "source": "static AER catalog derived from the benchmark annotations",
        },
        "ranking_configuration": {
            "bm25": {"k1": BM25_K1, "b": BM25_B},
            "weights": RANKING_WEIGHTS,
            "linked_chart_ids": LINKED_CHART_IDS,
            "tie_break": "ascending chart id",
        },
        "methods": {method: summarize(outcomes, args.bootstrap_samples, args.seed) for method, outcomes in outcomes_by_method.items()},
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
            "The AER catalog is derived from benchmark annotations and must be replaced by independently extracted dashboard metadata for a leakage-resistant benchmark.",
            "Noisy state conditions are synthetic stress tests, not observed interaction traces.",
            "Results measure source-chart retrieval, not end-to-end answer accuracy or causal user benefit.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
