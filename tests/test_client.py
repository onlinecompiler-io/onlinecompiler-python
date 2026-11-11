"""The client against a local stand-in for the API, so these tests need no network or key.
Run from packages/python with: PYTHONPATH=src python -m unittest discover tests"""

import base64
import datetime
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import onlinecompiler as oc


class FakeApi(BaseHTTPRequestHandler):
    """Answers with the next canned (status, body) and records each request."""

    replies: list = []
    requests: list = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        FakeApi.requests.append({
            "method": self.command, "path": self.path, "headers": dict(self.headers),
            "body": json.loads(raw) if raw and self.headers.get("Content-Type", "").startswith("application/json")
            else None,
            "raw": raw,
        })
        status, body = FakeApi.replies.pop(0)
        short = isinstance(body, tuple)  # ("truncated", data): promises more bytes than it sends, then closes
        if short:
            body = body[1]
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data) + (100 if short else 0)))
        self.end_headers()
        self.wfile.write(data)
        if short:
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

    def log_message(self, *args):
        pass


class StubApiCase(unittest.TestCase):
    """A client pointed at a FakeApi server of its own class."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeApi.replies, FakeApi.requests = [], []
        self.client = oc.OnlineCompiler("key-123", base_url=self.base_url)

    def reply(self, status, body):
        FakeApi.replies.append((status, body))

    @property
    def sent(self):
        return FakeApi.requests[-1]


class ClientTests(StubApiCase):
    def test_run_posts_the_program_with_the_bare_key_and_returns_the_result(self):
        self.reply(200, {"output": "hi\n", "error": "", "exit_code": 0, "time": "0.02", "memory": "8192"})
        result = self.client.run("python-3.14", "print(input())", "hi")
        self.assertEqual(result["output"], "hi\n")
        self.assertEqual((self.sent["method"], self.sent["path"]), ("POST", "/api/run-code-sync/"))
        self.assertEqual(self.sent["headers"]["Authorization"], "key-123")
        self.assertEqual(self.sent["headers"]["User-Agent"], f"onlinecompiler-python/{oc.__version__}")
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "print(input())", "input": "hi"})

    def test_options_are_sent_only_when_given(self):
        self.reply(200, {"output": ""})
        files = [{"name": "data.csv", "content": "a,b\n1,2\n"}]
        self.client.run("python-3.14", "x", time_limit=5, files=files)
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "x", "input": "", "time_limit": 5,
                                             "files": files})

    def test_run_tests_sends_the_cases_and_chosen_options(self):
        self.reply(200, {"verdict": "accepted", "score": 100, "passed": 1, "total": 1, "time_limit": 2,
                         "compile_error": None, "tests": [{"verdict": "accepted", "hidden": False}]})
        tests = [{"input": "1", "expected_output": "2"}, {"input": "5", "expected_output": "6", "hidden": True}]
        result = self.client.run_tests("python-3.14", "print(int(input())+1)", tests, checker="tokens",
                                       scoring="percent")
        self.assertEqual(result["verdict"], "accepted")
        self.assertEqual(self.sent["path"], "/api/run-tests/")
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "print(int(input())+1)",
                                             "tests": tests, "checker": "tokens", "scoring": "percent"})

    def test_submit_queues_with_extra_params(self):
        self.reply(202, {"status": "queued", "id": 42})
        self.assertEqual(self.client.submit("gcc-15", "int main(){}", extra_params={"user": 7})["id"], 42)
        self.assertEqual(self.sent["path"], "/api/run-code/")
        self.assertEqual(self.sent["body"]["extra_params"], {"user": 7})

    def test_public_endpoints_send_no_key(self):
        self.reply(200, {"compilers": [{"id": "python-3.14", "name": "Python"}]})
        self.reply(200, {"memory_mb": 512})
        self.assertEqual(self.client.compilers(), [{"id": "python-3.14", "name": "Python"}])
        self.assertNotIn("Authorization", FakeApi.requests[0]["headers"])
        self.assertEqual(self.client.limits()["memory_mb"], 512)
        self.assertNotIn("Authorization", self.sent["headers"])
        self.assertEqual([r["path"] for r in FakeApi.requests], ["/api/compilers/", "/api/limits/"])

    def test_usage_sends_the_key(self):
        self.reply(200, {"submissions": 3, "remaining": 99997})
        self.assertEqual(self.client.usage()["remaining"], 99997)
        self.assertEqual((self.sent["method"], self.sent["path"]), ("GET", "/api/usage/"))
        self.assertEqual(self.sent["headers"]["Authorization"], "key-123")

    def test_errors_carry_the_api_message_and_status(self):
        cases = [(429, oc.RateLimitError), (404, oc.NotFoundError), (403, oc.AuthenticationError),
                 (409, oc.OnlineCompilerError), (400, oc.OnlineCompilerError), (503, oc.OnlineCompilerError)]
        for status, error in cases:
            with self.subTest(status=status):
                self.reply(status, {"error": f"refused with {status}"})
                with self.assertRaises(error) as caught:
                    self.client.run("python-3.14", "x")
                self.assertEqual((caught.exception.status, caught.exception.message),
                                 (status, f"refused with {status}"))
                self.assertEqual(caught.exception.body, {"error": f"refused with {status}"})

    def test_an_error_page_that_is_not_json_keeps_its_text(self):
        self.reply(502, b"Bad Gateway")
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.run("python-3.14", "x")
        self.assertEqual((caught.exception.status, caught.exception.message), (502, "Bad Gateway"))

    def test_a_missing_key_fails_before_any_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            client = oc.OnlineCompiler(base_url=self.base_url)
        with self.assertRaises(oc.AuthenticationError) as caught:
            client.run("python-3.14", "x")
        self.assertIn("ONLINECOMPILER_API_KEY", caught.exception.message)
        self.assertEqual(FakeApi.requests, [])

    def test_the_key_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ONLINECOMPILER_API_KEY": "env-key"}):
            client = oc.OnlineCompiler(base_url=self.base_url)
        self.reply(200, {"output": ""})
        client.run("python-3.14", "x")
        self.assertEqual(self.sent["headers"]["Authorization"], "env-key")

    def test_an_unreachable_api_raises_with_status_0(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]  # free once closed, so nothing listens there
        client = oc.OnlineCompiler("k", base_url=f"http://127.0.0.1:{port}", timeout=2)
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            client.run("python-3.14", "x")
        self.assertEqual(caught.exception.status, 0)

    def test_an_unknown_key_is_an_authentication_error_and_a_missing_id_is_not(self):
        self.reply(404, {"error": "Invalid or inactive API key"})
        with self.assertRaises(oc.AuthenticationError):
            self.client.run("python-3.14", "x")
        self.reply(404, {"error": "Submission not found"})
        with self.assertRaises(oc.NotFoundError) as caught:
            self.client.submission("sub_8f2Kq7LmT3xR9vWb1cYd4hNe")
        self.assertNotIsInstance(caught.exception, oc.AuthenticationError)
        self.assertEqual((caught.exception.status, caught.exception.message), (404, "Submission not found"))


SUB = "sub_8f2Kq7LmT3xR9vWb1cYd4hNe"
PROB = "prob_7Hq2Zp4Wn8Ks1Ab6Ct3Dv5Ex"
TOKEN = "pst_" + "T0k3n" * 6 + "Zz"
TESTS = [{"input": "1 2", "expected_output": "3"}, {"input": "5 7", "expected_output": "12", "hidden": True}]
RESULT = {"verdict": "accepted", "score": 100, "passed": 2, "total": 2, "time_limit": 2, "compile_error": None,
          "tests": [{"verdict": "accepted", "hidden": False}, {"verdict": "accepted", "hidden": True}],
          "summary": "1 of 1 sample test passed. 1 of 1 hidden test passed."}


def finished(status="finished", **fields):
    return {"submission": SUB, "status": status, "created": "2026-11-05T09:00:00Z", "finished": None,
            "external_ref": None, "metadata": None, "problem": None, "result": None, "receipt": None, "error": None,
            "checker_log": None, "hash_salt": "salt", **fields}


class JudgingTests(StubApiCase):
    """Async run-tests, submissions, stored problems, sessions and the receipt keys."""

    def test_async_run_tests_sends_the_options_and_the_idempotency_key(self):
        self.reply(202, {"submission": SUB, "status": "queued"})
        queued = self.client.run_tests("python-3.14", "c", TESTS, async_=True, callback_url="https://example.com/hook",
                                       external_ref="attempt-1", metadata={"candidate": 7},
                                       idempotency_key="attempt-1")
        self.assertEqual(queued, {"submission": SUB, "status": "queued"})
        self.assertEqual((self.sent["method"], self.sent["path"]), ("POST", "/api/run-tests/"))
        self.assertEqual(self.sent["headers"]["Idempotency-Key"], "attempt-1")
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "c", "tests": TESTS, "async": True,
                                             "callback_url": "https://example.com/hook", "external_ref": "attempt-1",
                                             "metadata": {"candidate": 7}})

    def test_a_sync_run_tests_sends_no_idempotency_header_unless_given(self):
        self.reply(200, {**RESULT, "submission": SUB, "receipt": None})
        result = self.client.run_tests("python-3.14", "c", TESTS)
        self.assertEqual((result["verdict"], result["submission"]), ("accepted", SUB))
        self.assertNotIn("Idempotency-Key", self.sent["headers"])
        self.assertNotIn("async", self.sent["body"])

    def test_callback_url_needs_async(self):
        with self.assertRaises(ValueError):
            self.client.run_tests("python-3.14", "c", TESTS, callback_url="https://example.com/hook")
        with self.assertRaises(ValueError):
            self.client.judge("sum", "python-3.14", "c", callback_url="https://example.com/hook", async_=False)
        self.assertEqual(FakeApi.requests, [])

    def test_a_replayed_sync_request_still_running_is_waited_for(self):
        # An Idempotency-Key retry while the original sync request is still running answers 202.
        self.reply(202, {"submission": SUB, "status": "running"})
        self.reply(200, finished("running"))
        self.reply(200, finished(result=RESULT, receipt={"receipt": "rcpt_x"}))
        with mock.patch("onlinecompiler._client.time.sleep") as sleep:
            result = self.client.run_tests("python-3.14", "c", TESTS, idempotency_key="k1")
        self.assertEqual(sleep.call_count, 1)  # once after the lookup that found it still running
        self.assertEqual(result, {**RESULT, "submission": SUB, "receipt": {"receipt": "rcpt_x"}})
        self.assertEqual([r["path"] for r in FakeApi.requests[1:]], [f"/api/submissions/{SUB}/"] * 2)

    def test_a_replayed_sync_request_that_failed_raises_with_its_error(self):
        self.reply(202, {"submission": SUB, "status": "running"})
        self.reply(200, finished("failed", error="Internal error: judging failed. Send the submission again."))
        with mock.patch("onlinecompiler._client.time.sleep"), self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.judge(PROB, "python-3.14", "c", idempotency_key="k1")
        self.assertIn("Send the submission again", caught.exception.message)
        self.assertEqual(caught.exception.body["status"], "failed")

    def test_submission_and_delete_submission(self):
        self.reply(200, finished(result=RESULT))
        self.assertEqual(self.client.submission(SUB)["result"]["verdict"], "accepted")
        self.assertEqual((self.sent["method"], self.sent["path"]), ("GET", f"/api/submissions/{SUB}/"))
        self.reply(200, {"submission": SUB, "deleted": True})
        self.assertEqual(self.client.delete_submission(SUB), {"submission": SUB, "deleted": True})
        self.assertEqual((self.sent["method"], self.sent["path"]), ("DELETE", f"/api/submissions/{SUB}/"))
        self.assertEqual(self.sent["headers"]["Authorization"], "key-123")

    def test_submissions_sends_each_filter_given_as_the_query(self):
        page = {"submissions": [], "next_cursor": None}
        self.reply(200, page)
        self.assertEqual(self.client.submissions(), page)
        self.assertEqual((self.sent["method"], self.sent["path"]), ("GET", "/api/submissions/"))
        self.reply(200, page)
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        self.client.submissions(ids=[SUB, "sub_" + "b" * 24], external_ref="cand 1", problem="sum", status="finished",
                                verdict="accepted", since=datetime.date(2026, 11, 1),
                                until=datetime.datetime(2026, 11, 3, 9, 0, tzinfo=ist), limit=50, cursor="abc")
        self.assertEqual(self.sent["path"], (
            f"/api/submissions/?ids={SUB}%2Csub_{'b' * 24}&external_ref=cand+1&problem=sum&status=finished"
            "&verdict=accepted&since=2026-11-01&until=2026-11-03T09%3A00%3A00%2B05%3A30&limit=50&cursor=abc"))
        self.reply(200, page)
        self.client.submissions(ids=SUB, since="2026-11-01T00:00:00Z")
        self.assertEqual(self.sent["path"], f"/api/submissions/?ids={SUB}&since=2026-11-01T00%3A00%3A00Z")

    def test_an_empty_id_list_is_an_empty_page_without_a_request(self):
        self.assertEqual(self.client.submissions(ids=[]), {"submissions": [], "next_cursor": None})
        self.assertEqual(list(self.client.iter_submissions(ids=iter([]))), [])
        self.assertEqual(FakeApi.requests, [])

    def test_a_refused_list_keeps_the_apis_message(self):
        self.reply(400, {"error": "limit must be a whole number from 1 to 100"})
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.submissions(limit=500)
        self.assertEqual((str(caught.exception), caught.exception.status),
                         ("limit must be a whole number from 1 to 100", 400))

    def test_iter_submissions_follows_the_cursor_with_the_same_filters(self):
        entry = lambda n: {"submission": f"sub_{n:024d}", "status": "queued"}
        self.reply(200, {"submissions": [entry(1), entry(2)], "next_cursor": "c1"})
        self.reply(200, {"submissions": [entry(3)], "next_cursor": None})
        ids = iter([SUB])
        listed = [e["submission"] for e in self.client.iter_submissions(ids=ids, external_ref="cand-1", limit=2)]
        self.assertEqual(listed, [f"sub_{n:024d}" for n in (1, 2, 3)])
        first, second = (r["path"] for r in FakeApi.requests)
        self.assertEqual(first, f"/api/submissions/?ids={SUB}&external_ref=cand-1&limit=2")
        self.assertEqual(second, f"/api/submissions/?ids={SUB}&external_ref=cand-1&limit=2&cursor=c1")

    def test_wait_for_submission_polls_until_it_ends(self):
        for status in ("queued", "running", "running"):
            self.reply(200, finished(status))
        self.reply(200, finished(result=RESULT))
        done = self.client.wait_for_submission(SUB, interval=0.01, timeout=5)
        self.assertEqual((done["status"], len(FakeApi.requests)), ("finished", 4))
        self.reply(200, finished("failed", error="The API key that sent this submission was deleted before it ran."))
        self.assertEqual(self.client.wait_for_submission(SUB, interval=0.01)["status"], "failed")

    def test_wait_for_submission_gives_up_after_its_timeout(self):
        for _ in range(50):
            self.reply(200, finished("queued"))
        started = time.monotonic()
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.wait_for_submission(SUB, interval=0.02, timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("still queued", caught.exception.message)
        self.assertEqual(caught.exception.status, 0)

    def test_judge_by_id_or_slug_reaches_the_quoted_path(self):
        for ref, path in [(PROB, PROB), ("two-sum", "two-sum"), ("two sum/v2?x#y", "two%20sum%2Fv2%3Fx%23y")]:
            with self.subTest(ref=ref):
                self.reply(200, {**RESULT, "submission": SUB, "receipt": None, "problem": PROB, "version": 3})
                result = self.client.judge(ref, "python-3.14", "code")
                self.assertEqual((result["problem"], result["version"]), (PROB, 3))
                self.assertEqual((self.sent["method"], self.sent["path"]), ("POST", f"/api/problems/{path}/submissions/"))
                self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "code"})

    def test_args_memory_limit_and_tests_are_sent_as_given(self):
        self.reply(200, {"output": "", "memory_limit": 64})
        run = self.client.run("python-3.14", "c", args=["a b", "\u00e9"], memory_limit=64)
        self.assertEqual(run["memory_limit"], 64)
        self.assertEqual((self.sent["body"]["args"], self.sent["body"]["memory_limit"]), (["a b", "\u00e9"], 64))
        self.reply(200, {"submission": SUB, "status": "queued"})
        self.client.submit("python-3.14", "c", args=["x"], memory_limit=32)
        self.assertEqual((self.sent["body"]["args"], self.sent["body"]["memory_limit"]), (["x"], 32))
        self.reply(200, {**RESULT, "submission": SUB, "receipt": None, "memory_limit": 128})
        self.client.run_tests("python-3.14", "c", [{"input": "", "expected_output": "", "args": ["1"]}],
                              memory_limit=128)
        self.assertEqual(self.sent["body"]["memory_limit"], 128)
        self.assertEqual(self.sent["body"]["tests"][0]["args"], ["1"])
        self.reply(200, {**RESULT, "submission": SUB, "receipt": None, "problem": PROB, "version": 3})
        self.client.judge("sum", "python-3.14", "c", tests=[1, 3])
        self.assertEqual(self.sent["body"]["tests"], [1, 3])

    def test_rejudge_posts_only_the_fields_given_to_one_quoted_segment(self):
        answer = {"version": 2, "queued": [{"submission": SUB, "rejudge_of": "sub_old"}], "skipped": [],
                  "remaining": 0}
        self.reply(202, answer)
        self.assertEqual(self.client.rejudge("sum"), answer)
        self.assertEqual((self.sent["method"], self.sent["path"], self.sent["body"]),
                         ("POST", "/api/problems/sum/rejudge/", None))
        self.reply(202, answer)
        self.client.rejudge("two sum/v2", version=2, submissions=["sub_old"], notify=True)
        self.assertEqual(self.sent["path"], "/api/problems/two%20sum%2Fv2/rejudge/")
        self.assertEqual(self.sent["body"], {"version": 2, "submissions": ["sub_old"], "notify": True})
        self.reply(202, answer)
        self.client.rejudge(PROB, notify=False)
        self.assertIsNone(self.sent["body"])
        with self.assertRaises(TypeError):
            self.client.rejudge(None)

    def test_judge_async_with_every_option(self):
        self.reply(202, {"submission": SUB, "status": "queued"})
        queued = self.client.judge("sum", "python-3.14", "code", mode="samples", async_=True, external_ref="a-9",
                                   metadata={"x": 1}, callback_url="https://example.com/h", idempotency_key="a-9")
        self.assertEqual(queued["submission"], SUB)
        self.assertEqual(self.sent["headers"]["Idempotency-Key"], "a-9")
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14", "code": "code", "mode": "samples",
                                             "async": True, "callback_url": "https://example.com/h",
                                             "external_ref": "a-9", "metadata": {"x": 1}})

    def test_ids_that_are_not_path_segments_are_refused_before_any_request(self):
        for bad in ("", ".", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.client.submission(bad)
                with self.assertRaises(ValueError):
                    self.client.problem(bad)
                with self.assertRaises(ValueError):
                    self.client.session("sum", bad)
        with self.assertRaises(TypeError):
            self.client.judge(None, "python-3.14", "x")
        self.assertEqual(FakeApi.requests, [])

    def test_draft_data_files(self):
        def last():
            return FakeApi.requests[-1]
        FakeApi.replies.append((200, {"problem": PROB}))
        self.assertEqual(self.client.put_draft_file("sum", "my data/x.db", b"\x00\xff\n\x80"), {"problem": PROB})
        self.assertEqual((last()["method"], last()["path"]), ("PUT", "/api/problems/sum/draft/files/my%20data%2Fx.db/"))
        self.assertEqual(last()["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(last()["headers"]["Content-Length"], "4")
        self.assertEqual(last()["raw"], b"\x00\xff\n\x80")
        FakeApi.replies.append((200, {"problem": PROB}))
        self.client.put_draft_file("sum", "a.csv", "\u00e9,1\n")
        self.assertEqual(last()["raw"], "\u00e9,1\n".encode("utf-8"))
        FakeApi.replies.append((200, {"problem": PROB}))
        self.client.put_draft_file("sum", "empty.txt", b"")
        self.assertEqual((last()["raw"], last()["headers"]["Content-Length"]), (b"", "0"))
        FakeApi.replies.append((200, {"problem": PROB}))
        self.client.delete_draft_file("sum", "a.csv")
        self.assertEqual((last()["method"], last()["path"], last()["raw"]),
                         ("DELETE", "/api/problems/sum/draft/files/a.csv/", b""))
        count = len(FakeApi.requests)
        for bad in ("", ".", ".."):
            with self.assertRaisesRegex(ValueError, "^name must be a non-empty file name$"):
                self.client.put_draft_file("sum", bad, b"x")
            with self.assertRaisesRegex(ValueError, "^name must be a non-empty file name$"):
                self.client.delete_draft_file("sum", bad)
        with self.assertRaisesRegex(ValueError, "^problem must be a non-empty id$"):
            self.client.put_draft_file("", "a", b"x")
        with self.assertRaises(TypeError):
            self.client.put_draft_file("sum", 7, b"x")
        with self.assertRaises(TypeError):
            self.client.put_draft_file("sum", "a", 5)
        self.assertEqual(len(FakeApi.requests), count)
        FakeApi.replies.append((404, {"error": "No such file"}))
        with self.assertRaises(oc.NotFoundError):
            self.client.delete_draft_file("sum", "gone")

    def test_problem_authoring_calls(self):
        spec = {"title": "Sum", "slug": "sum", "tests": [{"input": "1 2\n", "expected_output": "3\n"}]}
        calls = [
            (lambda: self.client.problems(), {"problems": [{"problem": PROB}]}, ("GET", "/api/problems/", None),
             [{"problem": PROB}]),
            (lambda: self.client.create_problem(spec), {"problem": PROB}, ("POST", "/api/problems/", spec), None),
            (lambda: self.client.problem("sum"), {"problem": PROB}, ("GET", "/api/problems/sum/", None), None),
            (lambda: self.client.update_problem("sum", {"public": True}), {"problem": PROB},
             ("PATCH", "/api/problems/sum/", {"public": True}), None),
            (lambda: self.client.replace_draft(PROB, {"tests": spec["tests"]}), {"problem": PROB},
             ("PUT", f"/api/problems/{PROB}/draft/", {"tests": spec["tests"]}), None),
            (lambda: self.client.publish_problem("sum"), {"problem": PROB, "version": 1, "state": "published"},
             ("POST", "/api/problems/sum/publish/", None), None),
            (lambda: self.client.archive_problem("sum"), {"problem": PROB}, ("POST", "/api/problems/sum/archive/", None),
             None),
            (lambda: self.client.restore_problem("sum"), {"problem": PROB}, ("POST", "/api/problems/sum/restore/", None),
             None),
        ]
        for call, answer, (method, path, body), expected in calls:
            with self.subTest(path=path, method=method):
                self.reply(200, answer)
                self.assertEqual(call(), answer if expected is None else expected)
                self.assertEqual((self.sent["method"], self.sent["path"], self.sent["body"]), (method, path, body))
                self.assertEqual(self.sent["headers"]["Authorization"], "key-123")

    def test_the_public_view_sends_the_key_only_when_there_is_one(self):
        self.reply(200, {"title": "Sum", "version": 1})
        self.assertEqual(self.client.public_problem(PROB)["title"], "Sum")
        self.assertEqual(self.sent["path"], f"/api/problems/{PROB}/public/")
        self.assertEqual(self.sent["headers"]["Authorization"], "key-123")
        with mock.patch.dict(os.environ, {}, clear=True):
            keyless = oc.OnlineCompiler(base_url=self.base_url)
        self.reply(200, {"title": "Sum", "version": 1})
        keyless.public_problem(PROB)
        self.assertNotIn("Authorization", self.sent["headers"])

    def test_sessions(self):
        created = {"session": "pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1", "problem": PROB, "version": 2, "token": TOKEN}
        self.reply(201, created)
        session = self.client.create_session("sum", "attempt-7", metadata={"c": 1}, callback_url="https://example.com/h",
                                             expires_in=600, max_submits=2, share_receipts=False)
        self.assertEqual(session["token"], TOKEN)
        self.assertEqual((self.sent["method"], self.sent["path"]), ("POST", "/api/problems/sum/sessions/"))
        self.assertEqual(self.sent["body"], {"external_ref": "attempt-7", "metadata": {"c": 1},
                                             "callback_url": "https://example.com/h", "expires_in": 600,
                                             "max_submits": 2, "share_receipts": False})
        self.reply(201, created)
        self.client.create_session(PROB, "attempt-8")
        self.assertEqual(self.sent["body"], {"external_ref": "attempt-8"})
        self.reply(200, {"session": "pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1", "submits_used": 1})
        self.assertEqual(self.client.session("sum", "pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1")["submits_used"], 1)
        self.assertEqual((self.sent["method"], self.sent["path"]),
                         ("GET", "/api/problems/sum/sessions/pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1/"))
        self.reply(200, {"session": "pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1", "revoked": "2026-11-05T10:00:00Z"})
        self.assertIsNotNone(self.client.revoke_session("sum", "pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1")["revoked"])
        self.assertEqual((self.sent["method"], self.sent["path"]),
                         ("DELETE", "/api/problems/sum/sessions/pss_Qm4Tn8Vx2Kb7Hc3Ld9Wz5Ey1/"))

    def test_a_session_token_never_reaches_an_exception(self):
        # A reply that is not JSON would normally be quoted in the error; a session's reply never is.
        self.reply(201, f'{{"session": "pss_x", "token": "{TOKEN}"'.encode())
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.create_session("sum", "attempt-7")
        error = caught.exception
        shown = str(error) + repr(error) + "".join(traceback.format_exception(error)) + repr(error.body)
        self.assertNotIn(TOKEN, shown)
        self.assertNotIn("T0k3n", shown)
        self.reply(409, {"error": "This problem is archived."})
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.create_session("sum", "attempt-7")
        self.assertEqual((caught.exception.status, caught.exception.message), (409, "This problem is archived."))

    def test_receipt_keys_need_no_key_and_are_cached(self):
        keys = [{"key_id": "oc-2026-11", "algorithm": "Ed25519", "public_key": base64.b64encode(b"k" * 32).decode()}]
        self.reply(200, {"keys": keys})
        self.assertEqual(self.client.receipt_keys(), keys)
        self.assertEqual(self.client.receipt_keys(), keys)
        self.assertEqual([r["path"] for r in FakeApi.requests], ["/.well-known/receipt-keys.json"])
        self.assertNotIn("Authorization", self.sent["headers"])
        self.reply(200, {"keys": []})
        self.assertEqual(self.client.receipt_keys(refresh=True), [])
        self.assertEqual(len(FakeApi.requests), 2)

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs cryptography")
    def test_client_verify_receipt_fetches_the_keys_once_and_again_for_an_unknown_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        key = {"key_id": "new-key", "algorithm": "Ed25519", "public_key": base64.b64encode(raw).decode()}
        unsigned = {"receipt": "rcpt_x", "format": 1, "verdict": "accepted", "score": 100, "key_id": "new-key"}
        receipt = {**unsigned, "signature": base64.b64encode(private.sign(oc.canonical_json(unsigned))).decode()}
        self.reply(200, {"keys": [key]})
        self.assertTrue(self.client.verify_receipt(receipt))
        self.assertTrue(self.client.verify_receipt(receipt))
        self.assertFalse(self.client.verify_receipt({**receipt, "score": 99}))
        self.assertEqual(len(FakeApi.requests), 1)
        # A key_id the cached list lacks: the list is fetched again, at most once a minute.
        self.client._keys_fetched -= 61
        self.reply(200, {"keys": [key]})
        self.assertFalse(self.client.verify_receipt({**receipt, "key_id": "rotated-key"}))
        self.assertEqual(len(FakeApi.requests), 2)
        self.assertFalse(self.client.verify_receipt({**receipt, "key_id": "rotated-key"}))
        self.assertEqual(len(FakeApi.requests), 2)


    def test_a_replay_whose_original_was_refused_says_to_send_it_again(self):
        # The 202 named a submission that was then refused, so the lookup answers 404.
        self.reply(202, {"submission": SUB, "status": "running"})
        self.reply(404, {"error": "Submission not found"})
        with mock.patch("onlinecompiler._client.time.sleep"), self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.run_tests("python-3.14", "c", TESTS, idempotency_key="k1")
        self.assertNotIsInstance(caught.exception, oc.NotFoundError)
        self.assertIn("refused", caught.exception.message)
        self.assertIn("send it again", caught.exception.message.lower())

    def test_the_replay_wait_is_bounded_by_the_client_timeout(self):
        client = oc.OnlineCompiler("key-123", base_url=self.base_url, timeout=0.5)
        self.reply(202, {"submission": SUB, "status": "running"})
        for _ in range(5):
            self.reply(200, finished("running"))
        with mock.patch("onlinecompiler._client.time.sleep"), self.assertRaises(oc.OnlineCompilerError) as caught:
            client.judge(PROB, "python-3.14", "c", idempotency_key="k1")
        self.assertIn("still running", caught.exception.message)
        self.assertLessEqual(len(FakeApi.requests), 2)

    def test_a_cut_off_reply_is_a_transport_error(self):
        self.reply(200, ("truncated", b'{"output": "hi"'))
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.run("python-3.14", "x")
        self.assertEqual(caught.exception.status, 0)
        self.assertIn("Could not reach", caught.exception.message)

    def test_a_cut_off_session_reply_carries_no_token_anywhere(self):
        self.reply(201, ("truncated", f'{{"session": "pss_x", "token": "{TOKEN}"'.encode()))
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.create_session("sum", "attempt-7")
        error = caught.exception
        shown = str(error) + repr(error) + "".join(traceback.format_exception(error))
        self.assertNotIn("T0k3n", shown)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_receipt_keys_are_copies(self):
        keys = [{"key_id": "oc-2026-11", "algorithm": "Ed25519", "public_key": base64.b64encode(b"k" * 32).decode()}]
        self.reply(200, {"keys": keys})
        first = self.client.receipt_keys()
        first[0]["key_id"] = "changed"
        first.append({"key_id": "added"})
        self.assertEqual(self.client.receipt_keys(), keys)
        self.assertEqual(len(FakeApi.requests), 1)

    def test_an_idempotency_key_the_api_would_refuse_is_refused_before_any_request(self):
        for bad in ("", "x" * 101, "caf\u00e9", "line\nbreak", "tab\there", "\x7f"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    self.client.run_tests("python-3.14", "c", TESTS, idempotency_key=bad)
                self.assertIn("1 to 100 printable ASCII characters", str(caught.exception))
                with self.assertRaises(ValueError):
                    self.client.judge("sum", "python-3.14", "c", idempotency_key=bad)
        self.assertEqual(FakeApi.requests, [])
        for good in ("x" * 100, " ~ attempt 42 ~ "):
            self.reply(202, {"submission": SUB, "status": "queued"})
            self.client.run_tests("python-3.14", "c", TESTS, async_=True, idempotency_key=good)
            self.assertEqual(self.sent["headers"]["Idempotency-Key"].strip(), good.strip())

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs cryptography")
    def test_client_verify_receipt_of_none_is_false_without_a_request(self):
        self.assertIs(self.client.verify_receipt(None), False)
        self.assertEqual(FakeApi.requests, [])

    def test_client_verify_receipt_checks_the_extra_before_fetching_keys(self):
        blocked = dict.fromkeys(["cryptography", "cryptography.exceptions",
                                 "cryptography.hazmat.primitives.asymmetric.ed25519"])
        with mock.patch.dict(sys.modules, blocked), self.assertRaises(ImportError) as caught:
            self.client.verify_receipt({"key_id": "k", "signature": "AAAA"})
        self.assertIn('pip install "onlinecompiler[receipts]"', str(caught.exception))
        self.assertEqual(FakeApi.requests, [])


SUITE_FILES = [
    {"name": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
    {"name": "tests/test_calculator.py",
     "content": "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
]
SUITE_RESULT = {
    "verdict": "failed", "gradable": True, "passed": 1, "failed": 1, "errors": 0, "skipped": 0, "total": 2,
    "score": 50.0,
    "tests": [
        {"name": "test_add", "classname": "tests.test_calculator", "file": None, "status": "passed", "time": 0.001,
         "message": None, "score": None, "max_score": None},
        {"name": "test_add_negative", "classname": "tests.test_calculator", "file": None, "status": "failed",
         "time": 0.002, "message": "AssertionError: assert -1 == -2", "score": None, "max_score": None},
    ],
    "run": {"exit_code": 1, "signal": None, "time": 1.42, "memory": 61234, "output": "1 failed, 1 passed", "error": "",
            "truncated": False},
    "compiler": "python-3.14-tests", "preset": "pytest", "pack": {"version": "1.0"}, "report_truncated": False,
    "time_limit": 20, "memory_limit": 512, "summary": "1 of 2 tests passed.",
}
SUITE_RECEIPT = {
    "receipt": "rcpt_s", "format": 1, "submission": SUB, "account": "acct_1", "external_ref": None,
    "issued": "2026-11-11T09:00:00Z", "compiler": {"id": "python-3.14-tests", "version": "3.14-1.0-b1", "image": None},
    "suite": {"preset": "pytest", "command_sha256": "a" * 64, "pack_version": "1.0"}, "files_sha256": "b" * 64,
    "settings": {"time_limit": 20, "memory_limit": 512}, "verdict": "failed", "score": 50, "passed": 1, "total": 2,
    "tests": [{"status": "passed"}, {"status": "failed"}], "key_id": "oc-2026-11", "signature": "c2ln",
}


class SuiteTests(StubApiCase):
    """run_suite: a test pack's command over files, answered with a named result per test."""

    def test_run_suite_posts_the_suite_and_files_to_run_tests_and_returns_the_named_results(self):
        self.reply(200, {**SUITE_RESULT, "submission": SUB, "receipt": SUITE_RECEIPT})
        result = self.client.run_suite("python-3.14-tests", SUITE_FILES, preset="pytest")
        self.assertEqual((self.sent["method"], self.sent["path"]), ("POST", "/api/run-tests/"))
        self.assertEqual(self.sent["headers"]["Authorization"], "key-123")
        self.assertNotIn("Idempotency-Key", self.sent["headers"])
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14-tests", "suite": {"preset": "pytest"},
                                             "files": SUITE_FILES})
        self.assertEqual(result, {**SUITE_RESULT, "submission": SUB, "receipt": SUITE_RECEIPT})
        self.assertEqual([(t["name"], t["status"]) for t in result["tests"]],
                         [("test_add", "passed"), ("test_add_negative", "failed")])

    def test_an_empty_suite_runs_the_default_preset_and_every_option_given_is_sent(self):
        self.reply(200, {**SUITE_RESULT, "submission": SUB, "receipt": None})
        self.client.run_suite("nodejs-24-tests", SUITE_FILES)
        self.assertEqual(self.sent["body"], {"compiler": "nodejs-24-tests", "suite": {}, "files": SUITE_FILES})
        self.reply(200, {**SUITE_RESULT, "preset": None, "submission": SUB, "receipt": None})
        result = self.client.run_suite("python-3.14-tests", SUITE_FILES, command="python grade.py", time_limit=25,
                                       memory_limit=1024, external_ref="attempt-3", metadata={"learner": 9},
                                       idempotency_key="attempt-3")
        self.assertIsNone(result["preset"])
        self.assertEqual(self.sent["headers"]["Idempotency-Key"], "attempt-3")
        self.assertEqual(self.sent["body"], {"compiler": "python-3.14-tests", "suite": {"command": "python grade.py"},
                                             "files": SUITE_FILES, "time_limit": 25, "memory_limit": 1024,
                                             "external_ref": "attempt-3", "metadata": {"learner": 9}})

    def test_what_the_api_would_refuse_is_refused_before_any_request(self):
        with self.assertRaises(ValueError) as caught:
            self.client.run_suite("python-3.14-tests", SUITE_FILES, preset="pytest", command="pytest")
        self.assertIn("preset or command, not both", str(caught.exception))
        with self.assertRaises(ValueError):
            self.client.run_suite("python-3.14-tests", SUITE_FILES, idempotency_key="")
        self.assertEqual(FakeApi.requests, [])

    def test_a_refused_suite_and_a_busy_runner_keep_the_api_message_and_status(self):
        self.reply(400, {"error": "tests cannot be sent with suite: a suite's test command decides its tests"})
        with self.assertRaises(oc.OnlineCompilerError) as caught:
            self.client.run_suite("python-3.14-tests", SUITE_FILES)
        self.assertNotIsInstance(caught.exception, oc.RateLimitError)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("cannot be sent with suite", caught.exception.message)
        self.reply(429, {"error": "Too many runs in progress. Please try again shortly."})
        with self.assertRaises(oc.RateLimitError) as caught:
            self.client.run_suite("python-3.14-tests", SUITE_FILES)
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("try again shortly", caught.exception.message)

    def test_a_replayed_suite_request_still_running_is_waited_for(self):
        self.reply(202, {"submission": SUB, "status": "running"})
        self.reply(200, finished(result=SUITE_RESULT, receipt=SUITE_RECEIPT))
        with mock.patch("onlinecompiler._client.time.sleep"):
            result = self.client.run_suite("python-3.14-tests", SUITE_FILES, idempotency_key="k1")
        self.assertEqual(result, {**SUITE_RESULT, "submission": SUB, "receipt": SUITE_RECEIPT})
        self.assertEqual([r["path"] for r in FakeApi.requests[1:]], [f"/api/submissions/{SUB}/"])

    def test_compilers_passes_a_test_packs_pack_through(self):
        pack = {"version": "1.0", "presets": ["jest", "mocha", "node-test"], "default": "jest"}
        self.reply(200, {"compilers": [{"id": "nodejs-24-tests", "name": "Node.js 24 (tests)", "pack": pack}],
                         "retired": {}})
        self.assertEqual(self.client.compilers()[0]["pack"], pack)

    def test_the_suite_types_are_exported(self):
        for name in ("RunSuiteResult", "SuiteResult", "SuiteTestResult", "SuiteReceipt", "SuiteRun", "SuiteVerdict",
                     "SuiteTestStatus", "CompilerPack"):
            self.assertIn(name, oc.__all__)
            self.assertTrue(hasattr(oc, name))


@unittest.skipUnless(os.environ.get("ONLINECOMPILER_API_KEY"), "set ONLINECOMPILER_API_KEY to run against the API")
class LiveTests(unittest.TestCase):
    """Two real runs against api.onlinecompiler.io; each counts toward the key's allowance."""

    def test_run_and_run_tests(self):
        client = oc.OnlineCompiler()
        self.assertEqual(client.run("python-3.14", "print(input()[::-1])", "abc")["output"], "cba\n")
        result = client.run_tests("python-3.14", "print(int(input()) * 2)", [{"input": "21", "expected_output": "42"}])
        self.assertEqual(result["verdict"], "accepted")


if __name__ == "__main__":
    unittest.main()
