# OnlineCompiler.io Python client

Run code in 46 languages from Python, or judge it against test cases, through the
[OnlineCompiler.io](https://onlinecompiler.io/?utm_source=pypi) code execution API. Every run gets a
fresh gVisor sandbox with no network, 512 MB of memory (up to 2 GB on Pro and Business) and a 30-second limit. Free up to 100,000 runs
a month, commercial use included.

No dependencies beyond the standard library. Python 3.10 or newer. Verifying signed receipts needs
one optional extra, `pip install "onlinecompiler[receipts]"` (it adds `cryptography`).

```bash
pip install onlinecompiler
```

Get a free API key at https://api.onlinecompiler.io/compilers/api-keys/ and set it as
`ONLINECOMPILER_API_KEY`, or pass it to the client.

## Run a program

```python
from onlinecompiler import OnlineCompiler

client = OnlineCompiler()  # reads ONLINECOMPILER_API_KEY

result = client.run("python-3.14", "name = input()\nprint(f'Hello, {name}!')", input="Ada")
print(result["output"])                          # Hello, Ada!
print(result["exit_code"], result["time"], result["memory"])
```

The result has `output` (stdout), `error` (stderr or compiler errors), `exit_code`, `signal`, `time`
and `total` (seconds), `memory` (KB), `truncated`, `time_limit` and `files`. `time`, `total` and
`memory` are decimal strings, as the API sends them. Compiler ids such as `gcc-15`, `openjdk-25` and
`nodejs-24` come from `client.compilers()`. Pass `time_limit` (seconds, up to 30) to stop a run sooner.

Pass `args` (up to 32 strings) to give the program command-line arguments, and `memory_limit` (whole
MB, 16 to 2048; above 512, on Pro and Business, it raises the run's memory) to end a run whose peak memory is over it with exit code 137. Both also work on
`submit()`; in `run_tests()` `args` goes on each test and `memory_limit` applies to all of them.
`sqlite-3`, `duckdb-1.5` and `erlang-29` take no arguments.

## Judge a solution against test cases

```python
result = client.run_tests(
    "python-3.14",
    "a, b = map(int, input().split())\nprint(a + b)",
    tests=[
        {"input": "1 2", "expected_output": "3"},
        {"input": "10 20", "expected_output": "30", "hidden": True},
    ],
)
print(result["verdict"], result["score"], f"{result['passed']}/{result['total']}")
for case in result["tests"]:
    print(case["verdict"])
```

Each test counts as one run. Hidden tests return only their verdict. `checker` picks how output is
compared (`trim` by default, `exact`, `tokens`, `float`, `rows`, `table` or `table_ordered`), `scoring` is `all_or_nothing`
(default), `percent` or `weighted` (with a `weight` per test), and `time_limit` is seconds per test
(default 2). See the [run-tests docs](https://onlinecompiler.io/docs/run-tests?utm_source=pypi).

Every run-tests result also has `summary` (the result in plain words), `submission` (its id) and a
signed `receipt`.

## Judge in the background

```python
queued = client.run_tests(
    "python-3.14",
    "a, b = map(int, input().split())\nprint(a + b)",
    tests=[{"input": "1 2", "expected_output": "3"}, {"input": "10 20", "expected_output": "30", "hidden": True}],
    async_=True,
    external_ref="attempt-4821",
    idempotency_key="attempt-4821",
)
done = client.wait_for_submission(queued["submission"])  # looks it up every 2 seconds
result = done["result"]  # None when it failed, which means it was not judged
if result is not None:
    print(result["verdict"], result["score"])
else:
    print("Not judged:", done["error"])  # send it again
client.delete_submission(done["submission"])  # optional: drop the stored code and output
```

`async_=True` (`async` is a Python keyword) answers at once with the submission id, and the submission
waits in your account's queue instead of getting 429 in a burst. Add `callback_url` to have the result
POSTed to your server as a signed webhook (see below); it needs `async_=True`, and the client refuses it
without. `external_ref` is your own id, signed into the receipt; `metadata` is any JSON object, returned in
lookups and webhooks. `idempotency_key` (1 to 100 printable ASCII characters, checked before sending) is
sent as the `Idempotency-Key` header, so a retry after a timeout answers with the original result instead
of running again. `client.submission(id)` looks a submission up once.

## Test suites

A suite runs a real test framework over your files in one sandbox and answers with a result per test, by
name. It runs on a test pack, `python-3.14-tests` (pytest) or `nodejs-24-tests` (jest, mocha or
`node-test`), and the whole suite counts as one run.

```python
result = client.run_suite(
    "python-3.14-tests",
    files=[
        {"name": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        {"name": "tests/test_calculator.py", "content": (
            "from calculator import add\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_negative():\n    assert add(-1, -1) == -2\n"
        )},
    ],
    preset="pytest",
)
print(result["verdict"], result["score"], f"{result['passed']}/{result['total']}")  # accepted 100.0 2/2
for test in result["tests"]:
    print(test["status"], test["classname"], test["name"], test["message"] or "")
```

`files` carry every source: the code under test, the tests and any fixtures. Pass `preset` or `command`,
never both; pass neither for the pack's default preset. A custom `command` runs with bash and must write
`$OUTPUT_DIR/report.xml` (JUnit XML) or `$OUTPUT_DIR/results.json`. `time_limit` covers the whole suite
(default 20 seconds, at most 30). `client.compilers()` lists each pack's presets in its `pack` field.

Each entry of `tests` has `name`, `classname`, `file`, `status` (`passed`, `failed`, `error` or
`skipped`), `time` and `message` (the failure or error text, or `None`). The counts are in `passed`,
`failed`, `errors`, `skipped` and `total`. `verdict` is `accepted` when every test passed and `failed`
when one did not; a run that stopped (`time_limit_exceeded`, `memory_limit_exceeded` and the like) lists
no tests, and `no_report` means the command wrote no report (`gradable` is then `False`). `result["run"]`
holds the command's own `exit_code`, `output` and `error`, which help when there is no report. A suite is
synchronous only, while `external_ref`, `metadata` and `idempotency_key` work as in `run_tests()`. A
`RateLimitError` saying the runs are busy counted nothing, so send the suite again.

## List submissions

```python
import datetime

for entry in client.iter_submissions(external_ref="candidate-8841"):
    print(entry["submission"], entry["status"], entry["verdict"], entry["score"])

page = client.submissions(problem="sum-two", verdict="accepted", since=datetime.date(2026, 11, 1), limit=50)
more = client.submissions(problem="sum-two", verdict="accepted", since=datetime.date(2026, 11, 1), limit=50,
                          cursor=page["next_cursor"])  # when next_cursor is not None
```

`client.submissions()` returns one page of your submissions, newest first, filtered by `ids`, `external_ref`,
`problem` (id or slug), `status`, `verdict`, `since` (included) and `until` (not included). `since` and `until`
take a `datetime` (one without a zone is UTC), a `date` or ISO 8601 text. Each entry has the verdict, score and
tests passed, but not the tests or receipt: `client.submission(id)` has those. `client.iter_submissions()`
fetches page after page for you. With `ids` alone, every id comes back on one page. Draft tests, reference checks
and Submits under a test session made in the dashboard are not listed.

## Judge against a stored problem

A stored problem keeps its tests, checker and limits on the server, so you send only the candidate's
code and the hidden tests never leave it.

```python
client.create_problem({
    "title": "Sum of two numbers",
    "slug": "sum",
    "languages": ["python-3.14"],
    "tests": [
        {"name": "sample", "input": "1 2\n", "expected_output": "3\n", "hidden": False},
        {"input": "5 7\n", "expected_output": "12\n"},  # tests are hidden unless you say otherwise
    ],
})
client.publish_problem("sum")  # no reference solution, so it is published at once

result = client.judge("sum", "python-3.14", "a, b = map(int, input().split())\nprint(a + b)")
print(result["verdict"], result["score"], result["problem"], result["version"])
```

Pass `tests=[1, 3]` (1-based positions) to judge only those tests; the submission's mode becomes
`"tests"`. Stored-problem lookups and lists show `mode` and `rejudge_of`.

`judge` takes the problem's id (`prob_...`) or your slug for it. `mode="samples"` judges only the
visible tests, and `async_`, `callback_url`, `external_ref`, `metadata` and `idempotency_key` work as
above. Problems are plain dicts, as the
[stored problems docs](https://onlinecompiler.io/docs/problems?utm_source=pypi) describe them:
`problems()`, `problem()`, `create_problem()`, `update_problem()`, `replace_draft()`,
`publish_problem()`, `archive_problem()`, `restore_problem()` and `public_problem()`.

A problem's data files (a SQLite database, a CSV, any bytes the tests read) go in the draft's `files`
(`{"name", "content", "encoding"}`) through `create_problem()` or `replace_draft()`, or one at a time as
raw bytes, which suits large or binary files. Owner bodies list them as `files: [{name, bytes, sha256}]`.

```python
client.put_draft_file("sum", "orders.db", open("orders.db", "rb").read())  # str is sent as UTF-8
client.delete_draft_file("sum", "orders.db")  # NotFoundError when absent
```

### Re-judge earlier submissions

After publishing a new version, run earlier submissions against it. Each is queued as a new async
submission with `rejudge_of` set to the original; at most 100 a call.

```python
result = client.rejudge(
    "sum",
    version=2,  # default: the current version
    submissions=["sub_..."],  # default: every earlier submission that qualifies
    notify=True,  # send the original's callback_url a webhook; default False
)
print(len(result["queued"]), result["remaining"])
```

Call again while `remaining` is above 0. Every argument is optional.

## Sessions for the widget

A session lets one candidate's [embedded editor](https://onlinecompiler.io/docs/widget?utm_source=pypi)
Submit to a stored problem without your API key ever reaching the browser. Create it on your server and
put only its token in the page:

```python
problem_id = client.problem("sum")["problem"]  # the widget takes the id, never the slug
session = client.create_session(problem_id, "attempt-4821", max_submits=2, expires_in=3600)

html = (f'<div class="runcode" data-key="YOUR_WIDGET_KEY" data-problem="{problem_id}" '
        f'data-session="{session["token"]}"></div>\n'
        '<script src="https://onlinecompiler.io/widget.js"></script>')
```

The token is shown only this once and the client never logs it or puts it in an error, so keep it out
of your own logs too. `external_ref` should be an opaque id: the candidate's page sees it. Record the
score from the webhook or `client.submission(id)`, never from the browser.

Each Submit's signed receipt is shared, so the widget links to its public page; the page shows
`external_ref` and your account reference, never the code, tests or output. Pass `share_receipts=False` to keep a
session's receipts private.
`client.session(problem_id, session["session"])` shows the Submits used and the submission ids, and
`client.revoke_session(problem_id, session["session"])` ends it at once.

## Verify webhooks

An async submission with a `callback_url` (or a callback URL on the API key) gets one signed
`submission.finished` event. Check it before you trust it.

Verify the raw request body exactly as received: a body that was parsed and serialized again never
matches. In Flask that is `request.get_data()`, in Django `request.body`, in FastAPI
`await request.body()`.

```python
import os

from flask import Flask, abort, request
from onlinecompiler import verify_webhook

app = Flask(__name__)
SECRET = os.environ["ONLINECOMPILER_WEBHOOK_SECRET"]  # whsec_..., shown with the API key in your dashboard

@app.post("/hooks/oc")
def webhook():
    if not verify_webhook(SECRET, request.get_data(), request.headers):
        abort(400)
    event = request.get_json()
    print(event["submission"], event["status"], event["result"] and event["result"]["verdict"])
    return "", 204  # answer quickly; deduplicate on event["delivery"]
```

`verify_webhook(secret, raw_body, headers, tolerance=300)` needs no client and no network. It takes any
mapping of headers (Django's, Flask's, FastAPI's or a dict), compares in constant time, accepts either
signature while a secret is being rotated, takes a list of secrets while you rotate yours, and rejects a
timestamp more than `tolerance` seconds (default 300) from now. Anything missing or malformed returns
`False`. See the [webhooks docs](https://onlinecompiler.io/docs/webhooks?utm_source=pypi).

## Verify receipts

Every judged submission comes with a receipt signed with Ed25519: proof of what ran, against which
tests, and what came out. Anyone can check it with the published keys.

```bash
pip install "onlinecompiler[receipts]"
```

```python
from onlinecompiler import verify_receipt

receipt = client.submission("sub_8f2Kq7LmT3xR9vWb1cYd4hNe")["receipt"]
print(client.verify_receipt(receipt))  # fetches the published keys once, no API key needed

keys = client.receipt_keys()           # or save them and verify offline later
print(verify_receipt(receipt, keys))   # False once any field has been changed
```

Both return `False` for `None`, which a submission that was not judged has as its receipt. Without the
extra, verifying raises an error that says what to install. Store and deduplicate receipts by their
`submission` id (or the whole receipt), not by the signature string. `canonical_json(value)` returns
the canonical bytes receipts are signed over, for reproducing their hashes as the
[receipts docs](https://onlinecompiler.io/docs/receipts?utm_source=pypi) describe.

## Files in and out

```python
import base64

code = """
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv("sales.csv")
df.plot(x="month", y="units")
plt.show()   # saved as figure-1.png in $OUTPUT_DIR
"""
result = client.run("python-3.14", code, files=[{"name": "sales.csv", "content": open("sales.csv").read()}])
for f in result["files"]:
    with open(f["name"], "wb") as out:
        out.write(base64.b64decode(f["content_base64"]))
```

Input files sit in the program's working directory; send binary ones with `"encoding": "base64"`.
Files the program writes to `$OUTPUT_DIR` come back in `files`.

## Queue runs and get results by callback

```python
queued = client.submit("g++-15", source, input="3\n", extra_params={"submission_id": 981})
print(queued["id"])
```

`submit` returns at once. When the run finishes, its result is POSTed to the callback URL set on your
API key in the dashboard, with your `extra_params` echoed back.

## Other calls

- `client.compilers()`: every compiler id and language name, with `pack` on a test pack (no key needed).
- `client.usage()`: this month's runs and what's left, for the account and for the key.
- `client.limits()`: per-run limits and default account limits (no key needed).

## Errors

```python
from onlinecompiler import OnlineCompilerError, RateLimitError

try:
    client.run("python-3.14", "print(1)")
except RateLimitError as e:          # HTTP 429
    print(e.message)                 # which limit was hit, and how to raise it
except OnlineCompilerError as e:     # anything else, including AuthenticationError
    print(e.status, e.message)       # status is 0 when the API could not be reached
```

`AuthenticationError` covers a missing, unknown or unauthorized key; `NotFoundError` a submission,
problem or session that does not exist or is not yours. A conflict, such as an archived problem or an
`Idempotency-Key` reused with another body, is an `OnlineCompilerError` with status 409 and the API's
message.

The client never retries a request by itself, because every run the API accepts counts toward your
allowance. `OnlineCompiler(api_key=None, *, base_url="https://api.onlinecompiler.io", timeout=90.0)`
takes the key, the API address and a timeout in seconds.

## Links

[Docs](https://onlinecompiler.io/docs?utm_source=pypi) ·
[Languages](https://onlinecompiler.io/languages?utm_source=pypi) ·
[Limits](https://onlinecompiler.io/docs/limits?utm_source=pypi) ·
[Pricing](https://onlinecompiler.io/pricing?utm_source=pypi) ·
[Security](https://onlinecompiler.io/security?utm_source=pypi) ·
[Changelog](https://onlinecompiler.io/changelog?utm_source=pypi)

MIT licensed. OnlineCompiler.io is a product of RunCode Inc.
