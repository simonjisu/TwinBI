import sys
import tempfile
import unittest
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
if "" in sys.path:
    sys.path.remove("")
if str(project_root) in sys.path:
    sys.path.remove(str(project_root))
sys.path.append(str(project_root / "fastapi"))

import duckdb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fastapi_service.config import Settings  # noqa: E402
from fastapi_service.main import create_app  # noqa: E402


class FastAPITestCase(unittest.TestCase):
    def _build_settings(self, duckdb_path: str) -> Settings:
        return Settings(
            duckdb_path=duckdb_path,
            superset_meta_db_uri=None,
            superset_poll_interval_sec=2.0,
            superset_batch_size=200,
            superset_public_url=None,
            superset_internal_url=None,
            superset_username=None,
            superset_password=None,
            superset_guest_aud=None,
            superset_log_dashboard_id=None,
            superset_log_user_id=None,
            superset_log_username=None,
            cube_rest_url=None,
            cube_api_token=None,
            cube_sql_host=None,
            cube_sql_port=None,
            cube_conf_path=None,
            cors_origins=[],
        )

    def test_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = self._build_settings(str(Path(tmpdir) / "events.duckdb"))
            app = create_app(settings)
            with TestClient(app) as client:
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                status = client.get("/poller/status")
                self.assertEqual(status.status_code, 200)
                self.assertFalse(status.json()["enabled"])

                reset = client.post("/poller/reset")
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(reset.json()["status"], "ok")

                trigger = client.post("/poller/trigger")
                self.assertEqual(trigger.status_code, 200)
                self.assertIn(trigger.json()["status"], {"ok", "disabled"})

                sync = client.post("/poller/sync")
                self.assertEqual(sync.status_code, 200)
                self.assertIn(sync.json()["status"], {"ok", "disabled"})
                inserted = sync.json().get("inserted", {})
                self.assertIn("logs", inserted)
                self.assertIn("chat", inserted)

                latest = client.get("/superset/logs/latest")
                self.assertEqual(latest.status_code, 200)
                self.assertIsInstance(latest.json(), list)

                latest_sql = client.get("/superset/logs/latest_sql")
                self.assertEqual(latest_sql.status_code, 404)

                schema = client.get("/superset/datasets/12/schema")
                self.assertEqual(schema.status_code, 400)

                cube_meta = client.get("/cube/meta")
                self.assertEqual(cube_meta.status_code, 400)

                cube_schema = client.get("/cube/schema")
                self.assertEqual(cube_schema.status_code, 400)

                tab_map = client.get("/superset/dashboards/12/tab-map")
                self.assertEqual(tab_map.status_code, 400)

                with client.stream(
                    "GET",
                    "/superset/logs/stream",
                    headers={"Last-Event-ID": "0"},
                ) as stream:
                    self.assertEqual(stream.status_code, 200)
                    content_type = stream.headers.get("content-type", "")
                    self.assertTrue(content_type.startswith("text/event-stream"))

                with client.stream(
                    "GET",
                    "/events/stream",
                    headers={"Last-Event-ID": "0"},
                ) as stream:
                    self.assertEqual(stream.status_code, 200)
                    content_type = stream.headers.get("content-type", "")
                    self.assertTrue(content_type.startswith("text/event-stream"))

                clear = client.post("/poller/clear")
                self.assertEqual(clear.status_code, 200)
                self.assertEqual(clear.json()["status"], "ok")

                lookup = client.get("/superset/users/lookup", params={"username": "test"})
                self.assertEqual(lookup.status_code, 400)


    def test_events_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "events.duckdb"
            settings = self._build_settings(str(db_path))
            app = create_app(settings)
            with TestClient(app) as client:
                payload = {
                    "session_id": "s_1",
                    "user_id": "u_1",
                    "event_type": "chart_click",
                    "payload": {"chart_id": "sales_by_category"},
                }
                response = client.post("/events", json=payload)
                self.assertEqual(response.status_code, 200)
                client.app.state.writer.flush_blocking()
                client.app.state.writer.flush_blocking()

            conn = duckdb.connect(str(db_path))
            rows = conn.execute("SELECT COUNT(*) FROM ui_events").fetchone()
            conn.close()
            self.assertEqual(rows[0], 1)

    def test_chat_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "events.duckdb"
            settings = self._build_settings(str(db_path))
            app = create_app(settings)
            with TestClient(app) as client:
                payload = {
                    "session_id": "s_2",
                    "user_id": "u_2",
                    "message": "hello",
                }
                response = client.post("/chat", json=payload)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["session_id"], "s_2")
                self.assertIn("request_id", body)
                client.app.state.writer.flush_blocking()

            conn = duckdb.connect(str(db_path))
            rows = conn.execute("SELECT COUNT(*) FROM streamlit_chat_logs").fetchone()
            conn.close()
            self.assertEqual(rows[0], 1)

    def test_superset_dashboard_charts_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = self._build_settings(str(Path(tmpdir) / "events.duckdb"))
            app = create_app(settings)
            with TestClient(app) as client:
                response = client.get("/superset/dashboards/12/charts")
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
