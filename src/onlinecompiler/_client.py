"""The OnlineCompiler.io API client. Standard library only, so installing it adds nothing else."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from typing import Any, Literal, TypedDict, overload
from urllib.parse import quote, urlencode

from ._verify import _ed25519
from ._verify import verify_receipt as _verify_receipt

__version__ = "0.6.0"

DEFAULT_BASE_URL = "https://api.onlinecompiler.io"
# Long enough for a 30-second run or a 60-second run-tests request, plus queueing and network time.
DEFAULT_TIMEOUT = 90.0
SIGNUP_URL = "https://api.onlinecompiler.io/compilers/api-keys/"
RECEIPT_KEYS_PATH = "/.well-known/receipt-keys.json"
KEYS_MAX_AGE = 3600.0  # seconds the receipt key list is kept, as the API allows it to be cached
KEYS_REFETCH_AFTER = 60.0  # seconds before an unknown key_id fetches the list again
# The API answers 404 both for an unknown API key and for an id that is not the caller's.
KEY_REFUSED = "Invalid or inactive API key"
# What the API takes as an Idempotency-Key: 1 to 100 printable ASCII characters, space included.
IDEMPOTENCY_KEY = re.compile(r"[ -~]{1,100}")
REFUSED_ORIGINAL = ("The original request with this idempotency_key was refused, so it was not judged. Send it "
                    "again.")

Checker = Literal["trim", "exact", "tokens", "float", "rows"]
Scoring = Literal["all_or_nothing", "percent", "weighted"]
Mode = Literal["all", "samples", "tests"]


class _InputFile(TypedDict):
    name: str
    content: str


class InputFile(_InputFile, total=False):
    """A file the program can read from its working directory. encoding is "utf8" (default) or "base64"."""

    encoding: Literal["utf8", "base64"]


class OutputFile(TypedDict):
    """A file the program wrote to $OUTPUT_DIR. url is null when the file was not stored."""

    name: str
    mime: str
    size: int
    content_base64: str
    url: str | None


class RunResult(TypedDict, total=False):
    """time, total (seconds) and memory (KB) are decimal strings, as the API sends them."""

    output: str
    error: str
    status: str
    exit_code: int | None
    signal: int | None
    time: str
    total: str
    memory: str
    memory_limit: int | None
    truncated: bool
    time_limit: float
    files: list[OutputFile]
    files_truncated: bool


class _Case(TypedDict):
    expected_output: str


class Case(_Case, total=False):
    """One test case: stdin, the output it should print, and optionally hidden and weight.
    Named Case rather than Test so pytest never tries to collect it from your test files."""

    input: str
    hidden: bool
    weight: float
    args: list[str]


class CaseResult(TypedDict, total=False):
    """One test case's result. Hidden and skipped tests carry only verdict and hidden."""

    verdict: str
    hidden: bool
    time: str
    memory: str
    exit_code: int | None
    signal: int | None
    output: str
    error: str
    truncated: bool
    files: list[OutputFile]
    files_truncated: bool


class ReceiptKey(TypedDict):
    """A published receipt signing key: the base64 of its raw 32-byte Ed25519 public key."""

    key_id: str
    algorithm: str
    public_key: str


class Receipt(TypedDict, total=False):
    """A signed receipt (see https://onlinecompiler.io/docs/receipts). A stored problem's receipt has problem in
    place of tests_sha256 and files_sha256. Check it with verify_receipt."""

    receipt: str
    format: int
    submission: str
    account: str
    external_ref: str | None
    issued: str
    compiler: dict[str, Any]
    code_sha256: str
    tests_sha256: str
    files_sha256: str | None
    problem: dict[str, Any]
    settings: dict[str, Any]
    verdict: str
    score: float
    passed: int
    total: int
    tests: list[dict[str, Any]]
    key_id: str
    signature: str


class _Judged(TypedDict):
    verdict: str
    score: float
    passed: int
    total: int
    time_limit: float
    compile_error: str | None
    tests: list[CaseResult]
    summary: str


class _JudgedExtras(TypedDict, total=False):
    """memory_limit: the limit (MB) the run was judged with, if one was given."""

    memory_limit: int | None


class RunTestsResult(_Judged, _JudgedExtras):
    """A judged submission: summary is the result in plain words; submission is its id; receipt is None when
    none could be signed."""

    submission: str
    receipt: Receipt | None


class JudgeResult(RunTestsResult, total=False):
    """A stored problem's result: the run-tests result with the problem's id and the version judged."""

    problem: str
    version: int


class SubmissionResult(_Judged, _JudgedExtras, total=False):
    """A finished submission's result, as GET /api/submissions/{id}/ and webhooks give it: the run-tests result
    without submission and receipt (problem and version for a stored problem)."""

    problem: str
    version: int


class SubmissionQueued(TypedDict):
    """An async submission's answer: its id and status ("queued", or the original's status for a replayed
    Idempotency-Key)."""

    submission: str
    status: str


class SubmissionProblem(TypedDict):
    id: str
    version: int


class Submission(TypedDict):
    """A submission as its account sees it. status is queued, running, finished or failed; failed means it was not
    judged, and error says why. hash_salt salts the receipt's tests and files hashes."""

    submission: str
    status: str
    created: str
    finished: str | None
    external_ref: str | None
    metadata: dict[str, Any] | None
    problem: SubmissionProblem | None
    mode: Mode | None
    rejudge_of: str | None
    result: SubmissionResult | None
    receipt: Receipt | None
    error: str | None
    checker_log: str | None
    hash_salt: str


class SubmissionDeleted(TypedDict):
    submission: str
    deleted: bool


class SubmissionSummary(TypedDict):
    """A submission in a list: Submission without result, receipt, checker_log and hash_salt, plus the result's
    verdict, score, passed and total (None until it has finished). submission(id) gives the rest."""

    submission: str
    status: str
    created: str
    finished: str | None
    external_ref: str | None
    metadata: dict[str, Any] | None
    problem: SubmissionProblem | None
    mode: Mode | None
    rejudge_of: str | None
    verdict: str | None
    score: float | None
    passed: int | None
    total: int | None
    error: str | None


class SubmissionPage(TypedDict):
    """A page of submissions, newest first. next_cursor, when not None, fetches the next page."""

    submissions: list[SubmissionSummary]
    next_cursor: str | None


class RejudgeQueued(TypedDict):
    submission: str
    rejudge_of: str


class RejudgeSkipped(TypedDict, total=False):
    submission: str
    reason: str
    rejudge: str


class RejudgeResult(TypedDict):
    """version is the one judged against; remaining counts the candidates left for another call."""

    version: int
    queued: list[RejudgeQueued]
    skipped: list[RejudgeSkipped]
    remaining: int


class PublishResult(TypedDict, total=False):
    """state is "published" (with warnings) or "checking" (with check, the reference check's submission id)."""

    problem: str
    version: int
    state: str
    warnings: list[str]
    check: str


class ProblemSession(TypedDict):
    """A widget session as its owner sees it, never with its token."""

    session: str
    problem: str
    version: int
    external_ref: str
    metadata: dict[str, Any] | None
    callback_url: str
    max_submits: int
    share_receipts: bool
    submits_used: int
    expires: str
    revoked: str | None
    created: str
    submissions: list[str]


class ProblemSessionCreated(ProblemSession):
    """A new session with its token, which the API shows only this once: render it into the candidate's page as
    the widget's data-session, and keep it out of logs."""

    token: str


class Queued(TypedDict):
    status: str
    id: int


class Compiler(TypedDict, total=False):
    id: str
    name: str
    packages: list[dict[str, str]]


class OnlineCompilerError(Exception):
    """The API refused or failed a request. status is the HTTP status, or 0 when no response arrived
    (network error or timeout); body is the parsed error response, if any."""

    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


class AuthenticationError(OnlineCompilerError):
    """HTTP 401 or 403, or 404 for the key itself: the API key is missing, unknown, inactive, of the wrong kind, or
    not allowed from this address, or the account may not make this request (a 403 such as a plan's problem limit;
    the message says which)."""


class NotFoundError(OnlineCompilerError):
    """HTTP 404 for an id: the submission, problem or session does not exist or is not this account's."""


class RateLimitError(OnlineCompilerError):
    """HTTP 429: the monthly allowance, the per-minute rate, the pending runs or the account's
    sandboxes are used up. The message says which, and when it is the plan, how to raise it."""


def _error(status: int, raw: bytes) -> OnlineCompilerError:
    try:
        body = json.loads(raw)
        message = body["error"] if isinstance(body, dict) and "error" in body else raw.decode("utf-8", "replace")
    except ValueError:
        body, message = None, raw.decode("utf-8", "replace")
    message = message or f"HTTP {status}"
    if status == 404 and message != KEY_REFUSED:
        return NotFoundError(message, status, body)
    if status in (401, 403, 404):
        return AuthenticationError(message, status, body)
    if status == 429:
        return RateLimitError(message, status, body)
    return OnlineCompilerError(message, status, body)


def _without_none(**fields: Any) -> dict[str, Any]:
    """Options left as None are left out, so the API applies its own defaults."""
    return {key: value for key, value in fields.items() if value is not None}


def _segment(value: str, what: str, kind: str = "id") -> str:
    """value as one quoted path segment; an id, slug or file name that could not be one is refused before any
    request. kind names what value is in the message: an id, or a file name."""
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, not {type(value).__name__}")
    if value in ("", ".", ".."):
        raise ValueError(f"{what} must be a non-empty {kind}")
    return quote(value, safe="")


def _moment(value: datetime | date | str | None) -> str | None:
    """A since or until value as the API takes it: ISO 8601 text (a datetime without a zone is read as UTC)."""
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _judging_options(async_: bool | None, callback_url: str | None, external_ref: str | None,
                     metadata: dict[str, Any] | None) -> dict[str, Any]:
    if callback_url is not None and async_ is not True:
        raise ValueError("callback_url needs async_=True: the result goes to the URL, not into the response")
    return _without_none(callback_url=callback_url, external_ref=external_ref, metadata=metadata,
                         **{"async": async_})


class OnlineCompiler:
    """A client for the OnlineCompiler.io code execution API.

    api_key defaults to the ONLINECOMPILER_API_KEY environment variable. Get a free key at
    https://api.onlinecompiler.io/compilers/api-keys/. Requests are never retried, since every
    accepted run counts toward your allowance.
    """

    def __init__(self, api_key: str | None = None, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key or os.environ.get("ONLINECOMPILER_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._keys: list[ReceiptKey] | None = None
        self._keys_fetched = 0.0

    def run(self, compiler: str, code: str, input: str = "", *, time_limit: float | None = None,
            files: list[InputFile] | None = None, args: list[str] | None = None,
            memory_limit: int | None = None) -> RunResult:
        """Run a program and wait for its result: output, error, exit_code, time, memory and any
        files it wrote. compiler is an id such as "python-3.14" (see compilers()). args are command-line
        arguments for the program (up to 32 strings; not for sqlite-3, duckdb-1.5 or erlang-29). memory_limit
        (whole MB, 16 to 512) ends a run whose peak memory is over it with exit_code 137."""
        body = _without_none(compiler=compiler, code=code, input=input, time_limit=time_limit, files=files,
                             args=args, memory_limit=memory_limit)
        return self._request("POST", "/api/run-code-sync/", body)

    @overload
    def run_tests(self, compiler: str, code: str, tests: list[Case], *, time_limit: float | None = None,
                  checker: Checker | None = None, tolerance: float | None = None, scoring: Scoring | None = None,
                  stop_on_failure: bool | None = None, files: list[InputFile] | None = None,
                  memory_limit: int | None = None,
                  async_: Literal[False] | None = None, external_ref: str | None = None,
                  metadata: dict[str, Any] | None = None, idempotency_key: str | None = None) -> RunTestsResult: ...

    @overload
    def run_tests(self, compiler: str, code: str, tests: list[Case], *, time_limit: float | None = None,
                  checker: Checker | None = None, tolerance: float | None = None, scoring: Scoring | None = None,
                  stop_on_failure: bool | None = None, files: list[InputFile] | None = None,
                  memory_limit: int | None = None,
                  async_: Literal[True], callback_url: str | None = None, external_ref: str | None = None,
                  metadata: dict[str, Any] | None = None, idempotency_key: str | None = None) -> SubmissionQueued: ...

    @overload
    def run_tests(self, compiler: str, code: str, tests: list[Case], *, time_limit: float | None = None,
                  checker: Checker | None = None, tolerance: float | None = None, scoring: Scoring | None = None,
                  stop_on_failure: bool | None = None, files: list[InputFile] | None = None,
                  memory_limit: int | None = None,
                  async_: bool | None = None, callback_url: str | None = None, external_ref: str | None = None,
                  metadata: dict[str, Any] | None = None,
                  idempotency_key: str | None = None) -> RunTestsResult | SubmissionQueued: ...

    def run_tests(self, compiler: str, code: str, tests: list[Case], *, time_limit: float | None = None,
                  checker: Checker | None = None, tolerance: float | None = None, scoring: Scoring | None = None,
                  stop_on_failure: bool | None = None, files: list[InputFile] | None = None,
                  memory_limit: int | None = None,
                  async_: bool | None = None, callback_url: str | None = None, external_ref: str | None = None,
                  metadata: dict[str, Any] | None = None,
                  idempotency_key: str | None = None) -> RunTestsResult | SubmissionQueued:
        """Judge a program against test cases: an overall verdict, a score, a verdict per test, the submission id
        and a signed receipt. Each test counts as one run. time_limit is seconds per test (default 2).
        memory_limit (whole MB, 16 to 512) applies to every test; a test may carry its own args.

        async_=True answers at once with the submission id (async is a Python keyword); get the result with
        wait_for_submission(), submission(), or a signed webhook to callback_url (which needs async_=True).
        external_ref (your own id, signed into the receipt) and metadata (a JSON object, never in the receipt)
        come back in lookups and webhooks. idempotency_key is sent as the Idempotency-Key header: repeating a
        request with it answers with the original result instead of running again."""
        headers = self._idempotency(idempotency_key)
        body = _without_none(compiler=compiler, code=code, tests=tests, time_limit=time_limit, checker=checker,
                             tolerance=tolerance, scoring=scoring, stop_on_failure=stop_on_failure, files=files,
                             memory_limit=memory_limit)
        body.update(_judging_options(async_, callback_url, external_ref, metadata))
        answer = self._request("POST", "/api/run-tests/", body, headers=headers)
        return answer if async_ else self._settled(answer)

    def submit(self, compiler: str, code: str, input: str = "", *, time_limit: float | None = None,
               files: list[InputFile] | None = None, extra_params: dict[str, Any] | None = None,
               args: list[str] | None = None, memory_limit: int | None = None) -> Queued:
        """Queue a run and return at once with its id. The result is POSTed to the callback URL
        set on the API key in your dashboard, with extra_params echoed back."""
        body = _without_none(compiler=compiler, code=code, input=input, time_limit=time_limit, files=files,
                             extra_params=extra_params, args=args, memory_limit=memory_limit)
        return self._request("POST", "/api/run-code/", body)

    # Submissions

    def submission(self, submission_id: str) -> Submission:
        """A submission of this account: its status, and once it has ended its result and receipt (or error)."""
        return self._request("GET", f"/api/submissions/{_segment(submission_id, 'submission_id')}/")

    def delete_submission(self, submission_id: str) -> SubmissionDeleted:
        """Delete a queued, finished or failed submission and what it kept; a running one is refused with 409.
        Runs already counted stay counted. Frees its Idempotency-Key."""
        return self._request("DELETE", f"/api/submissions/{_segment(submission_id, 'submission_id')}/")

    def submissions(self, *, ids: Iterable[str] | str | None = None, external_ref: str | None = None,
                    problem: str | None = None, status: str | None = None, verdict: str | None = None,
                    since: datetime | date | str | None = None, until: datetime | date | str | None = None,
                    limit: int | None = None, cursor: str | None = None) -> SubmissionPage:
        """One page of this account's submissions, newest first; draft tests and reference checks are not listed.
        Each argument given narrows it: ids (up to 100), external_ref, problem (id or slug), status, verdict, since
        (included) and until (not included), the last two as datetimes (one without a zone is UTC), dates or
        ISO 8601 text. limit is 1 to 100 (20 if left out). For the next page, pass next_cursor back as cursor with
        the same filters, or use iter_submissions. ids given alone answers all of them on one page; an empty ids
        answers an empty page without a request."""
        if ids is not None and not isinstance(ids, str):
            ids = ",".join(ids)
            if not ids:
                return {"submissions": [], "next_cursor": None}
        query = _without_none(ids=ids,
                              external_ref=external_ref, problem=problem, status=status, verdict=verdict,
                              since=_moment(since), until=_moment(until), limit=limit, cursor=cursor)
        return self._request("GET", "/api/submissions/" + (f"?{urlencode(query)}" if query else ""))

    def iter_submissions(self, *, ids: Iterable[str] | str | None = None, external_ref: str | None = None,
                         problem: str | None = None, status: str | None = None, verdict: str | None = None,
                         since: datetime | date | str | None = None, until: datetime | date | str | None = None,
                         limit: int | None = None) -> Iterator[SubmissionSummary]:
        """Every submission the filters match (as submissions takes them), newest first, fetching the pages one
        after another as you go; limit is the page size."""
        if ids is not None and not isinstance(ids, str):
            ids = list(ids)  # read once, sent with every page
        cursor = None
        while True:
            page = self.submissions(ids=ids, external_ref=external_ref, problem=problem, status=status,
                                    verdict=verdict, since=since, until=until, limit=limit, cursor=cursor)
            yield from page["submissions"]
            cursor = page["next_cursor"]
            if cursor is None:
                return

    def wait_for_submission(self, submission_id: str, *, interval: float = 2.0, timeout: float = 600.0) -> Submission:
        """Look the submission up every interval seconds until it is finished or failed, and return it. A failed
        one has error and no result. Raises OnlineCompilerError (status 0) if it is still queued or running after
        timeout seconds; it is still judged, so look it up again later."""
        deadline = time.monotonic() + timeout
        while True:
            current = self.submission(submission_id)
            if current["status"] in ("finished", "failed"):
                return current
            if time.monotonic() + interval > deadline:
                raise OnlineCompilerError(
                    f"Submission {submission_id} is still {current['status']} after {timeout:g} seconds", 0, current)
            time.sleep(interval)

    # Stored problems: judging

    @overload
    def judge(self, problem: str, compiler: str, code: str, *, mode: Mode | None = None, tests: list[int] | None = None,
              async_: Literal[False] | None = None, external_ref: str | None = None,
              metadata: dict[str, Any] | None = None, idempotency_key: str | None = None) -> JudgeResult: ...

    @overload
    def judge(self, problem: str, compiler: str, code: str, *, mode: Mode | None = None, tests: list[int] | None = None, async_: Literal[True],
              callback_url: str | None = None, external_ref: str | None = None,
              metadata: dict[str, Any] | None = None, idempotency_key: str | None = None) -> SubmissionQueued: ...

    @overload
    def judge(self, problem: str, compiler: str, code: str, *, mode: Mode | None = None, tests: list[int] | None = None, async_: bool | None = None,
              callback_url: str | None = None, external_ref: str | None = None,
              metadata: dict[str, Any] | None = None,
              idempotency_key: str | None = None) -> JudgeResult | SubmissionQueued: ...

    def judge(self, problem: str, compiler: str, code: str, *, mode: Mode | None = None, tests: list[int] | None = None, async_: bool | None = None,
              callback_url: str | None = None, external_ref: str | None = None,
              metadata: dict[str, Any] | None = None,
              idempotency_key: str | None = None) -> JudgeResult | SubmissionQueued:
        """Judge code against a stored problem's current version. problem is its id (prob_...) or your slug for
        it. mode "samples" judges only the visible tests; tests, a list of 1-based
        positions, judges only those (the submission's mode becomes "tests"). The result is the run-tests result with problem and
        version; async_, callback_url, external_ref, metadata and idempotency_key work as in run_tests(). Send
        async_=True for a problem with a custom checker."""
        path = f"/api/problems/{_segment(problem, 'problem')}/submissions/"
        headers = self._idempotency(idempotency_key)
        body = _without_none(compiler=compiler, code=code, mode=mode, tests=tests)
        body.update(_judging_options(async_, callback_url, external_ref, metadata))
        answer = self._request("POST", path, body, headers=headers)
        return answer if async_ else self._settled(answer)

    # Stored problems: authoring. Specs and answers are plain dicts, as the API docs describe them.

    def problems(self) -> list[dict[str, Any]]:
        """This account's problems, newest first, without their tests."""
        return self._request("GET", "/api/problems/")["problems"]

    def problem(self, problem: str) -> dict[str, Any]:
        """A problem by id or slug, with its draft and current version in full, hidden tests included."""
        return self._request("GET", f"/api/problems/{_segment(problem, 'problem')}/")

    def create_problem(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create a problem from a title, an optional slug and public flag, and its first draft's fields (tests,
        statement, checker, drivers, reference_solution...), sent as given."""
        return self._request("POST", "/api/problems/", spec)

    def update_problem(self, problem: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Change a problem's title, slug or public flag."""
        return self._request("PATCH", f"/api/problems/{_segment(problem, 'problem')}/", fields)

    def replace_draft(self, problem: str, draft: dict[str, Any]) -> dict[str, Any]:
        """Replace the problem's draft with these fields (as create_problem takes, without title)."""
        return self._request("PUT", f"/api/problems/{_segment(problem, 'problem')}/draft/", draft)

    def put_draft_file(self, problem: str, name: str, data: bytes | str) -> dict[str, Any]:
        """Put one data file (a database, a CSV, any bytes) on the draft, as its raw bytes: a str is sent as UTF-8,
        bytes as is. The name is one path segment (no slashes). Answers the changed problem."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"data must be bytes or str, not {type(data).__name__}")
        path = f"/api/problems/{_segment(problem, 'problem')}/draft/files/{_segment(name, 'name', 'file name')}/"
        return self._request("PUT", path, raw_body=(bytes(data), "application/octet-stream"))

    def delete_draft_file(self, problem: str, name: str) -> dict[str, Any]:
        """Remove one data file from the draft. Answers the changed problem; a missing file is a NotFoundError."""
        return self._request("DELETE", f"/api/problems/{_segment(problem, 'problem')}/draft/files/{_segment(name, 'name', 'file name')}/")

    def publish_problem(self, problem: str) -> PublishResult:
        """Publish the draft as the next version: at once without a reference solution (state "published"), else
        once its reference check passes (state "checking"; problem() shows how the check went)."""
        return self._request("POST", f"/api/problems/{_segment(problem, 'problem')}/publish/")

    def rejudge(self, problem: str, *, version: int | None = None, submissions: list[str] | None = None,
                notify: bool = False) -> RejudgeResult:
        """Re-judge earlier submissions against a published version (default the current one): each is queued as
        a new async submission with rejudge_of set. At most 100 a call; remaining says how many are left.
        submissions limits it to those ids; notify=True sends the original's callback_url a webhook."""
        path = f"/api/problems/{_segment(problem, 'problem')}/rejudge/"
        body: dict[str, Any] = _without_none(version=version, submissions=submissions)
        if notify:
            body["notify"] = True
        return self._request("POST", path, body or None)

    def archive_problem(self, problem: str) -> dict[str, Any]:
        """Stop new submissions and free a problem slot."""
        return self._request("POST", f"/api/problems/{_segment(problem, 'problem')}/archive/")

    def restore_problem(self, problem: str) -> dict[str, Any]:
        """Undo archive_problem, if the plan has a free problem slot."""
        return self._request("POST", f"/api/problems/{_segment(problem, 'problem')}/restore/")

    def public_problem(self, problem: str) -> dict[str, Any]:
        """The current version as a candidate sees it: statement, languages, starter code and the visible tests.
        A public problem answers by id with no API key; the key, when the client has one, also shows your own
        private problems and works with a slug."""
        return self._request("GET", f"/api/problems/{_segment(problem, 'problem')}/public/", auth=bool(self.api_key))

    # Stored problems: sessions for the widget

    def create_session(self, problem: str, external_ref: str, *, metadata: dict[str, Any] | None = None,
                       callback_url: str | None = None, expires_in: int | None = None,
                       max_submits: int | None = None, share_receipts: bool | None = None) -> ProblemSessionCreated:
        """Let one candidate's widget Submit to a problem's current version. Returns the session with its token,
        shown only this once: render it into the candidate's page as data-session and keep it out of logs.
        external_ref is your opaque reference for the candidate or attempt (the page sees it; never a name or
        email). expires_in is seconds (60 to 86,400; default 3,600), max_submits 1 to 20 (default 1).
        share_receipts (default True) makes each Submit's signed receipt public by link, so the widget can link
        to it; a shared receipt shows external_ref and your account reference, never the code, tests or output."""
        body = _without_none(external_ref=external_ref, metadata=metadata, callback_url=callback_url,
                             expires_in=expires_in, max_submits=max_submits, share_receipts=share_receipts)
        return self._request("POST", f"/api/problems/{_segment(problem, 'problem')}/sessions/", body, secret=True)

    def session(self, problem: str, session_id: str) -> ProblemSession:
        """A session: its Submits used, expiry and the ids of its submissions. Never the token."""
        return self._request("GET", self._session_path(problem, session_id))

    def revoke_session(self, problem: str, session_id: str) -> ProblemSession:
        """End a session now: its next Submit is refused. Submissions already made are judged as usual."""
        return self._request("DELETE", self._session_path(problem, session_id))

    # Receipts

    def receipt_keys(self, *, refresh: bool = False) -> list[ReceiptKey]:
        """The published receipt signing keys. Needs no API key. Kept on the client for an hour; refresh=True
        fetches them again."""
        if refresh or self._keys is None or time.monotonic() - self._keys_fetched > KEYS_MAX_AGE:
            self._keys = self._request("GET", RECEIPT_KEYS_PATH, auth=False)["keys"]
            self._keys_fetched = time.monotonic()
        return [key.copy() for key in self._keys]  # copies, so that changing them leaves the cache alone

    def verify_receipt(self, receipt: Receipt | Mapping[str, Any] | None) -> bool:
        """Whether a receipt's signature is valid, against the published keys (fetched once and kept; fetched
        again, at most once a minute, for a key_id they lack). None (a submission without a receipt) is False.
        Needs onlinecompiler[receipts], checked before any request; see onlinecompiler.verify_receipt to verify
        with keys you already hold."""
        _ed25519()
        if not isinstance(receipt, Mapping):
            return False
        keys = self.receipt_keys()
        key_id = receipt.get("key_id")
        if (not any(key.get("key_id") == key_id for key in keys)
                and time.monotonic() - self._keys_fetched > KEYS_REFETCH_AFTER):
            keys = self.receipt_keys(refresh=True)
        return _verify_receipt(receipt, keys)

    # Account

    def compilers(self) -> list[Compiler]:
        """Every compiler id and its language name. Needs no API key."""
        return self._request("GET", "/api/compilers/", auth=False)["compilers"]

    def usage(self) -> dict[str, Any]:
        """This month's runs and remaining allowance for the account, and for the key itself."""
        return self._request("GET", "/api/usage/")

    def limits(self) -> dict[str, Any]:
        """Per-run limits (memory, time, output) and default account limits. Needs no API key."""
        return self._request("GET", "/api/limits/", auth=False)

    @staticmethod
    def _idempotency(key: str | None) -> dict[str, str] | None:
        if key is None:
            return None
        if not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key):
            raise ValueError("idempotency_key must be 1 to 100 printable ASCII characters")
        return {"Idempotency-Key": key}

    @staticmethod
    def _session_path(problem: str, session_id: str) -> str:
        return f"/api/problems/{_segment(problem, 'problem')}/sessions/{_segment(session_id, 'session_id')}/"

    def _settled(self, answer: Any) -> Any:
        """A sync judging answer. A retry whose Idempotency-Key finds the original still running answers 202 with
        its id; wait for it and answer as the original would have."""
        if not isinstance(answer, dict) or "verdict" in answer or "submission" not in answer:
            return answer
        try:
            # Bounded by the client's own timeout: the original was a sync request, which ends well within it.
            done = self.wait_for_submission(answer["submission"], interval=1.0, timeout=self.timeout)
        except NotFoundError as exc:
            # The 202 named a submission that was then refused (a 400 or 429 leaves none behind).
            raise OnlineCompilerError(REFUSED_ORIGINAL, exc.status, exc.body) from None
        if done["status"] != "finished" or done["result"] is None:
            raise OnlineCompilerError(done["error"] or f"Submission {done['submission']} was not judged", 202, done)
        return {**done["result"], "submission": done["submission"], "receipt": done["receipt"]}

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, auth: bool = True,
                 headers: dict[str, str] | None = None, secret: bool = False,
                 raw_body: tuple[bytes, str] | None = None) -> Any:
        """secret: the answer holds a secret (a session token), so no error quotes it. raw_body: (bytes, content type)
        sent as the body as is, instead of JSON."""
        sent = {"Accept": "application/json", "User-Agent": f"onlinecompiler-python/{__version__}", **(headers or {})}
        if auth:
            if not self.api_key:
                raise AuthenticationError(
                    f"No API key: pass api_key or set ONLINECOMPILER_API_KEY. Get a free key at {SIGNUP_URL}")
            sent["Authorization"] = self.api_key
        data = None
        if raw_body is not None:
            data, sent["Content-Type"] = raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=sent, method=method)
        failed = ""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:  # before URLError, which it subclasses
            with exc:  # closes the error response
                try:
                    error_body = exc.read()
                except (http.client.HTTPException, OSError):
                    error_body = b""
            raise _error(exc.code, error_body) from None
        # OSError covers timeouts; HTTPException a reply cut off (IncompleteRead) or malformed.
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if not secret:
                raise OnlineCompilerError(f"Could not reach {self.base_url}: {exc}") from exc
            # An IncompleteRead holds the partial body, which may hold the secret: name only its kind, and raise
            # below, outside this block, so that it is not attached as context either.
            failed = f"Could not reach {self.base_url}: {type(exc).__name__}"
        if failed:
            raise OnlineCompilerError(failed)
        try:
            return json.loads(raw)
        except ValueError:
            pass
        # Raised outside the except block, so that no exception context holds the text either.
        if secret:
            del raw  # not left in a frame that an error report could capture either
            raise OnlineCompilerError(f"Expected JSON from {path}")
        raise OnlineCompilerError(f"Expected JSON from {path}, got: {raw[:200]!r}")
