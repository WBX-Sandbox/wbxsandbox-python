"""Test MCP serveru WBX Sandboxu – chová se jako AI klient (bez závislostí).

    python3 mcp_test.py http  http://server:3000/mcp  KLÍČ
    python3 mcp_test.py stdio /usr/local/bin/wbxsandboxd  KLÍČ  http://server:3000
"""
import json
import subprocess
import sys
import urllib.request


class HTTPClient:
    def __init__(self, url, key):
        self.url, self.key, self.session, self.n = url, key, None, 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {self.key}", "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            self.session = r.headers.get("Mcp-Session-Id") or self.session
            body = r.read()
            return json.loads(body) if body else None


class StdioClient:
    def __init__(self, binary, key, url):
        self.p = subprocess.Popen([binary, "mcp", "-url", url, "-key", key],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        return json.loads(self.p.stdout.readline())


def call(c, name, **args):
    r = c.send("tools/call", {"name": name, "arguments": args})
    res = r["result"]
    text = res["content"][0]["text"]
    flag = " [isError]" if res.get("isError") else ""
    print(f"\n▶ {name} {json.dumps(args, ensure_ascii=False)[:90]}{flag}\n{text.rstrip()}")
    return res


def main():
    mode = sys.argv[1]
    c = HTTPClient(sys.argv[2], sys.argv[3]) if mode == "http" else StdioClient(sys.argv[2], sys.argv[3], sys.argv[4])

    init = c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "wbx-test", "version": "1"}})
    info = init["result"]
    print("server:", info["serverInfo"], "| protokol:", info["protocolVersion"])
    c.send("notifications/initialized", notify=True)
    tools = c.send("tools/list")["result"]["tools"]
    print("nástroje:", ", ".join(t["name"] for t in tools))
    assert c.send("ping")["result"] == {}

    call(c, "create_sandbox", timeout_s=120)
    call(c, "run_command", command="echo ahoj z MCP; uname -s; exit 3")
    call(c, "run_code", code="import sys\nprint(sum(range(101)), sys.version_info[0])")
    call(c, "write_file", path="/root/projekt/data.txt", content="řádek 1\nřádek 2\n")
    call(c, "read_file", path="/root/projekt/data.txt")
    call(c, "list_files", path="/root/projekt")
    call(c, "run_command", command="sleep 5", timeout_s=1)
    call(c, "read_file", path="/neexistuje")
    call(c, "list_sandboxes")
    call(c, "set_timeout", timeout_s=600)
    r = call(c, "kill_sandbox")
    assert not r.get("isError")
    # bez create_sandbox se sandbox vytvoří automaticky
    call(c, "run_command", command="echo auto-vytvořen")
    call(c, "kill_sandbox")
    bad = c.send("tools/call", {"name": "neexistuje", "arguments": {}})["result"]
    assert bad["isError"]
    err = c.send("neznama/metoda")
    assert err["error"]["code"] == -32601
    print("\n✔ MCP test prošel")


if __name__ == "__main__":
    main()
