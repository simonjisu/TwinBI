## Purpose

This directory is an experiment workspace for comparing a dashboard-only baseline against a TwinBI-style Streamlit agent on the same query set.

Goals of this experiment:
- measure the accuracy gap between the two settings on identical tasks
- separate UI-navigation failures from chat-synthesis failures
- keep traces, answer files, and gold answers reproducible in one place

Main contents:
- [`queries/`](queries): `query_01.txt` to `query_30.txt`, gold answer files, and chat templates
- [`src/vision_playwright_strict.py`](src/vision_playwright_strict.py): dashboard-only runner
- [`src/vision_playwright_strict2.py`](src/vision_playwright_strict2.py): Streamlit/TwinBI runner
- [`src/run_query_batch.py`](src/run_query_batch.py): batch runner
- [`answers.json`](answers.json): normalized answer snapshot for comparison

## Modes

`dashboard`
- interacts directly with the Superset dashboard
- focuses on tabs, filters, hover, and scroll

`streamlit`
- uses the Streamlit app, embedded dashboard, and chat
- focuses on tab switching plus chat-based synthesis

## Prerequisites

Required:
- Python/uv environment
- Playwright Chromium installed
- running Superset instance
- running Streamlit app
- running FastAPI chat backend
- required API keys configured in `.env` or the repository root `.env`

One-time setup:

```bash
uv sync
uv run playwright install chromium
```

Default endpoints:
- dashboard URL: `http://localhost:8088/superset/dashboard/13/?native_filters_key=lv80fGee9xY`
- streamlit URL: `http://localhost:8501/`

## Run One Query

Dashboard-only example:

```bash
uv run python experiments/src/vision_playwright_strict.py \
  --task-file experiments/queries/query_01.txt \
  --model gpt-5-mini \
  --start-url "http://localhost:8088/superset/dashboard/13/?native_filters_key=lv80fGee9xY" \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --login-mode dom \
  --viewport-width 1800 \
  --viewport-height 1200 \
  --max-steps 30 \
  --show-browser
```

Streamlit/TwinBI example:

```bash
uv run python experiments/src/vision_playwright_strict2.py \
  --task-file experiments/queries/query_01.txt \
  --model gpt-5-mini \
  --start-url http://localhost:8501/ \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --max-steps 30 \
  --show-browser
```

## Run Batch

Dashboard-only batch:

```bash
uv run python experiments/src/run_query_batch.py \
  --mode dashboard \
  --queries-dir experiments/queries \
  --model gpt-5-mini \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --login-mode dom \
  --viewport-width 1800 \
  --viewport-height 1200 \
  --max-steps 30
```

Streamlit/TwinBI batch:

```bash
uv run python experiments/src/run_query_batch.py \
  --mode streamlit \
  --queries-dir experiments/queries \
  --model gpt-5-mini \
  --dashboard-id 13 \
  --username abc \
  --password abc \
  --viewport-width 1800 \
  --viewport-height 1200 \
  --max-steps 30
```

Output locations:
- dashboard batch: [`logs/abtest_runs/`](logs/abtest_runs)
- streamlit batch: [`logs/abtest_runs_streamlit/`](logs/abtest_runs_streamlit)

Each batch produces a structure like this:

```text
experiments/logs/abtest_runs/<timestamp>/
  query_01/
    run_meta.json
    steps.jsonl
    step_001.png
    ...
  query_02/
  ...
  summary.jsonl
  summary.csv
```

## Outputs

Per-query trace directory:
- `run_meta.json`: final answer and run metadata
- `steps.jsonl`: step-by-step reasoning and action log
- `step_XXX.png`: screenshots for each step
- `memory_context.json`: persistent state used during the run

Gold answer files:
- [`queries/query_01_ans.json`](queries/query_01_ans.json) style answer files for each query
- [`answers.json`](answers.json): normalized answers grouped by system

## Visualize Traces

Annotate click and scroll actions for a single query:

```bash
uv run python experiments/src/annotate_trace_clicks.py \
  --trace-dir experiments/logs/abtest_runs/<timestamp>/query_23 \
  --include-scroll
```

Annotate an entire batch:

```bash
uv run python experiments/src/annotate_trace_clicks.py \
  --trace-dir experiments/logs/abtest_runs/<timestamp> \
  --include-scroll
```

## Notes

- The runners in this directory are configured to use `experiments/...` paths by default.
- [`queries/chat_templates.json`](queries/chat_templates.json) is intended to improve chat question quality, not to store final gold answers directly.
- [`answers.json`](answers.json) is a convenience snapshot for comparison; the original execution record remains in each batch trace directory.
# Offline AER Retrieval Evaluation

`retrieval/gold_aer.json` contains 30 task-level seed AERs and a live-schema
validated 19-task evaluation subset. It stores the
retrieval target (source chart, supporting chart, measure, dimensions, filters,
hierarchy, and interaction) while the existing `queries/query_*_ans.json` files
remain the source of truth for final answer values.

Run the deterministic 90 query-session retrieval evaluation in a detachable
`tmux` session:

```bash
tmux new-session -d -s twinbi-aer \
  'cd /Users/soo/code/Agent4OLAP && ./experiments/scripts/run_aer_offline.sh; status=$?; echo "exit=$status"; exec zsh'
tmux attach -t twinbi-aer
```

Inspect a detached session or terminate it after the report has been checked:

```bash
tmux capture-pane -pt twinbi-aer
tmux kill-session -t twinbi-aer
```

The runner first validates gold labels against live FastAPI/Superset chart
metadata, then produces timestamped `retrieval/runs/<timestamp>/report.json`. Its
 three methods are query-only BM25, BM25 with dialogue history, and a dialogue-
plus-state hybrid that adds active tab, filter, focused chart, and interaction
context. These are source-
chart retrieval metrics, not end-to-end answer accuracy; replay the dashboard
before using the numbers in the submitted paper.

For contextual and elliptical query variants, the prior dialogue deliberately
does not restate the original task. The active tab, focused chart, filter, and
interaction state are the disambiguating evidence being evaluated. The remaining
11 seed tasks require dashboard detail not exposed by their source charts. The
runner materializes them as verified query-result AERs from the existing
three-way answer provenance (database SQL, Cube API, and TwinBI query path),
then checks that visible-chart and query-result AERs cover all 30 tasks exactly
once. These records validate evidence coverage, not ranking performance.
