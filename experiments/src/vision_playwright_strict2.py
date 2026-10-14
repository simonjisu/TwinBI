from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import Frame, Page, async_playwright


SYSTEM_PROMPT = """
You are a Streamlit + embedded-dashboard automation agent.
You will receive:
- a screenshot of the current browser viewport
- actionable candidates collected from the Streamlit host page and the embedded dashboard frame
- the current task
- recent chat answer if any

Choose exactly one next action.
If the task is complete, return a done action.

Return strict JSON with this schema:
{
  "reasoning": "short Korean reasoning",
  "final_answer": "",
  "observed_facts": [],
  "working_hypothesis": "",
  "resolved_answers": [],
  "self_check": {
    "prerequisite_met": "yes|no|uncertain",
    "action_targets_prerequisite": "yes|no",
    "what_is_missing": "short Korean text"
  },
  "action": {
    "type": "click|hover|type|press|scroll|wait|chat|done",
    "source": "host|dashboard|chat",
    "dom_id": -1,
    "x": 0,
    "y": 0,
    "text": "",
    "key": "",
    "delta_y": 0,
    "seconds": 1,
    "result": ""
  }
}

Rules:
- Prefer dom_id when a relevant candidate exists.
- The dashboard is the primary tool until every required tab/filter/selection/hover state is verified.
- Perform the dashboard interaction required by the task before asking chat: tab navigation,
  existing dashboard filters, cross-filter selections, and hover for tooltip values are allowed.
- If a required department filter is not selectable on the target tab, use the existing Department
  table on Category-Level to apply the cross-filter, then return to the required target tab and verify
  that the filter is preserved. This is dashboard exploration, not filter configuration.
- Do not create or edit dashboard filters, change chart configuration, or enter dashboard edit mode.
- After the required interaction is visibly applied, use chat to interpret the resulting dashboard context.
- If the task specifies a district, time window, category, department, selected tab, or tooltip value,
  chat is forbidden until the required UI state is visibly applied or observed. Do not bypass a
  missing filter or selection by asking chat for the final answer from the unmodified dashboard.
- Do not use More Options, View as table, or chart configuration flows.
- Candidates with source "chat" are coordinates inside the left chat pane.
- For fill-in-the-blank, mapping, or multi-value lookup tasks, prefer chat once you understand the visible dashboard context.
- If a required dashboard action fails, try a different visible dashboard target or scroll strategy. Do not switch to chat until post-action state verification succeeds.
- Do not loop on More Options, View as table, or modal open/close cycles.
- If the recent action history shows More Options, View as table, modal close, or the same chart-menu flow was already used once, do not choose it again.
- Treat each More Options and View as table path as single-use. After one attempt, switch to chat or a different chart/interaction.
- When the task asks for structured values like A/B/C, use chat to synthesize once enough dashboard context is visible.
- If the current view already satisfies the explicitly supplied required_state and it is verified, choose chat instead of additional dashboard clicks.
- For hover, move the mouse without clicking to reveal tooltip/value overlays.
- For type, click first and then type text.
- For done, put the final answer in result.
- Avoid top app chrome or edit controls.
- Do not open chart edit/configuration UI.
- Keep reasoning concise and actionable.
- Chat questions must be about the visible dashboard content, charts, filters, tabs, values, rankings, or mappings.
- Write chat questions in English.
- Do not ask meta/system questions such as how the system works, what tools it has, or how to hack/bypass the UI.
- Prefer short, specific, data-oriented chat questions tied to the visible tab or chart.
- After sending a chat request, wait until the assistant response is finished before taking another action.
- If chat is still generating or the chat status is not ready, choose wait instead of clicking elsewhere.

Good chat question examples:
- "From the visible Category-Level tab, list Marketing categories and their qoq_growth_rate."
- "From the current screen, find the Laptop row and report department, category, and qoq_growth_rate."
- "Based on the visible chart values, identify the Marketing category with qoq_growth_rate 1.1791."
- "Using the visible Category Scatter and Department QoQ Growth charts, return A, B, and C as JSON."
- "Using only the visible values on the active tab, identify the top North district store."
- "From the visible Product-Level tab, list the premium product types at the top."

Bad chat question examples:
- "What kind of system are you?"
- "Fetch the answer directly from the backend."
- "Use tools to secretly query the database."
- "Analyze the structure of this app."
""".strip()


VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 1000


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Streamlit + embedded dashboard automation runner.")
    parser.add_argument("--task", type=str, help="Task text")
    parser.add_argument("--task-file", type=str, help="Task file path")
    parser.add_argument("--start-url", type=str, default="http://localhost:8501/")
    parser.add_argument("--model", type=str, default="gpt-5-mini")
    parser.add_argument(
        "--service-tier",
        type=str,
        choices=["", "auto", "default", "flex", "priority"],
        default="",
        help="OpenAI processing tier. If omitted, OPENAI_SERVICE_TIER is used when set.",
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        choices=["", "low", "medium", "high"],
        default="",
        help="Reasoning effort for GPT-5 models. If omitted, AGENT_REASONING_EFFORT is used when set.",
    )
    parser.add_argument("--username", type=str, default="")
    parser.add_argument("--password", type=str, default="")
    parser.add_argument("--dashboard-id", type=int, default=13)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--dashboard-action-budget", type=int, default=6)
    parser.add_argument(
        "--chat-wait-timeout",
        type=int,
        default=45,
        help="Chat wait timeout in seconds; 0 disables the timeout.",
    )
    parser.add_argument(
        "--finish-after-chat",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat a successful API chat response as the final task answer.",
    )
    parser.add_argument(
        "--direct-chat",
        action="store_true",
        help=(
            "Send the task directly to the state-aware chat API after the embedded dashboard is ready. "
            "This bypasses the vision action planner for a reproducible direct-chat evaluation."
        ),
    )
    parser.add_argument("--chat-api-url", type=str, default="http://localhost:8000/chat")
    parser.add_argument("--chat-status-url", type=str, default="http://localhost:8000/chat/session_status")
    parser.add_argument("--show-browser", dest="headless", action="store_false")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--trace-dir", type=str, default="logs/vision_streamlit_traces")
    parser.add_argument(
        "--trace-dir-is-run-dir",
        action="store_true",
        help="Treat --trace-dir as the final run directory and do not append a timestamp subdirectory.",
    )
    return parser


def _load_task(args: argparse.Namespace) -> str:
    if args.task:
        return args.task.strip()
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8").strip()
    raise ValueError("Either --task or --task-file is required.")


def _load_chat_template(workspace_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not args.task_file:
        return {}
    query_key = Path(args.task_file).stem
    for template_path in [
        workspace_root / "queries" / "chat_templates.json",
        workspace_root / "tasks" / "chat_templates.json",
    ]:
        if not template_path.exists():
            continue
        try:
            all_templates = json.loads(template_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        value = all_templates.get(query_key)
        if isinstance(value, dict):
            return value
    return {}


def _load_scenario_requirement(workspace_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Load operational state requirements without exposing answer values."""
    if not args.task_file:
        return {}
    query_id = Path(args.task_file).stem.replace("query_", "Q").upper()
    gold_path = workspace_root / "retrieval" / "gold_aer.json"
    try:
        entries = json.loads(gold_path.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, json.JSONDecodeError):
        return {}
    for entry in entries:
        if str(entry.get("query_id", "")).upper() == query_id:
            task_text = ""
            try:
                task_text = Path(args.task_file).read_text(encoding="utf-8").lower()
            except OSError:
                pass
            interaction = str(entry.get("interaction", "none") or "none").strip().lower()
            analytical_filters = entry.get("filters", {}) if isinstance(entry.get("filters"), dict) else {}
            operational_filters: dict[str, Any] = {}
            if interaction in {"cross_filter", "dashboard_filter", "native_filter", "tab_navigation"}:
                for key, value in analytical_filters.items():
                    if any(token in str(key).lower() for token in ("district", "department", "time_range")):
                        operational_filters[str(key)] = value
            clear_filters: list[str] = []
            if "clear" in task_text and "department filter" in task_text:
                clear_filters.append("department")
            hover_targets: list[str] = []
            if interaction == "hover":
                for key in ("category", "product_type"):
                    value = analytical_filters.get(key)
                    values = value if isinstance(value, list) else [value]
                    hover_targets.extend(
                        str(item).strip() for item in values if item is not None and str(item).strip()
                    )
            tab_sequence: list[str] = []
            if query_id == "Q19":
                tab_sequence = ["Store-Level", "Product-Level", "Category-Level", "Store-Level"]
            elif query_id == "Q29":
                tab_sequence = ["Store-Level", "Product-Level", "Store-Level"]
            return {
                "query_id": query_id,
                "tab": str(entry.get("tab", "") or "").strip(),
                "filters": operational_filters,
                "clear_filters": clear_filters,
                "hover_targets": hover_targets,
                "tab_sequence": tab_sequence,
                "interaction": interaction,
                "primary_chart_id": entry.get("primary_chart_id"),
                "acceptable_chart_ids": entry.get("acceptable_chart_ids", []),
            }
    return {}


def _scenario_requires_dashboard_action(requirement: dict[str, Any]) -> bool:
    return bool(
        requirement.get("tab")
        or requirement.get("filters")
        or str(requirement.get("interaction", "none")).lower() != "none"
    )


def _load_env(repo_root: Path, workspace_root: Path) -> None:
    load_dotenv(repo_root / ".env", override=False)
    load_dotenv(workspace_root / ".env", override=False)
    load_dotenv(repo_root / "fastapi" / ".env", override=False)


def _parse_action(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        l = raw.find("{")
        r = raw.rfind("}")
        if l >= 0 and r > l:
            data = json.loads(raw[l : r + 1])
        else:
            raise
    if not isinstance(data, dict) or "action" not in data:
        raise ValueError("Model output must be a JSON object with action.")
    return data


async def _find_chat_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        try:
            if await frame.locator(".chat-input").count() > 0:
                return frame
        except Exception:
            continue
    return None


async def _find_dashboard_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        url = frame.url or ""
        if "localhost:8088" in url and ("/superset/dashboard/" in url or "/login/" in url):
            return frame
    return None


async def _extract_session_id(page: Page) -> str:
    try:
        session_id = await page.evaluate(
            """
            () => {
              const text = document.body?.innerText || "";
              const match = text.match(/SESSION_ID:\\s*([A-Za-z0-9_-]+)/i);
              return match ? match[1] : "";
            }
            """
        )
        return str(session_id or "").strip()
    except Exception:
        return ""


async def _collect_candidates(frame: Frame, source: str, limit: int = 80) -> list[dict[str, Any]]:
    raw = await frame.evaluate(
        """
        () => {
          const selectors = [
            "button", "a", "input", "textarea", "select",
            "[role='button']", "[role='tab']", "[role='option']", "[role='menuitem']",
            "[role='gridcell']", "[role='rowheader']", "[role='cell']",
            "[aria-expanded]", ".ant-collapse-header", ".ant-popover [tabindex]",
            "[class*='popover'] [tabindex]", "[class*='Popover'] [tabindex]",
            "svg circle", "svg path", "svg rect", "[class*='legend']", "[class*='point']",
            ".chat-input", ".chat-send", ".chat-status", ".bubble.assistant", ".bubble.user"
          ];
          const out = [];
          const seen = new Set();
          const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
          for (const sel of selectors) {
            const nodes = document.querySelectorAll(sel);
            for (const el of nodes) {
              if (seen.has(el)) continue;
              seen.add(el);
              const r = el.getBoundingClientRect();
              if (!r || r.width < 4 || r.height < 4) continue;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) continue;
              const style = window.getComputedStyle(el);
              if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") continue;
              const label = clean(
                el.innerText ||
                el.textContent ||
                el.getAttribute("aria-label") ||
                el.getAttribute("title") ||
                el.getAttribute("placeholder") ||
                el.tagName
              ).slice(0, 120);
              const chartRoot = el.closest(".chart-slice, .dashboard-chart, [data-test-chart-id]");
              const chartTitleNode = chartRoot
                ? chartRoot.querySelector(".header-title, .slice-header-title, h4, h5")
                : null;
              const chartTitle = clean(chartTitleNode?.innerText || chartTitleNode?.textContent || "").slice(0, 80);
              out.push({
                tag: (el.tagName || "").toLowerCase(),
                role: (el.getAttribute("role") || "").trim(),
                label,
                chart_title: chartTitle,
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top + r.height / 2),
              });
            }
          }
          return out;
        }
        """
    )
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "source": source,
                "tag": str(item.get("tag", "")),
                "role": str(item.get("role", "")),
                "label": str(item.get("label", ""))[:120],
                "chart_title": str(item.get("chart_title", ""))[:80],
                "x": int(item.get("x", 0) or 0),
                "y": int(item.get("y", 0) or 0),
                "local_x": int(item.get("x", 0) or 0),
                "local_y": int(item.get("y", 0) or 0),
            }
        )
    return out


async def _frame_offset(frame: Frame, page: Page) -> tuple[int, int]:
    if frame == page.main_frame:
        return 0, 0
    try:
        el = await frame.frame_element()
        box = await el.bounding_box()
    except Exception:
        box = None
    if not box:
        return 0, 0
    return int(box.get("x", 0) or 0), int(box.get("y", 0) or 0)


async def _point_target_evidence_for_source(
    page: Page, source: str, global_x: int, global_y: int
) -> dict[str, Any]:
    """Describe the visible element hit inside the host/dashboard/chat frame."""
    frame: Frame | None = page.main_frame
    if source == "dashboard":
        frame = await _find_dashboard_frame(page)
    elif source == "chat":
        frame = await _find_chat_frame(page)
    if frame is None:
        return {"found": False, "source": source, "global_xy": [global_x, global_y]}
    offset_x, offset_y = await _frame_offset(frame, page)
    local_x = int(global_x) - offset_x
    local_y = int(global_y) - offset_y
    try:
        raw = await frame.evaluate(
            """
            ({x, y}) => {
              const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const hit = document.elementFromPoint(x, y);
              if (!hit) return {found: false, local_xy: [x, y]};
              const interactive = hit.closest(
                "button, a, input, textarea, select, [role], [aria-expanded], " +
                ".ant-collapse-header, [tabindex], [data-test], [data-testid]"
              ) || hit;
              const rect = interactive.getBoundingClientRect();
              return {
                found: true,
                local_xy: [x, y],
                tag: (interactive.tagName || '').toLowerCase(),
                role: clean(interactive.getAttribute('role')),
                label: clean(
                  interactive.innerText || interactive.textContent ||
                  interactive.getAttribute('aria-label') || interactive.getAttribute('title') || ''
                ).slice(0, 160),
                aria_expanded: clean(interactive.getAttribute('aria-expanded')),
                bounds: [
                  Math.round(rect.left), Math.round(rect.top),
                  Math.round(rect.width), Math.round(rect.height)
                ],
              };
            }
            """,
            {"x": local_x, "y": local_y},
        )
    except Exception as exc:
        raw = {"found": False, "error": str(exc)[:240], "local_xy": [local_x, local_y]}
    if not isinstance(raw, dict):
        raw = {"found": False, "local_xy": [local_x, local_y]}
    raw["source"] = source
    raw["global_xy"] = [int(global_x), int(global_y)]
    raw["frame_url"] = str(frame.url or "")[:240]
    return raw


def _format_candidates(candidates: list[dict[str, Any]], limit: int = 50) -> str:
    compact = []
    for idx, item in enumerate(candidates[:limit]):
        compact.append(
            {
                "dom_id": idx,
                "source": item.get("source", ""),
                "tag": item.get("tag", ""),
                "role": item.get("role", ""),
                "label": item.get("label", ""),
                "chart_title": item.get("chart_title", ""),
                "x": item.get("x", 0),
                "y": item.get("y", 0),
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def _format_recent_actions(history: list[str], limit: int = 8) -> str:
    if not history:
        return "none"
    return json.dumps(history[-limit:], ensure_ascii=False)


def _normalize_label_text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").strip().lower().split())[:limit]


def _candidate_fingerprint(action: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    dom_id = int(action.get("dom_id", -1) or -1)
    if 0 <= dom_id < len(candidates):
        item = candidates[dom_id]
        source = str(item.get("source", "") or "")
        label = _normalize_label_text(item.get("label", ""))
        chart_title = _normalize_label_text(item.get("chart_title", ""))
        return f"{source}|{chart_title}|{label}"
    source = str(action.get("source", "") or "")
    x = int(action.get("x", -1) or -1)
    y = int(action.get("y", -1) or -1)
    return f"{source}|xy:{x},{y}"


def _is_menu_loop_action(action: dict[str, Any], candidates: list[dict[str, Any]], recent_targets: list[str]) -> tuple[bool, str]:
    action_type = str(action.get("type", "")).strip().lower()
    if action_type != "click":
        return False, ""
    fingerprint = _candidate_fingerprint(action, candidates)
    if not fingerprint:
        return False, ""
    label = fingerprint.split("|")[-1]
    risky_tokens = [
        "more options",
        "more filters",
        "view as table",
        "close",
        "modal",
        "chart data",
        "next page",
        "pagination",
    ]
    if not any(token in label for token in risky_tokens):
        return False, fingerprint
    if fingerprint in recent_targets:
        return True, fingerprint
    return False, fingerprint


def _is_tab_candidate(action: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    dom_id = int(action.get("dom_id", -1) or -1)
    label = ""
    role = ""
    if 0 <= dom_id < len(candidates):
        item = candidates[dom_id]
        label = _normalize_label_text(item.get("label", ""))
        role = _normalize_label_text(item.get("role", ""))
    else:
        return False
    tab_tokens = ["store-level", "product-level", "category-level", "store level", "product level", "category level"]
    return role == "tab" or any(token in label for token in tab_tokens)


def _normalize_short_texts(values: list[Any], *, limit: int = 140, keep: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").strip().split())[:limit]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= keep:
            break
    return out


def _format_memory_context(memory_context: dict[str, Any]) -> str:
    observed_text = (
        ", ".join([str(item)[:72] for item in memory_context.get("observed_facts", [])[-6:]])
        if memory_context.get("observed_facts")
        else "none"
    )
    resolved_text = (
        ", ".join([str(item)[:72] for item in memory_context.get("resolved_answers", [])[-6:]])
        if memory_context.get("resolved_answers")
        else "none"
    )
    hypothesis_text = str(memory_context.get("working_hypothesis", "") or "none")
    generic_memory = memory_context.get("generic_memory", {})
    last_self_check = json.dumps(generic_memory.get("last_self_check", {}), ensure_ascii=False)
    return (
        "Memory context (persisted progress):\n"
        f"- flags: {json.dumps(memory_context.get('flags', {}), ensure_ascii=False)}\n"
        f"- intent_attempts: {json.dumps(memory_context.get('intent_attempts', {}), ensure_ascii=False)}\n"
        f"- recent_actions: {json.dumps(memory_context.get('recent_actions', [])[-8:], ensure_ascii=False)}\n"
        f"- observed_facts: {observed_text}\n"
        f"- working_hypothesis: {hypothesis_text}\n"
        f"- resolved_answers: {resolved_text}\n"
        f"- last_chat_answer: {str(memory_context.get('last_chat_answer', '') or 'none')[:200]}\n"
        f"- last_self_check: {last_self_check}\n"
        "- Do not repeat the same intent or same chart-menu flow excessively.\n"
        "- If a value is already in resolved_answers, avoid re-checking the same path.\n"
        "- If recent actions show repeated More Options/View as table/modal cycles, switch strategy.\n"
    )


def _format_chat_template(template: dict[str, Any]) -> str:
    if not template:
        return "Preferred chat questions: none"
    questions = template.get("preferred_chat_questions", [])
    if not isinstance(questions, list):
        questions = []
    lines = ["Preferred chat questions (use only after required_state is verified):"]
    for idx, item in enumerate(questions[:5], start=1):
        lines.append(f"{idx}. {str(item).strip()}")
    synthesis = str(template.get("final_synthesis_prompt", "")).strip()
    if synthesis:
        lines.append(f"Final synthesis prompt: {synthesis}")
    return "\n".join(lines)


async def _collect_visible_ui_facts(page: Page, dashboard_frame: Frame | None, chat_frame: Frame | None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "current_tab": "",
        "open_modal": False,
        "visible_chart_titles": [],
        "tooltip_texts": [],
        "chat_visible": chat_frame is not None,
        "menu_visible": False,
    }
    if dashboard_frame is not None:
        try:
            raw = await dashboard_frame.evaluate(
                """
                () => {
                  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const tabNodes = Array.from(document.querySelectorAll("[role='tab'], .ant-tabs-tab, [data-test-tab]"));
                  let currentTab = '';
                  for (const el of tabNodes) {
                    const text = clean(el.innerText || el.textContent || '');
                    if (!text) continue;
                    const cls = (el.className || '').toString().toLowerCase();
                    const aria = (el.getAttribute('aria-selected') || '').toLowerCase();
                    if (aria === 'true' || cls.includes('active') || cls.includes('selected')) {
                      currentTab = text;
                      break;
                    }
                  }

                  const titleNodes = Array.from(
                    document.querySelectorAll('.header-title, .slice-header-title, [data-test="dashboard-chart-title"], h4, h5')
                  );
                  const visibleChartTitles = [];
                  const seen = new Set();
                  for (const el of titleNodes) {
                    const text = clean(el.innerText || el.textContent || '');
                    if (!text || seen.has(text)) continue;
                    const r = el.getBoundingClientRect();
                    if (!r || r.width < 4 || r.height < 4) continue;
                    if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) continue;
                    seen.add(text);
                    visibleChartTitles.push(text);
                    if (visibleChartTitles.length >= 8) break;
                  }

                  const modalSelectors = [
                    "[role='dialog']",
                    ".ant-modal",
                    ".modal-content",
                    ".chart-data-table",
                    ".ReactModal__Content"
                  ];
                  let openModal = false;
                  for (const sel of modalSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                      const r = el.getBoundingClientRect();
                      if (!r || r.width < 40 || r.height < 40) continue;
                      const style = window.getComputedStyle(el);
                      if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                      openModal = true;
                      break;
                    }
                    if (openModal) break;
                  }

                  const menuSelectors = [
                    "[role='menu']",
                    ".ant-dropdown",
                    ".popover",
                    ".menu",
                    ".dropdown-menu"
                  ];
                  let menuVisible = false;
                  for (const sel of menuSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                      const r = el.getBoundingClientRect();
                      if (!r || r.width < 20 || r.height < 20) continue;
                      const style = window.getComputedStyle(el);
                      if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                      menuVisible = true;
                      break;
                    }
                    if (menuVisible) break;
                  }

                  const tooltipTexts = [];
                  const tooltipSelectors = [
                    "[role='tooltip']", ".ant-tooltip-inner", ".tooltip", ".chart-tooltip",
                    ".echarts-tooltip", ".superset-legacy-chart-tooltip"
                  ];
                  const tooltipSeen = new Set();
                  for (const sel of tooltipSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                      const text = clean(el.innerText || el.textContent || '');
                      if (!text || tooltipSeen.has(text)) continue;
                      const r = el.getBoundingClientRect();
                      if (!r || r.width < 4 || r.height < 4) continue;
                      const style = window.getComputedStyle(el);
                      if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                      tooltipSeen.add(text);
                      tooltipTexts.push(text.slice(0, 240));
                    }
                  }

                  return {
                    current_tab: currentTab,
                    open_modal: openModal,
                    visible_chart_titles: visibleChartTitles,
                    menu_visible: menuVisible,
                    tooltip_texts: tooltipTexts.slice(0, 8),
                  };
                }
                """
            )
            if isinstance(raw, dict):
                facts["current_tab"] = str(raw.get("current_tab", "") or "")
                facts["open_modal"] = bool(raw.get("open_modal", False))
                titles = raw.get("visible_chart_titles", [])
                if isinstance(titles, list):
                    facts["visible_chart_titles"] = [str(t).strip()[:80] for t in titles if str(t).strip()][:8]
                facts["menu_visible"] = bool(raw.get("menu_visible", False))
                tooltips = raw.get("tooltip_texts", [])
                if isinstance(tooltips, list):
                    facts["tooltip_texts"] = [str(t).strip()[:240] for t in tooltips if str(t).strip()][:8]
        except Exception:
            pass
    return facts


def _update_memory_context(
    memory_context: dict[str, Any],
    *,
    ui_facts: dict[str, Any],
    reasoning: str,
    parsed: dict[str, Any],
    self_check: dict[str, Any],
    action: dict[str, Any],
    executed: dict[str, Any],
    url: str,
    previous_action: str,
    last_chat_answer: str,
) -> None:
    memory_context["last_url"] = url
    memory_context["last_chat_answer"] = last_chat_answer[:500]
    memory_context.setdefault("recent_actions", []).append(previous_action)
    memory_context["recent_actions"] = memory_context["recent_actions"][-20:]

    intent = str(action.get("type", executed.get("type", "unknown"))).strip().lower() or "unknown"
    intent_attempts = memory_context.setdefault("intent_attempts", {})
    intent_attempts[intent] = int(intent_attempts.get(intent, 0)) + 1

    flags = memory_context.setdefault("flags", {})
    if "category-level" in reasoning.lower():
        flags["category_tab_seen"] = True
    if intent == "chat":
        flags["chat_used"] = True
    if intent == "done":
        flags["done_reached"] = True
    current_tab = str(ui_facts.get("current_tab", "") or "").strip()
    if current_tab:
        flags["current_tab"] = current_tab
        if current_tab.lower() == "category-level":
            flags["category_tab_seen"] = True
    flags["open_modal"] = bool(ui_facts.get("open_modal", False))
    flags["chat_visible"] = bool(ui_facts.get("chat_visible", False))
    flags["menu_visible"] = bool(ui_facts.get("menu_visible", False))

    auto_facts: list[str] = []
    if current_tab:
        auto_facts.append(f"Current tab: {current_tab}")
    if ui_facts.get("open_modal"):
        auto_facts.append("A modal is open")
    if ui_facts.get("menu_visible"):
        auto_facts.append("A menu or dropdown is open")
    if ui_facts.get("chat_visible"):
        auto_facts.append("The left chat pane is visible")
    chart_titles = ui_facts.get("visible_chart_titles", [])
    if isinstance(chart_titles, list) and chart_titles:
        auto_facts.append("Visible charts: " + ", ".join([str(t)[:40] for t in chart_titles[:5]]))

    observed = _normalize_short_texts(auto_facts + list(parsed.get("observed_facts", [])), limit=140, keep=10)
    if observed:
        merged = memory_context.get("observed_facts", []) + observed
        memory_context["observed_facts"] = _normalize_short_texts(merged, limit=140, keep=20)

    hypothesis = " ".join(str(parsed.get("working_hypothesis", "") or "").strip().split())[:200]
    if hypothesis:
        memory_context["working_hypothesis"] = hypothesis

    resolved = _normalize_short_texts(parsed.get("resolved_answers", []), limit=180, keep=8)
    if resolved:
        merged = memory_context.get("resolved_answers", []) + resolved
        memory_context["resolved_answers"] = _normalize_short_texts(merged, limit=180, keep=16)

    generic_memory = memory_context.setdefault("generic_memory", {})
    if isinstance(self_check, dict) and self_check:
        generic_memory["last_self_check"] = {
            "prerequisite_met": str(self_check.get("prerequisite_met", "")).strip().lower(),
            "action_targets_prerequisite": str(self_check.get("action_targets_prerequisite", "")).strip().lower(),
            "what_is_missing": str(self_check.get("what_is_missing", "")).strip()[:120],
        }


async def _streamlit_seed_credentials(page: Page, username: str, password: str) -> None:
    username_input = page.get_by_label("Superset Username")
    password_input = page.get_by_label("Superset Password")
    await username_input.fill(username)
    await username_input.press("Tab")
    await page.wait_for_timeout(400)
    await password_input.fill(password)
    await password_input.press("Tab")
    # Streamlit text_input updates on rerun/blur; give it time to propagate session_state.
    await page.wait_for_timeout(1800)


async def _streamlit_select_model(page: Page, model: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "requested_model": model,
        "verified": False,
        "visible_text": "",
        "error": "",
    }
    try:
        container = page.locator('[data-testid="stSelectbox"]').filter(has_text="OpenAI Model").first
        if await container.count() == 0:
            evidence["error"] = "OpenAI Model selectbox was not found."
            return evidence
        control = container.locator('[data-baseweb="select"]').first
        await control.click()
        option = page.get_by_role("option", name=model, exact=True).first
        if await option.count() > 0:
            await option.click()
        else:
            await page.keyboard.type(model)
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(1200)
        visible_text = " ".join((await container.inner_text()).split())
        evidence["visible_text"] = visible_text[:300]
        evidence["verified"] = model.lower() in visible_text.lower()
        if not evidence["verified"]:
            evidence["error"] = "Selected model is not visible in the Streamlit model control."
    except Exception as exc:
        evidence["error"] = str(exc)[:400]
    return evidence


async def _streamlit_apply_meta(page: Page) -> bool:
    try:
        button = page.get_by_role("button", name="Login & Apply Meta")
        if await button.count() > 0:
            await button.click()
            await page.wait_for_timeout(2500)
            return True
    except Exception:
        return False
    return False


async def _login_inside_dashboard_frame(frame: Frame, username: str, password: str) -> None:
    try:
        username_input = frame.locator(
            "input[name='username'], input#username, input[autocomplete='username']"
        ).first
        password_input = frame.locator(
            "input[type='password'], input[name='password'], input#password"
        ).first
        if await username_input.count() == 0 or await password_input.count() == 0:
            return
        await username_input.click()
        await username_input.fill(username)
        await password_input.click()
        await password_input.fill(password)
        submit = frame.locator(
            "button[type='submit'], input[type='submit'], button:has-text('Sign in'), button:has-text('Login')"
        ).first
        if await submit.count() > 0:
            await submit.click()
        else:
            await password_input.press("Enter")
        await frame.wait_for_timeout(3500)
    except Exception:
        return


async def _wait_for_dashboard_ready(page: Page, username: str, password: str, timeout_sec: int = 30) -> Frame | None:
    for _ in range(timeout_sec):
        frame = await _find_dashboard_frame(page)
        if frame is not None:
            if "/login/" in (frame.url or ""):
                await _login_inside_dashboard_frame(frame, username, password)
            else:
                return frame
        else:
            await _streamlit_apply_meta(page)
        await page.wait_for_timeout(1000)
    return await _find_dashboard_frame(page)


async def _chat_send(chat_frame: Frame, text: str) -> None:
    input_el = chat_frame.locator(".chat-input").first
    send_btn = chat_frame.locator(".chat-send").first
    await input_el.fill(text)
    await send_btn.click()


async def _chat_wait_for_dom_answer(chat_frame: Frame) -> str:
    status_el = chat_frame.locator(".chat-status").first
    while True:
        try:
            status = (await status_el.inner_text()).strip().lower()
        except Exception:
            status = ""
        if status == "ready":
            break
        await chat_frame.wait_for_timeout(1000)
    assistant = chat_frame.locator(".bubble.assistant").last
    try:
        return (await assistant.inner_text()).strip()
    except Exception:
        return ""


def _poll_chat_session_status(
    *,
    base_url: str,
    session_id: str,
    message: str,
    since_ts: str,
    username: str,
) -> dict[str, Any] | None:
    if not base_url or not session_id:
        return None
    query = urlencode(
        {
            "session_id": session_id,
            "message": message,
            "since_ts": since_ts,
        }
    )
    req = Request(
        f"{base_url}?{query}",
        headers={"X-Superset-Username": username or ""},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _fetch_verified_active_context(
    *, chat_api_url: str, session_id: str, dashboard_id: int, username: str
) -> dict[str, Any]:
    if not chat_api_url or not session_id:
        return {}
    base = chat_api_url.rsplit("/chat", 1)[0].rstrip("/")
    query = urlencode({"dashboard_id": dashboard_id, "session_id": session_id, "user_key": username})
    req = Request(
        f"{base}/superset/charts/active?{query}",
        headers={"X-Superset-Username": username or ""},
    )
    try:
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _evaluate_required_state(
    requirement: dict[str, Any],
    ui_facts: dict[str, Any],
    active_context: dict[str, Any],
    tab_history: list[str] | None = None,
) -> dict[str, Any]:
    expected_tab = str(requirement.get("tab", "") or "").strip()
    visible_tab = str(ui_facts.get("current_tab", "") or "").strip()
    context_tab_obj = active_context.get("active_tab") if isinstance(active_context, dict) else None
    context_tab = str(context_tab_obj.get("name", "") if isinstance(context_tab_obj, dict) else "").strip()
    tab_verified = not expected_tab or expected_tab.lower() in {visible_tab.lower(), context_tab.lower()}

    expected_filters = requirement.get("filters", {}) if isinstance(requirement.get("filters"), dict) else {}
    active_filter_payloads: list[Any] = []
    for chart in active_context.get("active_charts", []) if isinstance(active_context, dict) else []:
        if not isinstance(chart, dict):
            continue
        for field in ("native_filters", "cross_filters"):
            value = chart.get(field, [])
            if isinstance(value, list):
                active_filter_payloads.extend(value)
            elif value:
                active_filter_payloads.append(value)
    filter_blob = json.dumps(active_filter_payloads, ensure_ascii=False).lower()
    normalized_filter_blob = re.sub(r"[^a-z0-9]+", " ", filter_blob)
    filter_checks: dict[str, bool] = {}
    for key, value in expected_filters.items():
        key_token = str(key).strip().lower()
        value_tokens = value if isinstance(value, list) else [value]
        value_ok = all(
            re.sub(r"[^a-z0-9]+", " ", str(item).strip().lower()).strip() in normalized_filter_blob
            for item in value_tokens
        )
        normalized_key = re.sub(r"[^a-z0-9]+", " ", key_token).strip()
        key_ok = not key_token or normalized_key in normalized_filter_blob or key_token.split("_")[-1] in filter_blob
        filter_checks[str(key)] = bool(key_ok and value_ok)
    clear_filters = [str(item).strip().lower() for item in requirement.get("clear_filters", []) if str(item).strip()]
    clear_filter_checks = {
        key: key not in filter_blob for key in clear_filters
    }
    filters_verified = (all(filter_checks.values()) if filter_checks else True) and (
        all(clear_filter_checks.values()) if clear_filter_checks else True
    )

    interaction = str(requirement.get("interaction", "none") or "none").lower()
    interactions = []
    for chart in active_context.get("active_charts", []) if isinstance(active_context, dict) else []:
        if isinstance(chart, dict) and isinstance(chart.get("interaction"), dict):
            interactions.append(chart["interaction"])
    tooltip_texts = ui_facts.get("tooltip_texts", []) if isinstance(ui_facts, dict) else []
    hover_targets = [
        str(item).strip().lower()
        for item in requirement.get("hover_targets", [])
        if str(item).strip()
    ]
    tooltip_blob = " ".join(str(item).lower() for item in tooltip_texts)
    hover_target_checks = {target: target in tooltip_blob for target in hover_targets}
    history = [str(item).strip() for item in (tab_history or []) if str(item).strip()]
    required_sequence = [
        str(item).strip() for item in requirement.get("tab_sequence", []) if str(item).strip()
    ]
    sequence_cursor = 0
    for visited in history:
        if sequence_cursor < len(required_sequence) and visited.lower() == required_sequence[sequence_cursor].lower():
            sequence_cursor += 1
    tab_sequence_verified = not required_sequence or sequence_cursor == len(required_sequence)
    if interaction == "none":
        interaction_verified = True
    elif interaction == "hover":
        interaction_verified = bool(tooltip_texts) and (
            all(hover_target_checks.values()) if hover_target_checks else True
        )
    elif interaction in {"cross_filter", "filter", "native_filter", "dashboard_filter"}:
        interaction_verified = filters_verified and bool(expected_filters or clear_filters)
    elif interaction in {"tab_switch", "tab_navigation"}:
        interaction_verified = tab_verified and filters_verified and tab_sequence_verified
    else:
        interaction_verified = bool(interactions) or filters_verified and bool(expected_filters)

    verified = bool(tab_verified and filters_verified and interaction_verified)
    return {
        "verified": verified,
        "expected_tab": expected_tab,
        "visible_tab": visible_tab,
        "context_tab": context_tab,
        "tab_verified": tab_verified,
        "filter_checks": filter_checks,
        "clear_filter_checks": clear_filter_checks,
        "active_filter_payloads": active_filter_payloads,
        "filters_verified": filters_verified,
        "interaction": interaction,
        "interaction_verified": interaction_verified,
        "tooltip_texts": tooltip_texts[:5] if isinstance(tooltip_texts, list) else [],
        "hover_target_checks": hover_target_checks,
        "tab_history": history,
        "required_tab_sequence": required_sequence,
        "tab_sequence_verified": tab_sequence_verified,
        "active_context": active_context,
    }


def _contains_json_object(value: str) -> bool:
    text = str(value or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        return isinstance(json.loads(text[start : end + 1]), dict)
    except json.JSONDecodeError:
        return False


def _record_tab_visit(
    tab_history: list[str], ui_facts: dict[str, Any], active_context: dict[str, Any]
) -> None:
    visible_tab = str(ui_facts.get("current_tab", "") or "").strip()
    active_tab = active_context.get("active_tab") if isinstance(active_context, dict) else None
    context_tab = str(active_tab.get("name", "") if isinstance(active_tab, dict) else "").strip()
    tab_name = context_tab or visible_tab
    if tab_name and (not tab_history or tab_history[-1].lower() != tab_name.lower()):
        tab_history.append(tab_name)


def _chat_send_via_api(
    *,
    base_url: str,
    session_id: str,
    username: str,
    password: str,
    dashboard_id: int,
    message: str,
    agent_model: str,
    timeout_sec: int,
    verified_ui_evidence: dict[str, Any] | None = None,
    debug: bool = False,
) -> dict[str, Any] | None:
    if not base_url or not session_id or not message.strip():
        return None
    payload = {
        "session_id": session_id,
        "user_id": username or "abc",
        "message": message,
        "dashboard_id": dashboard_id,
        "superset_username": username or "",
        "superset_password": password or "",
        "agent_model": agent_model or "",
        "verified_ui_evidence": verified_ui_evidence or {},
        "debug": debug,
    }
    req = Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Superset-Username": username or "",
            "X-Superset-Password": password or "",
        },
        method="POST",
    )
    try:
        response = urlopen(req) if timeout_sec <= 0 else urlopen(req, timeout=max(1, timeout_sec))
        with response as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body)
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _scroll_to_top(page: Page) -> None:
    await page.evaluate(
        """
        () => {
          window.scrollTo(0, 0);
          const nodes = Array.from(document.querySelectorAll('*'));
          for (const el of nodes) {
            try {
              const style = window.getComputedStyle(el);
              const overflowY = style?.overflowY || '';
              const canScroll =
                (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay') &&
                el.scrollHeight > el.clientHeight + 20;
              if (canScroll) {
                el.scrollTop = 0;
              }
            } catch (err) {
              // ignore
            }
          }
        }
        """
    )
    await page.wait_for_timeout(400)


async def _set_run_overlay(page: Page, text: str, tone: str = "info") -> None:
    bg = {
        "info": "rgba(17, 24, 39, 0.88)",
        "active": "rgba(3, 105, 161, 0.9)",
        "done": "rgba(22, 101, 52, 0.9)",
        "warn": "rgba(146, 64, 14, 0.92)",
    }.get(tone, "rgba(17, 24, 39, 0.88)")
    safe_text = json.dumps(text)
    safe_bg = json.dumps(bg)
    await page.evaluate(
        f"""
        () => {{
          const id = '__strict2_run_overlay__';
          let el = document.getElementById(id);
          if (!el) {{
            el = document.createElement('div');
            el.id = id;
            el.style.position = 'fixed';
            el.style.top = '12px';
            el.style.right = '12px';
            el.style.zIndex = '2147483647';
            el.style.maxWidth = '420px';
            el.style.padding = '10px 12px';
            el.style.borderRadius = '10px';
            el.style.color = '#f8fafc';
            el.style.font = '600 13px/1.45 Menlo, Monaco, monospace';
            el.style.boxShadow = '0 8px 24px rgba(0,0,0,0.28)';
            el.style.whiteSpace = 'pre-wrap';
            el.style.pointerEvents = 'none';
            document.body.appendChild(el);
          }}
          el.style.background = {safe_bg};
          el.textContent = {safe_text};
        }}
        """
    )


async def _get_chat_status(page: Page) -> str:
    chat_frame = await _find_chat_frame(page)
    if chat_frame is None:
        return "missing"
    try:
        status_el = chat_frame.locator(".chat-status").first
        if await status_el.count() == 0:
            return "unknown"
        status = (await status_el.inner_text()).strip().lower()
        return status or "unknown"
    except Exception:
        return "unknown"


async def _execute_action(
    page: Page,
    action: dict[str, Any],
    candidates: list[dict[str, Any]],
    chat_wait_timeout: int,
    chat_api_url: str,
    chat_status_url: str,
    password: str,
    username: str,
    session_id: str,
    dashboard_id: int,
    agent_model: str,
    verified_ui_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    action_type = str(action.get("type", "")).strip().lower()
    source = str(action.get("source", "dashboard")).strip().lower() or "dashboard"
    dom_id = int(action.get("dom_id", -1) or -1)
    requested_x = int(action.get("x", VIEWPORT_WIDTH // 2) or VIEWPORT_WIDTH // 2)
    requested_y = int(action.get("y", VIEWPORT_HEIGHT // 2) or VIEWPORT_HEIGHT // 2)
    x = requested_x
    y = requested_y
    text = str(action.get("text", ""))
    key = str(action.get("key", "Enter"))
    seconds = max(1, min(10, int(action.get("seconds", 1) or 1)))
    delta_y = int(action.get("delta_y", 500) or 500)
    target: dict[str, Any] = {
        "dom_id": dom_id,
        "source": source,
        "action_requested_xy": [requested_x, requested_y],
    }

    if 0 <= dom_id < len(candidates):
        chosen = candidates[dom_id]
        source = str(chosen.get("source", source))
        x = int(chosen.get("x", x) or x)
        y = int(chosen.get("y", y) or y)
        target = {
            "dom_id": dom_id,
            "source": source,
            "label": str(chosen.get("label", ""))[:160],
            "role": str(chosen.get("role", ""))[:80],
            "chart_title": str(chosen.get("chart_title", ""))[:120],
            "action_requested_xy": [requested_x, requested_y],
            "resolved_xy": [x, y],
            "frame_local_xy": [
                int(chosen.get("local_x", 0) or 0),
                int(chosen.get("local_y", 0) or 0),
            ],
        }

    if action_type in {"click", "hover", "type", "press", "scroll"}:
        target["point_target_before"] = await _point_target_evidence_for_source(page, source, x, y)

    chat_frame = await _find_chat_frame(page)

    if action_type == "click":
        await page.mouse.click(x, y)
        await page.wait_for_timeout(1600)
        return {
            "type": "click",
            "source": source,
            "x": x,
            "y": y,
            "target": target,
            "point_target_after": await _point_target_evidence_for_source(page, source, x, y),
            "dispatch_mode": "visible_coordinate",
            "action_dispatched": True,
        }, f"click({source},{x},{y})"
    if action_type == "hover":
        await page.mouse.move(x, y)
        await page.wait_for_timeout(1500)
        return {
            "type": "hover",
            "source": source,
            "x": x,
            "y": y,
            "target": target,
            "point_target_after": await _point_target_evidence_for_source(page, source, x, y),
            "dispatch_mode": "visible_coordinate",
            "action_dispatched": True,
        }, f"hover({source},{x},{y})"
    if action_type == "type":
        await page.mouse.click(x, y)
        await page.keyboard.type(text)
        await page.wait_for_timeout(1200)
        return {
            "type": "type",
            "source": source,
            "x": x,
            "y": y,
            "text": text,
            "target": target,
            "point_target_after": await _point_target_evidence_for_source(page, source, x, y),
            "dispatch_mode": "visible_coordinate",
            "action_dispatched": True,
        }, f"type({source},{x},{y})"
    if action_type == "press":
        await page.keyboard.press(key)
        await page.wait_for_timeout(1200)
        return {
            "type": "press",
            "source": source,
            "key": key,
            "target": target,
            "dispatch_mode": "keyboard",
            "action_dispatched": True,
        }, f"press({source},{key})"
    if action_type == "scroll":
        await page.mouse.wheel(0, delta_y)
        await page.wait_for_timeout(1200)
        return {
            "type": "scroll",
            "source": source,
            "delta_y": delta_y,
            "target": target,
            "dispatch_mode": "wheel",
            "action_dispatched": True,
        }, f"scroll({source},{delta_y})"
    if action_type == "wait":
        await asyncio.sleep(seconds)
        return {"type": "wait", "seconds": seconds}, f"wait({seconds})"
    if action_type == "chat":
        answer = ""
        _ = chat_wait_timeout
        print(f"[chat] start session_id={session_id or 'missing'} dashboard_id={dashboard_id}")
        print(f"[chat] question: {text[:400]}")
        print(f"[chat] route: api-first")
        await _set_run_overlay(
            page,
            f"Chat running\nsession={session_id or 'missing'}\nquestion={text[:180]}",
            tone="active",
        )
        api_payload = None
        for api_attempt in range(1, 3):
            candidate_payload = await asyncio.to_thread(
                _chat_send_via_api,
                base_url=chat_api_url,
                session_id=session_id,
                username=username,
                password=password,
                dashboard_id=dashboard_id,
                message=text,
                agent_model=agent_model,
                timeout_sec=chat_wait_timeout,
                verified_ui_evidence=verified_ui_evidence,
            )
            candidate_answer = (
                str(candidate_payload.get("answer", "")).strip()
                if isinstance(candidate_payload, dict)
                else ""
            )
            if _contains_json_object(candidate_answer):
                api_payload = candidate_payload
                answer = candidate_answer
                print(f"[chat] valid API response received attempt={api_attempt}")
                await _set_run_overlay(page, "Chat completed via API", tone="done")
                break
            print(f"[chat] invalid/empty API response attempt={api_attempt}; retrying")
            if api_attempt < 2:
                await asyncio.sleep(2)
        if not answer and chat_frame is not None:
            print("[chat] api empty/unavailable, fallback to ui chat")
            await _set_run_overlay(page, "Chat API unavailable\nFalling back to UI chat", tone="warn")
            await _chat_send(chat_frame, text)
            send_started_at = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
            if session_id and chat_status_url:
                print("[chat] waiting for session-status completion")
                await _set_run_overlay(page, "Waiting for chat session completion...", tone="active")
                poll_count = 0
                while chat_wait_timeout <= 0 or poll_count < chat_wait_timeout:
                    poll_count += 1
                    status_payload = await asyncio.to_thread(
                        _poll_chat_session_status,
                        base_url=chat_status_url,
                        session_id=session_id,
                        message=text,
                        since_ts=send_started_at,
                        username=username,
                    )
                    if isinstance(status_payload, dict) and status_payload.get("has_final_answer"):
                        answer = str(status_payload.get("answer", "")).strip()
                        print("[chat] session-status completion detected")
                        await _set_run_overlay(page, "Chat completed via session status", tone="done")
                        break
                    await asyncio.sleep(1)
            if not answer:
                print("[chat] waiting for ui chat ready state")
                await _set_run_overlay(page, "Waiting for UI chat ready state...", tone="active")
                answer = await _chat_wait_for_dom_answer(chat_frame)
                await _set_run_overlay(page, "Chat completed via UI", tone="done")
        if not answer:
            print("[chat] no answer received")
            await _set_run_overlay(page, "Chat failed to return an answer", tone="warn")
            await asyncio.sleep(1)
            return {"type": "wait", "fallback": "chat_api_failed"}, "wait(1)"
        print(f"[chat] completed via={'api' if api_payload else 'ui'}")
        return {"type": "chat", "text": text, "answer": answer[:500], "via": "api" if api_payload else "ui"}, "chat(send)"
    if action_type == "done":
        return {"type": "done", "result": str(action.get("result", "")).strip()}, "done"
    await asyncio.sleep(1)
    return {"type": "wait", "seconds": 1, "fallback": "unknown_action"}, "wait(1)"


async def _run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    workspace_root = Path(__file__).resolve().parents[1]
    _load_env(repo_root, workspace_root)
    username = (str(args.username).strip() or os.getenv("SUPERSET_USERNAME", "abc").strip())
    password = (str(args.password).strip() or os.getenv("SUPERSET_PASSWORD", "abc").strip())
    api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"')
    if not api_key:
        print("OPENAI_API_KEY is empty. Set it in .env or fastapi/.env first.")
        return 1

    task_text = _load_task(args)
    chat_template = _load_chat_template(workspace_root, args)
    scenario_requirement = _load_scenario_requirement(workspace_root, args)
    scenario_requires_dashboard_action = _scenario_requires_dashboard_action(scenario_requirement)
    trace_root = workspace_root / args.trace_dir
    if args.trace_dir_is_run_dir:
        trace_dir = trace_root
    else:
        run_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        trace_dir = trace_root / run_at
    trace_dir.mkdir(parents=True, exist_ok=True)
    steps_jsonl = trace_dir / "steps.jsonl"
    memory_context_path = trace_dir / "memory_context.json"

    client = OpenAI(api_key=api_key)
    planner_kwargs: dict[str, Any] = {
        "model": args.model,
        "response_format": {"type": "json_object"},
    }
    if args.model.startswith("gpt-5"):
        service_tier = (args.service_tier or os.getenv("OPENAI_SERVICE_TIER", "")).strip()
        reasoning_effort = (
            args.reasoning_effort or os.getenv("AGENT_REASONING_EFFORT", "")
        ).strip().lower()
        if service_tier:
            planner_kwargs["service_tier"] = service_tier
        if reasoning_effort:
            planner_kwargs["reasoning_effort"] = reasoning_effort
    previous_action = "none"
    final_answer = ""
    last_chat_answer = ""
    session_id = ""
    global_step = 0
    recent_actions: list[str] = []
    recent_target_history: list[str] = []
    dashboard_action_executed = False
    dashboard_action_verified = False
    dashboard_actions_used = 0
    dashboard_actions_attempted = 0
    dashboard_actions_dispatched = 0
    dashboard_actions_visual_change = 0
    dashboard_actions_visible_target = 0
    dashboard_actions_coordinate_dispatch = 0
    dashboard_actions_blocked = 0
    last_state_verification: dict[str, Any] = {}
    tab_history: list[str] = []
    streamlit_model_evidence: dict[str, Any] = {}
    apply_meta_clicked = False
    memory_context: dict[str, Any] = {
        "last_url": args.start_url,
        "flags": {
            "login_done": False,
            "dashboard_visible": False,
            "chat_used": False,
            "done_reached": False,
            "category_tab_seen": False,
        },
        "intent_attempts": {},
        "observed_facts": [],
        "working_hypothesis": "",
        "resolved_answers": [],
        "generic_memory": {
            "last_self_check": {},
        },
        "recent_actions": [],
        "recent_targets": [],
        "last_chat_answer": "",
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=[f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}", "--force-device-scale-factor=1"],
        )
        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=1,
        )
        page = await context.new_page()
        await page.goto(args.start_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        streamlit_model_evidence = await _streamlit_select_model(page, args.model)
        dashboard_frame = None
        if not streamlit_model_evidence.get("verified"):
            final_answer = (
                "FAILED_MODEL_NOT_VERIFIED: Streamlit was not configured with the requested model. "
                + str(streamlit_model_evidence.get("error", ""))
            )
        else:
            await _streamlit_seed_credentials(page, username, password)
            apply_meta_clicked = await _streamlit_apply_meta(page)
            if not apply_meta_clicked:
                final_answer = "FAILED_APPLY_META_NOT_CLICKED: Login & Apply Meta was not dispatched."
            else:
                dashboard_frame = await _wait_for_dashboard_ready(page, username, password, timeout_sec=30)
        await _scroll_to_top(page)
        session_id = await _extract_session_id(page)
        if dashboard_frame is None and not final_answer:
            final_answer = "Could not find the embedded dashboard frame."
        else:
            memory_context["flags"]["login_done"] = True
            memory_context["flags"]["dashboard_visible"] = True
            for step in range(1, args.max_steps + 1):
                global_step = step
                screenshot_path = trace_dir / f"step_{step:03d}.png"
                await _scroll_to_top(page)
                await page.wait_for_timeout(1500)
                await page.screenshot(path=str(screenshot_path), full_page=False)
                image_b64 = base64.b64encode(screenshot_path.read_bytes()).decode("utf-8")

                if args.direct_chat:
                    if scenario_requires_dashboard_action:
                        final_answer = (
                            "FAILED_REQUIRED_STATE_NOT_VERIFIED: --direct-chat cannot bypass "
                            "a scenario that requires dashboard state verification."
                        )
                        break
                    action = {"type": "chat", "source": "chat", "text": task_text}
                    chat_started = perf_counter()
                    api_payload = await asyncio.to_thread(
                        _chat_send_via_api,
                        base_url=args.chat_api_url,
                        session_id=session_id,
                        username=username,
                        password=password,
                        dashboard_id=args.dashboard_id,
                        message=task_text,
                        agent_model=args.model,
                        timeout_sec=args.chat_wait_timeout,
                        debug=True,
                    )
                    chat_latency_ms = (perf_counter() - chat_started) * 1000
                    last_chat_answer = str((api_payload or {}).get("answer", "")).strip()
                    executed = {
                        "type": "chat",
                        "source": "chat",
                        "text": task_text,
                        "answer": last_chat_answer,
                        "route": "api-direct",
                        "latency_ms": round(chat_latency_ms, 2),
                        "agent_debug": (api_payload or {}).get("debug", []),
                    }
                    previous_action = "chat(api-direct)"
                    _update_memory_context(
                        memory_context,
                        ui_facts={},
                        reasoning="Direct state-aware chat evaluation.",
                        parsed={},
                        self_check={},
                        action=action,
                        executed=executed,
                        url=page.url,
                        previous_action=previous_action,
                        last_chat_answer=last_chat_answer,
                    )
                    memory_context_path.write_text(
                        json.dumps(memory_context, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    row = {
                        "step": step,
                        "phase": "streamlit_direct_chat",
                        "url": page.url,
                        "reasoning": "Direct state-aware chat evaluation.",
                        "self_check": {},
                        "model_action": action,
                        "executed": executed,
                        "screenshot": screenshot_path.name,
                        "candidates": [],
                        "last_chat_answer": last_chat_answer[:800],
                    }
                    with steps_jsonl.open("a", encoding="utf-8") as fp:
                        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                    final_answer = last_chat_answer or "State-aware chat API returned no answer."
                    break

                host_candidates = await _collect_candidates(page.main_frame, "host", limit=40)
                dashboard_frame = await _find_dashboard_frame(page)
                dashboard_candidates = []
                if dashboard_frame is not None:
                    dashboard_candidates = await _collect_candidates(dashboard_frame, "dashboard", limit=60)
                    dx, dy = await _frame_offset(dashboard_frame, page)
                    for item in dashboard_candidates:
                        item["x"] = int(item.get("x", 0) or 0) + dx
                        item["y"] = int(item.get("y", 0) or 0) + dy
                chat_candidates = []
                chat_frame = await _find_chat_frame(page)
                if chat_frame is not None:
                    chat_candidates = await _collect_candidates(chat_frame, "chat", limit=20)
                    cx, cy = await _frame_offset(chat_frame, page)
                    for item in chat_candidates:
                        item["x"] = int(item.get("x", 0) or 0) + cx
                        item["y"] = int(item.get("y", 0) or 0) + cy
                ui_facts = await _collect_visible_ui_facts(page, dashboard_frame, chat_frame)
                if not session_id:
                    session_id = await _extract_session_id(page)
                active_context = await asyncio.to_thread(
                    _fetch_verified_active_context,
                    chat_api_url=args.chat_api_url,
                    session_id=session_id,
                    dashboard_id=args.dashboard_id,
                    username=username,
                )
                _record_tab_visit(tab_history, ui_facts, active_context)
                last_state_verification = _evaluate_required_state(
                    scenario_requirement, ui_facts, active_context, tab_history
                )
                dashboard_action_verified = bool(last_state_verification.get("verified"))
                if (
                    scenario_requires_dashboard_action
                    and not dashboard_action_verified
                    and dashboard_actions_attempted >= max(0, args.dashboard_action_budget)
                ):
                    final_answer = (
                        "FAILED_REQUIRED_STATE_NOT_VERIFIED: dashboard action budget was exhausted "
                        "before the required tab/filter/interaction state was verified."
                    )
                    row = {
                        "step": step,
                        "phase": "required_state_budget_exhausted",
                        "url": page.url,
                        "reasoning": final_answer,
                        "model_action": {"type": "done", "source": "dashboard"},
                        "executed": {
                            "type": "done",
                            "result": final_answer,
                            "state_verified": False,
                            "verification": last_state_verification,
                            "before_screenshot": screenshot_path.name,
                        },
                        "screenshot": screenshot_path.name,
                    }
                    with steps_jsonl.open("a", encoding="utf-8") as fp:
                        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                    global_step = step
                    break
                candidates = host_candidates + dashboard_candidates + chat_candidates
                candidates_text = _format_candidates(candidates, limit=60)
                chat_status = await _get_chat_status(page)
                if (
                    chat_status not in {"ready", "missing", "unknown"}
                    and chat_frame is not None
                    and (not scenario_requires_dashboard_action or dashboard_action_verified)
                ):
                    print(f"[chat] status={chat_status}; waiting without consuming planner steps")
                    if args.chat_wait_timeout <= 0:
                        dom_answer = await _chat_wait_for_dom_answer(chat_frame)
                    else:
                        try:
                            dom_answer = await asyncio.wait_for(
                                _chat_wait_for_dom_answer(chat_frame),
                                timeout=max(1, args.chat_wait_timeout),
                            )
                        except TimeoutError:
                            dom_answer = ""
                    if _contains_json_object(dom_answer):
                        last_chat_answer = dom_answer
                        chat_status = "ready"
                if chat_status == "ready" and chat_frame is not None:
                    try:
                        dom_answer = (await chat_frame.locator(".bubble.assistant").last.inner_text()).strip()
                    except Exception:
                        dom_answer = ""
                    if _contains_json_object(dom_answer):
                        last_chat_answer = dom_answer
                if (
                    last_chat_answer
                    and (not scenario_requires_dashboard_action or dashboard_action_verified)
                    and _contains_json_object(last_chat_answer)
                ):
                    final_answer = last_chat_answer
                    row = {
                        "step": step,
                        "phase": "streamlit_chat_capture",
                        "url": page.url,
                        "reasoning": "Captured completed JSON answer after verified dashboard state.",
                        "self_check": {"prerequisite_met": "yes"},
                        "model_action": {"type": "done", "source": "chat"},
                        "executed": {
                            "type": "done",
                            "result": last_chat_answer,
                            "state_verified": dashboard_action_verified,
                            "verification": last_state_verification,
                            "before_screenshot": screenshot_path.name,
                        },
                        "screenshot": screenshot_path.name,
                        "candidates": candidates[:20],
                        "last_chat_answer": last_chat_answer[:800],
                    }
                    with steps_jsonl.open("a", encoding="utf-8") as fp:
                        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                    global_step = step
                    break

                prompt = (
                    f"# Task:\n{task_text}\n\n"
                    f"{_format_chat_template(chat_template)}\n\n"
                    f"{_format_memory_context(memory_context)}\n"
                    f"Current URL: {page.url}\n"
                    f"Previous action: {previous_action}\n"
                    f"Recent action history: {_format_recent_actions(recent_actions)}\n"
                    f"Required state: {json.dumps(scenario_requirement, ensure_ascii=False)}\n"
                    f"Dashboard action attempted: {dashboard_action_executed}\n"
                    f"Dashboard action budget: {dashboard_actions_attempted}/{args.dashboard_action_budget} attempts "
                    f"({dashboard_actions_used} dispatched)\n"
                    f"Required state verified: {dashboard_action_verified}\n"
                    f"State verification evidence: {json.dumps(last_state_verification, ensure_ascii=False)[:2400]}\n"
                    f"Step: {step}/{args.max_steps}\n"
                    f"Chat status: {chat_status}\n"
                    f"Recent chat answer: {last_chat_answer[:800] or 'none'}\n"
                    f"Actionable candidates: {candidates_text}\n\n"
                    "Decide exactly one next action."
                )

                completion = client.chat.completions.create(
                    **planner_kwargs,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                            ],
                        },
                    ],
                )
                raw = completion.choices[0].message.content or "{}"
                parsed = _parse_action(raw)
                reasoning = str(parsed.get("reasoning", "")).strip()
                self_check = parsed.get("self_check", {}) if isinstance(parsed.get("self_check"), dict) else {}
                action = parsed.get("action", {}) if isinstance(parsed.get("action"), dict) else {}
                final_answer_candidate = str(parsed.get("final_answer", "")).strip()
                if final_answer_candidate and str(action.get("type", "")).strip().lower() != "done":
                    action = {"type": "done", "result": final_answer_candidate}
                planned_action_type = str(action.get("type", "")).strip().lower()
                planned_source = str(action.get("source", "")).strip().lower()
                planned_dom_id = int(action.get("dom_id", -1) or -1)
                if 0 <= planned_dom_id < len(candidates):
                    planned_source = str(candidates[planned_dom_id].get("source", planned_source)).strip().lower()
                if (
                    planned_source == "dashboard"
                    and planned_action_type in {"click", "hover", "type", "press", "scroll"}
                ):
                    dashboard_actions_attempted += 1
                if chat_status not in {"ready", "missing", "unknown"}:
                    action_type = str(action.get("type", "")).strip().lower()
                    if action_type not in {"wait", "done"}:
                        action = {"type": "wait", "seconds": 2}
                blocked_loop, target_fingerprint = _is_menu_loop_action(action, candidates, recent_target_history)
                if blocked_loop:
                    template_questions = chat_template.get("preferred_chat_questions", []) if isinstance(chat_template, dict) else []
                    fallback_text = (
                        str(template_questions[0]).strip()
                        if isinstance(template_questions, list) and template_questions and str(template_questions[0]).strip()
                        else (
                            "Using only the visible active tab and chart content, provide only the values needed to solve the task. "
                            "Do not rely on More Options or View as table flows. "
                            "Ground the answer in the visible chart names and values, and reply briefly in JSON or table form if possible."
                        )
                    )
                    action = {
                        "type": "chat",
                        "source": "chat",
                        "text": fallback_text,
                    }
                action_type = str(action.get("type", "")).strip().lower()
                resolved_source = str(action.get("source", "")).strip().lower()
                dom_id = int(action.get("dom_id", -1) or -1)
                resolved_candidate: dict[str, Any] = {}
                if 0 <= dom_id < len(candidates):
                    resolved_candidate = candidates[dom_id]
                    resolved_source = str(resolved_candidate.get("source", resolved_source)).strip().lower()
                resolved_label = str(resolved_candidate.get("label", "")).strip().lower()
                resolved_x = int(resolved_candidate.get("x", action.get("x", 0)) or 0)
                resolved_y = int(resolved_candidate.get("y", action.get("y", 0)) or 0)
                forbidden_filter_editor = (
                    resolved_source == "dashboard"
                    and action_type == "click"
                    and (
                        "add or edit filters" in resolved_label
                        or (
                            resolved_label in {"", "button"}
                            and resolved_x < 500
                            and 190 <= resolved_y <= 275
                        )
                    )
                )
                if forbidden_filter_editor:
                    dashboard_actions_blocked += 1
                    action = {
                        "type": "wait",
                        "seconds": 1,
                        "blocked_reason": "Filter configuration controls are forbidden in this experiment.",
                    }
                    action_type = "wait"
                    resolved_source = ""
                if (
                    scenario_requires_dashboard_action
                    and not dashboard_action_verified
                    and action_type in {"chat", "done"}
                ):
                    action = {
                        "type": "wait",
                        "seconds": 1,
                        "blocked_reason": "The required dashboard state is not verified; chat and completion are forbidden.",
                    }
                    action_type = "wait"
                    resolved_source = ""
                if (
                    resolved_source == "chat"
                    and action_type in {"click", "type", "press"}
                    and (not scenario_requires_dashboard_action or dashboard_action_verified)
                ):
                    template_questions = (
                        chat_template.get("preferred_chat_questions", [])
                        if isinstance(chat_template, dict)
                        else []
                    )
                    typed_text = str(action.get("text", "")).strip()
                    chat_text = typed_text or (
                        str(template_questions[0]).strip()
                        if isinstance(template_questions, list)
                        and template_questions
                        and str(template_questions[0]).strip()
                        else task_text
                    )
                    action = {
                        "type": "chat",
                        "source": "chat",
                        "text": chat_text,
                        "forced_route": "api_with_requested_model",
                    }
                    action_type = "chat"
                    resolved_source = "chat"
                if action_type == "chat":
                    all_questions = (
                        chat_template.get("preferred_chat_questions", [])
                        if isinstance(chat_template, dict)
                        else []
                    )
                    synthesis = (
                        str(chat_template.get("final_synthesis_prompt", "")).strip()
                        if isinstance(chat_template, dict)
                        else ""
                    )
                    chat_text = str(action.get("text", "")).strip()
                    if isinstance(all_questions, list):
                        for question in all_questions:
                            question_text = str(question).strip()
                            if question_text and question_text.lower() not in chat_text.lower():
                                chat_text = f"{chat_text} {question_text}".strip()
                    if synthesis:
                        if synthesis.lower() not in chat_text.lower():
                            chat_text = f"{chat_text} {synthesis}".strip()
                    action = dict(action)
                    action["text"] = chat_text
                if (
                    resolved_source == "dashboard"
                    and action_type in {"click", "hover", "type", "press", "scroll"}
                    and dashboard_actions_attempted > max(0, args.dashboard_action_budget)
                ):
                    action = {
                        "type": "wait",
                        "seconds": 1,
                        "blocked_reason": "Dashboard action budget exhausted.",
                    }
                executed, previous_action = await _execute_action(
                    page,
                    action,
                    candidates,
                    args.chat_wait_timeout,
                    args.chat_api_url,
                    args.chat_status_url,
                    password,
                    username,
                    session_id,
                    args.dashboard_id,
                    args.model,
                    {
                        "state_verified": dashboard_action_verified,
                        "tooltip_texts": last_state_verification.get("tooltip_texts", []),
                        "hover_target_checks": last_state_verification.get("hover_target_checks", {}),
                        "tab_history": last_state_verification.get("tab_history", []),
                        "required_tab_sequence": last_state_verification.get("required_tab_sequence", []),
                    },
                )
                after_screenshot_path = trace_dir / f"step_{step:03d}_after.png"
                await page.screenshot(path=str(after_screenshot_path), full_page=False)
                executed["before_screenshot"] = screenshot_path.name
                executed["after_screenshot"] = after_screenshot_path.name
                before_hash = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
                after_hash = hashlib.sha256(after_screenshot_path.read_bytes()).hexdigest()
                screenshot_changed = before_hash != after_hash
                executed["before_sha256"] = before_hash
                executed["after_sha256"] = after_hash
                executed["screenshot_changed"] = screenshot_changed
                if (
                    executed.get("source") == "dashboard"
                    and executed.get("type") in {"click", "hover", "type", "press", "scroll"}
                ):
                    dashboard_action_executed = True
                    dashboard_actions_used += 1
                    if bool(executed.get("action_dispatched")):
                        dashboard_actions_dispatched += 1
                    else:
                        dashboard_actions_blocked += 1
                    if screenshot_changed:
                        dashboard_actions_visual_change += 1
                    point_before = (executed.get("target") or {}).get("point_target_before") or {}
                    if bool(point_before.get("found")):
                        dashboard_actions_visible_target += 1
                    if str(executed.get("dispatch_mode", "")).startswith("visible_coordinate"):
                        dashboard_actions_coordinate_dispatch += 1
                    post_dashboard_frame = await _find_dashboard_frame(page)
                    post_chat_frame = await _find_chat_frame(page)
                    post_ui_facts = await _collect_visible_ui_facts(
                        page, post_dashboard_frame, post_chat_frame
                    )
                    post_active_context = await asyncio.to_thread(
                        _fetch_verified_active_context,
                        chat_api_url=args.chat_api_url,
                        session_id=session_id,
                        dashboard_id=args.dashboard_id,
                        username=username,
                    )
                    _record_tab_visit(tab_history, post_ui_facts, post_active_context)
                    last_state_verification = _evaluate_required_state(
                        scenario_requirement, post_ui_facts, post_active_context, tab_history
                    )
                    dashboard_action_verified = bool(last_state_verification.get("verified"))
                    executed["state_verified"] = dashboard_action_verified
                    executed["verification"] = last_state_verification
                recent_actions.append(previous_action)
                if target_fingerprint:
                    recent_target_history.append(target_fingerprint)
                    recent_target_history = recent_target_history[-12:]
                    memory_context["recent_targets"] = recent_target_history[-12:]
                if executed.get("type") == "chat":
                    last_chat_answer = str(executed.get("answer", "")).strip()
                finish_after_chat = (
                    bool(last_chat_answer)
                    and args.finish_after_chat
                    and (not scenario_requires_dashboard_action or dashboard_action_verified)
                    and _contains_json_object(last_chat_answer)
                )
                _update_memory_context(
                    memory_context,
                    ui_facts=ui_facts,
                    reasoning=reasoning,
                    parsed=parsed,
                    self_check=self_check,
                    action=action,
                    executed=executed,
                    url=page.url,
                    previous_action=previous_action,
                    last_chat_answer=last_chat_answer,
                )
                memory_context_path.write_text(
                    json.dumps(memory_context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                row = {
                    "step": step,
                    "phase": "streamlit",
                    "url": page.url,
                    "reasoning": reasoning,
                    "self_check": self_check,
                    "model_action": action,
                    "executed": executed,
                    "screenshot": screenshot_path.name,
                    "candidates": candidates[:20],
                    "last_chat_answer": last_chat_answer[:800],
                }
                with steps_jsonl.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"[streamlit step {step}] {page.url}")
                print(f"  reasoning: {reasoning[:220]}")
                print(f"  action: {json.dumps(action, ensure_ascii=False)}")
                executed_log = json.dumps(executed, ensure_ascii=False)
                print(f"  executed: {executed_log[:1400]}")
                if str(action.get("type", "")).strip().lower() == "done":
                    final_answer = str(action.get("result", "")).strip() or "done"
                    break
                if finish_after_chat:
                    final_answer = last_chat_answer
                    break

        await context.close()
        await browser.close()

    if not final_answer:
        if scenario_requires_dashboard_action and not dashboard_action_verified:
            final_answer = (
                "FAILED_REQUIRED_STATE_NOT_VERIFIED: browser actions did not produce the required "
                "tab/filter/interaction state. Check before/after screenshots and verification evidence."
            )
        else:
            final_answer = "Stopped after reaching the maximum number of steps. Check the trace screenshots and steps.jsonl."

    meta = {
        "start_url": args.start_url,
        "model": args.model,
        "service_tier": str(planner_kwargs.get("service_tier", "")),
        "reasoning_effort": str(planner_kwargs.get("reasoning_effort", "")),
        "task": task_text,
        "trace_dir": str(trace_dir),
        "steps_jsonl": str(steps_jsonl),
        "memory_context": str(memory_context_path),
        "final_answer": final_answer,
        "login_success": dashboard_frame is not None,
        "scenario_steps_executed": global_step,
        "max_steps": args.max_steps,
        "finish_after_chat": args.finish_after_chat,
        "direct_chat": args.direct_chat,
        "dashboard_id": args.dashboard_id,
        "chat_wait_timeout": args.chat_wait_timeout,
        "chat_api_url": args.chat_api_url,
        "session_id": session_id,
        "chat_status_url": args.chat_status_url,
        "scenario_requires_dashboard_action": scenario_requires_dashboard_action,
        "scenario_requirement": scenario_requirement,
        "dashboard_action_executed": dashboard_action_executed,
        "dashboard_actions_used": dashboard_actions_used,
        "dashboard_action_budget": args.dashboard_action_budget,
        "dashboard_actions_attempted": dashboard_actions_attempted,
        "dashboard_actions_dispatched": dashboard_actions_dispatched,
        "dashboard_actions_visual_change": dashboard_actions_visual_change,
        "dashboard_actions_no_visual_change": max(
            0, dashboard_actions_attempted - dashboard_actions_visual_change
        ),
        "dashboard_actions_visible_target": dashboard_actions_visible_target,
        "dashboard_actions_dom_dispatch": 0,
        "dashboard_actions_coordinate_dispatch": dashboard_actions_coordinate_dispatch,
        "dashboard_actions_occlusion_override": 0,
        "dashboard_actions_blocked": dashboard_actions_blocked,
        "dashboard_action_verified": dashboard_action_verified,
        "tab_history": tab_history,
        "streamlit_model_evidence": streamlit_model_evidence,
        "apply_meta_clicked": apply_meta_clicked,
        "state_verification": last_state_verification,
    }
    (trace_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Final answer: {final_answer}")
    print(f"Trace dir: {trace_dir}")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130
    except Exception as exc:
        print(f"Run failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
