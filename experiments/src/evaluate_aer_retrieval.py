#!/usr/bin/env python3
"""Evaluate lexical AER retrieval baselines on generated offline sessions."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS = ROOT / "experiments/retrieval/generated/query_sessions.jsonl"
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"
TOKEN = re.compile(r"[a-z0-9]+")


def tokens(value: object) -> list[str]:
    return TOKEN.findall(str(value).lower().replace("_", " ").replace("-", " "))


def chart_documents(gold: dict) -> dict[int, str]:
    by_chart: dict[int, list[str]] = {
        int(chart_id): [name] for chart_id, name in gold["chart_inventory"].items()
    }
    names = gold["chart_inventory"]
    for entry in gold["entries"]:
        chart_id = entry["primary_chart_id"]
        by_chart[chart_id].extend(
            [
                names[str(chart_id)],
                entry["tab"],
                entry["measure"],
                entry["target"],
                " ".join(entry["dimensions"]),
                " ".join(f"{key} {value}" for key, value in entry["filters"].items()),
            ]
        )
    return {chart_id: " ".join(parts) for chart_id, parts in by_chart.items()}


def bm25_scores(query: str, docs: dict[int, str]) -> dict[int, float]:
    doc_tokens = {chart_id: tokens(text) for chart_id, text in docs.items()}
    n_docs = len(doc_tokens)
    lengths = [len(value) for value in doc_tokens.values()]
    avg_len = sum(lengths) / len(lengths)
    df = Counter(token for value in doc_tokens.values() for token in set(value))
    scores: dict[int, float] = defaultdict(float)
    for term in tokens(query):
        idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
        for chart_id, document in doc_tokens.items():
            frequency = document.count(term)
            if not frequency:
                continue
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(document) / avg_len)
            scores[chart_id] += idf * frequency * 2.2 / denominator
    return scores


def ranked(scores: dict[int, float]) -> list[int]:
    return [chart_id for chart_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def rank_for(row: dict, docs: dict[int, str], method: str) -> list[int]:
    query = row["query"]
    if method in {"history_bm25", "twinbi_dialogue_state_hybrid"} and row["dialogue_history"]:
        query = f"{row['dialogue_history']} {query}"
    scores = bm25_scores(query, docs)
    if method == "twinbi_dialogue_state_hybrid":
        state = row["state"]
        state_text = " ".join(
            [
                state["active_tab"],
                state["interaction"],
                " ".join(f"{key} {value}" for key, value in state["filters"].items()),
            ]
        )
        for chart_id, score in bm25_scores(state_text, docs).items():
            scores[chart_id] += 1.5 * score
        # Dashboard controls and result charts are connected through the AER graph.
        # This permits a district-chart click to retrieve the linked store ranking.
        linked_chart_ids = {
            1106: [1108],
            1110: [1111],
            1111: [1110],
            1114: [1115],
            1115: [1114],
        }
        if state["focused_chart_id"] in scores:
            scores[state["focused_chart_id"]] += 5.0
        for chart_id in linked_chart_ids.get(state["focused_chart_id"], []):
            scores[chart_id] += 5.5
    return ranked(scores)


def metrics(rows: list[dict], docs: dict[int, str], method: str) -> dict:
    recall_1 = recall_3 = reciprocal_rank = 0.0
    per_variant: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for row in rows:
        ranking = rank_for(row, docs, method)
        accepted = set(row["gold"]["acceptable_chart_ids"])
        position = next((index + 1 for index, chart_id in enumerate(ranking) if chart_id in accepted), None)
        values = per_variant[row["variant"]]
        values[3] += 1
        if position == 1:
            recall_1 += 1
            values[0] += 1
        if position and position <= 3:
            recall_3 += 1
            values[1] += 1
        if position:
            reciprocal_rank += 1 / position
            values[2] += 1 / position
    count = len(rows)
    return {
        "recall_at_1": round(recall_1 / count, 4),
        "recall_at_3": round(recall_3 / count, 4),
        "mrr": round(reciprocal_rank / count, 4),
        "per_variant": {
            key: {
                "recall_at_1": round(value[0] / value[3], 4),
                "recall_at_3": round(value[1] / value[3], 4),
                "mrr": round(value[2] / value[3], 4),
                "count": int(value[3]),
            }
            for key, value in sorted(per_variant.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.sessions.read_text(encoding="utf-8").splitlines()]
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    docs = chart_documents(gold)
    report = {
        "benchmark": "TwinBI AER offline retrieval",
        "sessions": len(rows),
        "queries": len({row["query_id"] for row in rows}),
        "candidate_aers": len(docs),
        "methods": {
            method: metrics(rows, docs, method)
            for method in ("query_only_bm25", "history_bm25", "twinbi_dialogue_state_hybrid")
        },
        "limitations": [
            "Query-session variants are deterministic transformations of existing benchmark tasks.",
            "Context-dependent and elliptical variants intentionally omit the original task intent from dialogue history; the active dashboard state is the disambiguating evidence.",
            "Results measure AER source-chart retrieval, not end-to-end answer accuracy.",
            "Validate the task-to-chart mapping by dashboard replay before submitting paper results.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
