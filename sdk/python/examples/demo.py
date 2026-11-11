"""WBX Sandbox demo: create a sandbox, run code, work with files.

    export WBXSANDBOX_URL=https://sandbox.wbx.cz
    export WBXSANDBOX_API_KEY=<your API key>
    python3 examples/demo.py
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/examples/", 1)[0])
from wbxsandbox import Sandbox  # noqa: E402

t0 = time.time()
with Sandbox.create(template="base", timeout=120, metadata={"user": "demo"}, env={"GREETING": "hello"}) as sb:
    print(f"sandbox {sb.id} ready in {(time.time() - t0) * 1000:.0f} ms")

    print(sb.run("uname -a && echo $GREETING").stdout)

    r = sb.run_code("import math, platform\nprint('pi =', math.pi)\nprint(platform.python_version())")
    print("python:", r.stdout.strip(), "| exit", r.exit_code)

    sb.files.write("/home/user/hello.txt", "Hello from WBX Sandbox\n")
    print("file:", sb.files.read_text("/home/user/hello.txt").strip())
    print("ls:", [e["name"] for e in sb.files.list("/home/user")])

    print("streamed output:")
    sb.run("for i in 1 2 3; do echo step $i; sleep 0.3; done", on_stdout=lambda s: print("  >", s, end=""))

    r = sb.run("sleep 10", timeout=1)
    print("timeout works:", r.timed_out)

    r = sb.run("curl -sI https://example.com | head -1", timeout=15)
    print("internet:", r.stdout.strip())
print("sandbox destroyed")
