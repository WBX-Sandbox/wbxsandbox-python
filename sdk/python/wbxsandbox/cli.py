"""Command line: `wbx connect <sandbox-id>` – interactive terminal in a running sandbox.

Configuration: WBXSANDBOX_URL (e.g. https://sandbox.wbx.cz) and WBXSANDBOX_API_KEY,
or --url / --key-file (a file with the key, or - to read it from standard input).
A key on the command line (--key) is visible to other users in the process list and stays
in the shell history – it still works, but prints a warning.
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
    c.add_argument("--key-file", metavar="PATH", help="read the API key from a file, or - for standard input "
                                                       "(default: WBXSANDBOX_API_KEY)")
    c.add_argument("--key", help=argparse.SUPPRESS)  # nedoporučeno: klíč v seznamu procesů a v historii shellu
    a = p.parse_args(argv)
    if a.cmd == "connect":
        from .terminal import connect
        key = None
        if a.key_file:
            try:
                if a.key_file == "-":
                    if sys.stdin.isatty():
                        import getpass
                        key = getpass.getpass("API key: ")
                    else:
                        key = sys.stdin.readline()
                else:
                    with open(a.key_file, encoding="utf-8") as f:
                        key = f.readline()
            except OSError as e:
                sys.stderr.write(f"Cannot read the API key: {e}\n")
                return 2
            key = key.strip()
        elif a.key:
            sys.stderr.write("Warning: --key exposes the API key in the process list and shell history; "
                             "use WBXSANDBOX_API_KEY or --key-file instead.\n")
            key = a.key
        return connect(a.sandbox_id, url=a.url, api_key=key)
    p.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
