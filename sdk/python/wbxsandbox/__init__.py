"""WBX Sandbox – Python SDK.

Isolated cloud sandboxes (Firecracker microVMs) for AI agents. No dependencies –
only the Python standard library.

    from wbxsandbox import Sandbox

    with Sandbox.create(template="base") as sb:
        r = sb.run("python3 -c 'print(1 + 1)'")
        print(r.stdout)                      # "2\\n"
        sb.files.write("/home/user/data.txt", "hello")
        print(sb.files.read_text("/home/user/data.txt"))
    # the sandbox is destroyed when the block ends

Background jobs – one call, no open connection, results kept after the sandbox is gone:

    from wbxsandbox import run_job

    job = run_job(command="python analyze.py", template="base", timeout_seconds=3600,
                  max_cost="0.10", save_artifacts=True)
    print(job.id)                      # returns immediately
    job.wait()                         # or come back later: get_job(job.id)
    print(job.summary()["status"], job.receipt()["exit_code"])
    job.download("output/result.json", "result.json")

Batches (fan-out) – many jobs, a concurrency limit and a total price limit:

    from wbxsandbox import run_batch

    batch = run_batch([{"command": f"python worker.py {i}"} for i in range(100)],
                      defaults={"template": "base", "save_artifacts": True},
                      max_concurrency=10, max_total_cost="2.00")
    batch.wait()
    print(batch.summary()["status"])   # completed / partial / failed
    rows = batch.report()["jobs"]      # one row per job

WebAssembly jobs (runtime="wasi", no network, fast start):

    job = run_job(wasm_module=open("tool.wasm", "rb").read(), wasm_args=["--fast"],
                  inputs=[job_input(previous.id, "output/data.csv")], save_artifacts=True)

Configuration (environment variables, or url= / api_key= arguments):
    WBXSANDBOX_URL       API address, e.g. https://sandbox.wbx.cz
    WBXSANDBOX_API_KEY   your API key
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Union

__version__ = "0.15.2"
__all__ = ["Sandbox", "CommandResult", "SandboxError", "Client", "Job", "run_job", "get_job", "list_jobs",
           "Batch", "run_batch", "get_batch", "job_input", "batch_input", "get_budget", "set_budget", "runtimes",
           "compute_preferences", "set_default_compute_class", "wait_jobs", "webhook_secret", "verify_webhook"]


class SandboxError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False   # output longer than the server limit was cut off

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def check(self) -> "CommandResult":
        """Raise SandboxError unless the command succeeded (exit code 0, no timeout)."""
        if not self.ok:
            why = "timed out" if self.timed_out else f"exited with code {self.exit_code}"
            raise SandboxError(0, f"command {why}: {self.stderr.strip()[:500]}")
        return self


class Client:
    """Low-level HTTP client for the WBX Sandbox REST API."""

    def __init__(self, url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 120):
        self.url = (url or os.environ.get("WBXSANDBOX_URL", "https://sandbox.wbx.cz")).rstrip("/")
        self.api_key = api_key or os.environ.get("WBXSANDBOX_API_KEY", "")
        self.timeout = timeout

    def request(self, method: str, path: str, body: Union[None, dict, bytes] = None,
                query: Optional[dict] = None, timeout: Optional[float] = None, stream: bool = False):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"X-API-Key": self.api_key}
        data = None
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw).get("error", raw)
            except ValueError:
                msg = raw
            raise SandboxError(e.code, msg) from None
        except urllib.error.URLError as e:
            raise SandboxError(0, f"cannot connect to {self.url}: {e.reason}") from None
        if stream:
            return resp
        with resp:
            raw = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw) if raw else None
            return raw


class Files:
    """Files inside the sandbox (sandbox.files)."""

    def __init__(self, sb: "Sandbox"):
        self._sb = sb

    def read(self, path: str) -> bytes:
        """Download a file as bytes."""
        return self._sb._c.request("GET", f"/v1/sandboxes/{self._sb.id}/files", query={"path": path})

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Download a text file."""
        return self.read(path).decode(encoding)

    def write(self, path: str, data: Union[str, bytes]) -> None:
        """Upload a file (missing directories are created)."""
        if isinstance(data, str):
            data = data.encode()
        self._sb._c.request("PUT", f"/v1/sandboxes/{self._sb.id}/files", body=data, query={"path": path})

    def remove(self, path: str) -> None:
        """Delete a file or a directory."""
        self._sb._c.request("DELETE", f"/v1/sandboxes/{self._sb.id}/files", query={"path": path})

    def list(self, path: str = "/home/user") -> List[dict]:
        """List a directory: [{"name": ..., "is_dir": bool, "size": bytes}, ...]."""
        return self._sb._c.request("GET", f"/v1/sandboxes/{self._sb.id}/ls", query={"path": path})


@dataclass
class Sandbox:
    id: str
    template: str = ""
    state: str = ""
    info: Dict = field(default_factory=dict)
    _c: Client = field(default=None, repr=False)

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, template: str = "base", timeout: int = 300, metadata: Optional[Dict[str, str]] = None,
               vcpu: int = 0, mem_mb: int = 0, url: Optional[str] = None, api_key: Optional[str] = None,
               internet: bool = True, allow: Optional[List[str]] = None, block: Optional[List[str]] = None,
               env: Optional[Dict[str, str]] = None, compute_class: Optional[str] = None,
               placement: Optional[str] = None, dependency_cache: Optional[bool] = None,
               optimize_for: Optional[str] = None) -> "Sandbox":
        """Create a new sandbox.

        template  what is preinstalled ("base", "code-interpreter", "browser", "node", ...)
        timeout   lifetime in seconds; extend it any time with set_timeout()
        metadata  your own labels, returned when you list sandboxes
        env       environment variables for every command in the sandbox (e.g. API keys)
        internet  False = no network at all
        allow     only these domains/addresses are reachable, e.g. ["pypi.org", "files.pythonhosted.org"]
        block     additional blocked destinations
        compute_class     economy | balanced | performance | memory | dedicated – without it the server uses
                          your account's default class (Settings → Compute Preferences); never downgraded
        optimize_for      price | balanced | performance – picks the server inside the class (never changes it)
        placement         auto | fast | spread
        dependency_cache  False = do not use the node's verified package cache
        Network settings can only make your team's policy stricter, never looser.
        """
        c = Client(url, api_key)
        body = {"template": template, "timeout_s": timeout, "metadata": metadata or {},
                "vcpu": vcpu, "mem_mb": mem_mb}
        if env:
            body["env"] = dict(env)
        if compute_class:
            body["compute_class"] = compute_class
        if placement:
            body["placement"] = placement
        if optimize_for:
            body["optimize_for"] = optimize_for
        if dependency_cache is not None:
            body["dependency_cache"] = bool(dependency_cache)
        if not internet:
            body["network"] = {"mode": "none"}
        elif allow:
            body["network"] = {"mode": "allowlist", "allow": list(allow), "block": list(block or [])}
        elif block:
            body["network"] = {"mode": "full", "block": list(block)}
        d = c.request("POST", "/v1/sandboxes", body=body)
        return cls._from(d, c)

    @classmethod
    def connect(cls, sandbox_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> "Sandbox":
        """Attach to an existing sandbox by its id (e.g. in the next turn of a conversation)."""
        c = Client(url, api_key)
        return cls._from(c.request("GET", f"/v1/sandboxes/{sandbox_id}"), c)

    @classmethod
    def list(cls, url: Optional[str] = None, api_key: Optional[str] = None) -> List["Sandbox"]:
        """Your running and paused sandboxes."""
        c = Client(url, api_key)
        return [cls._from(d, c) for d in c.request("GET", "/v1/sandboxes")]

    @classmethod
    def _from(cls, d: dict, c: Client) -> "Sandbox":
        return cls(id=d["id"], template=d.get("template", ""), state=d.get("state", ""), info=d.get("info") or {}, _c=c)

    @property
    def files(self) -> Files:
        return Files(self)

    def kill(self) -> None:
        """Destroy the sandbox with its disk and memory (no error if it is already gone)."""
        try:
            self._c.request("DELETE", f"/v1/sandboxes/{self.id}")
        except SandboxError as e:
            if e.status != 404:
                raise

    def set_timeout(self, seconds: int) -> None:
        """Set the remaining lifetime to `seconds` from now."""
        self._c.request("POST", f"/v1/sandboxes/{self.id}/timeout", body={"timeout_s": seconds})

    def get_url(self, port: int) -> dict:
        """Public HTTPS URL of a TCP port in the sandbox (e.g. a web app you started there).

        Returns a dict with ``url``, ``url_with_token`` and ``token``. Open ``url_with_token``
        in a browser (the token moves to a cookie), or send the header ``X-WBX-Token: <token>``.
        The app must listen on 0.0.0.0, not only 127.0.0.1.
        """
        return self._c.request("GET", f"/v1/sandboxes/{self.id}/url", query={"port": int(port)})

    def pause(self) -> None:
        """Freeze the sandbox (memory and files are kept, you don't pay while paused)."""
        self.state = self._c.request("POST", f"/v1/sandboxes/{self.id}/pause")["state"]

    def resume(self) -> None:
        """Continue a paused sandbox exactly where it stopped."""
        self.state = self._c.request("POST", f"/v1/sandboxes/{self.id}/resume")["state"]

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.kill()

    # ------------------------------------------------------------ commands

    def _exec_body(self, cmd, env, cwd, timeout, stdin):
        return {"cmd": cmd, "env": env or {}, "cwd": cwd, "timeout_ms": int((timeout or 0) * 1000), "stdin": stdin}

    def run(self, cmd: str, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
            timeout: Optional[float] = 60, stdin: Optional[str] = None,
            on_stdout: Optional[Callable[[str], None]] = None,
            on_stderr: Optional[Callable[[str], None]] = None) -> CommandResult:
        """Run a shell command (sh -c) and wait for it to finish.

        timeout   seconds; the command is killed when it runs longer (result.timed_out = True)
        on_stdout / on_stderr   callbacks to receive output live while the command runs
        Returns CommandResult(exit_code, stdout, stderr, duration_ms, timed_out, truncated).
        """
        if on_stdout or on_stderr:
            out, err, last = [], [], {}
            for ev in self.stream(cmd, env=env, cwd=cwd, timeout=timeout, stdin=stdin):
                if ev["type"] == "stdout":
                    out.append(ev["data"])
                    on_stdout and on_stdout(ev["data"])
                elif ev["type"] == "stderr":
                    err.append(ev["data"])
                    on_stderr and on_stderr(ev["data"])
                elif ev["type"] == "exit":
                    last = ev
            return CommandResult(last.get("exit_code", -1), "".join(out), "".join(err),
                                 last.get("duration_ms", 0), last.get("timed_out", False))
        http_timeout = (timeout or 0) + 30 if timeout else None
        d = self._c.request("POST", f"/v1/sandboxes/{self.id}/exec",
                            body=self._exec_body(cmd, env, cwd, timeout, stdin),
                            timeout=http_timeout or 24 * 3600)
        return CommandResult(d["exit_code"], d["stdout"], d["stderr"], d.get("duration_ms", 0), d.get("timed_out", False),
                             bool(d.get("truncated", False)))

    def stream(self, cmd: str, env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
               timeout: Optional[float] = None, stdin: Optional[str] = None) -> Iterator[dict]:
        """Run a command and yield events as they happen:
        {"type": "start"|"stdout"|"stderr"|"exit", "data": ..., "exit_code": ...}"""
        resp = self._c.request("POST", f"/v1/sandboxes/{self.id}/exec/stream",
                               body=self._exec_body(cmd, env, cwd, timeout, stdin),
                               timeout=24 * 3600, stream=True)
        with resp:
            for line in resp:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def run_code(self, code: str, language: str = "python", timeout: Optional[float] = 60) -> CommandResult:
        """Run a piece of code: language "python", "node" or "bash"."""
        runners = {"python": ("/tmp/_wbx_code.py", "python3 /tmp/_wbx_code.py"),
                   "node": ("/tmp/_wbx_code.js", "node /tmp/_wbx_code.js"),
                   "bash": ("/tmp/_wbx_code.sh", "bash /tmp/_wbx_code.sh")}
        if language not in runners:
            raise ValueError(f"unsupported language {language!r} (use python, node or bash)")
        path, cmd = runners[language]
        self.files.write(path, code)
        return self.run(cmd, timeout=timeout)


# --------------------------------------------------------------------------- background jobs

TERMINAL = ("completed", "failed", "cancelled", "expired")
# cleanup_pending / recovery_pending: platforma ještě potvrzuje odstranění sandboxu (úloha běží dál)


class Job:
    """A background job (async). The job runs on WBX without an open connection; its log,
    result files and execution receipt are kept after the sandbox is removed."""

    def __init__(self, data: dict, client: Client):
        self._c = client
        self.data = data
        self.id: str = data["id"]

    @property
    def status(self) -> str:
        return self.data.get("status", "")

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def refresh(self) -> "Job":
        self.data = self._c.request("GET", f"/v1/jobs/{self.id}")
        return self

    def summary(self) -> dict:
        """Short status (state, exit code, duration, cost, file names, last log lines)."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/summary")

    def wait(self, timeout: Optional[float] = None, poll: float = 5.0) -> "Job":
        """Wait until the job ends. Uses server-side waiting (POST /v1/jobs/wait, up to 60 s per
        call), so it reacts within a second without polling. Raises TimeoutError."""
        end = time.time() + timeout if timeout else None
        while not self.refresh().done:
            left = (end - time.time()) if end else 60
            if left <= 0:
                raise TimeoutError(f"job {self.id} is still {self.status}")
            try:
                self._c.request("POST", "/v1/jobs/wait", body={"job_ids": [self.id], "timeout_s": max(1, min(60, int(left)))})
            except SandboxError as e:
                if e.status != 404:  # starší server bez /wait: klasické dotazování
                    raise
                time.sleep(poll)
        return self

    def digest(self, mode: str = "summary", max_chars: int = 6000) -> dict:
        """A compact log for LLM context: {"text", "test_results", "exit_code", ...}.
        mode: "summary" (test results + errors + end of log), "errors" or "tail"."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/logs", query={"mode": mode, "max_chars": max_chars})

    def test_results(self) -> List[dict]:
        """Recognised test results (pytest, go test, Jest, Vitest, cargo, PHPUnit, JUnit, mocha)."""
        return self.digest("summary", 500).get("test_results") or []

    def logs(self, offset: int = 0, limit: int = 65536) -> dict:
        """A chunk of the log: {"data", "next_offset", "complete", ...}. Negative offset = from the end."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/logs", query={"offset": offset, "limit": limit})

    def log_text(self) -> str:
        """The whole stored log."""
        out, off = [], 0
        while True:
            chunk = self.logs(off, 1 << 20)
            out.append(chunk["data"])
            if chunk["next_offset"] <= off or chunk["next_offset"] >= chunk["size"]:
                return "".join(out)
            off = chunk["next_offset"]

    def follow(self, poll: float = 2.0) -> Iterator[str]:
        """Yield new log text as the job runs, until it ends."""
        off = 0
        while True:
            chunk = self.logs(off)
            if chunk["data"]:
                yield chunk["data"]
            off = chunk["next_offset"]
            if chunk["complete"]:
                return
            if not chunk["data"]:
                time.sleep(poll)

    def receipt(self) -> dict:
        """The execution receipt (inputs, runtime, exit code, measured usage, file checksums, cost)."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/receipt")["execution_receipt"]

    def artifacts(self) -> List[dict]:
        return self._c.request("GET", f"/v1/jobs/{self.id}/artifacts")["artifacts"]

    def download(self, name: str, dest: Optional[str] = None) -> bytes:
        """Download a result file; verifies its SHA-256. Writes it to dest when given."""
        import hashlib
        path = "/".join(urllib.parse.quote(p, safe="") for p in name.split("/"))
        data = self._c.request("GET", f"/v1/jobs/{self.id}/artifacts/{path}", timeout=3600)
        want = next((a["sha256"] for a in self.artifacts() if a["name"] == name), None)
        if want and hashlib.sha256(data).hexdigest() != want:
            raise SandboxError(0, f"checksum mismatch for {name}")
        if dest:
            with open(dest, "wb") as f:
                f.write(data)
        return data

    def cancel(self) -> "Job":
        self.data = self._c.request("POST", f"/v1/jobs/{self.id}/cancel", body={})
        return self

    def retry(self, env: Optional[Dict[str, str]] = None, idempotency_key: Optional[str] = None) -> "Job":
        """Start a new job with the same inputs (pass env again – values are not stored)."""
        body = {"env": env or {}}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return Job(self._c.request("POST", f"/v1/jobs/{self.id}/retry", body=body), self._c)

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.status}>"


def job_input(job_id: str, name: str, as_name: Optional[str] = None) -> dict:
    """A result file of your earlier finished job, given to a new job as input/<as_name>."""
    d = {"job_id": job_id, "name": name}
    if as_name:
        d["as"] = as_name
    return d


def batch_input(index: int, name: str, as_name: Optional[str] = None) -> dict:
    """In run_batch: a result file of another job of the same batch (the job waits for it)."""
    d = {"from_index": index, "name": name}
    if as_name:
        d["as"] = as_name
    return d


def job_body(command: Optional[str] = None, template: Optional[str] = None, timeout_seconds: Optional[int] = None,
             artifacts: Optional[List[str]] = None, save_artifacts: bool = False,
             required_artifacts: Optional[List[str]] = None,
             env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
             internet: bool = True, allow: Optional[List[str]] = None,
             max_cost_eur: Union[None, str, float] = None, max_attempts: Optional[int] = None, retry_on: str = "system",
             metadata: Optional[Dict[str, str]] = None, idempotency_key: Optional[str] = None,
             compute_class: Optional[str] = None, placement: Optional[str] = None,
             dependency_cache: Optional[bool] = None, inputs: Optional[List[dict]] = None,
             runtime: Optional[str] = None, wasm_module: Optional[bytes] = None, wasm_module_input: Optional[str] = None,
             wasm_args: Optional[List[str]] = None, wasm_stdin: Optional[str] = None,
             wasm_memory_mb: Optional[int] = None, depends_on: Optional[List[int]] = None,
             optimize_for: Optional[str] = None, max_cost: Union[None, str, float] = None,
             webhook_url: Optional[str] = None) -> dict:
    """The REST body of one job (used by run_job and run_batch). max_cost is in your account's
    billing currency (CZK, EUR or USD); max_cost_eur only works for accounts billed in EUR."""
    import base64
    body: dict = {}
    if command is not None:
        body["command"] = command
    if template:
        body["template"] = template
    if timeout_seconds:
        body["timeout_s"] = timeout_seconds
    if save_artifacts:
        body["save_artifacts"] = True
    if max_attempts:
        body["retry"] = {"max_attempts": max_attempts, "on": retry_on}
    for k, v in (("artifacts", artifacts), ("required_artifacts", required_artifacts), ("env", env), ("cwd", cwd),
                 ("metadata", metadata), ("idempotency_key", idempotency_key), ("compute_class", compute_class),
                 ("placement", placement), ("inputs", inputs), ("depends_on", depends_on), ("optimize_for", optimize_for)):
        if v:
            body[k] = v
    if dependency_cache is not None:
        body["dependency_cache"] = bool(dependency_cache)
    if not internet:
        body["network"] = {"mode": "none"}
    elif allow:
        body["network"] = {"mode": "allowlist", "allow": allow}
    if max_cost is not None:
        body["max_cost"] = str(max_cost)
    if webhook_url:
        body["webhook_url"] = webhook_url
    if max_cost_eur is not None:
        body["max_cost_eur"] = str(max_cost_eur)
    if wasm_module is not None or wasm_module_input:
        runtime = runtime or "wasi"
        w: dict = {}
        if wasm_module is not None:
            w["module_b64"] = base64.b64encode(wasm_module).decode()
        if wasm_module_input:
            w["module"] = wasm_module_input
        if wasm_args:
            w["args"] = list(wasm_args)
        if wasm_stdin:
            w["stdin"] = wasm_stdin
        if wasm_memory_mb:
            w["memory_mb"] = wasm_memory_mb
        body["wasm"] = w
    if runtime:
        body["runtime"] = runtime
    return body


def run_job(command: Optional[str] = None, template: Optional[str] = None, timeout_seconds: int = 3600,
            artifacts: Optional[List[str]] = None, save_artifacts: bool = False,
            required_artifacts: Optional[List[str]] = None,
            env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
            internet: bool = True, allow: Optional[List[str]] = None,
            max_cost_eur: Union[None, str, float] = None, max_attempts: int = 1, retry_on: str = "system",
            metadata: Optional[Dict[str, str]] = None, idempotency_key: Optional[str] = None,
            url: Optional[str] = None, api_key: Optional[str] = None, **kw) -> Job:
    """Submit a background job and return immediately (see Job).

    Extra keyword arguments: max_cost (price limit in your account's billing currency – preferred over
    max_cost_eur), compute_class (without it: your account's default), optimize_for,
    placement, dependency_cache, inputs (job_input(...)),
    runtime ("firecracker" | "wasi"), wasm_module (bytes), wasm_module_input, wasm_args, wasm_stdin,
    wasm_memory_mb. A WebAssembly job needs no command and no template.
    """
    if command is None and kw.get("wasm_module") is None and not kw.get("wasm_module_input"):
        raise ValueError("command is required (or wasm_module for a WebAssembly job)")
    wasm = kw.get("wasm_module") is not None or kw.get("wasm_module_input") or kw.get("runtime") == "wasi"
    if template is None and not wasm:
        template = "base"
    if wasm and timeout_seconds == 3600:
        timeout_seconds = 60
    body = job_body(command, template, timeout_seconds, artifacts, save_artifacts, required_artifacts, env, cwd,
                    internet, allow, max_cost_eur, max_attempts, retry_on, metadata, idempotency_key, **kw)
    c = Client(url, api_key)
    return Job(c.request("POST", "/v1/jobs", body=body), c)


class Batch:
    """A batch of jobs (fan-out) with a concurrency limit and an optional total price limit.
    Each job has its own result; failed jobs do not stop the others."""

    def __init__(self, data: dict, client: Client):
        self._c = client
        self.data = data
        self.id: str = (data.get("batch") or data)["id"]
        self.job_ids: List[str] = list(data.get("job_ids") or [])

    def summary(self) -> dict:
        """Counts by state, cost, durations, first errors; "done" is True when all jobs ended."""
        return self._c.request("GET", f"/v1/batches/{self.id}")

    def report(self) -> dict:
        """The summary plus one row per job (state, exit code, cost, result files)."""
        return self._c.request("GET", f"/v1/batches/{self.id}/report")

    def report_csv(self) -> str:
        data = self._c.request("GET", f"/v1/batches/{self.id}/report", query={"format": "csv"})
        return data.decode() if isinstance(data, bytes) else str(data)

    def wait(self, timeout: Optional[float] = None, poll: float = 5.0) -> "Batch":
        """Wait until all jobs of the batch ended (server-side waiting, no polling)."""
        end = time.time() + timeout if timeout else None
        while True:
            if self.summary().get("done"):
                return self
            left = (end - time.time()) if end else 60
            if left <= 0:
                raise TimeoutError(f"batch {self.id} has not finished")
            try:
                self._c.request("POST", "/v1/jobs/wait", body={"batch_ids": [self.id], "timeout_s": max(1, min(60, int(left)))})
            except SandboxError as e:
                if e.status != 404:
                    raise
                time.sleep(poll)

    def jobs(self) -> List[Job]:
        return [Job({"id": i}, self._c) for i in self.job_ids]

    def cancel(self) -> dict:
        return self._c.request("POST", f"/v1/batches/{self.id}/cancel", body={})

    def __repr__(self) -> str:
        return f"<Batch {self.id} {len(self.job_ids)} jobs>"


def run_batch(jobs: List[dict], defaults: Optional[dict] = None, max_concurrency: int = 10,
              max_total_cost_eur: Union[None, str, float] = None, name: Optional[str] = None,
              idempotency_key: Optional[str] = None, url: Optional[str] = None,
              api_key: Optional[str] = None,
              max_total_cost: Union[None, str, float] = None,
              webhook_url: Optional[str] = None) -> Batch:
    """Submit many jobs at once. Each item takes the job_body() keyword names (command, template,
    artifacts, inputs, depends_on, wasm_module, ...) or already-built REST fields."""
    def norm(d: dict) -> dict:
        names = job_body.__code__.co_varnames[:job_body.__code__.co_argcount]
        known = {k: v for k, v in d.items() if k in names}
        rest = {k: v for k, v in d.items() if k not in names}
        out = job_body(**known)
        out.update(rest)
        return out
    body: dict = {"jobs": [norm(j) for j in jobs], "max_concurrency": max_concurrency}
    if defaults:
        body["defaults"] = norm(defaults)
    if max_total_cost is not None:
        body["max_total_cost"] = str(max_total_cost)
    if webhook_url:
        body["webhook_url"] = webhook_url
    if max_total_cost_eur is not None:
        body["max_total_cost_eur"] = str(max_total_cost_eur)
    if name:
        body["name"] = name
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    c = Client(url, api_key)
    return Batch(c.request("POST", "/v1/batches", body=body), c)


def get_batch(batch_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> Batch:
    c = Client(url, api_key)
    rep = c.request("GET", f"/v1/batches/{batch_id}/report")
    return Batch({"batch": rep["summary"], "job_ids": [r["job_id"] for r in sorted(rep["jobs"], key=lambda r: r["index"])]}, c)


def wait_jobs(job_ids: Optional[List[str]] = None, batch_ids: Optional[List[str]] = None, until: str = "any",
              timeout_s: int = 30, url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Block until any (or all) of the jobs/batches finished, or timeout_s (max 60) passed.
    Returns {"done", "finished", "total", "items": [{"id", "kind", "status", "done"}]}."""
    return Client(url, api_key).request("POST", "/v1/jobs/wait", body={
        "job_ids": job_ids or [], "batch_ids": batch_ids or [], "until": until, "timeout_s": timeout_s})


def webhook_secret(rotate: bool = False, url: Optional[str] = None, api_key: Optional[str] = None) -> str:
    """The secret that signs your webhooks (X-WBX-Signature). rotate=True issues a new one."""
    c = Client(url, api_key)
    if rotate:
        return c.request("POST", "/v1/webhooks/secret/rotate", body={})["secret"]
    return c.request("GET", "/v1/webhooks/secret")["secret"]


def verify_webhook(secret: str, body: bytes, signature: str, tolerance_s: int = 300) -> bool:
    """Check the X-WBX-Signature header of a received webhook (body = raw request bytes)."""
    import hashlib
    import hmac
    try:
        parts = dict(p.split("=", 1) for p in signature.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerance_s:
        return False
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, parts.get("v1", ""))


def get_budget(url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Your budget limits, spend this month/today and amounts reserved by running jobs."""
    return Client(url, api_key).request("GET", "/v1/budget")


def set_budget(monthly: Union[None, str, float] = None, daily: Union[None, str, float] = None,
               alert_pct: Optional[int] = None, url: Optional[str] = None, api_key: Optional[str] = None,
               monthly_eur: Union[None, str, float] = None, daily_eur: Union[None, str, float] = None) -> dict:
    """Set your own monthly/daily limits in your account's billing currency (the ``currency``
    field of get_budget()); "" or 0 removes a limit. Limits set by the operator still apply.

    ``monthly_eur`` / ``daily_eur`` are the older names of ``monthly`` / ``daily`` and are kept
    for compatibility – the amount is in the billing currency in both cases."""
    if monthly is None:
        monthly = monthly_eur
    if daily is None:
        daily = daily_eur
    body: dict = {}
    if monthly is not None:
        body["monthly_limit"] = str(monthly)
    if daily is not None:
        body["daily_limit"] = str(daily)
    if alert_pct is not None:
        body["alert_pct"] = alert_pct
    return Client(url, api_key).request("PUT", "/v1/budget", body=body)


def compute_preferences(url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Compute classes available to your account (relative price, availability) and your default class.
    The default is decided by the server: compute_class in a request > account default > economy."""
    return Client(url, api_key).request("GET", "/v1/compute/preferences")


def set_default_compute_class(name: str, url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Set your account's default compute class ("" = global default). Applies only to new sandboxes and
    jobs; running ones keep their class and price. A class you are not allowed to use is refused."""
    return Client(url, api_key).request("PUT", "/v1/compute/preferences", body={"default_compute_class": name})


def runtimes(url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Available runtimes with their isolation level and capabilities."""
    return Client(url, api_key).request("GET", "/v1/runtimes")


def get_job(job_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> Job:
    c = Client(url, api_key)
    return Job(c.request("GET", f"/v1/jobs/{job_id}"), c)


def list_jobs(status: Optional[str] = None, limit: int = 50, url: Optional[str] = None,
              api_key: Optional[str] = None) -> List[dict]:
    """Your jobs, newest first (short summaries)."""
    q = {"limit": limit}
    if status:
        q["status"] = status
    return Client(url, api_key).request("GET", "/v1/jobs", query=q)["jobs"]
