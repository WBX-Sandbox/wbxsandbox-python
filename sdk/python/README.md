# WBX Sandbox – Python SDK

Python SDK for [WBX Sandbox](https://sandbox.wbx.cz): isolated cloud sandboxes for AI agents
and code execution. Each sandbox is a Firecracker microVM with its own Linux kernel, disk and
network rules. No dependencies – only the Python standard library (Python 3.9+).

## Install

    pip install wbxsandbox

## Configure

Create an API key in the portal (API keys) and set it in the environment:

    export WBXSANDBOX_API_KEY=...
    # optional, defaults to https://sandbox.wbx.cz
    export WBXSANDBOX_URL=https://sandbox.wbx.cz

Both can also be passed as `url=` and `api_key=` arguments.

## Quick start

```python
from wbxsandbox import Sandbox

with Sandbox.create(template="base", timeout=300) as sb:
    r = sb.run("python3 -c 'print(1 + 1)'")
    print(r.exit_code, r.stdout)          # 0 "2\n"
    sb.files.write("/home/user/data.txt", "hello")
    print(sb.files.read_text("/home/user/data.txt"))
# the sandbox is destroyed when the block ends
```

A named sandbox that falls asleep instead of being destroyed – its files are there next time:

```python
sb = Sandbox.create(template="base", name="my-project", on_timeout="pause", idle_timeout=600,
                    keep_days=30)  # kept 30 days after it falls asleep (up to your plan's limit)
sb.files.write("/home/user/notes.txt", "kept while asleep")
sb.sleep()                                   # files kept, programs stop, no compute billed
sb = Sandbox.create(name="my-project")       # same sandbox, woken up
```

Background jobs and batches:

```python
from wbxsandbox import run_job

job = run_job(command="python3 -c 'print(42)'", template="base", timeout_seconds=600)
job.wait()
print(job.summary()["status"])
```

Signed execution receipts – after the job ends, WBX signs its receipt (Ed25519): command,
exact template build, input and output file checksums. Anyone with the public key can check it
was not edited (`pip install "wbxsandbox[verify]"`):

```python
from wbxsandbox import receipt_keys, verify_receipt

signed = job.signed_receipt()                     # keep or send it; the portal's "Download signed receipt" file works too
key = receipt_keys()[0]["public_key"]             # pin this key instead of fetching it each time
receipt = verify_receipt(signed, key)["execution_receipt"]   # ValueError if anything was changed
print(receipt["template_build"]["image_digest"], receipt["artifacts"])
```

The signature proves WBX issued the receipt unchanged – not that the computation could not
have been altered.

IPv6 – the sandbox gets its own public IPv6 address (`"dual"`, or `"ipv6"` for IPv6-only internet):

```python
with Sandbox.create(template="base", ip="dual") as sb:
    print(sb.ipv6)
```

Interactive terminal in a running sandbox (installed as the `wbx` command):

    wbx connect sb_...

Behind an HTTP proxy, set `HTTPS_PROXY` – the terminal connects through it.

Metrics – CPU, memory, network and disk of a running sandbox, measured by the host every
10 seconds, one hour of history (feature `metrics_history`, on by default):

```python
from wbxsandbox import Sandbox, features

print(features())                         # {"metrics_history": True, "desktop": False}
sb = Sandbox.connect("sb_...")            # a running sandbox
h = sb.metrics_history()                  # {"state", "interval_s", "samples": [{"t", "cpu_pct", "mem_mb", ...}]}
for ev in sb.metrics_stream(since=0):     # since=0: the stored hour first, then live samples
    if ev["event"] == "sample":
        print(ev["data"]["cpu_pct"], ev["data"]["mem_mb"])
    elif ev["event"] == "end":            # after 15 minutes, or when the sandbox stops
        break
```

Neither call wakes a sleeping sandbox. A feature that is not enabled for your account raises
`SandboxError` with status 403 and code `feature_disabled`.

Desktop control (template `desktop`, feature `desktop` – beta, enabled on request):

```python
from wbxsandbox import ComputerActionError

with Sandbox.create(template="desktop") as sb:
    sb.computer.click(640, 400)
    sb.computer.type("hello")
    sb.computer.scroll(640, 400, dy=5)    # pass dx / dy by name – see below
    sb.computer.screenshot().save("screen.png")
    try:
        sb.computer.key("ctrl+c")
    except ComputerActionError as e:      # a subclass of SandboxError
        print(e.failed_index, e.results)
```

`scroll(x, y, dy=0, dx=0)` takes **dy (vertical) before dx** as positional arguments – the
TypeScript SDK's `scroll(x, y, dx, dy)` has them the other way round. Pass them by name
(`dy=5`, `dx=-2`) so the code reads the same in both SDKs.

`sb.computer.actions([...])` returns the response also when an action failed (`ok` is
`False`, with `error`, `failed_index` and `results`); the shortcuts (`click`, `type`, `key`, …)
raise `ComputerActionError` with `failed_index` and `results` instead. Its `status` is 200 (the
server answered), never 0.

Errors – every API error is a `SandboxError`:

```python
from wbxsandbox import SandboxError

try:
    Sandbox.create(template="base")
except SandboxError as e:
    if e.status == 429 and e.code == "quota":
        print("stop another sandbox first; retry in", e.retry_after, "s")
    else:
        raise
```

| attribute | meaning |
|---|---|
| `status` | HTTP status; `0` = the server could not be reached (or the SDK found the error itself) |
| `message` | human-readable message |
| `code` | machine-readable code from the error body (`quota`, `queue_full`, `feature_disabled`, `human_in_control`, …) or `None` |
| `retry_after` | seconds from the `Retry-After` header (some 429 and 503 responses) or `None` |
| `body` | the parsed JSON error body (`dict`), the raw text, or `None` |

The TypeScript SDK has the same fields on `APIError` (`status`, `code`, `retryAfter`, `body`).

Timeouts: the SDK waits up to 120 s for an answer unless the call sets its own limit –
`run()` the command's `timeout` + 30 s, desktop actions 5 minutes, `metrics_stream()`
16 minutes, `job.download()` an hour. Failed requests are not retried automatically.

Documentation: https://sandbox.wbx.cz/docs · TypeScript SDK: `npm install wbxsandbox`
(https://sandbox.wbx.cz/docs/sdk/typescript) · Pricing: https://sandbox.wbx.cz/pricing

## Versioning

The SDK has its own version. Its `major.minor` follows the platform release line
(SDK 0.15.x works with platform 0.15.x); the patch number is raised only when the SDK
itself changes. `pyproject.toml` and `wbxsandbox.__version__` always carry the same
version (checked by `tests/test_sdk.py`).

## Tests

    cd sdk/python && python3 -m unittest discover -s tests

The tests do not need network access.

## License

MIT – see `LICENSE`.
