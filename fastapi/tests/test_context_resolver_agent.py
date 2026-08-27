from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi_service import agent as agent_module
from fastapi_service.context_manager import (
    AERStore,
    build_aer_candidates,
    build_answer_evidence_payload,
    refresh_aer_store,
    validate_aer_record,
)


class _FakeAgent:
    def __init__(self, **kwargs):
        self.name = kwargs["name"]
        self.tools = kwargs.get("tools", [])
        self.instructions = kwargs.get("instructions")


class _FakeModelSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_function_tool(function):
    return function


class ContextResolverAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sdk_patch = patch.multiple(
            agent_module,
            Agent=_FakeAgent,
            ModelSettings=_FakeModelSettings,
            function_tool=_fake_function_tool,
        )
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)

    def test_context_resolver_is_a_separate_orchestrator_tool(self) -> None:
        runner = agent_module.AgentRunner()

        agents = runner._build_agents(model_name="gpt-5-mini")

        resolver = agents["context_resolver"]
        orchestrator = agents["orchestrator"]
        self.assertEqual(resolver.name, "Context Resolver Agent")
        self.assertIn("specialist within the Context Manager", resolver.instructions)
        self.assertIn(
            "run_context_resolver_agent",
            [tool.__name__ for tool in orchestrator.tools],
        )
        self.assertIn("FIRST call ContextResolver", orchestrator.instructions)

    async def test_resolver_call_receives_runtime_state_and_aer_candidates(self) -> None:
        runner = agent_module.AgentRunner()
        agents = runner._build_agents(model_name="gpt-5-mini")
        resolver_tool = next(
            tool
            for tool in agents["orchestrator"].tools
            if tool.__name__ == "run_context_resolver_agent"
        )
        runner._run_subagent = AsyncMock(return_value='{"answer":"resolved"}')
        active_context = agent_module.AgentContext(
            settings=None,
            conn=None,
            trace_logs=[{"superset_log_id": 42}],
            active_chart={"active_tab": {"name": "Product-Level"}},
            aer_candidates=[{"aer_id": "existing-chart:7:42"}],
        )
        token = agent_module._ACTIVE_CONTEXT_VAR.set(active_context)
        try:
            result = await resolver_tool(
                json.dumps(
                    {
                        "user_question": "What was its growth rate?",
                        "live_state": {"active_tab": {"name": "stale"}},
                    }
                )
            )
        finally:
            agent_module._ACTIVE_CONTEXT_VAR.reset(token)

        self.assertEqual(result, '{"answer":"resolved"}')
        called_agent, payload_json = runner._run_subagent.await_args.args
        payload = json.loads(payload_json)
        self.assertIs(called_agent, agents["context_resolver"])
        self.assertEqual(payload["live_state"], active_context.active_chart)
        self.assertEqual(payload["aer_candidates"], active_context.aer_candidates)
        self.assertEqual(payload["trace_logs"], active_context.trace_logs)

    def test_aer_candidates_bind_chart_context_to_live_state(self) -> None:
        live_state = {
            "dashboard_id": 13,
            "session_id": "session-1",
            "active_tab": {"id": "TAB-product", "name": "Product-Level"},
        }
        chart_context = [
            {
                "slice_id": 7,
                "name": "Sales by Product",
                "native_filters": [{"col": "department", "op": "IN", "val": ["Marketing"]}],
                "cross_filters": [{"col": "category", "op": "IN", "val": ["Mobile"]}],
                "interaction": {"action": "chart_click", "payload": {"value": "Mobile"}},
                "chart_log": {
                    "superset_log_id": 42,
                    "observed_at": "2026-07-30T00:00:00+00:00",
                    "form_data": {
                        "metric": "growth_rate",
                        "groupby": ["category"],
                    },
                    "queries": [
                        {
                            "columns": ["category"],
                            "filters": [
                                {
                                    "col": "quarter",
                                    "op": "IN",
                                    "val": ["Q4"],
                                }
                            ],
                        }
                    ],
                    "datasource": {"id": 3},
                },
            }
        ]

        candidates = build_aer_candidates(live_state, chart_context)

        self.assertEqual(candidates[0]["aer_id"], "existing-chart:7:42")
        self.assertEqual(candidates[0]["state_scope"]["active_tab"], live_state["active_tab"])
        self.assertEqual(
            candidates[0]["state_scope"]["cross_filters"][0]["val"],
            ["Mobile"],
        )
        self.assertEqual(candidates[0]["provenance"]["superset_log_id"], 42)
        self.assertEqual(
            candidates[0]["semantic_scope"]["measures"], ["growth_rate"]
        )
        self.assertEqual(
            candidates[0]["semantic_scope"]["dimensions"], ["category"]
        )
        self.assertEqual(candidates[0]["freshness"]["status"], "fresh")
        self.assertFalse(candidates[0]["result_available"])
        self.assertEqual(candidates[0]["validation_errors"], [])

    def test_chart_manager_result_refreshes_existing_aer(self) -> None:
        records = build_aer_candidates(
            {
                "dashboard_id": 13,
                "session_id": "session-1",
                "active_tab": {"name": "Category-Level"},
            },
            [
                {
                    "slice_id": 1114,
                    "name": "Category Scatter",
                    "cross_filters": [
                        {
                            "col": "department",
                            "op": "IN",
                            "val": ["Marketing"],
                        }
                    ],
                    "chart_log": {
                        "superset_log_id": 50,
                        "form_data": {
                            "metrics": ["AVG(qoq_growth_rate)"],
                            "groupby": ["category"],
                        },
                    },
                }
            ],
        )
        output = {
            "answer": json.dumps(
                {
                    "status": "ok",
                    "tools_executed": ["get_chart_data_by_id"],
                    "chart_id": 1114,
                    "query_details": {
                        "metric": "AVG(qoq_growth_rate)",
                        "group_by": ["category"],
                        "filters": {"department": ["Marketing"]},
                    },
                    "result": {
                        "category": "Mobile",
                        "qoq_growth_rate": 2.789474,
                    },
                }
            )
        }

        refreshed = refresh_aer_store(
            records,
            output,
            chart_manager_request={
                "candidate_aer_ids": [records[0]["aer_id"]]
            },
        )

        self.assertEqual(refreshed, [records[0]])
        self.assertTrue(records[0]["result_available"])
        self.assertEqual(records[0]["result"]["category"], "Mobile")
        self.assertIn(
            "get_chart_data_by_id",
            records[0]["provenance"]["tools_executed"],
        )
        self.assertEqual(records[0]["validation_errors"], [])

    def test_dataset_query_creates_on_demand_aer(self) -> None:
        records: list[dict] = []
        output = {
            "answer": json.dumps(
                {
                    "status": "ok",
                    "tools_executed": ["query_superset_dataset"],
                    "ids": {"dashboard_id": 13, "dataset_id": 49},
                    "query_details": {
                        "metric": "SUM(total_units_sold)",
                        "group_by": ["department", "category"],
                        "filters": {"quarter": ["Q3", "Q4"]},
                    },
                    "result": {
                        "department": "Marketing",
                        "category": "Mobile",
                        "qoq_growth_rate": 2.789474,
                    },
                }
            )
        }

        refreshed = refresh_aer_store(
            records,
            output,
            chart_manager_request={"candidate_aer_ids": []},
            live_state={
                "dashboard_id": 13,
                "session_id": "session-1",
                "active_tab": {"name": "Category-Level"},
            },
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(refreshed[0]["evidence_type"], "on-demand-query")
        self.assertTrue(refreshed[0]["result_available"])
        self.assertEqual(
            refreshed[0]["semantic_scope"]["dimensions"],
            ["department", "category"],
        )
        self.assertEqual(refreshed[0]["validation_errors"], [])

    def test_answer_composer_receives_only_valid_result_aers(self) -> None:
        valid = {
            "aer_id": "on-demand-query:1",
            "evidence_type": "on-demand-query",
            "source": {"dataset_id": 49},
            "semantic_scope": {"measures": ["sales"]},
            "result": {"value": 3},
            "result_available": True,
            "provenance": {"tools_executed": ["query_superset_dataset"]},
            "freshness": {"state_version": "s:1", "status": "fresh"},
            "validation_errors": [],
        }
        unavailable = {
            "aer_id": "existing-chart:1:1",
            "evidence_type": "existing-chart",
            "source": {"chart_id": 1},
            "semantic_scope": {"measures": ["sales"]},
            "result": None,
            "result_available": False,
            "provenance": {"source_chart_id": 1},
            "freshness": {"state_version": "s:1", "status": "fresh"},
            "validation_errors": [],
        }
        stale = {
            **valid,
            "aer_id": "on-demand-query:stale",
            "freshness": {"state_version": "s:0", "status": "stale"},
        }

        payload = json.loads(
            build_answer_evidence_payload(
                json.dumps({"user_question": "What is the value?"}),
                [valid, unavailable, stale],
            )
        )

        self.assertEqual(payload["aer_evidence"], [valid])

    def test_validation_rejects_structurally_present_but_empty_aer(self) -> None:
        errors = validate_aer_record(
            {
                "aer_id": "existing-chart:unknown:current",
                "evidence_type": "existing-chart",
                "source": {
                    "chart_id": None,
                    "chart_name": None,
                    "datasource": None,
                },
                "semantic_scope": {
                    "measures": [],
                    "dimensions": [],
                    "filters": [],
                    "hierarchy": {"tab": None, "level": None},
                    "time_scope": {},
                },
                "result": None,
                "result_available": False,
                "provenance": {
                    "superset_log_id": None,
                    "source_chart_id": None,
                },
                "freshness": {
                    "state_version": None,
                    "status": "unknown",
                },
            }
        )

        self.assertIn("source.empty", errors)
        self.assertIn("semantic_scope.empty", errors)
        self.assertIn("provenance.empty", errors)
        self.assertIn("freshness.state_version", errors)
        self.assertIn("freshness.status", errors)

    def test_repeated_dataset_result_updates_one_stable_aer(self) -> None:
        records: list[dict] = []
        output = {
            "answer": json.dumps(
                {
                    "tools_executed": ["query_superset_dataset"],
                    "ids": {"dataset_id": 49},
                    "query_details": {
                        "metric": "SUM(total_units_sold)",
                        "group_by": ["department"],
                    },
                    "result": [{"department": "Marketing", "value": 10}],
                }
            )
        }
        live_state = {
            "dashboard_id": 13,
            "session_id": "session-1",
            "active_tab": {"name": "Category-Level"},
        }

        first = refresh_aer_store(records, output, live_state=live_state)
        second = refresh_aer_store(records, output, live_state=live_state)

        self.assertEqual(len(records), 1)
        self.assertIs(first[0], second[0])
        self.assertEqual(first[0]["aer_id"], second[0]["aer_id"])

    def test_aer_store_isolates_sessions_and_reuses_same_state_results(self) -> None:
        store = AERStore()
        chart_context = [
            {
                "slice_id": 7,
                "name": "Sales",
                "chart_log": {
                    "superset_log_id": 42,
                    "form_data": {"metric": "sales"},
                },
            }
        ]
        session_one = {
            "dashboard_id": 13,
            "session_id": "session-1",
            "active_tab": {"name": "Store-Level"},
        }
        session_two = {
            "dashboard_id": 13,
            "session_id": "session-2",
            "active_tab": {"name": "Store-Level"},
        }
        records = store.refresh_snapshot(session_one, chart_context)
        output = {
            "answer": json.dumps(
                {
                    "tools_executed": ["query_superset_dataset"],
                    "query_details": {
                        "metric": "SUM(sales)",
                        "group_by": ["store_name"],
                    },
                    "result": [{"store_name": "A", "sales": 10}],
                }
            )
        }
        on_demand = refresh_aer_store(
            records,
            output,
            chart_manager_request={
                "candidate_aer_ids": [records[0]["aer_id"]]
            },
            live_state=session_one,
        )[0]

        same_state = store.refresh_snapshot(session_one, chart_context)
        other_session = store.refresh_snapshot(session_two, chart_context)

        self.assertIn(on_demand["aer_id"], [item["aer_id"] for item in same_state])
        self.assertNotIn(
            on_demand["aer_id"],
            [item["aer_id"] for item in other_session],
        )

    def test_aer_store_drops_on_demand_result_after_state_version_changes(self) -> None:
        store = AERStore()
        live_state = {
            "dashboard_id": 13,
            "session_id": "session-1",
            "active_tab": {"name": "Store-Level"},
        }
        first_context = [
            {
                "slice_id": 7,
                "name": "Sales",
                "chart_log": {
                    "superset_log_id": 42,
                    "form_data": {"metric": "sales"},
                },
            }
        ]
        records = store.refresh_snapshot(live_state, first_context)
        on_demand = refresh_aer_store(
            records,
            {
                "answer": json.dumps(
                    {
                        "tools_executed": ["query_superset_dataset"],
                        "query_details": {"metric": "SUM(sales)"},
                        "result": [{"sales": 10}],
                    }
                )
            },
            chart_manager_request={
                "candidate_aer_ids": [records[0]["aer_id"]]
            },
            live_state=live_state,
        )[0]
        changed_context = [
            {
                **first_context[0],
                "chart_log": {
                    **first_context[0]["chart_log"],
                    "superset_log_id": 43,
                },
            }
        ]

        changed_state = store.refresh_snapshot(live_state, changed_context)

        self.assertNotIn(
            on_demand["aer_id"],
            [item["aer_id"] for item in changed_state],
        )

    async def test_chart_manager_tool_refreshes_runtime_aer_store(self) -> None:
        runner = agent_module.AgentRunner()
        agents = runner._build_agents(model_name="gpt-5-mini")
        chart_tool = next(
            tool
            for tool in agents["orchestrator"].tools
            if tool.__name__ == "run_chart_manager_agent"
        )
        candidate = {
            "aer_id": "existing-chart:7:42",
            "evidence_type": "existing-chart",
            "source": {"chart_id": 7},
            "semantic_scope": {"measures": ["sales"]},
            "state_scope": {},
            "query_context": {},
            "result": None,
            "result_available": False,
            "provenance": {"source_chart_id": 7},
            "freshness": {"state_version": "s:42", "status": "fresh"},
            "validation_errors": [],
        }
        runner._run_subagent = AsyncMock(
            return_value=json.dumps(
                {
                    "answer": json.dumps(
                        {
                            "tools_executed": ["get_chart_data_by_id"],
                            "chart_id": 7,
                            "result": {"sales": 10},
                        }
                    )
                }
            )
        )
        active_context = agent_module.AgentContext(
            settings=None,
            conn=None,
            active_chart={"session_id": "s"},
            aer_candidates=[candidate],
        )
        token = agent_module._ACTIVE_CONTEXT_VAR.set(active_context)
        try:
            result = await chart_tool(
                json.dumps({"candidate_aer_ids": [candidate["aer_id"]]})
            )
        finally:
            agent_module._ACTIVE_CONTEXT_VAR.reset(token)

        self.assertTrue(candidate["result_available"])
        self.assertEqual(candidate["result"], {"sales": 10})
        inner = json.loads(json.loads(result)["answer"])
        self.assertEqual(inner["aer_records"][0]["aer_id"], candidate["aer_id"])

    async def test_answer_composer_tool_receives_runtime_aer_evidence(self) -> None:
        runner = agent_module.AgentRunner()
        agents = runner._build_agents(model_name="gpt-5-mini")
        answer_tool = next(
            tool
            for tool in agents["orchestrator"].tools
            if tool.__name__ == "run_answer_composer_agent"
        )
        valid = {
            "aer_id": "on-demand-query:1",
            "evidence_type": "on-demand-query",
            "source": {"dataset_id": 49},
            "semantic_scope": {"measures": ["sales"]},
            "result": {"value": 10},
            "result_available": True,
            "provenance": {"tools_executed": ["query_superset_dataset"]},
            "freshness": {"state_version": "s:1", "status": "fresh"},
            "validation_errors": [],
        }
        runner._run_subagent = AsyncMock(return_value='{"answer":"10"}')
        active_context = agent_module.AgentContext(
            settings=None,
            conn=None,
            aer_candidates=[valid],
        )
        token = agent_module._ACTIVE_CONTEXT_VAR.set(active_context)
        try:
            await answer_tool(json.dumps({"user_question": "What is the value?"}))
        finally:
            agent_module._ACTIVE_CONTEXT_VAR.reset(token)

        called_agent, payload_json = runner._run_subagent.await_args.args
        payload = json.loads(payload_json)
        self.assertIs(called_agent, agents["answer_composer"])
        self.assertEqual(payload["aer_evidence"], [valid])


if __name__ == "__main__":
    unittest.main()
