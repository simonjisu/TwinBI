from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright


SYSTEM_PROMPT = """
You are a screenshot + DOM web automation agent.
You will receive:
- a screenshot of the current browser viewport
- a DOM candidate list (dom_id, label, type, x, y)
- a dashboard action candidate list collected from chart marks, legends, rows, and filter chips
- a task
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
    "type": "click|hover|type|press|scroll|wait|done",
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
- Prefer dom_id when a relevant DOM candidate exists.
- If you have enough evidence to answer the task, fill `final_answer` and choose `done`.
- Use `observed_facts` for short factual observations from the current or previous screens.
- Use `working_hypothesis` for your current best guess about what to do or what the answer may be.
- Use `resolved_answers` for conclusions you are confident are already established.
- Before choosing an action, run a self-check:
  - Are prerequisites for the goal already met?
  - If not, does this action directly help satisfy the prerequisite?
- For click/type actions, provide x and y viewport coordinates.
- For hover actions, move the mouse to (x, y) without clicking so tooltip/value overlays can appear.
- For type action, click at (x, y) first, then type text.
- If dom_id >= 0, dom_id target has priority over raw x/y.
- For scroll action, use delta_y (positive down, negative up).
- For wait action, use seconds in [1, 10].
- For done action, include final answer in result.
- If a login form is visible, prioritize login first using provided credentials.
- If a filter option/row already looks selected, highlighted, or active, do not click it again because that may toggle the filter off.
- Dashboard policy:
  - Allowed: tab navigation, filter clicks (global/cross filter), drill-by via right-click menu.
  - Forbidden: chart edit, chart configuration changes, dashboard structure changes.
  - Avoid top app header/upper-right chrome controls (..., Settings area).
  - Use only elements already present inside the Dashboard screen.
  - Do NOT add/create new filters (including Add/Edit Filters dialogs).
  - Filters must be applied by clicking existing dashboard elements, not by creating new filters.
  - If an action appears to enter edit mode, cancel it and choose another action.
- Keep reasoning concise and actionable.
""".strip()

LOGIN_ACTION_PROMPT = """
You are a login automation agent using screenshot + DOM candidates.
You will receive:
- a screenshot
- a DOM candidate list (dom_id, label, type, x, y)
- login credentials
Return strict JSON with this schema:
{
  "reasoning": "short Korean reasoning",
  "self_check": {
    "prerequisite_met": "yes|no|uncertain",
    "action_targets_prerequisite": "yes|no",
    "what_is_missing": "short Korean text"
  },
  "action": {
    "type": "click|hover|type|press|scroll|wait|done",
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
- If a relevant DOM candidate exists, prefer dom_id.
- For click/type actions, include x and y as fallback coordinates.
- If dom_id >= 0, dom_id target should be used first.
- Focus on completing login only.
""".strip()

LOGIN_STATE_PROMPT = """
You are a vision-only login-state checker.
Look only at the screenshot and decide whether login is still required.
Return strict JSON:
{
  "login_state": "needs_login|logged_in|uncertain",
  "evidence": "short Korean explanation"
}
Rules:
- needs_login: login form/button clearly visible and user is not authenticated.
- logged_in: dashboard/app content is visible and login form is not blocking.
- uncertain: cannot determine confidently.
""".strip()

REFLECTION_PROMPT = """
You are a reflection module for a vision-only browser agent.
The agent is stuck in repeated actions.
Return strict JSON:
{
  "reflection": "what went wrong (Korean, concise)",
  "new_strategy": "next strategy (Korean, concrete)",
  "force_next_action": {
    "type": "click|scroll|press|wait",
    "x": 0,
    "y": 0,
    "delta_y": 0,
    "key": "",
    "seconds": 1
  }
}
Rules:
- Choose a different strategy from the repeated action.
- Prefer scroll or a different click area when loop is detected.
""".strip()

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900
DEFAULT_STORAGE_STATE = '.auth/superset_storage_state.json'
SELECT_ALL_SHORTCUT = 'Meta+A' if platform.system() == 'Darwin' else 'Control+A'
PRE_ACTION_SETTLE_MS = 1200
POST_ACTION_SETTLE_MS = 2200


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Screenshot + DOM Playwright automation loop.')
    parser.add_argument('--task', type=str, help='Task text')
    parser.add_argument('--task-file', type=str, help='Task file path')
    parser.add_argument('--start-url', type=str, default='http://localhost:8088')
    parser.add_argument('--model', type=str, default='gpt-4.1-mini')
    parser.add_argument('--max-steps', type=int, default=30)
    parser.add_argument(
        '--headless',
        dest='headless',
        action='store_true',
        default=True,
        help='Run in hidden browser mode (default: enabled).',
    )
    parser.add_argument(
        '--show-browser',
        dest='headless',
        action='store_false',
        help='Show the browser window instead of running headless.',
    )
    parser.add_argument('--username', type=str, default='')
    parser.add_argument('--password', type=str, default='')
    parser.add_argument('--login-max-steps', type=int, default=20)
    parser.add_argument('--login-mode', type=str, choices=['dom', 'llm', 'hybrid'], default='dom')
    parser.add_argument('--superset-login-url', type=str, default='http://localhost:8088/login/')
    parser.add_argument(
        '--skip-login',
        dest='skip_login',
        action='store_true',
        default=False,
        help='Skip login pre-processing and start scenario directly (default: disabled).',
    )
    parser.add_argument(
        '--require-login',
        dest='skip_login',
        action='store_false',
        help='Run login pre-processing with LLM actions (default behavior).',
    )
    parser.add_argument(
        '--storage-state',
        type=str,
        default=DEFAULT_STORAGE_STATE,
        help='Playwright storage state JSON to preload authenticated session.',
    )
    parser.add_argument(
        '--save-storage-state',
        type=str,
        default=DEFAULT_STORAGE_STATE,
        help='Path to save Playwright storage state JSON after successful login.',
    )
    parser.add_argument('--dashboard-id', type=int, default=12)
    parser.add_argument('--viewport-width', type=int, default=VIEWPORT_WIDTH)
    parser.add_argument('--viewport-height', type=int, default=VIEWPORT_HEIGHT)
    parser.add_argument(
        '--force-dashboard-url',
        dest='force_dashboard_url',
        action='store_true',
        default=True,
        help='Always start scenario from the dashboard URL after login/session restore (default: enabled).',
    )
    parser.add_argument(
        '--keep-current-url',
        dest='force_dashboard_url',
        action='store_false',
        help='Keep the current page after login instead of redirecting to the dashboard URL.',
    )
    parser.add_argument('--max-reflections', type=int, default=1)
    parser.add_argument('--trace-dir', type=str, default='logs/vision_only_traces')
    parser.add_argument(
        '--trace-dir-is-run-dir',
        action='store_true',
        help='Treat --trace-dir as the final run directory and do not append a timestamp subdirectory.',
    )
    return parser


def _load_task(args: argparse.Namespace) -> str:
    if args.task:
        return args.task.strip()
    if args.task_file:
        return Path(args.task_file).read_text(encoding='utf-8').strip()
    raise ValueError('Either --task or --task-file is required.')


def _load_env(project_root: Path) -> None:
    load_dotenv(project_root / '.env', override=False)
    load_dotenv(project_root / 'fastapi' / '.env', override=False)


def _parse_action(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        # Fallback for providers that wrap JSON with extra text.
        l = raw.find('{')
        r = raw.rfind('}')
        if l >= 0 and r > l:
            data = json.loads(raw[l : r + 1])
        else:
            raise
    if not isinstance(data, dict) or 'action' not in data:
        raise ValueError('Model output must be a JSON object with action.')
    return data


def _parse_login_state(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except Exception:
        l = raw.find('{')
        r = raw.rfind('}')
        if l >= 0 and r > l:
            data = json.loads(raw[l : r + 1])
        else:
            raise
    if not isinstance(data, dict):
        raise ValueError('Login-state output must be a JSON object.')
    state = str(data.get('login_state', 'uncertain')).strip().lower()
    if state not in {'needs_login', 'logged_in', 'uncertain'}:
        state = 'uncertain'
    evidence = str(data.get('evidence', '')).strip()
    return {'login_state': state, 'evidence': evidence}


def _clamp(n: int, low: int, high: int) -> int:
    return max(low, min(high, n))


def _resolve_optional_path(project_root: Path, raw_value: str) -> Path | None:
    value = str(raw_value or '').strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    return path


def _is_on_dashboard_url(current_url: str, dashboard_id: int) -> bool:
    try:
        parsed = urlparse(current_url)
    except Exception:
        return False
    expected_path = f'/superset/dashboard/{dashboard_id}/'
    return parsed.path == expected_path


def _force_coordinate_only_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    out = dict(action)
    out['dom_id'] = -1
    return out


def _is_click_blocked(x: int, y: int) -> tuple[bool, str]:
    # Top global header/chrome area
    if y < 90:
        return True, 'top_header'
    # Upper-right app chrome controls (... / settings / profile)
    if x > 1220 and y < 170:
        return True, 'top_right_chrome'
    return False, ''


def _apply_tab_hint_override(
    action: dict[str, Any], reasoning: str, tab_hints: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    action_type = str(action.get('type', '')).strip().lower()
    if action_type != 'click':
        return action

    text = ' '.join(
        [
            str(reasoning or ''),
            str(action.get('text', '') or ''),
            str(action.get('result', '') or ''),
        ]
    ).lower()
    text = text.replace('_', '-')

    target_key = ''
    if 'category-level' in text or 'category level' in text:
        target_key = 'category_level'
    elif 'product-level' in text or 'product level' in text:
        target_key = 'product_level'
    elif 'store-level' in text or 'store level' in text:
        target_key = 'store_level'

    if not target_key:
        return action

    hint = tab_hints.get(target_key, {})
    hx = int(hint.get('x', 0) or 0)
    hy = int(hint.get('y', 0) or 0)
    if hx <= 0 or hy <= 0:
        return action

    out = dict(action)
    out['x'] = hx
    out['y'] = hy
    out['dom_id'] = -1
    return out


async def _collect_dom_candidates(page: Any, limit: int = 80) -> list[dict[str, Any]]:
    raw = await page.evaluate(
        """
        () => {
          const selectors = [
            "button", "a", "input", "textarea", "select",
            "[role='button']", "[role='tab']", "[role='option']", "[role='menuitem']",
            "[role='checkbox']", "[role='radio']",
            "[contenteditable='true']", "[data-test]", "[data-testid]"
          ];
          const seen = new Set();
          const out = [];
          for (const sel of selectors) {
            const nodes = document.querySelectorAll(sel);
            for (const el of nodes) {
              if (seen.has(el)) continue;
              seen.add(el);
              const r = el.getBoundingClientRect();
              if (!r || r.width < 8 || r.height < 8) continue;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) continue;
              const style = window.getComputedStyle(el);
              if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") continue;
              const txt = ((el.innerText || el.textContent || "") + "").replace(/\\s+/g, " ").trim().slice(0, 80);
              const aria = (el.getAttribute("aria-label") || "").trim();
              const placeholder = (el.getAttribute("placeholder") || "").trim();
              const title = (el.getAttribute("title") || "").trim();
              const role = (el.getAttribute("role") || "").trim();
              const tag = (el.tagName || "").toLowerCase();
              const type = (el.getAttribute("type") || "").trim();
              const label = (txt || aria || placeholder || title || tag).slice(0, 120);
              out.push({
                tag,
                role,
                type,
                label,
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
    for idx, item in enumerate(raw[:limit]):
        if not isinstance(item, dict):
            continue
        item['dom_id'] = idx
        out.append(item)
    return out


async def _collect_row_hints(page: Any) -> dict[str, list[dict[str, Any]]]:
    raw = await page.evaluate(
        """
        () => {
          const targets = {
            district: ['south', 'west', 'east', 'north'],
            department: ['sales', 'marketing', 'tech'],
          };
          const out = { district: [], department: [] };
          const seen = new Set();
          const nodes = document.querySelectorAll(
            "table td, table th, [role='gridcell'], [role='rowheader'], [role='cell']"
          );

          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const pushIfMatch = (bucket, label, x, y) => {
            const key = `${bucket}:${label}`;
            if (seen.has(key)) return;
            seen.add(key);
            out[bucket].push({ label, x, y });
          };

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '').toLowerCase();
            if (!text) continue;
            const r = el.getBoundingClientRect();
            if (!r || r.width < 6 || r.height < 6) continue;
            const x = Math.round(r.left + r.width / 2);
            const y = Math.round(r.top + r.height / 2);
            if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) continue;

            for (const label of targets.district) {
              if (text === label) pushIfMatch('district', label, x, y);
            }
            for (const label of targets.department) {
              if (text === label) pushIfMatch('department', label, x, y);
            }
          }
          return out;
        }
        """
    )
    if not isinstance(raw, dict):
        return {'district': [], 'department': []}
    district = raw.get('district', [])
    department = raw.get('department', [])
    if not isinstance(district, list):
        district = []
    if not isinstance(department, list):
        department = []
    return {'district': district[:8], 'department': department[:8]}


async def _collect_tab_hints(page: Any) -> dict[str, dict[str, Any]]:
    raw = await page.evaluate(
        """
        () => {
          const out = {
            store_level: null,
            product_level: null,
            category_level: null,
          };
          const nodes = document.querySelectorAll("[role='tab'], button, a");

          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const norm = (s) => clean(s).replace(/_/g, '-').replace(/\\s+/g, '-');
          const targets = [
            ['store_level', ['store-level', 'store level']],
            ['product_level', ['product-level', 'product level']],
            ['category_level', ['category-level', 'category level']],
          ];

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            if (!text) continue;
            const n = norm(text);
            const r = el.getBoundingClientRect();
            if (!r || r.width < 8 || r.height < 8) continue;
            const x = Math.round(r.left + r.width / 2);
            const y = Math.round(r.top + r.height / 2);
            if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) continue;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            // Ignore broad containers that merge multiple tab labels into one text block.
            let target_hits = 0;
            for (const [, labels] of targets) {
              for (const label of labels) {
                const needle = norm(label);
                if (n.includes(needle)) {
                  target_hits += 1;
                  break;
                }
              }
            }
            if (target_hits > 1) continue;
            for (const [key, labels] of targets) {
              for (const label of labels) {
                const needle = norm(label);
                if (n === needle) {
                  const area = Math.round(r.width * r.height);
                  const existing = out[key];
                  if (!existing || area < existing.area) {
                    out[key] = { label: text, x, y, area };
                  }
                  break;
                }
              }
            }
          }
          return out;
        }
        """
    )
    if not isinstance(raw, dict):
        return {'store_level': {}, 'product_level': {}, 'category_level': {}}
    out: dict[str, dict[str, Any]] = {}
    for key in ('store_level', 'product_level', 'category_level'):
        value = raw.get(key)
        if isinstance(value, dict):
            out[key] = {
                'label': str(value.get('label', '')),
                'x': int(value.get('x', 0) or 0),
                'y': int(value.get('y', 0) or 0),
            }
        else:
            out[key] = {}
    return out


async def _collect_selection_hints(page: Any) -> dict[str, Any]:
    raw = await page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const labels = ['south', 'west', 'east', 'north'];
          const selectedDistricts = [];
          const seen = new Set();
          const nodes = document.querySelectorAll(
            "table td, table th, [role='gridcell'], [role='rowheader'], [role='cell'], [aria-selected='true']"
          );

          const looksSelected = (el, style) => {
            if (!el || !style) return false;
            const aria = (el.getAttribute('aria-selected') || '').toLowerCase() === 'true';
            const cls = (el.className || '').toString().toLowerCase();
            const classHit = ['selected', 'active', 'highlight'].some((token) => cls.includes(token));
            const bg = style.backgroundColor || '';
            const bgHit =
              bg &&
              bg !== 'rgba(0, 0, 0, 0)' &&
              bg !== 'transparent' &&
              !bg.endsWith(', 0)');
            const fw = parseInt(style.fontWeight || '400', 10);
            return aria || classHit || bgHit || fw >= 600;
          };

          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || '');
            if (!labels.includes(text)) continue;
            const style = window.getComputedStyle(el);
            if (!looksSelected(el, style)) continue;
            if (seen.has(text)) continue;
            seen.add(text);
            selectedDistricts.push(text);
          }

          return { selected_districts: selectedDistricts };
        }
        """
    )
    if not isinstance(raw, dict):
        return {'selected_districts': []}
    selected = raw.get('selected_districts', [])
    if not isinstance(selected, list):
        selected = []
    return {'selected_districts': [str(item).strip().lower() for item in selected if str(item).strip()][:8]}


async def _collect_dashboard_action_candidates(page: Any, limit: int = 60) -> list[dict[str, Any]]:
    raw = await page.evaluate(
        """
        () => {
          const out = [];
          const seen = new Set();
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const push = (item) => {
            if (!item) return;
            const key = JSON.stringify(item);
            if (seen.has(key)) return;
            seen.add(key);
            out.push(item);
          };

          const visibleRect = (el) => {
            const r = el.getBoundingClientRect();
            if (!r || r.width < 4 || r.height < 4) return null;
            if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return null;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return null;
            return r;
          };

          const findChartTitle = (el) => {
            const chartRoot = el.closest('.chart-slice, [data-test-chart-id], [data-ui-anchor="chart"], .dashboard-chart');
            if (!chartRoot) return '';
            const titleNode = chartRoot.querySelector(
              '.header-title, .slice-header-title, [data-test="dashboard-chart-title"], h4, h5'
            );
            return clean(titleNode?.innerText || titleNode?.textContent || '');
          };

          const candidates = document.querySelectorAll(
            [
              "svg circle",
              "svg path",
              "svg rect",
              "[role='graphics-symbol']",
              "[class*='legend']",
              "[class*='Legend']",
              "[class*='mark']",
              "[class*='point']",
              ".echarts-for-react canvas",
              "table td",
              "table th",
              "[role='gridcell']",
              "[role='rowheader']",
              ".filter-item",
              ".dashboard-filter",
              ".ant-tag",
              ".ant-select-selection-item",
              "[data-test='filter-chip']"
            ].join(',')
          );

          for (const el of candidates) {
            const r = visibleRect(el);
            if (!r) continue;
            const x = Math.round(r.left + r.width / 2);
            const y = Math.round(r.top + r.height / 2);
            const text = clean(el.innerText || el.textContent || '');
            const aria = clean(el.getAttribute('aria-label') || '');
            const title = clean(el.getAttribute('title') || '');
            const cls = clean((el.getAttribute('class') || '').toString());
            const tag = (el.tagName || '').toLowerCase();
            const chart_title = findChartTitle(el);
            const label = text || aria || title || cls || tag;
            if (!label) continue;
            push({
              kind: tag === 'canvas' ? 'chart_canvas' : (
                tag === 'td' || tag === 'th' ? 'table_cell' :
                cls.toLowerCase().includes('legend') ? 'legend' :
                cls.toLowerCase().includes('filter') || cls.toLowerCase().includes('tag') ? 'filter_chip' :
                (tag === 'circle' || tag === 'path' || tag === 'rect') ? 'chart_mark' : 'dashboard_element'
              ),
              label: label.slice(0, 80),
              chart_title: chart_title.slice(0, 80),
              x,
              y,
              width: Math.round(r.width),
              height: Math.round(r.height),
            });
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
                'kind': str(item.get('kind', '')),
                'label': str(item.get('label', ''))[:80],
                'chart_title': str(item.get('chart_title', ''))[:80],
                'x': int(item.get('x', 0) or 0),
                'y': int(item.get('y', 0) or 0),
                'width': int(item.get('width', 0) or 0),
                'height': int(item.get('height', 0) or 0),
            }
        )
    return out


def _format_dom_candidates(dom_candidates: list[dict[str, Any]], limit: int = 40) -> str:
    if not dom_candidates:
        return '[]'
    compact = []
    for item in dom_candidates[:limit]:
        compact.append(
            {
                'dom_id': item.get('dom_id', -1),
                'tag': item.get('tag', ''),
                'role': item.get('role', ''),
                'type': item.get('type', ''),
                'label': item.get('label', ''),
                'x': item.get('x', 0),
                'y': item.get('y', 0),
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def _format_dashboard_action_candidates(candidates: list[dict[str, Any]], limit: int = 40) -> str:
    if not candidates:
        return '[]'
    compact = []
    for item in candidates[:limit]:
        compact.append(
            {
                'kind': item.get('kind', ''),
                'label': item.get('label', ''),
                'chart_title': item.get('chart_title', ''),
                'x': item.get('x', 0),
                'y': item.get('y', 0),
                'width': item.get('width', 0),
                'height': item.get('height', 0),
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def _infer_intent(reasoning: str) -> str:
    text = reasoning.lower()
    if 'login' in text:
        return 'login'
    if 'dashboard' in text:
        return 'open_dashboard'
    if 'north' in text or 'district filter' in text:
        return 'apply_north_filter'
    if '15' in reasoning and 'days' in text:
        return 'apply_15day_filter'
    if 'ranking' in text:
        return 'check_ranking'
    if 'survey' in text:
        return 'survey'
    return 'other'


async def _run(args: argparse.Namespace) -> int:
    # This file lives in experiments/src; load credentials from the repository root.
    project_root = Path(__file__).resolve().parents[2]
    _load_env(project_root)
    # Credential precedence: CLI arg > .env > hardcoded fallback.
    args.username = (str(args.username).strip() or os.getenv('SUPERSET_USERNAME', 'def').strip())
    args.password = (str(args.password).strip() or os.getenv('SUPERSET_PASSWORD', 'abc').strip())
    model_name = args.model.strip()
    use_gemini = model_name.lower().startswith('gemini')

    if use_gemini:
        api_key = os.getenv('GEMINI_API_KEY', '').strip().strip('"')
        if not api_key:
            print('GEMINI_API_KEY is empty. Set it in .env or fastapi/.env first.')
            return 1
        gemini_base_url = os.getenv(
            'GEMINI_OPENAI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai/'
        ).strip()
    else:
        api_key = os.getenv('OPENAI_API_KEY', '').strip().strip('"')
        if not api_key:
            print('OPENAI_API_KEY is empty. Set it in .env or fastapi/.env first.')
            return 1

    task_text = _load_task(args)
    run_at = datetime.now().strftime('%Y%m%d_%H%M%S')
    if args.trace_dir_is_run_dir:
        trace_dir = project_root / args.trace_dir
    else:
        trace_dir = project_root / args.trace_dir / run_at
    trace_dir.mkdir(parents=True, exist_ok=True)
    steps_jsonl = trace_dir / 'steps.jsonl'
    memory_context_path = trace_dir / 'memory_context.json'

    if use_gemini:
        client = OpenAI(api_key=api_key, base_url=gemini_base_url)
        chat_kwargs: dict[str, Any] = {'model': model_name}
    else:
        client = OpenAI(api_key=api_key)
        chat_kwargs = {'model': model_name, 'response_format': {'type': 'json_object'}}
    if not model_name.startswith('gpt-5'):
        chat_kwargs['temperature'] = 0
    viewport_width = max(800, int(args.viewport_width))
    viewport_height = max(600, int(args.viewport_height))
    start_url = str(args.start_url).strip()
    default_dashboard_url = f'http://localhost:8088/superset/dashboard/{args.dashboard_id}/'
    dashboard_url = start_url if '/superset/dashboard/' in start_url else default_dashboard_url
    loaded_storage_state = ''
    saved_storage_state = ''
    state_path = _resolve_optional_path(project_root, args.storage_state)
    save_state_path = _resolve_optional_path(project_root, args.save_storage_state)

    if state_path and state_path.exists():
        loaded_storage_state = str(state_path)
    elif state_path:
        print(f'Warning: storage state file not found: {state_path}')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=[
                f'--window-size={viewport_width},{viewport_height}',
                '--force-device-scale-factor=1',
            ],
        )
        context_kwargs: dict[str, Any] = {
            'viewport': {'width': viewport_width, 'height': viewport_height},
            'device_scale_factor': 1,
        }
        if loaded_storage_state:
            context_kwargs['storage_state'] = loaded_storage_state
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        await page.add_init_script(
            "(() => { document.documentElement.style.zoom = '100%'; document.body.style.zoom = '100%'; })();"
        )
        await page.goto(start_url, wait_until='domcontentloaded', timeout=45000)
        await page.evaluate("window.scrollTo(0, 0)")

        previous_action = 'none'
        final_answer = ''
        repeat_count = 0
        last_signature = ''
        global_step = 0
        login_success = False
        scenario_steps = 0
        logged_in_streak = 0
        reflections_used = 0
        memory_context: dict[str, Any] = {
            'run_id': run_at,
            'last_url': start_url,
            'flags': {
                'login_done': False,
                'dashboard_opened': False,
                'north_filter_applied': False,
                'days15_filter_applied': False,
                'ranking_checked': False,
                'survey_done': False,
            },
            'intent_attempts': {},
            'click_hotspots': {},
            'recent_observations': [],
            'observed_facts': [],
            'working_hypothesis': '',
            'resolved_answers': [],
            'reflection_notes': [],
            'generic_memory': {
                'check_attempts': [],
                'confirmed': [],
                'unconfirmed': [],
                'last_self_check': {},
            },
        }

        def _safe_int(value: Any, default: int = -1) -> int:
            try:
                return int(value)
            except Exception:
                return default

        def _normalize_label(value: Any, limit: int = 48) -> str:
            text = str(value or '').strip().lower()
            text = ' '.join(text.split())
            return text[:limit]

        def _grid(value: int, size: int = 40) -> int:
            return max(0, value // size)

        def _candidate_signature(candidate: dict[str, Any]) -> str:
            tag = _normalize_label(candidate.get('tag', ''), 16)
            role = _normalize_label(candidate.get('role', ''), 24)
            label = _normalize_label(candidate.get('label', ''), 48)
            cx = _safe_int(candidate.get('x', -1), -1)
            cy = _safe_int(candidate.get('y', -1), -1)
            if cx >= 0 and cy >= 0:
                return f'sig:{tag}|{role}|{label}|g{_grid(cx)},{_grid(cy)}'
            return f'sig:{tag}|{role}|{label}'

        def _attach_target_signature(action: dict[str, Any], dom_candidates: list[dict[str, Any]]) -> dict[str, Any]:
            if not isinstance(action, dict):
                return {}
            out = dict(action)
            dom_id = _safe_int(out.get('dom_id', -1), -1)
            if dom_id >= 0:
                for candidate in dom_candidates:
                    if _safe_int(candidate.get('dom_id', -1), -1) == dom_id:
                        out['target_signature'] = _candidate_signature(candidate)
                        return out
            action_type = str(out.get('type', '')).strip().lower()
            if action_type in {'click', 'hover', 'type'}:
                x = _safe_int(out.get('x', -1), -1)
                y = _safe_int(out.get('y', -1), -1)
                if x >= 0 and y >= 0:
                    nearest: dict[str, Any] | None = None
                    best_dist = 1_000_000
                    for candidate in dom_candidates:
                        cx = _safe_int(candidate.get('x', -1), -1)
                        cy = _safe_int(candidate.get('y', -1), -1)
                        if cx < 0 or cy < 0:
                            continue
                        dist = abs(cx - x) + abs(cy - y)
                        if dist < best_dist:
                            best_dist = dist
                            nearest = candidate
                    if nearest is not None and best_dist <= 140:
                        out['target_signature'] = _candidate_signature(nearest)
                    else:
                        out['target_signature'] = f'xy:g{_grid(x)},{_grid(y)}'
            return out

        def _action_target_summary(model_action: dict[str, Any], executed: dict[str, Any]) -> str:
            signature = str(model_action.get('target_signature', '')).strip()
            if signature:
                return signature
            dom_id = _safe_int(model_action.get('dom_id', -1), -1)
            if dom_id >= 0:
                return f'dom:{dom_id}'
            action_type = str(model_action.get('type', '')).strip().lower()
            if action_type in {'click', 'hover', 'type'}:
                x = _safe_int(executed.get('x', model_action.get('x', -1)), -1)
                y = _safe_int(executed.get('y', model_action.get('y', -1)), -1)
                return f'xy:{x},{y}'
            if action_type == 'press':
                return f"key:{str(model_action.get('key', executed.get('key', ''))).strip()}"
            if action_type == 'scroll':
                return f"dy:{_safe_int(model_action.get('delta_y', executed.get('delta_y', 0)), 0)}"
            return action_type or str(executed.get('type', 'other')).strip().lower() or 'other'

        def _action_hits_named_row(action: dict[str, Any], row_hints: dict[str, list[dict[str, Any]]], label: str) -> bool:
            candidates = row_hints.get('district', []) if isinstance(row_hints, dict) else []
            target_x = _safe_int(action.get('x', -1), -1)
            target_y = _safe_int(action.get('y', -1), -1)
            if target_x < 0 or target_y < 0:
                return False
            for item in candidates:
                if str(item.get('label', '')).strip().lower() != label:
                    continue
                hx = _safe_int(item.get('x', -1), -1)
                hy = _safe_int(item.get('y', -1), -1)
                if hx < 0 or hy < 0:
                    continue
                if abs(hx - target_x) <= 120 and abs(hy - target_y) <= 36:
                    return True
            return False

        def _resolve_check_status(phase: str, model_action: dict[str, Any], executed: dict[str, Any]) -> str:
            action_type = str(model_action.get('type', executed.get('type', ''))).strip().lower()
            if action_type == 'done':
                return 'verified'
            return 'unknown'

        def _compact_json(value: Any, limit: int = 220) -> str:
            try:
                text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            except Exception:
                text = str(value)
            text = ' '.join(text.split())
            if len(text) > limit:
                return text[: limit - 3] + '...'
            return text

        def _candidate_snapshot(items: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                out.append(
                    {
                        'label': str(item.get('label', ''))[:80],
                        'kind': str(item.get('kind', ''))[:32],
                        'chart_title': str(item.get('chart_title', ''))[:80],
                        'x': _safe_int(item.get('x', 0), 0),
                        'y': _safe_int(item.get('y', 0), 0),
                    }
                )
            return out

        def _log_step(
            *,
            phase: str,
            step: int,
            url: str,
            reasoning: str,
            model_action: dict[str, Any],
            executed: dict[str, Any],
            self_check: dict[str, Any] | None = None,
        ) -> None:
            print(f'[{phase} step {step}] {url}')
            if reasoning:
                print(f'  reasoning: {" ".join(reasoning.split())[:220]}')
            if self_check:
                print(f'  self_check: {_compact_json(self_check, limit=180)}')
            print(f'  action: {_compact_json(model_action)}')
            print(f'  executed: {_compact_json(executed)}')

        def _upsert_bucket(
            bucket: list[dict[str, Any]],
            *,
            key: str,
            label: str,
            step: int,
            phase: str,
            url: str,
            keep: int = 20,
        ) -> None:
            for item in bucket:
                if str(item.get('key', '')) == key:
                    item['label'] = label
                    item['count'] = int(item.get('count', 0)) + 1
                    item['last_step'] = step
                    item['last_phase'] = phase
                    item['last_url'] = url
                    return
            bucket.append(
                {
                    'key': key,
                    'label': label,
                    'count': 1,
                    'last_step': step,
                    'last_phase': phase,
                    'last_url': url,
                }
            )
            if len(bucket) > keep:
                del bucket[:-keep]

        def _normalize_short_texts(value: Any, limit: int = 120, keep: int = 12) -> list[str]:
            if isinstance(value, str):
                items = [value]
            elif isinstance(value, list):
                items = value
            else:
                items = []
            out: list[str] = []
            seen: set[str] = set()
            for item in items:
                text = ' '.join(str(item or '').strip().split())
                if not text:
                    continue
                text = text[:limit]
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
                if len(out) >= keep:
                    break
            return out

        def _update_memory(
            *,
            phase: str,
            step: int,
            reasoning: str,
            self_check: dict[str, Any] | None,
            model_action: dict[str, Any],
            executed: dict[str, Any],
            url: str,
            parsed: dict[str, Any] | None = None,
        ) -> None:
            intent = _infer_intent(reasoning)
            intent_attempts = memory_context['intent_attempts']
            intent_attempts[intent] = int(intent_attempts.get(intent, 0)) + 1

            if executed.get('type') == 'click':
                cx = int(executed.get('x', -1))
                cy = int(executed.get('y', -1))
                key = f'{cx},{cy}'
                hotspots = memory_context['click_hotspots']
                hotspots[key] = int(hotspots.get(key, 0)) + 1

            # Heuristic progress flags from reasoning/action intent.
            if phase.startswith('login') and ('logged_in' in reasoning or intent == 'login'):
                if 'streak=2' in reasoning or 'login_success' in str(model_action):
                    memory_context['flags']['login_done'] = True
            if intent == 'open_dashboard':
                memory_context['flags']['dashboard_opened'] = True
            if intent == 'apply_north_filter':
                memory_context['flags']['north_filter_applied'] = True
            if intent == 'apply_15day_filter':
                memory_context['flags']['days15_filter_applied'] = True
            if intent == 'check_ranking':
                memory_context['flags']['ranking_checked'] = True
            if intent == 'survey':
                memory_context['flags']['survey_done'] = True

            memory_context['last_url'] = url
            obs = {
                'step': step,
                'phase': phase,
                'intent': intent,
                'reasoning': reasoning[:240],
                'action_type': model_action.get('type', ''),
                'executed_type': executed.get('type', ''),
                'url': url,
            }
            recent = memory_context['recent_observations']
            recent.append(obs)
            if len(recent) > 12:
                del recent[:-12]

            parsed = parsed if isinstance(parsed, dict) else {}
            observed = _normalize_short_texts(parsed.get('observed_facts', []), limit=140, keep=8)
            if observed:
                existing = memory_context.get('observed_facts', [])
                if not isinstance(existing, list):
                    existing = []
                merged = existing[:]
                seen = {str(item).strip().lower() for item in merged}
                for item in observed:
                    key = item.lower()
                    if key in seen:
                        continue
                    merged.append(item)
                    seen.add(key)
                memory_context['observed_facts'] = merged[-20:]

            hypothesis = ' '.join(str(parsed.get('working_hypothesis', '') or '').strip().split())[:200]
            if hypothesis:
                memory_context['working_hypothesis'] = hypothesis

            resolved = _normalize_short_texts(parsed.get('resolved_answers', []), limit=180, keep=6)
            final_answer_text = ' '.join(str(parsed.get('final_answer', '') or '').strip().split())[:200]
            if final_answer_text:
                resolved.append(final_answer_text)
            if resolved:
                existing = memory_context.get('resolved_answers', [])
                if not isinstance(existing, list):
                    existing = []
                merged = existing[:]
                seen = {str(item).strip().lower() for item in merged}
                for item in resolved:
                    key = item.lower()
                    if key in seen:
                        continue
                    merged.append(item)
                    seen.add(key)
                memory_context['resolved_answers'] = merged[-12:]

            generic_memory = memory_context['generic_memory']
            action_type = str(model_action.get('type', executed.get('type', ''))).strip().lower() or 'other'
            target_summary = _action_target_summary(model_action, executed)
            check_key = f'{intent}|{action_type}|{target_summary}'
            label = (reasoning or check_key).replace('\n', ' ').strip()[:140]
            status = _resolve_check_status(phase, model_action, executed)

            attempts = generic_memory['check_attempts']
            attempts.append(
                {
                    'step': step,
                    'phase': phase,
                    'key': check_key,
                    'label': label,
                    'status': status,
                    'action_type': action_type,
                }
            )
            if len(attempts) > 40:
                del attempts[:-40]

            if status == 'verified':
                _upsert_bucket(
                    generic_memory['confirmed'],
                    key=check_key,
                    label=label,
                    step=step,
                    phase=phase,
                    url=url,
                    keep=20,
                )
                generic_memory['unconfirmed'] = [
                    item for item in generic_memory['unconfirmed'] if str(item.get('key', '')) != check_key
                ]
            elif status == 'unverified':
                _upsert_bucket(
                    generic_memory['unconfirmed'],
                    key=check_key,
                    label=label,
                    step=step,
                    phase=phase,
                    url=url,
                    keep=20,
                )

            if isinstance(self_check, dict) and self_check:
                generic_memory['last_self_check'] = {
                    'step': step,
                    'phase': phase,
                    'prerequisite_met': str(self_check.get('prerequisite_met', '')).strip().lower(),
                    'action_targets_prerequisite': str(self_check.get('action_targets_prerequisite', '')).strip().lower(),
                    'what_is_missing': str(self_check.get('what_is_missing', '')).strip()[:120],
                }

            memory_context_path.write_text(
                json.dumps(memory_context, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )

        def _memory_prompt_block() -> str:
            hotspots = memory_context['click_hotspots']
            crowded = sorted(hotspots.items(), key=lambda kv: kv[1], reverse=True)[:5]
            crowded_text = ', '.join([f'{k}:{v}' for k, v in crowded]) if crowded else 'none'
            generic_memory = memory_context.get('generic_memory', {})
            recent_attempts = generic_memory.get('check_attempts', [])[-5:]
            attempt_text = (
                '; '.join(
                    [
                        f"[{item.get('step', '?')}] {item.get('label', '')[:48]} ({item.get('status', 'unknown')})"
                        for item in recent_attempts
                    ]
                )
                if recent_attempts
                else 'none'
            )
            confirmed_text = (
                ', '.join([str(item.get('label', ''))[:48] for item in generic_memory.get('confirmed', [])[-5:]])
                if generic_memory.get('confirmed')
                else 'none'
            )
            unconfirmed_text = (
                ', '.join([str(item.get('label', ''))[:48] for item in generic_memory.get('unconfirmed', [])[-5:]])
                if generic_memory.get('unconfirmed')
                else 'none'
            )
            last_self_check = json.dumps(generic_memory.get('last_self_check', {}), ensure_ascii=False)
            observed_text = (
                ', '.join([str(item)[:72] for item in memory_context.get('observed_facts', [])[-6:]])
                if memory_context.get('observed_facts')
                else 'none'
            )
            hypothesis_text = str(memory_context.get('working_hypothesis', '') or 'none')
            resolved_text = (
                ', '.join([str(item)[:72] for item in memory_context.get('resolved_answers', [])[-6:]])
                if memory_context.get('resolved_answers')
                else 'none'
            )
            return (
                'Memory context (persisted progress):\n'
                f"- flags: {json.dumps(memory_context['flags'], ensure_ascii=False)}\n"
                f"- intent_attempts: {json.dumps(memory_context['intent_attempts'], ensure_ascii=False)}\n"
                f"- repeated click hotspots: {crowded_text}\n"
                f"- observed_facts: {observed_text}\n"
                f"- working_hypothesis: {hypothesis_text}\n"
                f"- resolved_answers: {resolved_text}\n"
                f"- recent verification attempts: {attempt_text}\n"
                f"- confirmed items: {confirmed_text}\n"
                f"- unresolved items: {unconfirmed_text}\n"
                f"- last self_check: {last_self_check}\n"
                "- Do not repeat the same intent or same click hotspot excessively.\n"
                "- If an item stays in 'unresolved items', choose a different action strategy.\n"
            )

        def _run_reflection(current_image_b64: str) -> dict[str, Any]:
            reflect_user = (
                "Loop detected.\n"
                f"Current URL: {page.url}\n"
                f"Memory: {json.dumps(memory_context, ensure_ascii=False)}\n"
                "Provide one reflection and one immediate recovery action."
            )
            resp = client.chat.completions.create(
                **chat_kwargs,
                messages=[
                    {'role': 'system', 'content': REFLECTION_PROMPT},
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': reflect_user},
                            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{current_image_b64}'}},
                        ],
                    },
                ],
            )
            raw = resp.choices[0].message.content or '{}'
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault('reflection', 'Progress has stalled because the same action was repeated.')
            data.setdefault('new_strategy', 'Try a different region of the screen.')
            data.setdefault('force_next_action', {'type': 'scroll', 'delta_y': 320})
            return data

        async def _stabilize_view() -> None:
            await page.evaluate(
                "(() => { document.documentElement.style.zoom='100%'; document.body.style.zoom='100%'; window.scrollTo(0,0); })();"
            )

        async def _find_first_visible(selectors: list[str]) -> Any | None:
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        return locator
                except Exception:
                    continue
            return None

        async def _locator_center(locator: Any) -> tuple[int, int]:
            try:
                box = await locator.bounding_box()
            except Exception:
                box = None
            if not box:
                return 0, 0
            cx = int(box.get('x', 0) + box.get('width', 0) / 2)
            cy = int(box.get('y', 0) + box.get('height', 0) / 2)
            return cx, cy

        async def _attempt_dom_login() -> dict[str, Any] | None:
            username_locator = await _find_first_visible(
                [
                    "input[name='username']",
                    "input#username",
                    "input[autocomplete='username']",
                    "input[placeholder*='username' i]",
                    "input[placeholder*='email' i]",
                    # Superset's current login form exposes only an accessible label.
                    "input[type='text']",
                    "input",
                ]
            )
            password_locator = await _find_first_visible(
                [
                    "input[type='password']",
                    "input[name='password']",
                    "input#password",
                    "input[autocomplete='current-password']",
                ]
            )
            if username_locator is None or password_locator is None:
                return None

            ux, uy = await _locator_center(username_locator)
            px, py = await _locator_center(password_locator)
            await username_locator.click()
            await username_locator.fill(args.username)
            await password_locator.click()
            await password_locator.fill(args.password)

            submit_locator = await _find_first_visible(
                [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Sign in')",
                    "button:has-text('Login')",
                    "[role='button']:has-text('Sign in')",
                    "[role='button']:has-text('Login')",
                ]
            )
            if submit_locator is not None:
                sx, sy = await _locator_center(submit_locator)
                await submit_locator.click()
            else:
                sx, sy = px, py
                await password_locator.press('Enter')
            # Superset finishes its SPA redirect a few seconds after form submission.
            await page.wait_for_timeout(4000)
            return {
                'type': 'submit_login_form',
                'mode': 'dom',
                'username_xy': [ux, uy],
                'password_xy': [px, py],
                'submit_xy': [sx, sy],
            }

        async def _settle_before_observe() -> None:
            await page.wait_for_timeout(PRE_ACTION_SETTLE_MS)

        async def _settle_after_action(action_type: str) -> None:
            wait_ms = POST_ACTION_SETTLE_MS
            if action_type == 'wait':
                wait_ms = 1200
            elif action_type == 'done':
                wait_ms = 0
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)

        async def _execute_one_action(
            action: dict[str, Any], action_type: str, dom_candidates: list[dict[str, Any]]
        ) -> tuple[dict[str, Any], str]:
            width = viewport_width
            height = viewport_height
            raw_x = int(action.get('x', width // 2) or width // 2)
            raw_y = int(action.get('y', height // 2) or height // 2)
            dom_id = _safe_int(action.get('dom_id', -1), -1)
            if dom_id >= 0 and action_type in {'click', 'hover', 'type'}:
                for candidate in dom_candidates:
                    if _safe_int(candidate.get('dom_id', -1), -1) == dom_id:
                        raw_x = _safe_int(candidate.get('x', raw_x), raw_x)
                        raw_y = _safe_int(candidate.get('y', raw_y), raw_y)
                        break
            x = _clamp(raw_x, 0, width - 1)
            y = _clamp(raw_y, 0, height - 1)
            executed_local = {'type': action_type}
            prev_local = previous_action

            if action_type == 'click':
                blocked, reason = _is_click_blocked(x, y)
                if blocked:
                    await asyncio.sleep(1)
                    executed_local = {
                        'type': 'wait',
                        'seconds': 1,
                        'fallback': 'blocked_click_zone',
                        'blocked_zone': reason,
                        'x': x,
                        'y': y,
                    }
                    prev_local = 'wait(1)'
                    return executed_local, prev_local
                await page.mouse.click(x, y)
                await page.wait_for_timeout(800)
                executed_local.update({'x': x, 'y': y})
                prev_local = f'click({x},{y})'
            elif action_type == 'hover':
                await page.mouse.move(x, y)
                await page.wait_for_timeout(900)
                executed_local.update({'x': x, 'y': y})
                prev_local = f'hover({x},{y})'
            elif action_type == 'type':
                text = str(action.get('text', ''))
                await page.mouse.click(x, y)
                await page.keyboard.press(SELECT_ALL_SHORTCUT)
                await page.keyboard.type(text)
                await page.wait_for_timeout(500)
                executed_local.update({'x': x, 'y': y, 'text': text})
                prev_local = f'type({x},{y},len={len(text)})'
            elif action_type == 'press':
                key = str(action.get('key', 'Enter'))
                await page.keyboard.press(key)
                executed_local.update({'key': key})
                prev_local = f'press({key})'
            elif action_type == 'scroll':
                delta_y = int(action.get('delta_y', 500) or 500)
                await page.mouse.wheel(0, delta_y)
                executed_local.update({'delta_y': delta_y})
                prev_local = f'scroll({delta_y})'
            elif action_type == 'wait':
                seconds = int(action.get('seconds', 2) or 2)
                seconds = _clamp(seconds, 1, 10)
                await asyncio.sleep(seconds)
                executed_local.update({'seconds': seconds})
                prev_local = f'wait({seconds})'
            elif action_type == 'done':
                result = str(action.get('result', '')).strip()
                executed_local.update({'result': result})
                prev_local = 'done'
            else:
                await asyncio.sleep(1)
                executed_local = {'type': 'wait', 'seconds': 1, 'fallback': 'unknown_action'}
                prev_local = 'wait(1)'

            return executed_local, prev_local

        def _is_login_route(url: str) -> bool:
            low = url.lower()
            return '/login' in low or 'login' in low

        def _vision_login_check(image_b64: str, current_url: str) -> dict[str, str]:
            login_check = client.chat.completions.create(
                **chat_kwargs,
                messages=[
                    {'role': 'system', 'content': LOGIN_STATE_PROMPT},
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': f'Current URL: {current_url}'},
                            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image_b64}'}},
                        ],
                    },
                ],
            )
            login_state_raw = login_check.choices[0].message.content or '{}'
            return _parse_login_state(login_state_raw)

        # Phase 1: login pre-processing
        if args.skip_login:
            login_success = True
            memory_context['flags']['login_done'] = True
            previous_action = 'login_skipped'
        else:
            if args.login_mode == 'dom':
                await page.goto(args.superset_login_url, wait_until='domcontentloaded', timeout=45000)
                await _stabilize_view()
                # The Superset SPA renders login inputs after DOMContentLoaded.
                await page.wait_for_timeout(1500)
                if _is_login_route(page.url):
                    dom_login_result = await _attempt_dom_login()
                    if dom_login_result is not None:
                        previous_action = 'direct_login_submit'
                await _settle_before_observe()
                verify_path = trace_dir / '_login_verify.png'
                await page.screenshot(path=str(verify_path), full_page=False)
                verify_b64 = base64.b64encode(verify_path.read_bytes()).decode('utf-8')
                verify_login_state = _vision_login_check(verify_b64, page.url)
                if not _is_login_route(page.url):
                    login_success = True
                    memory_context['flags']['login_done'] = True
                    previous_action = 'login_success'
            else:
            # Try to reuse preloaded authenticated session first.
                await _stabilize_view()
                global_step += 1
                precheck_path = trace_dir / f'step_{global_step:03d}.png'
                await _settle_before_observe()
                await page.screenshot(path=str(precheck_path), full_page=False)
                precheck_b64 = base64.b64encode(precheck_path.read_bytes()).decode('utf-8')
                pre_login_state = _vision_login_check(precheck_b64, page.url)
                if pre_login_state['login_state'] == 'logged_in' and not _is_login_route(page.url):
                    login_success = True
                    memory_context['flags']['login_done'] = True
                    previous_action = 'login_restored'
                    row = {
                        'step': global_step,
                        'phase': 'login_restore',
                        'url': page.url,
                        'reasoning': f"{pre_login_state['evidence']} | restored_session=true",
                        'model_action': {'type': 'done', 'result': 'login_restored'},
                        'executed': {'type': 'done', 'result': 'login_restored'},
                        'screenshot': precheck_path.name,
                    }
                    with steps_jsonl.open('a', encoding='utf-8') as fp:
                        fp.write(json.dumps(row, ensure_ascii=False) + '\n')
                    _log_step(
                        phase=row['phase'],
                        step=global_step,
                        url=page.url,
                        reasoning=row['reasoning'],
                        model_action=row['model_action'],
                        executed=row['executed'],
                    )
                    _update_memory(
                        phase='login_restore',
                        step=global_step,
                        reasoning=row['reasoning'],
                        self_check=None,
                        model_action=row['model_action'],
                        executed=row['executed'],
                        url=page.url,
                    )
                else:
                    await page.goto(args.superset_login_url, wait_until='domcontentloaded', timeout=45000)
                    await _stabilize_view()
                    dom_login_attempted = False
                    for _ in range(args.login_max_steps):
                        global_step += 1
                        screenshot_path = trace_dir / f'step_{global_step:03d}.png'
                        await _settle_before_observe()
                        await page.screenshot(path=str(screenshot_path), full_page=False)
                        image_b64 = base64.b64encode(screenshot_path.read_bytes()).decode('utf-8')

                        login_state = _vision_login_check(image_b64, page.url)
                        if login_state['login_state'] == 'logged_in':
                            logged_in_streak += 1
                        else:
                            logged_in_streak = 0
                        if logged_in_streak >= 2 and not _is_login_route(page.url):
                            login_success = True
                            row = {
                                'step': global_step,
                                'phase': 'login',
                                'url': page.url,
                                'reasoning': f"{login_state['evidence']} | logged_in_streak={logged_in_streak}",
                                'model_action': {'type': 'done', 'result': 'login_success'},
                                'executed': {'type': 'done', 'result': 'login_success'},
                                'screenshot': screenshot_path.name,
                            }
                            with steps_jsonl.open('a', encoding='utf-8') as fp:
                                fp.write(json.dumps(row, ensure_ascii=False) + '\n')
                            _log_step(
                                phase=row['phase'],
                                step=global_step,
                                url=page.url,
                                reasoning=row['reasoning'],
                                model_action=row['model_action'],
                                executed=row['executed'],
                            )
                            _update_memory(
                                phase='login',
                                step=global_step,
                                reasoning=row['reasoning'],
                                self_check=None,
                                model_action=row['model_action'],
                                executed=row['executed'],
                                url=page.url,
                            )
                            break

                        should_try_dom_login = (
                            login_state['login_state'] == 'needs_login'
                            and args.login_mode in {'dom', 'hybrid'}
                            and not dom_login_attempted
                        )
                        if should_try_dom_login:
                            dom_login_result = await _attempt_dom_login()
                            dom_login_attempted = dom_login_result is not None
                            if dom_login_result is not None:
                                previous_action = 'direct_login_submit'
                                row = {
                                    'step': global_step,
                                    'phase': 'login',
                                    'url': page.url,
                                    'reasoning': 'Submit the login form by filling username and password with DOM selectors.',
                                    'self_check': {
                                        'prerequisite_met': 'no',
                                        'action_targets_prerequisite': 'yes',
                                        'what_is_missing': 'Need to verify whether login completed successfully.',
                                    },
                                    'model_action': {'type': 'submit_login_form', 'mode': 'dom'},
                                    'executed': dom_login_result,
                                    'screenshot': screenshot_path.name,
                                }
                                with steps_jsonl.open('a', encoding='utf-8') as fp:
                                    fp.write(json.dumps(row, ensure_ascii=False) + '\n')
                                _log_step(
                                    phase=row['phase'],
                                    step=global_step,
                                    url=page.url,
                                    reasoning=row['reasoning'],
                                    model_action=row['model_action'],
                                    executed=row['executed'],
                                    self_check=row.get('self_check'),
                                )
                                _update_memory(
                                    phase='login',
                                    step=global_step,
                                    reasoning=row['reasoning'],
                                    self_check=row.get('self_check'),
                                    model_action=row['model_action'],
                                    executed=row['executed'],
                                    url=page.url,
                                )
                                continue

                        if args.login_mode == 'dom':
                            previous_action = 'dom_login_unavailable'
                            await page.wait_for_timeout(800)
                            continue

                        dom_candidates = await _collect_dom_candidates(page)
                        dom_candidates_text = _format_dom_candidates(dom_candidates, limit=25)
                        dashboard_action_candidates = await _collect_dashboard_action_candidates(page, limit=30)
                        dashboard_action_candidates_text = _format_dashboard_action_candidates(
                            dashboard_action_candidates, limit=25
                        )
                        login_prompt = (
                            'Login pre-processing phase. '
                            'If login form is visible, complete login first with provided credentials. '
                            'Use screenshot + DOM candidates and one action.\n\n'
                            f'DOM candidates: {dom_candidates_text}\n\n'
                            f'Dashboard action candidates: {dashboard_action_candidates_text}\n\n'
                            f'Credentials:\n- username: {args.username}\n- password: {args.password}\n\n'
                            f'Current URL: {page.url}\n'
                            f'Previous action: {previous_action}\n'
                            f'Login step: {global_step}/{args.login_max_steps}'
                        )
                        completion = client.chat.completions.create(
                            **chat_kwargs,
                            messages=[
                                {'role': 'system', 'content': LOGIN_ACTION_PROMPT},
                                {
                                    'role': 'user',
                                    'content': [
                                        {'type': 'text', 'text': login_prompt},
                                        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{image_b64}'}},
                                    ],
                                },
                            ],
                        )
                        raw = completion.choices[0].message.content or '{}'
                        parsed = _parse_action(raw)
                        reasoning = str(parsed.get('reasoning', '')).strip()
                        self_check = parsed.get('self_check', {}) if isinstance(parsed.get('self_check'), dict) else {}
                        action = parsed.get('action', {}) if isinstance(parsed.get('action'), dict) else {}
                        action = _attach_target_signature(action, dom_candidates)
                        action_type = str(action.get('type', '')).strip().lower()
                        executed, previous_action = await _execute_one_action(action, action_type, dom_candidates)
                        await _settle_after_action(action_type)

                        row = {
                            'step': global_step,
                            'phase': 'login',
                            'url': page.url,
                            'reasoning': f"{reasoning} | login_state={login_state['login_state']} streak={logged_in_streak}",
                            'self_check': self_check,
                            'model_action': action,
                            'executed': executed,
                            'screenshot': screenshot_path.name,
                            'dom_candidates': _candidate_snapshot(dom_candidates),
                            'dashboard_action_candidates': _candidate_snapshot(dashboard_action_candidates),
                        }
                        with steps_jsonl.open('a', encoding='utf-8') as fp:
                            fp.write(json.dumps(row, ensure_ascii=False) + '\n')
                        _log_step(
                            phase=row['phase'],
                            step=global_step,
                            url=page.url,
                            reasoning=row['reasoning'],
                            model_action=row['model_action'],
                            executed=row['executed'],
                            self_check=row.get('self_check'),
                        )
                        _update_memory(
                            phase='login',
                            step=global_step,
                            reasoning=row['reasoning'],
                            self_check=row.get('self_check'),
                            model_action=row['model_action'],
                            executed=row['executed'],
                            url=page.url,
                        )

        if not login_success:
            final_answer = 'Stopped because the login pre-processing stage could not confirm a successful login.'
        else:
            previous_action = 'login_success'
            if args.force_dashboard_url and not _is_on_dashboard_url(page.url, args.dashboard_id):
                await page.goto(dashboard_url, wait_until='domcontentloaded', timeout=45000)
                await _stabilize_view()
            if save_state_path:
                save_path = save_state_path
                save_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(save_path))
                saved_storage_state = str(save_path)

        # Phase 2: main scenario, only after successful login
        while login_success and scenario_steps < args.max_steps:
            if args.force_dashboard_url and not _is_on_dashboard_url(page.url, args.dashboard_id):
                await page.goto(dashboard_url, wait_until='domcontentloaded', timeout=45000)
                await _stabilize_view()
            scenario_steps += 1
            global_step += 1
            screenshot_path = trace_dir / f'step_{global_step:03d}.png'
            await _settle_before_observe()
            await page.screenshot(path=str(screenshot_path), full_page=False)
            image_b64 = base64.b64encode(screenshot_path.read_bytes()).decode('utf-8')
            dom_candidates = await _collect_dom_candidates(page)
            dashboard_action_candidates = await _collect_dashboard_action_candidates(page)
            dashboard_action_candidates_text = _format_dashboard_action_candidates(
                dashboard_action_candidates, limit=35
            )
            row_hints = await _collect_row_hints(page)
            row_hints_text = json.dumps(row_hints, ensure_ascii=False)
            tab_hints = await _collect_tab_hints(page)
            tab_hints_text = json.dumps(tab_hints, ensure_ascii=False)
            selection_hints = await _collect_selection_hints(page)
            selected_districts = selection_hints.get('selected_districts', [])
            selected_districts_text = ', '.join(selected_districts) if selected_districts else 'none'
            if 'north' in selected_districts:
                memory_context['flags']['north_filter_applied'] = True

            user_prompt = (
                f'# Task:\n{task_text}\n\n'
                f"{_memory_prompt_block()}\n"
                f"Reflections used: {reflections_used}/{args.max_reflections}\n"
                f'Dashboard action candidates: {dashboard_action_candidates_text}\n'
                f'Row coordinate hints (if visible): {row_hints_text}\n'
                f'Selected district hints (if visible): {selected_districts_text}\n'
                f'Tab coordinate hints (if visible): {tab_hints_text}\n'
                f'Credentials if login is required:\n'
                f'- username: {args.username}\n'
                f'- password: {args.password}\n\n'
                f'Current URL: {page.url}\n'
                f'Previous action: {previous_action}\n'
                f'Scenario step: {scenario_steps}/{args.max_steps}\n\n'
                'Decide exactly one next action using screenshot only.'
            )

            completion = client.chat.completions.create(
                **chat_kwargs,
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'text', 'text': user_prompt},
                            {
                                'type': 'image_url',
                                'image_url': {'url': f'data:image/png;base64,{image_b64}'},
                            },
                        ],
                    },
                ],
            )

            raw = completion.choices[0].message.content or '{}'
            parsed = _parse_action(raw)
            reasoning = str(parsed.get('reasoning', '')).strip()
            self_check = parsed.get('self_check', {}) if isinstance(parsed.get('self_check'), dict) else {}
            action = parsed.get('action', {}) if isinstance(parsed.get('action'), dict) else {}
            action = _force_coordinate_only_action(action)
            action = _apply_tab_hint_override(action, reasoning, tab_hints)
            action = _attach_target_signature(action, dom_candidates)
            action_type = str(action.get('type', '')).strip().lower()
            if (
                action_type == 'click'
                and 'north' in selected_districts
                and _action_hits_named_row(action, row_hints, 'north')
            ):
                reasoning = f'{reasoning} | north_already_selected=true'
                action = {
                    'type': 'wait',
                    'seconds': 1,
                    'result': 'avoid_toggle_selected_filter',
                }
                action_type = 'wait'
            final_answer_candidate = str(parsed.get('final_answer', '')).strip()
            if final_answer_candidate and action_type != 'done':
                reasoning = f'{reasoning} | auto_done_on_answer=true'
                action = {'type': 'done', 'result': final_answer_candidate}
                action_type = 'done'
            executed, previous_action = await _execute_one_action(action, action_type, [])
            await _settle_after_action(action_type)

            if action_type == 'done':
                done_result = str(action.get('result', '')).strip()
                done_text = str(action.get('text', '')).strip()
                final_answer = done_result or done_text or 'Scenario completed.'
                row = {
                    'step': global_step,
                    'phase': 'scenario',
                    'url': page.url,
                    'reasoning': reasoning,
                    'self_check': self_check,
                    'model_action': action,
                    'executed': executed,
                    'screenshot': screenshot_path.name,
                    'dom_candidates': _candidate_snapshot(dom_candidates),
                    'dashboard_action_candidates': _candidate_snapshot(dashboard_action_candidates),
                }
                with steps_jsonl.open('a', encoding='utf-8') as fp:
                    fp.write(json.dumps(row, ensure_ascii=False) + '\n')
                _log_step(
                    phase=row['phase'],
                    step=global_step,
                    url=page.url,
                    reasoning=row['reasoning'],
                    model_action=row['model_action'],
                    executed=row['executed'],
                    self_check=row.get('self_check'),
                )
                _update_memory(
                    phase='scenario',
                    step=global_step,
                    reasoning=row['reasoning'],
                    self_check=row.get('self_check'),
                    model_action=row['model_action'],
                    executed=row['executed'],
                    url=page.url,
                    parsed=parsed,
                )
                break

            await page.wait_for_timeout(800)

            row = {
                'step': global_step,
                'phase': 'scenario',
                'url': page.url,
                'reasoning': reasoning,
                'self_check': self_check,
                'model_action': action,
                'executed': executed,
                'screenshot': screenshot_path.name,
                'dom_candidates': _candidate_snapshot(dom_candidates),
                'dashboard_action_candidates': _candidate_snapshot(dashboard_action_candidates),
            }
            with steps_jsonl.open('a', encoding='utf-8') as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + '\n')
            _log_step(
                phase=row['phase'],
                step=global_step,
                url=page.url,
                reasoning=row['reasoning'],
                model_action=row['model_action'],
                executed=row['executed'],
                self_check=row.get('self_check'),
            )
            _update_memory(
                phase='scenario',
                step=global_step,
                reasoning=row['reasoning'],
                self_check=row.get('self_check'),
                model_action=row['model_action'],
                executed=row['executed'],
                url=page.url,
                parsed=parsed,
            )

        await context.close()
        await browser.close()

    if not final_answer:
        final_answer = 'Stopped after reaching the maximum number of steps. Check the trace screenshots and steps.jsonl.'

    meta = {
        'start_url': start_url,
        'dashboard_url': dashboard_url,
        'model': args.model,
        'login_max_steps': args.login_max_steps,
        'max_steps': args.max_steps,
        'skip_login': args.skip_login,
        'storage_state_loaded': loaded_storage_state,
        'storage_state_saved': saved_storage_state,
        'login_success': login_success,
        'scenario_steps_executed': scenario_steps,
        'task': task_text,
        'trace_dir': str(trace_dir),
        'steps_jsonl': str(steps_jsonl),
        'memory_context': str(memory_context_path),
        'reflections_used': reflections_used,
        'final_answer': final_answer,
    }
    (trace_dir / 'run_meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'Final answer: {final_answer}')
    print(f'Trace dir: {trace_dir}')
    print(f'Trace steps: {steps_jsonl}')
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print('Interrupted by user.')
        return 130
    except Exception as exc:
        print(f'Run failed: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
