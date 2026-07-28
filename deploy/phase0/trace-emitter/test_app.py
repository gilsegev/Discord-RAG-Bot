import asyncio
import unittest
from unittest.mock import Mock, patch

import app


class FakeRequest:
    async def json(self):
        return {"resourceSpans": []}


class TraceEmitterAuthTests(unittest.TestCase):
    def test_adds_bearer_token_when_configured(self):
        response = Mock(status_code=200, text="")
        with (
            patch.object(app, "PHOENIX_AUTH_TOKEN", "test-admin-secret"),
            patch.object(app.requests, "post", return_value=response) as post,
        ):
            result = asyncio.run(app.emit_traces(FakeRequest()))

        self.assertEqual(result["status"], "forwarded")
        self.assertEqual(
            post.call_args.kwargs["headers"]["authorization"],
            "Bearer test-admin-secret",
        )

    def test_omits_authorization_when_not_configured(self):
        response = Mock(status_code=200, text="")
        with (
            patch.object(app, "PHOENIX_AUTH_TOKEN", ""),
            patch.object(app.requests, "post", return_value=response) as post,
        ):
            asyncio.run(app.emit_traces(FakeRequest()))

        self.assertNotIn("authorization", post.call_args.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
