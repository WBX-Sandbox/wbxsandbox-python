# WBX Sandbox – Python SDK

Python SDK for [WBX Sandbox](https://sandbox.wbx.cz): isolated cloud sandboxes for AI agents
and code execution. Each sandbox is a Firecracker microVM with its own Linux kernel, disk and
network rules. No dependencies – only the Python standard library (Python 3.9+).

What it covers:

- **Sandboxes** – commands (also streamed), code in Python, Node.js or bash, files, public HTTPS
  URLs of ports, network rules and IPv6, pause, sleep and wake with named sandboxes
- **Background jobs and batches** – no open connection, result files with SHA-256 checks, price
  limits and budgets, log digests for LLMs, webhooks, WebAssembly jobs
- **Signed execution receipts** – verify what ran, on which template build, with which inputs
- **Metrics** – CPU, memory, network and disk measured by the host, history and live stream
- **Snapshots and clones** – save a sandbox disk, start new sandboxes from it, or make up to
  10 copies of a sandbox in one call; copies keep the source's lifetime and network settings
- **Desktop & Computer Use** (paid plans, or after topping up at least US$15) – screenshots, mouse and keyboard
- **`wbx connect`** – an interactive shell in a running sandbox

Full reference: https://sandbox.wbx.cz/docs/sdk

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
sb.sleep()                                   # files and running programs kept, no compute billed
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

The key comes from `WBXSANDBOX_API_KEY`, or from a file with `--key-file PATH`
(`--key-file -` reads it from standard input) – not from the command line, where other
users could see it in the process list.

Behind an HTTP proxy, set `HTTPS_PROXY` – the terminal connects through it.

Metrics – CPU, memory, network and disk of a running sandbox, measured by the host every
10 seconds, one hour of history (feature `metrics_history`, on by default):

```python
from wbxsandbox import Sandbox, features

print(features())                         # {"metrics_history": True, "desktop": False, "snapshots": True, "clones": True}
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

Snapshots and clones – save a sandbox disk and start copies of it; see
[Snapshots and clones](#snapshots-and-clones) below.

Desktop control (template `desktop`, feature `desktop` – on paid plans or after topping up at least US$15):

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
| `code` | machine-readable code from the error body (`quota`, `queue_full`, `feature_disabled`, `human_in_control`, `snapshot_limit`, …) or `None` |
| `retry_after` | seconds from the `Retry-After` header (some 429 and 503 responses) or `None` |
| `body` | the parsed JSON error body (`dict`), the raw text, or `None` |

The TypeScript SDK has the same fields on `APIError` (`status`, `code`, `retryAfter`, `body`).

Timeouts: the SDK waits up to 120 s for an answer unless the call sets its own limit –
`run()` the command's `timeout` + 30 s, desktop actions 5 minutes, `metrics_stream()`
16 minutes, `job.download()` an hour. Safe requests (GET, HEAD, DELETE, PUT and job
submissions, which always carry an idempotency key) are retried up to twice after a connection
error, 502/503/504 or 429 with `Retry-After`. The SDK talks to `https://` addresses only
(plain `http://` just for localhost, or with `allow_insecure=True`) and does not follow
redirects, so the API key never goes to another address.

Documentation: https://sandbox.wbx.cz/docs · TypeScript SDK: `npm install wbxsandbox`
(https://sandbox.wbx.cz/docs/sdk/typescript) · Pricing: https://sandbox.wbx.cz/pricing

## Snapshots and clones

A **snapshot** is a copy of a sandbox disk: installed packages, your files and everything else
written to the disk (also `.env` files or shell history) – no memory, no running programs and no
environment variables passed with `env`. It belongs to your account only, stays until you
delete it (also after the source sandbox ends) and its size is billed as storage, like a
sleeping sandbox. A **clone** is a snapshot plus 1–10 new sandboxes started from it in one call.

Snapshots and clones are available on every plan (features `snapshots` and `clones`, shown by
`features()`). If they have been turned off for your account, the calls raise `SandboxError`
403 with code `feature_disabled`.

```python
from wbxsandbox import Sandbox, Snapshot

sb = Sandbox.create(template="base", timeout=1800, on_timeout="pause")
sb.run("pip install pandas").check()

# snapshot of a running, paused or sleeping sandbox (a running one keeps running)
snap = sb.snapshot(name="after-setup")          # Snapshot(id="snap_...", size_bytes=..., ...)

# a new sandbox from the snapshot: settings come from this call, not from the snapshot
sb2 = Sandbox.create(from_snapshot=snap.id, timeout=600, env={"TOKEN": "..."})
# or: snap.create_sandbox(timeout=600)

for s in Snapshot.list():
    print(s.id, s.name, s.source_sandbox, s.size_bytes, s.created_at)
Snapshot.get("snap_...").delete()               # or snap.delete()
```

Clone – the copies keep the source's lifetime and network settings unless you pass them:

```python
copies = sb.clone(count=3)                      # 30 minutes, sleep after it – as sb
print([c.id for c in copies], copies.errors)    # errors: copies that could not be created
                                                # ({"index", "name", "status", "code", "error"})
one = sb.clone(timeout=3600, name="long-run")   # own lifetime, the rest as sb
safe = sb.clone(count=2, internet=False)        # network can only be narrowed
kept = sb.clone(count=2, keep_snapshot=True, snapshot_name="base-env")
print(kept.snapshot_id, kept.snapshot_kept)     # the snapshot stays and can start more sandboxes
```

What a copy takes from the source sandbox when the argument is left out:

| argument | without it |
|---|---|
| `timeout` | the source's lifetime (as created, or as last set with `set_timeout()`) |
| `on_timeout`, `idle_timeout` | the source's (sleep or destroy after the lifetime; inactivity limit with `"pause"`) |
| `keep_days` | the source's, at most what your plan allows |
| `compute_class` | the source's class (your account's default class if you can no longer use the source's) |
| `internet`, `allow`, `block`, `ip` | the source's network access and IP version. Passed values can only narrow it: the stricter mode wins, blocked destinations add up, two allowlists give only destinations allowed by both; `ip="ipv4"` gives copies of an IPv6 source no IPv6 |
| `env`, `metadata` | not taken over – pass what the copies need |

A zero or empty value counts as left out: `timeout=0`, `keep_days=0`, `idle_timeout=0` and
`compute_class=""` keep the source's setting – pass the value you want instead.

`name` names the copies (`<name>-1` … `<name>-N` for several). A sandbox started with
`from_snapshot` takes nothing from the source sandbox – only the files of the snapshot.

Every copy and every sandbox from a snapshot gets a new ID, its own IP addresses and a new
identity (new machine-id, no SSH host keys of the source); it restarts once during its first
start, so starting it takes a few seconds longer. Copies start on the server that holds the
snapshot and count towards your concurrent-sandbox, vCPU and memory limits.

Limits and errors:

- Number of snapshots: set by your plan (by default Free 3, Solo 10, Pro 50, Team 200) –
  `SandboxError` 429, code `snapshot_limit`. Copies made by a clone do not keep a snapshot
  unless `keep_snapshot=True`.
- An archived sandbox must be woken up first – 409, code `sandbox_archived`.
- `clone()` returns the copies that were created and lists the others in `.errors`; it raises
  `SandboxError` only when no copy could be created.
- 503 `snapshot_node_unavailable`: the server with the snapshot is full or not responding;
  503 `clone_network_unknown`: the network settings of the source cannot be read right now –
  retry shortly.
- The SDK waits up to 630 s for a snapshot, 300 s for a sandbox from a snapshot and 930 s for
  a clone.

Details: https://sandbox.wbx.cz/docs/sandboxes/snapshots

## Published apps

Put an app running in a sandbox at a permanent public address – anyone can open it without a
token. The sandbox is kept while the app is published, sleeps without visitors and wakes on the
next visit; when nothing listens on the port, `start_command` runs. Paid plans
(`SandboxError` 403 `feature_disabled` otherwise).

```python
from wbxsandbox import App

app = sb.publish("dog-club", 8000, start_command="python3 app.py")
print(app.url)                                   # https://dog-club.box.wbxbox.net/

d = app.add_domain("booking.example.com")        # your own domain
print(d["state"], d["dns"]["cname"])             # pending – create this CNAME record

for a in App.list():
    print(a.name, a.url, [(x["domain"], x["state"]) for x in a.domains])
app.unpublish()                                  # the sandbox stays
```

Details: https://sandbox.wbx.cz/docs/sandboxes/apps

## Versioning

The SDK has its own version. Its `major.minor` follows the platform release line
(SDK 0.16.x works with platform 0.16.x); the patch number is raised only when the SDK
itself changes. `pyproject.toml` and `wbxsandbox.__version__` always carry the same
version (checked by `tests/test_sdk.py`).

## Tests

    cd sdk/python && python3 -m unittest discover -s tests

The tests do not need network access.

## License

MIT – see `LICENSE`.
