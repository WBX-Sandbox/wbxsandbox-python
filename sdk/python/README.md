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
sb = Sandbox.create(template="base", name="my-project", on_timeout="pause", idle_timeout=600)
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

signed = job.signed_receipt()                     # keep or send the whole envelope
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

Documentation: https://sandbox.wbx.cz/docs · Pricing: https://sandbox.wbx.cz/pricing

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
