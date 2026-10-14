# TwinBI Experiment Results

This document separates reproducible retrieval evaluation from browser-agent
evaluation. Do not combine the two metric families or treat historical and
fresh end-to-end runs as a controlled comparison.

## 1. AER Retrieval Evaluation

Run: `experiments/retrieval/runs/20260718_195921/`

The retrieval benchmark contains 57 deterministic query sessions created from
19 live-schema-validated task-to-chart mappings. Each task has explicit,
context-dependent, and elliptical variants. The metric is source-chart
retrieval, not final-answer accuracy.

| Method | R@1 | R@3 | MRR |
| --- | ---: | ---: | ---: |
| Query-only BM25 | 0.3158 | 0.3333 | 0.3246 |
| History BM25 | 0.4912 | 0.5088 | 0.5000 |
| TwinBI dialogue + state hybrid | 0.9123 | 1.0000 | 0.9561 |

The main retrieval result is concentrated in underspecified requests:
query-only BM25 obtains R@1 = 0.0000 on both context-dependent and elliptical
variants, while the dialogue-state hybrid obtains R@1 = 0.8947 on each.
These variants are deterministic transformations of the task set and must not
be described as a user study or as end-to-end agent accuracy.

## 2. Evidence Validation

- `19/19` visible-chart AER task mappings passed live Superset schema and chart
  inventory validation.
- The 19 visible-chart AERs plus 11 query-result AERs cover all 30 benchmark
  tasks exactly once.
- All 11 query-result AERs matched gold answers when replayed through local
  database SQL, Cube, and Superset chart-data paths.

This validates evidence availability and answer reproducibility for the full
30-task benchmark. It does not validate browser-agent behavior.

## 3. Fresh Dashboard-Only End-to-End Run

Run: `experiments/logs/ir_e2e_dashboard_timeout_safe/20260718_212550/`

The dashboard-only browser agent was run on all 30 gold tasks with a per-query
timeout of 180 seconds. The runner uses `gpt-5-mini`, the corrected Superset
login flow, and a gold-answer scorer with exact numeric comparison.

| Outcome | Count | Rate |
| --- | ---: | ---: |
| Exact | 8 | 26.7% |
| Partial | 5 | 16.7% |
| Failed | 17 | 56.7% |
| Exact or partial | 13 | 43.3% |
| Timeout | 15 | 50.0% |

Fifteen tasks completed their browser trace. Across those completed traces,
the mean scenario length was 3.73 steps (maximum 9). Timeouts are failures,
not missing observations. A common failure is repeated tab or tooltip probing;
another is returning a displayed rounded value, such as `12300.0`, instead of
the gold value `12264.798884297521` for Q01.

## 4. Historical TwinBI Trace

The existing historical TwinBI run at
`simulation/logs/abtest_runs_streamlit/20260311_220829/` was re-scored with the
same gold scorer:

| Outcome | Count | Rate |
| --- | ---: | ---: |
| Exact | 17 | 56.7% |
| Partial | 5 | 16.7% |
| Failed | 8 | 26.7% |
| Exact or partial | 22 | 73.3% |

This is useful diagnostic evidence, but it is not a fresh paired comparison
with the dashboard-only run above: the two runs were made at different times
and under different runtime states. A new 30-task TwinBI run using the same
timeout policy, model, dashboard state, and scorer is required before reporting
an end-to-end improvement claim.

## 5. Reporting Guidance

- Report the AER retrieval table as a controlled, deterministic retrieval
  experiment on 19 live-schema-validated chart tasks.
- Report the evidence validation counts as reproducibility and coverage checks.
- Report the fresh dashboard-only result as a standalone baseline result.
- Do not report the historical TwinBI trace as a direct comparator in the
  paper until a fresh matched 30-task TwinBI run completes.
