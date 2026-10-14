#!/usr/bin/env python3
"""Run contrastive same-session follow-up E2E scenarios in state-on and state-masked modes."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import Frame, Locator, Page, async_playwright


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS_SRC = ROOT / "experiments" / "src"
if str(EXPERIMENTS_SRC) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS_SRC))

import vision_playwright_strict2 as browser_helpers  # noqa: E402


DEFAULT_SCENARIOS = ROOT / "experiments" / "followup" / "pilot_scenarios.json"
ACTION_WAIT_MS = 1800


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--username", default="abc")
    parser.add_argument("--password", default="abc")
    parser.add_argument("--dashboard-id", type=int, default=13)
    parser.add_argument("--start-url", default="http://localhost:8501/")
    parser.add_argument("--chat-api-url", default="http://localhost:8000/chat")
    parser.add_argument("--conditions", default="state_aware,state_masked")
    parser.add_argument(
        "--scenario-ids",
        default="",
        help="Optional comma-separated scenario IDs to run.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show-browser", dest="headless", action="store_false")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="Rebuild report.json from an existing summary.jsonl without running a browser.",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_answer(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json|python)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(candidate)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
    return text


def _leaf_matches(gold: Any, predicted: Any) -> tuple[int, int]:
    if isinstance(gold, dict):
        matched = total = 0
        for key, value in gold.items():
            child_matched, child_total = _leaf_matches(
                value, predicted.get(key) if isinstance(predicted, dict) else None
            )
            matched += child_matched
            total += child_total
        return matched, total
    if isinstance(gold, list):
        matched = total = 0
        for index, value in enumerate(gold):
            item = predicted[index] if isinstance(predicted, list) and index < len(predicted) else None
            child_matched, child_total = _leaf_matches(value, item)
            matched += child_matched
            total += child_total
        return matched, total
    if isinstance(gold, (int, float)) and not isinstance(gold, bool):
        try:
            return int(math.isclose(float(gold), float(predicted), rel_tol=1e-4, abs_tol=1e-6)), 1
        except (TypeError, ValueError):
            return 0, 1
    return int(gold == predicted), 1


def _target_matches(gold: dict[str, Any], predicted: Any, keys: list[str]) -> bool:
    if not isinstance(predicted, dict):
        return False
    return all(_leaf_matches(gold.get(key), predicted.get(key))[0] == 1 for key in keys)


def _post_chat(
    *,
    url: str,
    session_id: str,
    username: str,
    password: str,
    dashboard_id: int,
    model: str,
    message: str,
    history: list[dict[str, str]] | None = None,
    mask_active_state_for_evaluation: bool = False,
) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "user_id": username,
        "message": message,
        "history": history or [],
        "dashboard_id": dashboard_id,
        "superset_username": username,
        "superset_password": password,
        "agent_model": model,
        "verified_ui_evidence": {},
        "mask_active_state_for_evaluation": mask_active_state_for_evaluation,
        "debug": True,
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Superset-Username": username,
            "X-Superset-Password": password,
        },
        method="POST",
    )
    try:
        with urlopen(request) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"chat HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"chat connection failed: {exc}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("chat response is not a JSON object")
    return parsed


def _context_from_debug(payload: dict[str, Any]) -> dict[str, Any]:
    debug = payload.get("debug")
    if not isinstance(debug, list):
        return {}
    for item in debug:
        if isinstance(item, dict) and item.get("type") == "context":
            active = item.get("active_context")
            return active if isinstance(active, dict) else {}
    return {}


def _active_filter_blob(context: dict[str, Any]) -> str:
    values: list[Any] = []
    for chart in context.get("active_charts", []) if isinstance(context, dict) else []:
        if not isinstance(chart, dict):
            continue
        for key in ("native_filters", "cross_filters"):
            item = chart.get(key)
            if isinstance(item, list):
                values.extend(item)
    return json.dumps(values, ensure_ascii=False).lower()


class TraceWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "steps.jsonl"
        self.step = 0

    def write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def screenshot(self, page: Page, suffix: str) -> tuple[str, str]:
        path = self.run_dir / f"step_{self.step:03d}_{suffix}.png"
        await page.screenshot(path=str(path), full_page=False)
        return path.name, _sha256(path)


async def _click_locator(
    *,
    page: Page,
    frame: Frame,
    locator: Locator,
    label: str,
    kind: str,
    trace: TraceWriter,
) -> dict[str, Any]:
    trace.step += 1
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError(f"No bounding box for {kind} {label}")
    # Playwright reports Locator.bounding_box() in main-frame viewport
    # coordinates even when the locator belongs to an iframe. Adding the frame
    # offset here would double-translate the click into a neighboring chart.
    x = int(box["x"] + box["width"] / 2)
    y = int(box["y"] + box["height"] / 2)
    before_name, before_hash = await trace.screenshot(page, "before")
    point_before = await browser_helpers._point_target_evidence_for_source(
        page, "dashboard", x, y
    )
    if not point_before.get("found"):
        raise RuntimeError(f"No visible hit target for {kind} {label} at {(x, y)}")
    if kind in {"table_cell", "tab"}:
        expected = re.sub(r"\s+", " ", label).strip().casefold()
        hit_label = re.sub(
            r"\s+", " ", str(point_before.get("label", ""))
        ).strip().casefold()
        if expected not in hit_label:
            raise RuntimeError(
                f"Resolved {kind} point does not contain the requested label: "
                f"expected={label!r}, point_target={point_before}"
            )
    await page.mouse.click(x, y)
    await page.wait_for_timeout(ACTION_WAIT_MS)
    after_name, after_hash = await trace.screenshot(page, "after")
    point_after = await browser_helpers._point_target_evidence_for_source(
        page, "dashboard", x, y
    )
    executed = {
        "type": "click",
        "kind": kind,
        "label": label,
        "action_requested_xy": [x, y],
        "resolved_xy": [x, y],
        "point_target_before": point_before,
        "point_target_after": point_after,
        "action_dispatched": True,
        "before_screenshot": before_name,
        "after_screenshot": after_name,
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "screenshot_changed": before_hash != after_hash,
    }
    trace.write({"step": trace.step, "phase": "dashboard_action", "executed": executed})
    return executed


async def _ensure_tab(page: Page, frame: Frame, tab: str, trace: TraceWriter) -> None:
    facts = await browser_helpers._collect_visible_ui_facts(page, frame, None)
    if str(facts.get("current_tab", "")).strip().lower() == tab.lower():
        return
    pattern = re.compile(rf"^\s*{re.escape(tab)}\s*$", re.IGNORECASE)
    pools = [
        frame.get_by_role("tab", name=tab, exact=True),
        frame.locator(".ant-tabs-tab").filter(has_text=pattern),
        frame.locator("[data-test-tab]").filter(has_text=pattern),
    ]
    locator: Locator | None = None
    for pool in pools:
        for index in range(await pool.count()):
            candidate = pool.nth(index)
            if await candidate.is_visible():
                locator = candidate
                break
        if locator is not None:
            break
    if locator is None:
        raise RuntimeError(f"Dashboard tab not found: {tab}")
    await _click_locator(
        page=page, frame=frame, locator=locator, label=tab, kind="tab", trace=trace
    )


async def _click_cell(page: Page, frame: Frame, value: str, trace: TraceWriter) -> None:
    # Superset/ECharts may expose off-screen accessibility cells with the same
    # accessible name as the visible table. Restrict selection to rendered DOM
    # table cells, then independently validate the actual hit target above.
    pattern = re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
    matches = frame.locator("td").filter(has_text=pattern)
    locator: Locator | None = None
    rejected: list[dict[str, Any]] = []
    expected = re.sub(r"\s+", " ", value).strip().casefold()
    for index in range(await matches.count()):
        candidate = matches.nth(index)
        if not await candidate.is_visible():
            continue
        await candidate.scroll_into_view_if_needed()
        box = await candidate.bounding_box()
        if not box:
            continue
        x = int(box["x"] + box["width"] / 2)
        y = int(box["y"] + box["height"] / 2)
        hit = await browser_helpers._point_target_evidence_for_source(
            page, "dashboard", x, y
        )
        hit_label = re.sub(
            r"\s+", " ", str(hit.get("label", ""))
        ).strip().casefold()
        if hit.get("found") and expected in hit_label:
            locator = candidate
            break
        rejected.append({"candidate_index": index, "xy": [x, y], "hit": hit})
    if locator is None:
        raise RuntimeError(
            f"Visible dashboard table cell not found: {value}; "
            f"rejected_candidates={rejected[:8]}"
        )
    await _click_locator(
        page=page, frame=frame, locator=locator, label=value, kind="table_cell", trace=trace
    )


async def _apply_scope(
    page: Page,
    frame: Frame,
    family: str,
    scope: str,
    trace: TraceWriter,
) -> None:
    if family == "store_district":
        await _ensure_tab(page, frame, "Store-Level", trace)
        await _click_cell(page, frame, scope, trace)
        return
    if family == "product_department":
        await _ensure_tab(page, frame, "Category-Level", trace)
        await _click_cell(page, frame, scope, trace)
        await _ensure_tab(page, frame, "Product-Level", trace)
        return
    if family == "category_department":
        await _ensure_tab(page, frame, "Category-Level", trace)
        await _click_cell(page, frame, scope, trace)
        return
    raise RuntimeError(f"Unknown scenario family: {family}")


def _requirement(family: str, scope: str) -> dict[str, Any]:
    if family == "store_district":
        return {
            "tab": "Store-Level",
            "filters": {"sales_district": scope},
            "interaction": "cross_filter",
        }
    if family == "product_department":
        return {
            "tab": "Product-Level",
            "filters": {"department": scope},
            "interaction": "dashboard_filter",
        }
    if family == "category_department":
        return {
            "tab": "Category-Level",
            "filters": {"department": scope},
            "interaction": "dashboard_filter",
        }
    return {}


async def _verify_state(
    *,
    page: Page,
    frame: Frame,
    chat_api_url: str,
    session_id: str,
    dashboard_id: int,
    username: str,
    requirement: dict[str, Any],
) -> dict[str, Any]:
    verification: dict[str, Any] = {}
    for _ in range(12):
        facts = await browser_helpers._collect_visible_ui_facts(page, frame, None)
        context = await asyncio.to_thread(
            browser_helpers._fetch_verified_active_context,
            chat_api_url=chat_api_url,
            session_id=session_id,
            dashboard_id=dashboard_id,
            username=username,
        )
        verification = browser_helpers._evaluate_required_state(
            requirement, facts, context, []
        )
        if verification.get("verified"):
            return verification
        await page.wait_for_timeout(1000)
    return verification


async def _chat_turn(
    *,
    page: Page,
    trace: TraceWriter,
    phase: str,
    condition: str,
    scenario_id: str,
    chat_api_url: str,
    chat_session_id: str,
    username: str,
    password: str,
    dashboard_id: int,
    model: str,
    message: str,
    history: list[dict[str, str]] | None,
    mask_active_state_for_evaluation: bool,
) -> dict[str, Any]:
    trace.step += 1
    await browser_helpers._set_run_overlay(
        page, f"{scenario_id} | {condition} | {phase}\nchat running", tone="active"
    )
    before_name, before_hash = await trace.screenshot(page, "before")
    started = perf_counter()
    payload = await asyncio.to_thread(
        _post_chat,
        url=chat_api_url,
        session_id=chat_session_id,
        username=username,
        password=password,
        dashboard_id=dashboard_id,
        model=model,
        message=message,
        history=history,
        mask_active_state_for_evaluation=mask_active_state_for_evaluation,
    )
    latency_ms = round((perf_counter() - started) * 1000, 2)
    answer = str(payload.get("answer", "") or "").strip()
    await browser_helpers._set_run_overlay(
        page,
        f"{scenario_id} | {condition} | {phase}\n{answer[:220]}",
        tone="done" if answer else "warn",
    )
    after_name, after_hash = await trace.screenshot(page, "after")
    executed = {
        "type": "chat",
        "phase": phase,
        "message": message,
        "history": history or [],
        "chat_session_id": chat_session_id,
        "answer": answer,
        "latency_ms": latency_ms,
        "before_screenshot": before_name,
        "after_screenshot": after_name,
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "screenshot_changed": before_hash != after_hash,
        "active_context": _context_from_debug(payload),
        "agent_debug": payload.get("debug", []),
    }
    trace.write({"step": trace.step, "phase": phase, "executed": executed})
    return executed


async def _run_condition(
    *,
    browser: Any,
    scenario: dict[str, Any],
    condition: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    scenario_id = str(scenario["scenario_id"])
    run_dir = args.output_dir / scenario_id / condition
    trace = TraceWriter(run_dir)
    requested_session_id = uuid.uuid4().hex
    context = await browser.new_context(
        viewport={"width": browser_helpers.VIEWPORT_WIDTH, "height": browser_helpers.VIEWPORT_HEIGHT},
        device_scale_factor=1,
    )
    page = await context.new_page()
    result: dict[str, Any] = {
        "scenario_id": scenario_id,
        "condition": condition,
        "source_query_ids": scenario.get("source_query_ids", []),
        "family": scenario["family"],
        "initial_scope": scenario["initial_scope"],
        "followup_scope": scenario["followup_scope"],
        "requested_session_id": requested_session_id,
        "trace_dir": str(run_dir.resolve()),
        "steps_jsonl": str(trace.path.resolve()),
        "model": args.model,
    }
    try:
        start_url = args.start_url.rstrip("/") + f"/?session_id={requested_session_id}"
        await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        model_evidence = await browser_helpers._streamlit_select_model(page, args.model)
        if not model_evidence.get("verified"):
            raise RuntimeError(f"Model selection failed: {model_evidence}")
        await browser_helpers._streamlit_seed_credentials(page, args.username, args.password)
        if not await browser_helpers._streamlit_apply_meta(page):
            raise RuntimeError("Login & Apply Meta was not clicked")
        frame = await browser_helpers._wait_for_dashboard_ready(
            page, args.username, args.password, timeout_sec=40
        )
        if frame is None:
            raise RuntimeError("Embedded dashboard did not become ready")
        await browser_helpers._scroll_to_top(page)
        actual_session_id = await browser_helpers._extract_session_id(page)
        actual_session_id = actual_session_id or requested_session_id
        chat_session_id = actual_session_id
        mask_active_state_for_evaluation = condition == "state_masked"
        result.update(
            {
                "actual_session_id": actual_session_id,
                "chat_session_id": chat_session_id,
                "mask_active_state_for_evaluation": mask_active_state_for_evaluation,
                "model_verified": True,
                "apply_meta_clicked": True,
            }
        )

        await _apply_scope(
            page, frame, scenario["family"], scenario["initial_scope"], trace
        )
        initial_verification = await _verify_state(
            page=page,
            frame=frame,
            chat_api_url=args.chat_api_url,
            session_id=actual_session_id,
            dashboard_id=args.dashboard_id,
            username=args.username,
            requirement=_requirement(scenario["family"], scenario["initial_scope"]),
        )
        if not initial_verification.get("verified"):
            raise RuntimeError(f"Initial UI state not verified: {initial_verification}")
        initial_turn = await _chat_turn(
            page=page,
            trace=trace,
            phase="initial_chat",
            condition=condition,
            scenario_id=scenario_id,
            chat_api_url=args.chat_api_url,
            chat_session_id=chat_session_id,
            username=args.username,
            password=args.password,
            dashboard_id=args.dashboard_id,
            model=args.model,
            message=scenario["initial_question"],
            history=None,
            mask_active_state_for_evaluation=mask_active_state_for_evaluation,
        )

        await _apply_scope(
            page, frame, scenario["family"], scenario["followup_scope"], trace
        )
        followup_verification = await _verify_state(
            page=page,
            frame=frame,
            chat_api_url=args.chat_api_url,
            session_id=actual_session_id,
            dashboard_id=args.dashboard_id,
            username=args.username,
            requirement=_requirement(scenario["family"], scenario["followup_scope"]),
        )
        if not followup_verification.get("verified"):
            raise RuntimeError(f"Follow-up UI state not verified: {followup_verification}")

        controlled_history = [
            {"role": "user", "content": scenario["initial_question"]},
            {
                "role": "assistant",
                "content": json.dumps(scenario["initial_gold"], ensure_ascii=False),
            },
        ]
        followup_turn = await _chat_turn(
            page=page,
            trace=trace,
            phase="followup_chat",
            condition=condition,
            scenario_id=scenario_id,
            chat_api_url=args.chat_api_url,
            chat_session_id=chat_session_id,
            username=args.username,
            password=args.password,
            dashboard_id=args.dashboard_id,
            model=args.model,
            message=scenario["followup_question"],
            history=controlled_history,
            mask_active_state_for_evaluation=mask_active_state_for_evaluation,
        )

        initial_prediction = _parse_answer(initial_turn["answer"])
        followup_prediction = _parse_answer(followup_turn["answer"])
        initial_matched, initial_total = _leaf_matches(
            scenario["initial_gold"], initial_prediction
        )
        followup_matched, followup_total = _leaf_matches(
            scenario["followup_gold"], followup_prediction
        )
        context_correct = _target_matches(
            scenario["followup_gold"], followup_prediction, scenario["target_keys"]
        )
        history_carryover = (
            _target_matches(
                scenario["initial_gold"], followup_prediction, scenario["target_keys"]
            )
            and not context_correct
        )
        followup_context = followup_turn.get("active_context", {})
        followup_filter_blob = _active_filter_blob(followup_context)
        expected_scope_seen = scenario["followup_scope"].lower() in followup_filter_blob
        result.update(
            {
                "status": "completed",
                "initial_gold": scenario["initial_gold"],
                "followup_gold": scenario["followup_gold"],
                "initial_answer": initial_turn["answer"],
                "followup_answer": followup_turn["answer"],
                "initial_prediction": initial_prediction,
                "followup_prediction": followup_prediction,
                "initial_exact": initial_matched == initial_total,
                "initial_matched_leaves": initial_matched,
                "initial_gold_leaves": initial_total,
                "followup_exact": followup_matched == followup_total,
                "followup_matched_leaves": followup_matched,
                "followup_gold_leaves": followup_total,
                "context_resolution_correct": context_correct,
                "history_carryover_error": history_carryover,
                "initial_chat_latency_ms": initial_turn["latency_ms"],
                "followup_chat_latency_ms": followup_turn["latency_ms"],
                "dashboard_action_count": sum(
                    1
                    for line in trace.path.read_text(encoding="utf-8").splitlines()
                    if json.loads(line).get("phase") == "dashboard_action"
                ),
                "initial_state_verification": initial_verification,
                "followup_state_verification": followup_verification,
                "chat_context_active_tab": (
                    followup_context.get("active_tab")
                    if isinstance(followup_context, dict)
                    else None
                ),
                "chat_context_filter_blob": followup_filter_blob,
                "expected_followup_scope_seen_by_chat": expected_scope_seen,
                "mask_verified": (
                    expected_scope_seen
                    if condition == "state_aware"
                    else not expected_scope_seen
                ),
            }
        )
    except Exception as exc:
        result.update({"status": "failed", "error": str(exc)[:2000]})
    finally:
        (run_dir / "run_meta.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        await context.close()
    return result


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    conditions: dict[str, Any] = {}
    for condition in sorted({str(item.get("condition")) for item in results}):
        rows = [item for item in results if item.get("condition") == condition]
        completed = [item for item in rows if item.get("status") == "completed"]
        scenario_count = len(rows)
        completed_count = len(completed)
        followup_exact_count = sum(bool(item.get("followup_exact")) for item in completed)
        context_correct_count = sum(
            bool(item.get("context_resolution_correct")) for item in completed
        )
        mask_verified_count = sum(bool(item.get("mask_verified")) for item in completed)
        conditions[condition] = {
            "scenario_count": scenario_count,
            "completed_count": completed_count,
            "failed_count": scenario_count - completed_count,
            "completion_rate": completed_count / scenario_count if scenario_count else 0.0,
            "initial_exact_count": sum(bool(item.get("initial_exact")) for item in completed),
            "followup_exact_count": followup_exact_count,
            "followup_exact_rate": (
                followup_exact_count / completed_count if completed_count else 0.0
            ),
            "context_resolution_correct_count": context_correct_count,
            "context_resolution_rate": (
                context_correct_count / completed_count if completed_count else 0.0
            ),
            "history_carryover_error_count": sum(
                bool(item.get("history_carryover_error")) for item in completed
            ),
            "mask_verified_count": mask_verified_count,
            "mask_verified_rate": (
                mask_verified_count / completed_count if completed_count else 0.0
            ),
            "mean_followup_latency_ms": round(
                sum(float(item.get("followup_chat_latency_ms") or 0) for item in completed)
                / len(completed),
                2,
            )
            if completed
            else 0.0,
            "dashboard_action_count": sum(
                int(item.get("dashboard_action_count") or 0) for item in completed
            ),
        }
    paired: list[dict[str, Any]] = []
    by_key = {(item.get("scenario_id"), item.get("condition")): item for item in results}
    for scenario_id in sorted({str(item.get("scenario_id")) for item in results}):
        state = by_key.get((scenario_id, "state_aware"), {})
        masked = by_key.get((scenario_id, "state_masked"), {})
        paired.append(
            {
                "scenario_id": scenario_id,
                "state_aware_context_correct": state.get("context_resolution_correct"),
                "state_masked_context_correct": masked.get("context_resolution_correct"),
                "state_aware_followup_exact": state.get("followup_exact"),
                "state_masked_followup_exact": masked.get("followup_exact"),
                "state_aware_answer": state.get("followup_prediction"),
                "state_masked_answer": masked.get("followup_prediction"),
            }
        )
    families: dict[str, Any] = {}
    for family in sorted({str(item.get("family")) for item in results}):
        family_rows = [item for item in results if item.get("family") == family]
        family_conditions: dict[str, Any] = {}
        for condition in ("state_aware", "state_masked"):
            rows = [item for item in family_rows if item.get("condition") == condition]
            completed = [item for item in rows if item.get("status") == "completed"]
            exact = sum(bool(item.get("followup_exact")) for item in completed)
            family_conditions[condition] = {
                "scenario_count": len(rows),
                "completed_count": len(completed),
                "followup_exact_count": exact,
                "followup_exact_rate": exact / len(completed) if completed else 0.0,
                "context_resolution_correct_count": sum(
                    bool(item.get("context_resolution_correct")) for item in completed
                ),
                "dashboard_action_count": sum(
                    int(item.get("dashboard_action_count") or 0) for item in completed
                ),
            }
        families[family] = family_conditions

    aware_failures = [
        {
            "scenario_id": item.get("scenario_id"),
            "family": item.get("family"),
            "initial_scope": item.get("initial_scope"),
            "followup_scope": item.get("followup_scope"),
            "followup_answer": item.get("followup_answer"),
            "followup_gold": item.get("followup_gold"),
            "state_verified": bool(
                (item.get("followup_state_verification") or {}).get("verified")
            ),
            "expected_scope_seen_by_chat": item.get(
                "expected_followup_scope_seen_by_chat"
            ),
        }
        for item in results
        if item.get("condition") == "state_aware"
        and item.get("status") == "completed"
        and not item.get("followup_exact")
    ]
    aware_rate = float(conditions.get("state_aware", {}).get("followup_exact_rate", 0.0))
    masked_rate = float(conditions.get("state_masked", {}).get("followup_exact_rate", 0.0))
    protocol_integrity_pass = bool(results) and all(
        item.get("status") == "completed" and item.get("mask_verified") is True
        for item in results
    )
    return {
        "conditions": conditions,
        "families": families,
        "comparison": {
            "state_aware_followup_exact_rate": aware_rate,
            "state_masked_followup_exact_rate": masked_rate,
            "absolute_uplift": aware_rate - masked_rate,
        },
        "protocol": {
            "result_count": len(results),
            "unique_session_count": len(
                {str(item.get("actual_session_id")) for item in results}
            ),
            "protocol_integrity_pass": protocol_integrity_pass,
        },
        "state_aware_failures": aware_failures,
        "paired": paired,
        "results": results,
    }


async def _run(args: argparse.Namespace) -> int:
    browser_helpers._load_env(ROOT, ROOT / "experiments")
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))
    selected_ids = {
        item.strip() for item in args.scenario_ids.split(",") if item.strip()
    }
    if selected_ids:
        scenarios = [
            scenario
            for scenario in scenarios
            if str(scenario.get("scenario_id", "")) in selected_ids
        ]
        missing = selected_ids - {
            str(scenario.get("scenario_id", "")) for scenario in scenarios
        }
        if missing:
            raise SystemExit(f"Unknown scenario IDs: {sorted(missing)}")
    if args.limit > 0:
        scenarios = scenarios[: args.limit]
    conditions = [item.strip() for item in args.conditions.split(",") if item.strip()]
    invalid = [item for item in conditions if item not in {"state_aware", "state_masked"}]
    if invalid:
        raise SystemExit(f"Unsupported conditions: {invalid}")

    if args.aggregate_only:
        summary_path = args.output_dir / "summary.jsonl"
        if not summary_path.exists():
            raise SystemExit(f"Existing summary not found: {summary_path}")
        results = [
            json.loads(line)
            for line in summary_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        report = _aggregate(results)
        report.update(
            {
                "generated_at": datetime.now().isoformat(),
                "scenario_file": str(args.scenarios.resolve()),
                "model": args.model,
                "aggregate_only": True,
            }
        )
        (args.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report["conditions"], ensure_ascii=False), flush=True)
        return 0

    results: list[dict[str, Any]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=args.headless,
            args=[
                f"--window-size={browser_helpers.VIEWPORT_WIDTH},{browser_helpers.VIEWPORT_HEIGHT}",
                "--force-device-scale-factor=1",
            ],
        )
        try:
            for scenario in scenarios:
                for condition in conditions:
                    print(f"[{scenario['scenario_id']}] {condition} start", flush=True)
                    result = await _run_condition(
                        browser=browser,
                        scenario=scenario,
                        condition=condition,
                        args=args,
                    )
                    results.append(result)
                    with (args.output_dir / "summary.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                    print(
                        f"[{scenario['scenario_id']}] {condition} {result.get('status')} "
                        f"context={result.get('context_resolution_correct')} exact={result.get('followup_exact')}",
                        flush=True,
                    )
        finally:
            await browser.close()

    report = _aggregate(results)
    report.update(
        {
            "generated_at": datetime.now().isoformat(),
            "scenario_file": str(args.scenarios.resolve()),
            "model": args.model,
        }
    )
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["conditions"], ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
