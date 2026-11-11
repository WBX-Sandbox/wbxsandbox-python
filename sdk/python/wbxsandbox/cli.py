"""Command line: `wbx connect <sandbox-id>` – interactive terminal in a running sandbox.

Configuration: WBXSANDBOX_URL (e.g. https://sandbox.wbx.cz) and WBXSANDBOX_API_KEY,
or --url / --key.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="wbx", description="WBX Sandbox command line")
    p.add_argument("--version", action="version", version=f"wbx {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    c = sub.add_parser("connect", help="open a terminal in a running sandbox",
                       description="Open an interactive shell in a running sandbox. Type exit to end it, "
                                   "Ctrl-] disconnects. The sandbox keeps running until its timeout.")
    c.add_argument("sandbox_id", help="sandbox ID, e.g. sb_n01_0123456789abcdef")
    c.add_argument("--url", help="API address (default: WBXSANDBOX_URL)")
    c.add_argument("--key", help="API key (default: WBXSANDBOX_API_KEY)")
    a = p.parse_args(argv)
    if a.cmd == "connect":
        from .terminal import connect
        return connect(a.sandbox_id, url=a.url, api_key=a.key)
    p.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
