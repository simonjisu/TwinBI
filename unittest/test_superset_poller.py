import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "fastapi"))

from fastapi_service.superset import SupersetPoller  # noqa: E402


class SupersetPollerTestCase(unittest.TestCase):
    def test_build_query_base(self) -> None:
        poller = SupersetPoller(
            meta_db_uri="postgresql://user:pass@host/db",
            poll_interval_sec=2.0,
            batch_size=100,
            dashboard_id=None,
            user_id=None,
            username=None,
            writer=None,  # type: ignore[arg-type]
            conn=None,  # type: ignore[arg-type]
        )

        sql, params = poller._build_query(
            last_id=10, batch_size=100, dashboard_id=None, user_id=None
        )

        self.assertIn("WHERE id > %s", sql)
        self.assertNotIn("dashboard_id =", sql)
        self.assertNotIn("user_id =", sql)
        self.assertEqual(params, (10, 100))

    def test_build_query_with_filters(self) -> None:
        poller = SupersetPoller(
            meta_db_uri="postgresql+psycopg2://user:pass@host/db",
            poll_interval_sec=2.0,
            batch_size=100,
            dashboard_id=12,
            user_id=7,
            username=None,
            writer=None,  # type: ignore[arg-type]
            conn=None,  # type: ignore[arg-type]
        )

        sql, params = poller._build_query(
            last_id=5, batch_size=50, dashboard_id=12, user_id=7
        )

        self.assertIn("dashboard_id = %s", sql)
        self.assertIn("user_id = %s", sql)
        self.assertEqual(params, (5, 12, 7, 50))

    def test_normalize_uri(self) -> None:
        poller = SupersetPoller(
            meta_db_uri="postgresql+psycopg2://user:pass@host/db",
            poll_interval_sec=2.0,
            batch_size=100,
            dashboard_id=None,
            user_id=None,
            username=None,
            writer=None,  # type: ignore[arg-type]
            conn=None,  # type: ignore[arg-type]
        )
        self.assertEqual(
            poller._normalize_db_uri("postgresql+psycopg2://user:pass@host/db"),
            "postgresql://user:pass@host/db",
        )


if __name__ == "__main__":
    unittest.main()
