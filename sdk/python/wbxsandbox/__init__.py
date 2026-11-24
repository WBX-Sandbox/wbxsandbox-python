"""WBX Sandbox – Python SDK.

Isolated cloud sandboxes (Firecracker microVMs) for AI agents. No dependencies –
only the Python standard library (verify_receipt needs the optional cryptography package).

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

Snapshots and clones (features "snapshots" and "clones"):

    snap = sb.snapshot(name="after-setup")          # copy of the disk
    sb2 = Sandbox.create(from_snapshot=snap.id)     # new sandbox with those files
    copies = sb.clone(count=3)                      # three copies (same lifetime and network as sb)

WebAssembly jobs (runtime="wasi", no network, fast start):

    job = run_job(wasm_module=open("tool.wasm", "rb").read(), wasm_args=["--fast"],
                  inputs=[job_input(previous.id, "output/data.csv")], save_artifacts=True)

Configuration (environment variables, or url= / api_key= arguments):
    WBXSANDBOX_URL       API address, e.g. https://sandbox.wbx.cz
    WBXSANDBOX_API_KEY   your API key
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional, Union

__version__ = "0.17.0"
__all__ = ["Sandbox", "CommandResult", "SandboxError", "ComputerActionError", "Client", "Computer", "Screenshot", "Job", "run_job", "get_job", "list_jobs",
           "Batch", "run_batch", "get_batch", "list_batches", "job_input", "batch_input", "get_budget", "set_budget", "runtimes",
           "compute_preferences", "set_default_compute_class", "wait_jobs", "webhook_secret", "verify_webhook",
           "receipt_keys", "verify_receipt", "features", "Snapshot", "CloneList"]


class SandboxError(Exception):
    """An error of the API or of the SDK.

    status       HTTP status of the response; 0 when there was no HTTP error response
                 (the server could not be reached, or the SDK found the error itself)
    message      human-readable message ("error" of the JSON body, or the raw text)
    code         machine-readable code from the JSON body ("quota", "queue_full",
                 "feature_disabled", "human_in_control", "snapshot_limit", "sandbox_archived", ...) or None
    retry_after  seconds the server asked to wait before retrying (Retry-After header,
                 seconds or an HTTP date), or None – sent with some 429 and 503 responses
    body         the parsed JSON error body (dict), the raw text when it was not JSON, or None
    """

    def __init__(self, status: int, message: str, code: Optional[str] = None,
                 retry_after: Optional[float] = None, body: Union[None, dict, str] = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.body = body


class ComputerActionError(SandboxError):
    """A desktop action failed (raised by the sandbox.computer shortcuts such as click() or type()).

    The server answers such a failure with HTTP 200 and ok=false, so status is 200 – unlike
    status 0, which means the server could not be reached.

    failed_index  index of the action that failed (the actions before it were performed)
    results       one {"type", "ok", "error"?} entry per performed action, the failed one last
    response      the whole response of the actions request
    """

    def __init__(self, message: str, failed_index: int = 0, results: Optional[List[dict]] = None,
                 response: Optional[dict] = None):
        super().__init__(200, message, body=response)
        self.failed_index = failed_index
        self.results = list(results or [])
        self.response = response

    def __str__(self) -> str:
        return f"desktop action {self.failed_index} failed: {self.message}"


def _parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Retry-After (delay in seconds or an HTTP date) as seconds, or None."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        secs = float(v)
        return secs if secs >= 0 else None
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        t = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, t.timestamp() - (time.time() if now is None else now))


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: urllib would copy the X-API-Key header to the new address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SandboxError(0, f"the server answered with a redirect to {newurl} – redirects are not followed so the "
                              "API key is never sent to another address; set the API address to the final one")


_OPENER = urllib.request.build_opener(_NoRedirect())


def _urlopen(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def check_url(url: str, allow_insecure: bool = False) -> str:
    """The API address: https, or plain http only for localhost (or with allow_insecure /
    WBXSANDBOX_ALLOW_INSECURE=1) – over http the API key would travel unencrypted."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return url
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    insecure = allow_insecure or os.environ.get("WBXSANDBOX_ALLOW_INSECURE", "") in ("1", "true", "yes")
    if u.scheme == "http" and u.hostname and (local or insecure):
        return url
    raise SandboxError(0, f"refusing to use {url!r}: the API key would be sent unencrypted – use an https:// address "
                          "(or allow_insecure=True / WBXSANDBOX_ALLOW_INSECURE=1 for a trusted network)")


_RETRY_STATUS = (502, 503, 504)


class Client:
    """Low-level HTTP client for the WBX Sandbox REST API.

    Safe requests (GET, HEAD, DELETE, PUT and POST with an idempotency_key) are retried up to
    max_retries times after a connection error, 502/503/504 or 429 with Retry-After."""

    def __init__(self, url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 120,
                 max_retries: int = 2, allow_insecure: bool = False):
        self.url = check_url((url or os.environ.get("WBXSANDBOX_URL", "https://sandbox.wbx.cz")).rstrip("/"), allow_insecure)
        self.api_key = api_key or os.environ.get("WBXSANDBOX_API_KEY", "")
        self.timeout = timeout
        self.max_retries = max_retries

    def __repr__(self) -> str:
        return f"<Client {self.url} key={'set' if self.api_key else 'missing'}>"

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
        safe = method in ("GET", "HEAD", "DELETE", "PUT") or (isinstance(body, dict) and bool(body.get("idempotency_key")))
        retries = self.max_retries if safe else 0
        for attempt in range(retries + 1):
            try:
                return self._once(method, url, data, headers, timeout, stream)
            except SandboxError as e:
                if attempt >= retries:
                    raise
                if e.status in _RETRY_STATUS or (e.status == 0 and getattr(e, "_conn", False)):
                    delay = e.retry_after if e.retry_after is not None else min(8.0, 0.5 * 2 ** attempt) * (0.5 + random.random() / 2)
                elif e.status == 429 and e.retry_after is not None:
                    delay = e.retry_after
                else:
                    raise
                if delay > 60:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _once(self, method, url, data, headers, timeout, stream):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = _urlopen(req, timeout or self.timeout)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode(errors="replace")
            except Exception:  # noqa: BLE001 – the body is optional
                raw = ""
            body: Union[None, dict, str] = raw or None
            msg, code = raw, None
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
                if isinstance(parsed.get("error"), str):
                    msg = parsed["error"]
                if isinstance(parsed.get("code"), str):
                    code = parsed["code"]
            retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers is not None else None)
            raise SandboxError(e.code, msg, code=code, retry_after=retry_after, body=body) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            err = SandboxError(0, f"cannot connect to {self.url}: {getattr(e, 'reason', e)}")
            err._conn = True  # přechodná chyba spojení – bezpečný požadavek se zopakuje
            raise err from None
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

    def download(self, path: str, dest: str) -> None:
        """Download a file to a local path in chunks (large files never sit in memory)."""
        resp = self._sb._c.request("GET", f"/v1/sandboxes/{self._sb.id}/files", query={"path": path},
                                   timeout=3600, stream=True)
        tmp = dest + ".part"
        with resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Download a text file."""
        return self.read(path).decode(encoding)

    def write(self, path: str, data: Union[str, bytes]) -> None:
        """Upload a file (missing directories are created)."""
        if isinstance(data, str):
            data = data.encode()
        self._sb._c.request("PUT", f"/v1/sandboxes/{self._sb.id}/files", body=data, query={"path": path})

    def remove(self, path: str, recursive: bool = False) -> None:
        """Delete a file or an empty directory; recursive=True deletes a directory with its contents."""
        if recursive:
            self._sb._c.request("POST", f"/v1/sandboxes/{self._sb.id}/fs/remove",
                                body={"path": path, "recursive": True})
            return
        self._sb._c.request("DELETE", f"/v1/sandboxes/{self._sb.id}/files", query={"path": path})

    def mkdir(self, path: str) -> None:
        """Create a directory (with missing parents); an existing directory is not an error."""
        self._sb._c.request("POST", f"/v1/sandboxes/{self._sb.id}/fs/mkdir", body={"path": path, "parents": True})

    def list(self, path: str = "/home/user") -> List[dict]:
        """List a directory: [{"name": ..., "is_dir": bool, "size": bytes}, ...]."""
        return self._sb._c.request("GET", f"/v1/sandboxes/{self._sb.id}/ls", query={"path": path})


@dataclass
class Screenshot:
    """A screenshot of the sandbox desktop (sandbox.computer.screenshot())."""
    data: bytes
    format: str            # "png" or "jpeg"
    width: int             # image size in pixels
    height: int
    screen_width: int = 0  # screen size in pixels – mouse coordinates use these
    screen_height: int = 0

    def save(self, path: str) -> None:
        """Write the image to a local file."""
        with open(path, "wb") as f:
            f.write(self.data)


class Computer:
    """The graphical desktop of a sandbox (sandbox.computer) – needs the "desktop" template
    and the Desktop & Computer Use feature on your account (paid plans, or after topping up
    at least US$15).

        with Sandbox.create(template="desktop") as sb:
            sb.computer.click(640, 400)
            sb.computer.type("hello")
            sb.computer.key("ctrl+a")
            sb.computer.screenshot().save("screen.png")

    Coordinates are screen pixels, (0, 0) is the top left corner. While a person controls the
    desktop from the portal, actions raise SandboxError with status 409 and code
    "human_in_control" (screenshots still work).

    actions() returns the response as it is, also when an action failed (ok=False, failed_index,
    results). The shortcuts (move, click, type, ...) raise ComputerActionError instead.
    """

    def __init__(self, sb: "Sandbox"):
        self._sb = sb

    def _path(self, op: str) -> str:
        return f"/v1/sandboxes/{self._sb.id}/computer/{op}"

    def status(self) -> dict:
        """Desktop state: available, display, width, height, controller ("agent" or "human"),
        controller_until (unix ms or None), viewers – and reason when not available.
        Does not wake a sleeping sandbox."""
        return self._sb._c.request("GET", self._path("status"))

    def screenshot(self, format: str = "png", quality: Optional[int] = None,
                   max_width: Optional[int] = None) -> Screenshot:
        """Take a screenshot. format "png" or "jpeg", quality 1–100 (jpeg), max_width scales the
        image down (mouse coordinates stay in screen pixels). Does not wake a sleeping sandbox."""
        query = {"format": format}
        if quality is not None:
            query["quality"] = int(quality)
        if max_width:
            query["max_width"] = int(max_width)
        resp = self._sb._c.request("GET", self._path("screenshot"), query=query, stream=True)
        with resp:
            data = resp.read()
            h = resp.headers
            ctype = h.get("Content-Type", "")
            if ctype not in ("image/png", "image/jpeg"):
                raise SandboxError(0, f"unexpected screenshot type {ctype!r}")

            def num(k: str) -> int:
                try:
                    return int(h.get(k) or 0)
                except ValueError:
                    return 0
            return Screenshot(data=data, format=ctype.split("/", 1)[1], width=num("X-WBX-Width"),
                              height=num("X-WBX-Height"), screen_width=num("X-WBX-Screen-Width"),
                              screen_height=num("X-WBX-Screen-Height"))

    def actions(self, actions: List[dict], screenshot: bool = False, screenshot_format: Optional[str] = None,
                screenshot_quality: Optional[int] = None, screenshot_max_width: Optional[int] = None) -> dict:
        """Run up to 50 actions in order, e.g.
        [{"type": "click", "x": 10, "y": 20}, {"type": "type", "text": "hi"}, {"type": "key", "keys": "Return"}].
        Types: move, click (button, count), mouse_down, mouse_up, drag (to_x, to_y), scroll (dx, dy),
        type (text), key (keys), wait (ms). Returns {"ok", "results", ...}; with screenshot=True also
        "screenshot": {"format", "width", "height", "data_base64", ...}. Wakes a sleeping sandbox.

        A failed action does not raise here: the server stops at it and answers ok=False with
        "error" and "failed_index" (the actions before it were performed). The HTTP timeout is
        300 s, as long actions (typing, waits) can take more than a minute."""
        body: Dict = {"actions": list(actions), "screenshot": bool(screenshot)}
        if screenshot:
            if screenshot_format:
                body["screenshot_format"] = screenshot_format
            if screenshot_quality:
                body["screenshot_quality"] = int(screenshot_quality)
            if screenshot_max_width:
                body["screenshot_max_width"] = int(screenshot_max_width)
        return self._sb._c.request("POST", self._path("actions"), body=body, timeout=300)

    def control(self, mode: str, ttl_s: Optional[int] = None) -> dict:
        """Who controls the desktop. A person takes control ("human") only in the portal – with an
        API key that is refused (403); "agent" succeeds when no person is in control. Returns the status."""
        body: Dict = {"mode": mode}
        if ttl_s is not None:
            body["ttl_s"] = int(ttl_s)
        return self._sb._c.request("POST", self._path("control"), body=body)

    # zkratky pro jednotlivé akce (každá je jeden požadavek)
    def _one(self, action: dict) -> dict:
        r = self.actions([action])
        if not r.get("ok", False):
            idx = r.get("failed_index")
            results = r.get("results") or []
            if not isinstance(idx, int) or isinstance(idx, bool):
                idx = max(0, len(results) - 1)
            raise ComputerActionError(r.get("error") or "the action failed", idx, results, r)
        return r

    def move(self, x: int, y: int) -> dict:
        return self._one({"type": "move", "x": int(x), "y": int(y)})

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> dict:
        return self._one({"type": "click", "x": int(x), "y": int(y), "button": button, "count": int(count)})

    def double_click(self, x: int, y: int) -> dict:
        return self.click(x, y, count=2)

    def drag(self, x: int, y: int, to_x: int, to_y: int, button: str = "left") -> dict:
        return self._one({"type": "drag", "x": int(x), "y": int(y), "to_x": int(to_x), "to_y": int(to_y), "button": button})

    def scroll(self, x: int, y: int, dy: int = 0, dx: int = 0) -> dict:
        """Scroll at (x, y): dy wheel steps down (negative = up), dx to the right (negative = left).

        Note the order: the third positional argument is dy (vertical), the fourth dx – the
        TypeScript SDK takes scroll(x, y, dx, dy). Pass them by name to avoid mistakes:
        scroll(640, 400, dy=5) or scroll(640, 400, dx=-2)."""
        a = {"type": "scroll", "x": int(x), "y": int(y)}
        if dy:
            a["dy"] = int(dy)
        if dx:
            a["dx"] = int(dx)
        return self._one(a)

    def type(self, text: str) -> dict:
        """Type text (up to 4096 characters; newlines press Enter)."""
        return self._one({"type": "type", "text": text})

    def key(self, keys: str) -> dict:
        """Press a key or combination, e.g. "Return", "ctrl+c", "ctrl+shift+t"."""
        return self._one({"type": "key", "keys": keys})


def _create_body(timeout, metadata, vcpu, mem_mb, internet, allow, block, env, compute_class, placement,
                 dependency_cache, optimize_for, ip, name, on_timeout, idle_timeout, keep_days) -> dict:
    """Body of POST /v1/sandboxes (and of a clone) without the template."""
    body = {"timeout_s": timeout, "metadata": metadata or {}, "vcpu": vcpu, "mem_mb": mem_mb}
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
    if ip:
        body.setdefault("network", {})["ip"] = ip
    if name:
        body["name"] = name
    if on_timeout:
        body["on_timeout"] = on_timeout
    if idle_timeout:
        body["idle_timeout_s"] = int(idle_timeout)
    if keep_days:
        body["keep_days"] = int(keep_days)
    return body


@dataclass
class Sandbox:
    id: str
    template: str = ""
    state: str = ""
    info: Dict = field(default_factory=dict)
    _c: Client = field(default=None, repr=False)

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, template: Optional[str] = None, timeout: int = 300, metadata: Optional[Dict[str, str]] = None,
               vcpu: int = 0, mem_mb: int = 0, url: Optional[str] = None, api_key: Optional[str] = None,
               internet: bool = True, allow: Optional[List[str]] = None, block: Optional[List[str]] = None,
               env: Optional[Dict[str, str]] = None, compute_class: Optional[str] = None,
               placement: Optional[str] = None, dependency_cache: Optional[bool] = None,
               optimize_for: Optional[str] = None, ip: Optional[str] = None,
               name: Optional[str] = None, on_timeout: Optional[str] = None,
               idle_timeout: Optional[int] = None, keep_days: Optional[int] = None,
               from_snapshot: Optional[str] = None) -> "Sandbox":
        """Create a new sandbox.

        template  what is preinstalled ("base" when omitted, "code-interpreter", "browser", "node", ...)
        from_snapshot  start from the disk of one of your snapshots ("snap_..." or a Snapshot) instead
                  of a fresh template disk; the sandbox gets the snapshot's template, a new ID and
                  network, and env / settings from this call (snapshots never contain env).
                  Requires the snapshots feature (see features()).
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
        ip        "ipv4" | "dual" | "ipv6" – IP version (default: your team's setting, usually ipv4).
                  With IPv6 the sandbox gets its own public IPv6 address (sandbox.ipv6); "ipv6" = no IPv4
                  internet (DNS still works). Refused, never downgraded, if no server with IPv6 is free.
        name      optional name ("my-project"); if you already have a sandbox with this name it is
                  returned instead of a new one (woken up if it is asleep)
        on_timeout  "kill" (default) destroys the sandbox when its lifetime ends; "pause" puts it to
                  sleep instead: files are kept (several days), running programs stop, and the next
                  command wakes it up in a few seconds
        idle_timeout  with on_timeout="pause": fall asleep after this many seconds without commands
        keep_days  with on_timeout="pause": keep the sandbox this many days after it falls asleep
                  before it is deleted (default: your plan's retention; the plan sets the maximum).
                  Change it later with keep(); disk of a sleeping sandbox is billed.
        Network settings can only make your team's policy stricter, never looser.
        """
        c = Client(url, api_key)
        body = _create_body(timeout, metadata, vcpu, mem_mb, internet, allow, block, env, compute_class, placement,
                            dependency_cache, optimize_for, ip, name, on_timeout, idle_timeout, keep_days)
        if from_snapshot:
            body["from_snapshot"] = from_snapshot.id if isinstance(from_snapshot, Snapshot) else str(from_snapshot)
            if template:
                body["template"] = template
        else:
            body["template"] = template or "base"
        # ze snapshotu se kopíruje disk (bez reflinku celý) – delší limit než běžný požadavek
        d = c.request("POST", "/v1/sandboxes", body=body,
                      timeout=max(_FROM_SNAPSHOT_TIMEOUT, c.timeout) if from_snapshot else None)
        return cls._from(d, c)

    @property
    def ipv6(self) -> Optional[str]:
        """The sandbox's public IPv6 address (ip="dual" or "ipv6"), otherwise None."""
        return (self.info or {}).get("ipv6") or None

    @classmethod
    def connect(cls, sandbox_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> "Sandbox":
        """Attach to an existing sandbox by its id (e.g. in the next turn of a conversation)."""
        c = Client(url, api_key)
        return cls._from(c.request("GET", f"/v1/sandboxes/{sandbox_id}"), c)

    @classmethod
    def list(cls, url: Optional[str] = None, api_key: Optional[str] = None) -> List["Sandbox"]:
        """Your running, paused and sleeping sandboxes."""
        c = Client(url, api_key)
        return [cls._from(d, c) for d in c.request("GET", "/v1/sandboxes")]

    @classmethod
    def _from(cls, d: dict, c: Client) -> "Sandbox":
        return cls(id=d["id"], template=d.get("template", ""), state=d.get("state", ""), info=d.get("info") or {}, _c=c)

    @property
    def files(self) -> Files:
        return Files(self)

    @property
    def computer(self) -> Computer:
        """The graphical desktop (template "desktop"): screenshots, mouse and keyboard."""
        return Computer(self)

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

    def sleep(self) -> None:
        """Put the sandbox to sleep: files are kept, running programs stop, no compute is billed.
        Any later command (or resume()) wakes it up in a few seconds."""
        self.state = self._c.request("POST", f"/v1/sandboxes/{self.id}/sleep")["state"]

    def keep(self, days: int) -> dict:
        """Keep the sandbox `days` days after it falls asleep before it is deleted with its files
        (0 = your plan's default; the plan sets the maximum). Returns keep_days,
        default_keep_days, max_keep_days and, for a sleeping sandbox, delete_at."""
        return self._c.request("PUT", f"/v1/sandboxes/{self.id}/keep", body={"keep_days": int(days)})

    def resume(self) -> None:
        """Continue a paused sandbox exactly where it stopped, or wake up a sleeping one."""
        self.state = self._c.request("POST", f"/v1/sandboxes/{self.id}/resume")["state"]

    # ------------------------------------------------------------ snapshots and clones (feature "snapshots")

    def snapshot(self, name: Optional[str] = None) -> "Snapshot":
        """Save a copy of the sandbox disk as a snapshot (running, paused or sleeping sandbox).

        A snapshot holds the files only – no memory and no running programs. A running sandbox
        is paused only for a moment and keeps running. An archived sandbox must be woken up first
        (409 sandbox_archived). Start new sandboxes from it with Snapshot.create_sandbox() or
        Sandbox.create(from_snapshot=...). Snapshots are kept until you delete them; their size is
        billed as storage. Requires the snapshots feature; the number of snapshots is limited by
        your plan (429 snapshot_limit).
        """
        body = {"name": name} if name else {}
        d = self._c.request("POST", f"/v1/sandboxes/{self.id}/snapshots", body=body, timeout=_SNAPSHOT_TIMEOUT)
        return Snapshot._from(d, self._c)

    def clone(self, count: int = 1, name: Optional[str] = None, keep_snapshot: bool = False,
              timeout: Optional[int] = None, metadata: Optional[Dict[str, str]] = None, vcpu: int = 0, mem_mb: int = 0,
              internet: bool = True, allow: Optional[List[str]] = None, block: Optional[List[str]] = None,
              env: Optional[Dict[str, str]] = None, compute_class: Optional[str] = None,
              placement: Optional[str] = None, dependency_cache: Optional[bool] = None,
              optimize_for: Optional[str] = None, ip: Optional[str] = None, on_timeout: Optional[str] = None,
              idle_timeout: Optional[int] = None, keep_days: Optional[int] = None,
              snapshot_name: Optional[str] = None) -> "CloneList":
        """Create `count` (1–10) new sandboxes with a copy of this sandbox's disk.

        Takes a snapshot of the disk and starts the copies from it on the same server; the
        snapshot is deleted afterwards unless keep_snapshot=True (snapshot_name names it – same
        rules as name). name names the copies (<name>-1 … <name>-N for several). Each copy has its own ID,
        IP addresses and identity (new machine-id, no SSH host keys of the source). Memory and
        running programs are not copied – start what you need again.

        Copies keep the source sandbox's lifetime and network settings unless you pass them: without
        timeout, on_timeout, idle_timeout, keep_days or compute_class the copies get the source's
        values, and its network access (no internet, allowed destinations, IP version). Network
        options passed here can only narrow the source's access, never widen it. env and metadata
        are not inherited (pass what the copies need); vCPU and memory come from the snapshot
        unless you pass vcpu / mem_mb.

        Returns a list of the new sandboxes. When only some copies could be created, the list
        holds those and .errors the reasons (one dict per failed copy: index, name, status, code,
        error); .snapshot_kept tells whether the snapshot was kept. When none could be created,
        SandboxError is raised. Requires the snapshots and clones features; the copies
        count towards your concurrent-sandbox limits.
        """
        if not 1 <= int(count) <= 10:
            raise ValueError("count must be between 1 and 10")
        body = _create_body(timeout, metadata, vcpu, mem_mb, internet, allow, block, env, compute_class, placement,
                            dependency_cache, optimize_for, ip, name, on_timeout, idle_timeout, keep_days)
        if timeout is None:
            body.pop("timeout_s")
        body["count"] = int(count)
        if keep_snapshot:
            body["keep_snapshot"] = True
        if snapshot_name:
            body["snapshot_name"] = snapshot_name
        d = self._c.request("POST", f"/v1/sandboxes/{self.id}/clone", body=body,
                            timeout=_CLONE_TIMEOUT)
        d = d if isinstance(d, dict) else {}
        out = CloneList((Sandbox._from(x, self._c) for x in d.get("sandboxes") or []),
                        snapshot_id=d.get("snapshot_id"), errors=d.get("errors"),
                        snapshot_kept=bool(d.get("snapshot_kept", keep_snapshot)))
        if not out and out.errors:
            e = out.errors[0] if isinstance(out.errors[0], dict) else {"error": str(out.errors[0])}
            raise SandboxError(int(e.get("status") or 0), str(e.get("error") or "no copy could be created"),
                               code=e.get("code"), body=d)
        return out

    # ------------------------------------------------------------ metrics (measured by the host)

    def metrics_history(self, since: Union[None, int, datetime] = None) -> dict:
        """CPU, memory, network and disk of the sandbox over the last hour, measured by the host
        every 10 seconds: {"source": "host", "interval_s": 10, "retention_s": 3600, "vcpu", "mem_mb",
        "state", "samples": [{"t": unix_ms, "cpu_pct", "cpu_ms", "mem_mb", "net_rx_bytes", ...}]}.

        since  only samples newer than this (Unix milliseconds or a datetime) – poll with the
               "t" of the last sample you have
        Does not wake a sleeping sandbox (it returns its state and no samples) and does not count
        as activity. History starts again after the sandbox wakes up. Requires the
        metrics_history feature (see features()).
        """
        q = {"since": _unix_ms(since)} if since is not None else None
        return self._c.request("GET", f"/v1/sandboxes/{self.id}/metrics/history", query=q)

    def metrics_stream(self, since: Union[None, int, datetime] = None, timeout: float = 16 * 60) -> Iterator[dict]:
        """Live metrics (Server-Sent Events) as a generator of {"event": ..., "data": {...}}:
        "state" first (the sandbox state), then a "sample" every 10 s (same fields as in
        metrics_history), finally "end" ({"reason": "timeout" | "not_running" | "ended"}).
        The server closes the stream after 15 minutes – open a new one to continue. A sleeping
        sandbox is not woken up: you get its state and "end" right away.

        since  also send already measured samples newer than this (Unix ms or a datetime);
               since=0 sends the whole stored history first. Without it only new samples come.
        """
        q = {"since": _unix_ms(since)} if since is not None else None
        resp = self._c.request("GET", f"/v1/sandboxes/{self.id}/metrics/stream", query=q, timeout=timeout, stream=True)
        with resp:
            yield from _sse_events(resp)

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
            if not last:
                # proud skončil bez události exit (přerušené spojení) – jako TS SDK chyba, ne exit_code -1
                raise SandboxError(0, "the output stream ended before the command finished (connection lost); "
                                      "the command may still be running in the sandbox")
            return CommandResult(last.get("exit_code", -1), "".join(out), "".join(err),
                                 last.get("duration_ms", 0), last.get("timed_out", False),
                                 bool(last.get("truncated", False)))
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
        runners = {"python": (".py", "python3"), "node": (".js", "node"), "bash": (".sh", "bash")}
        if language not in runners:
            raise ValueError(f"unsupported language {language!r} (use python, node or bash)")
        ext, interp = runners[language]
        # own file per call – parallel run_code() calls in one sandbox must not overwrite each other
        path = f"/tmp/_wbx_code_{uuid.uuid4().hex}{ext}"
        self.files.write(path, code)
        return self.run(f"{interp} {path}; rc=$?; rm -f {path}; exit $rc", timeout=timeout)


# --------------------------------------------------------------------------- snapshots

# kopie disku bez reflinku může trvat déle než běžný požadavek
_SNAPSHOT_TIMEOUT = 630
# nový sandbox ze snapshotu (kopie disku snapshotu; jako portál)
_FROM_SNAPSHOT_TIMEOUT = 300
# klon: snapshot (server ≤ ~620 s) + souběžné kopie ze snapshotu (≤ ~230 s) + úklid ≈ 910 s
_CLONE_TIMEOUT = 930


class CloneList(list):
    """Result of Sandbox.clone(): a list of the new sandboxes.

    snapshot_id    the snapshot the copies were started from
    snapshot_kept  True when the snapshot was kept (keep_snapshot=True); otherwise it was deleted
    errors         one dict per copy that could not be created: {"index", "name", "status", "code", "error"}
                   (index = position of the copy 0..count-1, name = its name or "")
    """

    def __init__(self, items=(), snapshot_id: Optional[str] = None, errors: Optional[List[dict]] = None,
                 snapshot_kept: bool = False):
        super().__init__(items)
        self.snapshot_id = snapshot_id
        self.snapshot_kept = snapshot_kept
        self.errors = list(errors or [])


@dataclass
class Snapshot:
    """A saved copy of a sandbox disk (files only – no memory, no running programs, no env).

        snap = sb.snapshot(name="after-setup")
        sb2 = snap.create_sandbox(timeout=600)       # or Sandbox.create(from_snapshot=snap.id)
        Snapshot.list(); Snapshot.get("snap_..."); snap.delete()

    A snapshot belongs to your account only and stays until you delete it (deleting the source
    sandbox does not delete it). Its size_bytes is billed as storage, like sleeping sandboxes.
    """

    id: str
    name: str = ""
    source_sandbox: str = ""
    template: str = ""
    vcpu: int = 0
    mem_mb: int = 0
    disk_mb: int = 0
    size_bytes: int = 0
    created_at: str = ""
    info: Dict = field(default_factory=dict)   # the whole API record
    _c: Client = field(default=None, repr=False)

    @classmethod
    def _from(cls, d: dict, c: Client) -> "Snapshot":
        d = d or {}
        return cls(id=d.get("id", ""), name=d.get("name") or "", source_sandbox=d.get("source_sandbox") or "",
                   template=d.get("template") or "", vcpu=int(d.get("vcpu") or 0), mem_mb=int(d.get("mem_mb") or 0),
                   disk_mb=int(d.get("disk_mb") or 0), size_bytes=int(d.get("size_bytes") or 0),
                   created_at=d.get("created_at") or "", info=dict(d), _c=c)

    @classmethod
    def list(cls, url: Optional[str] = None, api_key: Optional[str] = None) -> List["Snapshot"]:
        """Your snapshots."""
        c = Client(url, api_key)
        d = c.request("GET", "/v1/snapshots")
        rows = d.get("snapshots") if isinstance(d, dict) else d
        return [cls._from(x, c) for x in rows or []]

    @classmethod
    def get(cls, snapshot_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> "Snapshot":
        """One snapshot by its ID (404 when it does not exist or is not yours)."""
        c = Client(url, api_key)
        return cls._from(c.request("GET", f"/v1/snapshots/{urllib.parse.quote(snapshot_id, safe='')}"), c)

    def delete(self) -> None:
        """Delete the snapshot (no error if it is already gone). Sandboxes created from it are not affected."""
        try:
            self._c.request("DELETE", f"/v1/snapshots/{urllib.parse.quote(self.id, safe='')}")
        except SandboxError as e:
            if e.status != 404:
                raise

    def create_sandbox(self, **kw) -> "Sandbox":
        """Start a new sandbox from this snapshot; takes the arguments of Sandbox.create()."""
        kw.setdefault("url", self._c.url)
        kw.setdefault("api_key", self._c.api_key)
        return Sandbox.create(from_snapshot=self.id, **kw)


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
        """The execution receipt (inputs, template build, runtime, exit code, measured usage, file checksums, cost)."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/receipt")["execution_receipt"]

    def signed_receipt(self) -> dict:
        """The receipt signed by WBX (Ed25519) – only once the job has ended. Keep or send the whole
        envelope; anyone can check it with verify_receipt(). The signature proves WBX issued the receipt
        unchanged, not that the computation could not have been altered."""
        return self._c.request("GET", f"/v1/jobs/{self.id}/receipt", query={"signed": "1"})["signed_receipt"]

    def artifacts(self) -> List[dict]:
        return self._c.request("GET", f"/v1/jobs/{self.id}/artifacts")["artifacts"]

    def download(self, name: str, dest: Optional[str] = None, verify: bool = True) -> Optional[bytes]:
        """Download a result file and verify its SHA-256 against the X-WBX-SHA256 header the server
        sends with it (missing or wrong checksum = SandboxError; verify=False skips the check).

        With dest the file is streamed to disk in chunks (large files never sit in memory) and None
        is returned; a file that fails the check is removed. Without dest the bytes are returned."""
        path = "/".join(urllib.parse.quote(p, safe="") for p in name.split("/"))
        resp = self._c.request("GET", f"/v1/jobs/{self.id}/artifacts/{path}", timeout=3600, stream=True)
        with resp:
            want = (resp.headers.get("X-WBX-SHA256") or "").strip().lower()
            if verify and len(want) != 64:
                raise SandboxError(0, f"missing checksum for {name}: the response has no valid X-WBX-SHA256 header "
                                      "(pass verify=False to skip the check)")
            h = hashlib.sha256()
            if dest is None:
                data = resp.read()
                h.update(data)
            else:
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        f.write(chunk)
        if verify and h.hexdigest() != want:
            if dest is not None:
                os.remove(tmp)
            raise SandboxError(0, f"checksum mismatch for {name}: expected {want}, got {h.hexdigest()}")
        if dest is None:
            return data
        os.replace(tmp, dest)
        return None

    def cancel(self) -> "Job":
        self.data = self._c.request("POST", f"/v1/jobs/{self.id}/cancel", body={})
        return self

    def retry(self, env: Optional[Dict[str, str]] = None, idempotency_key: Optional[str] = None) -> "Job":
        """Start a new job with the same inputs (pass env again – values are not stored)."""
        body = {"env": env or {}, "idempotency_key": idempotency_key or "py-" + uuid.uuid4().hex}
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
             webhook_url: Optional[str] = None, ip: Optional[str] = None) -> dict:
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
    if ip:
        body.setdefault("network", {})["ip"] = ip
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
    max_cost_eur), compute_class (without it: your account's default), optimize_for, ip (ipv4 | dual | ipv6),
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
    # klíč idempotence vždy: opakované odeslání po výpadku spojení nespustí úlohu dvakrát
    idempotency_key = idempotency_key or "py-" + uuid.uuid4().hex
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
    body["idempotency_key"] = idempotency_key or "py-" + uuid.uuid4().hex
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


def receipt_keys(url: Optional[str] = None) -> List[dict]:
    """Public keys that sign receipts (no API key needed). Pin the key you trust instead of fetching
    it every time from the same server whose receipts you check."""
    return Client(url, "").request("GET", "/v1/receipt-keys")["keys"]


def verify_receipt(signed: dict, public_key: Optional[str] = None, url: Optional[str] = None) -> dict:
    """Check a signed receipt and return what was signed: {"execution_receipt": ..., "key_id": ...,
    "signed_at": ...}. signed = Job.signed_receipt(), or the whole API response / file downloaded from
    the portal ({"signed_receipt": ...}). public_key = base64 Ed25519 key from receipt_keys(); without it
    the key is fetched from the server (url). Raises ValueError when the signature does not match.

    Needs the optional package cryptography: pip install "wbxsandbox[verify]"."""
    import base64
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ImportError('verify_receipt needs the cryptography package: pip install "wbxsandbox[verify]"') from None
    if isinstance(signed.get("signed_receipt"), dict):  # API response or file downloaded from the portal
        signed = signed["signed_receipt"]
    if signed.get("alg") != "Ed25519" or signed.get("format") != "wbx-signed-receipt-1":
        raise ValueError("unsupported receipt signature format")
    if public_key is None:
        keys = [k for k in receipt_keys(url) if k.get("key_id") == signed.get("key_id")]
        if not keys:
            raise ValueError(f"unknown signing key {signed.get('key_id')!r}")
        public_key = keys[0]["public_key"]
    try:
        payload = base64.b64decode(signed["payload"], validate=True)
        sig = base64.b64decode(signed["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(sig, payload)
    except (InvalidSignature, ValueError, KeyError):
        raise ValueError("receipt signature does not match") from None
    out = json.loads(payload)
    if out.get("key_id") != signed.get("key_id"):
        raise ValueError("key_id in the payload does not match the envelope")
    return out


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


def features(url: Optional[str] = None, api_key: Optional[str] = None) -> Dict[str, bool]:
    """Which optional features your account has enabled, e.g. {"metrics_history": True, "desktop": False}.
    The server always decides; a disabled feature answers HTTP 403 with code "feature_disabled"."""
    return Client(url, api_key).request("GET", "/v1/features").get("features", {})


def _unix_ms(t: Union[int, float, datetime]) -> int:
    """Unix milliseconds from an int/float (already ms) or a datetime (naive = UTC)."""
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int(t.timestamp() * 1000)
    return int(t)


def _sse_events(lines) -> Iterator[dict]:
    """Parse Server-Sent Events from an iterable of byte lines; stops after the "end" event."""
    event, data = "message", []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data:
                text = "\n".join(data)
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = text
                yield {"event": event, "data": payload}
                if event == "end":
                    return
            event, data = "message", []
        elif line.startswith(":"):
            continue  # keep-alive comment (": ping")
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


def runtimes(url: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """Available runtimes with their isolation level and capabilities."""
    return Client(url, api_key).request("GET", "/v1/runtimes")


def get_job(job_id: str, url: Optional[str] = None, api_key: Optional[str] = None) -> Job:
    c = Client(url, api_key)
    return Job(c.request("GET", f"/v1/jobs/{job_id}"), c)


def _since(since: Union[None, str, datetime]) -> Optional[str]:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return since


def list_jobs(status: Optional[str] = None, limit: int = 50, url: Optional[str] = None,
              api_key: Optional[str] = None, batch_id: Optional[str] = None,
              since: Union[None, str, datetime] = None, cursor: Optional[str] = None) -> List[dict]:
    """Your jobs, newest first (short summaries, each with a shortened ``command``).

    since: a datetime or an RFC 3339 time ("2026-11-15T08:00:00Z"); cursor: the ``id`` of the last job
    of the previous page.
    """
    q = {"limit": limit}
    if status:
        q["status"] = status
    if batch_id:
        q["batch_id"] = batch_id
    if _since(since):
        q["since"] = _since(since)
    if cursor:
        q["cursor"] = cursor
    return Client(url, api_key).request("GET", "/v1/jobs", query=q)["jobs"]


def list_batches(limit: int = 20, since: Union[None, str, datetime] = None, cursor: Optional[str] = None,
                 url: Optional[str] = None, api_key: Optional[str] = None) -> List[dict]:
    """Your batches, newest first (summaries). cursor: the ``id`` of the last batch of the previous page."""
    q = {"limit": limit}
    if _since(since):
        q["since"] = _since(since)
    if cursor:
        q["cursor"] = cursor
    return Client(url, api_key).request("GET", "/v1/batches", query=q)["batches"]
