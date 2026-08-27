from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a batch of experiment query files.")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["dashboard", "streamlit"],
        default="dashboard",
        help="Batch mode. dashboard uses vision_playwright_strict.py, streamlit uses vision_playwright_strict2.py.",
    )
    parser.add_argument(
        "--queries-dir",
        type=str,
        default="experiments/queries",
        help="Directory containing query_*.txt files.",
    )
    parser.add_argument(
        "--runner",
        type=str,
        default="",
        help="Path to the experiment runner script. If omitted, a mode-specific default is used.",
    )
    parser.add_argument("--model", type=str, default="gpt-5-mini")
    parser.add_argument(
        "--start-url",
        type=str,
        default="",
    )
    parser.add_argument("--dashboard-id", type=int, default=13)
    parser.add_argument("--username", type=str, default="abc")
    parser.add_argument("--password", type=str, default="abc")
    parser.add_argument("--login-mode", type=str, default="dom")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument(
        "--per-query-timeout-sec",
        type=int,
        default=180,
        help="Kill a stalled runner after this many seconds; 0 disables the timeout.",
    )
    parser.add_argument("--viewport-width", type=int, default=1800)
    parser.add_argument("--viewport-height", type=int, default=1200)
    parser.add_argument(
        "--trace-root",
        type=str,
        default="",
        help="Base directory where per-query trace folders will be created. If omitted, a mode-specific default is used.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Run only the first N query files (0 means all).",
    )
    parser.add_argument(
        "--batch-id",
        type=str,
        default="",
        help="Stable batch directory name. If omitted, use the current timestamp.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run with visible browser windows.",
    )
    parser.add_argument(
        "--restart-container",
        type=str,
        default="",
        help="Restart this Docker container before every query and wait for start-url readiness.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument to forward to the runner. Repeat for multiple args.",
    )
    return parser


def _resolve_runner(mode: str, runner_arg: str) -> str:
    if runner_arg.strip():
        return runner_arg
    if mode == "streamlit":
        return "experiments/src/vision_playwright_strict2.py"
    return "experiments/src/vision_playwright_strict.py"


def _resolve_start_url(mode: str, start_url_arg: str) -> str:
    if start_url_arg.strip():
        return start_url_arg
    if mode == "streamlit":
        return "http://localhost:8501/"
    return "http://localhost:8088/superset/dashboard/13/?native_filters_key=lv80fGee9xY"


def _resolve_trace_root(mode: str, trace_root_arg: str) -> str:
    if trace_root_arg.strip():
        return trace_root_arg
    if mode == "streamlit":
        return "experiments/logs/abtest_runs_streamlit"
    return "experiments/logs/abtest_runs"


def _load_run_meta(trace_dir: Path) -> dict:
    meta_path = trace_dir / "run_meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_trace_dir(per_query_trace_root: Path) -> Path | None:
    if (per_query_trace_root / "run_meta.json").exists():
        return per_query_trace_root
    trace_dirs = sorted([p for p in per_query_trace_root.glob("*") if p.is_dir()])
    return trace_dirs[-1] if trace_dirs else None


def main() -> int:
    args = _build_parser().parse_args()
    project_root = Path(__file__).resolve().parents[2]
    queries_dir = (project_root / args.queries_dir).resolve()
    runner_path = (project_root / _resolve_runner(args.mode, args.runner)).resolve()
    start_url = _resolve_start_url(args.mode, args.start_url)
    trace_root = _resolve_trace_root(args.mode, args.trace_root)

    if not queries_dir.exists():
        print(f"Queries directory not found: {queries_dir}")
        return 1
    if not runner_path.exists():
        print(f"Runner not found: {runner_path}")
        return 1

    query_files = sorted(queries_dir.glob("*.txt"))
    if args.limit > 0:
        query_files = query_files[: args.limit]
    if not query_files:
        print(f"No query files found in {queries_dir}")
        return 1

    batch_id = args.batch_id.strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_root = (project_root / trace_root / batch_id).resolve()
    batch_root.mkdir(parents=True, exist_ok=True)
    summary_jsonl = batch_root / "summary.jsonl"
    summary_csv = batch_root / "summary.csv"

    rows: list[dict[str, str]] = []
    total = len(query_files)

    for idx, query_file in enumerate(query_files, start=1):
        query_name = query_file.stem
        per_query_trace_root = batch_root / query_name
        per_query_trace_root.mkdir(parents=True, exist_ok=True)
        # Both runners accept absolute paths; this avoids coupling trace placement
        # to their different source-directory roots.
        trace_dir_arg = str(per_query_trace_root)

        cmd = [
            sys.executable,
            str(runner_path),
            "--task-file",
            str(query_file),
            "--model",
            args.model,
            "--start-url",
            start_url,
            "--dashboard-id",
            str(args.dashboard_id),
            "--username",
            args.username,
            "--password",
            args.password,
            "--max-steps",
            str(args.max_steps),
            "--trace-dir",
            trace_dir_arg,
            "--trace-dir-is-run-dir",
        ]
        if args.mode == "dashboard":
            cmd.extend(
                [
                    "--login-mode",
                    args.login_mode,
                    "--viewport-width",
                    str(args.viewport_width),
                    "--viewport-height",
                    str(args.viewport_height),
                ]
            )
        if args.show_browser:
            cmd.append("--show-browser")
        cmd.extend(args.extra_arg)

        print(f"[{idx}/{total}] Running {query_name}")
        if args.restart_container.strip():
            restart = subprocess.run(
                ["docker", "restart", args.restart_container.strip()],
                cwd=project_root,
                check=False,
            )
            if restart.returncode != 0:
                print(f"[{idx}/{total}] Container restart failed: {args.restart_container}")
            ready = False
            for _ in range(30):
                try:
                    with urlopen(start_url, timeout=3) as response:
                        ready = response.status < 500
                except (URLError, TimeoutError, OSError):
                    ready = False
                if ready:
                    break
                time.sleep(1)
            if not ready:
                print(f"[{idx}/{total}] Start URL not ready after container restart: {start_url}")
        try:
            completed = subprocess.run(
                cmd,
                cwd=project_root,
                check=False,
                timeout=None if args.per_query_timeout_sec <= 0 else max(1, args.per_query_timeout_sec),
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
            print(f"[{idx}/{total}] Timed out after {args.per_query_timeout_sec}s: {query_name}")

        latest_trace_dir = _resolve_trace_dir(per_query_trace_root)
        run_meta = _load_run_meta(latest_trace_dir) if latest_trace_dir else {}

        row = {
            "query": query_name,
            "task_file": str(query_file.relative_to(project_root)),
            "exit_code": str(exit_code),
            "trace_dir": str(latest_trace_dir.relative_to(project_root)) if latest_trace_dir else "",
            "final_answer": str(run_meta.get("final_answer", "")),
            "login_success": str(run_meta.get("login_success", "")),
            "scenario_steps_executed": str(run_meta.get("scenario_steps_executed", "")),
            "dashboard_action_executed": str(run_meta.get("dashboard_action_executed", "")),
            "dashboard_action_verified": str(run_meta.get("dashboard_action_verified", "")),
            "dashboard_actions_used": str(run_meta.get("dashboard_actions_used", "")),
            "dashboard_action_budget": str(run_meta.get("dashboard_action_budget", "")),
            "dashboard_actions_attempted": str(run_meta.get("dashboard_actions_attempted", "")),
            "dashboard_actions_dispatched": str(run_meta.get("dashboard_actions_dispatched", "")),
            "dashboard_actions_visual_change": str(run_meta.get("dashboard_actions_visual_change", "")),
            "dashboard_actions_no_visual_change": str(
                run_meta.get("dashboard_actions_no_visual_change", "")
            ),
            "dashboard_actions_visible_target": str(
                run_meta.get("dashboard_actions_visible_target", "")
            ),
            "dashboard_actions_dom_dispatch": str(
                run_meta.get("dashboard_actions_dom_dispatch", "")
            ),
            "dashboard_actions_coordinate_dispatch": str(
                run_meta.get("dashboard_actions_coordinate_dispatch", "")
            ),
            "dashboard_actions_occlusion_override": str(
                run_meta.get("dashboard_actions_occlusion_override", "")
            ),
            "dashboard_actions_blocked": str(run_meta.get("dashboard_actions_blocked", "")),
            "tab_history": json.dumps(run_meta.get("tab_history", []), ensure_ascii=False),
            "streamlit_model_verified": str(
                (run_meta.get("streamlit_model_evidence") or {}).get("verified", "")
            ),
            "apply_meta_clicked": str(run_meta.get("apply_meta_clicked", "")),
            "model": args.model,
            "mode": args.mode,
        }
        rows.append(row)
        with summary_jsonl.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")

    with summary_csv.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "query",
                "task_file",
                "exit_code",
                "trace_dir",
                "final_answer",
                "login_success",
                "scenario_steps_executed",
                "dashboard_action_executed",
                "dashboard_action_verified",
                "dashboard_actions_used",
                "dashboard_action_budget",
                "dashboard_actions_attempted",
                "dashboard_actions_dispatched",
                "dashboard_actions_visual_change",
                "dashboard_actions_no_visual_change",
                "dashboard_actions_visible_target",
                "dashboard_actions_dom_dispatch",
                "dashboard_actions_coordinate_dispatch",
                "dashboard_actions_occlusion_override",
                "dashboard_actions_blocked",
                "tab_history",
                "streamlit_model_verified",
                "apply_meta_clicked",
                "model",
                "mode",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Batch root: {batch_root}")
    print(f"Summary JSONL: {summary_jsonl}")
    print(f"Summary CSV: {summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
