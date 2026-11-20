# wbxsandbox – Python SDK for WBX Sandbox

[![PyPI](https://img.shields.io/pypi/v/wbxsandbox)](https://pypi.org/project/wbxsandbox/)

The official Python SDK for [WBX Sandbox](https://sandbox.wbx.cz): isolated Linux microVMs for
AI agents and code execution. Every sandbox is a Firecracker virtual machine with its own kernel,
disk and network rules, running on servers in the EU. One small package with **no dependencies**
– only the Python standard library, Python 3.9+.

```bash
pip install wbxsandbox
export WBXSANDBOX_API_KEY=...        # create one in the portal: API keys
```

```python
from wbxsandbox import Sandbox, run_job

with Sandbox.create(template="base") as sb:            # a fresh microVM
    r = sb.run("python3 -c 'print(6 * 7)'")
    print(r.exit_code, r.stdout)                       # 0 42
    sb.files.write("/home/user/hello.txt", "hi")
# leaving the block destroys the sandbox

job = run_job(command="python3 -c 'print(sum(range(10**7)))'", max_cost="0.10")
print(job.wait().summary()["status"])                  # runs without an open connection
```

## What the SDK covers

| Area | | Docs |
|---|---|---|
| **Sandboxes** | Create, connect later, list, extend the lifetime, pause / resume, destroy; templates, environment variables, labels, compute class | [Create and manage](https://sandbox.wbx.cz/docs/sandboxes) |
| **Commands and code** | `run()` with exit code and output, live output callbacks, `stream()` events, `run_code()` for Python, Node.js and bash | [Commands](https://sandbox.wbx.cz/docs/sandboxes/commands) |
| **Files** | Write, read, list, create directories, remove | [Files](https://sandbox.wbx.cz/docs/sandboxes/files) |
| **Sleep and wake** | Named sandboxes that fall asleep with their files (`on_timeout="pause"`, `sleep()`, `keep(days)`) and wake on the next request | [Sleep and wake](https://sandbox.wbx.cz/docs/sandboxes/sleep) |
| **Network and IPv6** | No network, an allowlist, blocked destinations; a public IPv6 address per sandbox (`ip="dual"`) | [Network](https://sandbox.wbx.cz/docs/sandboxes/network) |
| **Public URLs** | HTTPS address with an access token for any port (`get_url(port)`) | [Public URLs](https://sandbox.wbx.cz/docs/sandboxes/urls) |
| **Background jobs** | `run_job()` with result files, price limit, retries, idempotency; `wait()`, LLM-friendly log digest and test results, SHA-256-checked downloads, files passed between jobs | [Jobs](https://sandbox.wbx.cz/docs/jobs) |
| **Batches** | `run_batch()` – up to 1,000 jobs with a concurrency and total price limit, dependencies, reports as JSON or CSV | [Batches](https://sandbox.wbx.cz/docs/jobs/batches) |
| **WebAssembly jobs** | Run a WASI module as a job without a virtual machine | [WebAssembly](https://sandbox.wbx.cz/docs/jobs/wasm) |
| **Signed receipts** | `job.signed_receipt()` and `verify_receipt()` – an Ed25519-signed record of the command, template build and checksums | [Receipts](https://sandbox.wbx.cz/docs/jobs/receipts) |
| **Webhooks** | Signed notification when a job or batch finishes; `verify_webhook()` | [Webhooks](https://sandbox.wbx.cz/docs/jobs/webhooks) |
| **Budgets and compute** | Monthly and daily spending limits, compute classes and your default class | [Budgets](https://sandbox.wbx.cz/docs/jobs/budget) · [Compute classes](https://sandbox.wbx.cz/docs/jobs/compute) |
| **Metrics** | CPU, memory, network and disk measured by the host every 10 s: `metrics_history()` and the live `metrics_stream()` | [Metrics](https://sandbox.wbx.cz/docs/sandboxes/metrics) |
| **Desktop & Computer Use** (beta) | `sb.computer` – screenshots, mouse, keyboard and scroll on a Linux desktop; enabled per team on request | [Desktop](https://sandbox.wbx.cz/docs/sandboxes/desktop) |
| **Terminal** | The `wbx connect SB_ID` command opens an interactive shell in a running sandbox | [Terminal](https://sandbox.wbx.cz/docs/sandboxes/terminal) |

Errors are raised as `SandboxError` with the HTTP status, a machine-readable `code` and
`retry_after`.

**Full usage, configuration and examples:** [`sdk/python/README.md`](sdk/python/README.md) ·
SDK reference: https://sandbox.wbx.cz/docs/sdk · More examples:
[WBX-Sandbox/examples](https://github.com/WBX-Sandbox/examples) · TypeScript SDK:
[WBX-Sandbox/wbxsandbox-js](https://github.com/WBX-Sandbox/wbxsandbox-js) ·
Pricing: https://sandbox.wbx.cz/pricing

## About this repository

The SDK is developed together with the WBX Sandbox platform. This repository holds the published
copy of `sdk/python` from a platform release (see [`RELEASE`](RELEASE)) and publishes it to PyPI.
The SDK's `major.minor` follows the platform release line, so `RELEASE` also lets the version test
check that the two match.

Releases: push a tag `vX.Y.Z` equal to `version` in `sdk/python/pyproject.toml`; the Release
workflow runs the tests, builds the package and publishes it to PyPI with trusted publishing.

## License

MIT – see [LICENSE](LICENSE).
