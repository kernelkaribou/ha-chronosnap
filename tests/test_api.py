"""Exercise the API client against a real local HTTP server."""

import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.helpers import api


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.requests = []
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.respond)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.client = api.ChronoSnapClient(
            str(self.server.make_url("")).rstrip("/"), "test-key", self.session
        )

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def respond(self, request):
        self.requests.append(
            {
                "method": request.method,
                "path": str(request.rel_url),
                "key": request.headers.get("X-API-Key"),
                "body": await request.json() if request.can_read_body else None,
            }
        )
        return self.responses.pop(0)

    async def test_complete_job_sends_status_and_end_time(self):
        self.responses = [web.json_response({"id": 7, "status": "completed"})]
        await self.client.complete_job(7, "2026-01-01T00:00:00+00:00")
        request = self.requests[0]
        self.assertEqual(request["method"], "PATCH")
        self.assertEqual(request["path"], "/api/jobs/7")
        self.assertEqual(request["key"], "test-key")
        self.assertEqual(request["body"]["status"], "completed")
        self.assertIn("end_datetime", request["body"])

    async def test_delete_handles_empty_response(self):
        self.responses = [web.Response(status=204)]
        await self.client.delete_job(7)
        self.assertEqual(self.requests[0]["method"], "DELETE")

    async def test_auth_notfound_and_transient_errors_are_distinct(self):
        for status, error in (
            (401, api.ChronoSnapAuthError),
            (403, api.ChronoSnapAuthError),
            (404, api.ChronoSnapNotFoundError),
            (429, api.ChronoSnapAPIError),
            (503, api.ChronoSnapAPIError),
        ):
            with self.subTest(status=status):
                self.responses = [
                    web.Response(status=status, text="sensitive-server-detail")
                ]
                with self.assertRaises(error) as caught:
                    await self.client.get_job(1)
                self.assertNotIn("sensitive-server-detail", str(caught.exception))

    async def test_unexpected_object_shape_is_not_empty_success(self):
        self.responses = [web.json_response([])]
        with self.assertRaises(api.ChronoSnapError):
            await self.client.get_storage_stats()

    async def test_unexpected_list_shape_is_not_empty_success(self):
        self.responses = [web.json_response({"jobs": []})]
        with self.assertRaises(api.ChronoSnapError):
            await self.client.get_jobs()

    async def test_invalid_json_and_html_are_protocol_errors(self):
        for response in (
            web.Response(text="not-json", content_type="application/json"),
            web.Response(text="<html>login</html>", content_type="text/html"),
        ):
            self.responses = [response]
            with self.assertRaises(api.ChronoSnapError) as caught:
                await self.client.get_job(1)
            self.assertNotIsInstance(caught.exception, api.ChronoSnapConnectionError)

    async def test_capture_count_must_be_nonnegative_integer(self):
        for count in (None, -1, "12", True):
            self.responses = [web.json_response({"count": count})]
            with self.assertRaises(api.ChronoSnapError):
                await self.client.get_capture_count(1)

    async def test_job_listing_paginates_beyond_server_default_limit(self):
        self.responses = [
            web.json_response([{"id": n} for n in range(1000)]),
            web.json_response([{"id": 1000}]),
        ]
        result = await self.client.get_jobs(status="active")
        self.assertEqual(len(result), 1001)
        self.assertIn("offset=1000", self.requests[1]["path"])
        self.assertIn("status=active", self.requests[1]["path"])

    async def test_shared_session_is_not_closed_by_client(self):
        await self.client.close()
        self.assertFalse(self.session.closed)
