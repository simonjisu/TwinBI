import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "streamlit-app"))

import fastapi_client  # noqa: E402


class FastAPIClientTestCase(unittest.TestCase):
    def test_join_url(self) -> None:
        self.assertEqual(
            fastapi_client._join_url("http://api:8000/", "/chat"),
            "http://api:8000/chat",
        )
        self.assertEqual(
            fastapi_client._join_url("http://api:8000", "/events"),
            "http://api:8000/events",
        )

    @mock.patch("fastapi_client.requests.post")
    def test_post_chat(self, mock_post: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.json.return_value = {"answer": "ok"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = fastapi_client.post_chat(
            base_url="http://api:8000",
            session_id="s1",
            user_id="u1",
            message="hello",
        )

        self.assertEqual(result["answer"], "ok")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "http://api:8000/chat")
        self.assertEqual(kwargs["json"]["session_id"], "s1")
        self.assertEqual(kwargs["json"]["message"], "hello")

    @mock.patch("fastapi_client.requests.post")
    def test_post_event(self, mock_post: mock.Mock) -> None:
        mock_response = mock.Mock()
        mock_response.json.return_value = {"status": "ok"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = fastapi_client.post_event(
            base_url="http://api:8000",
            session_id="s2",
            user_id="u2",
            event_type="chat_message",
            payload={"message": "hello"},
        )

        self.assertEqual(result["status"], "ok")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "http://api:8000/events")
        self.assertEqual(kwargs["json"]["event_type"], "chat_message")


if __name__ == "__main__":
    unittest.main()
