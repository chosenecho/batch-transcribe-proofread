"""Composition entry point for a supervised publication CLI subprocess."""
from __future__ import annotations

import re
import socket
import sys

from bili_asr.cli.main import _main
from bili_asr.diagnostics import write_stderr
from bili_asr.services.publication_supervisor import publication_progress


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 2:
        write_stderr("publication worker: progress address and token are required")
        return 1
    port, token, *command = arguments
    if (len(port) > 5 or not port.isascii() or not port.isdigit()
            or not 1 <= int(port) <= 65535 or not re.fullmatch(r"[0-9a-f]{32}", token)):
        write_stderr("publication worker: invalid progress address or token")
        return 1
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as progress:
        progress.sendall(token.encode("ascii") + b"\n")
        with publication_progress(progress):
            return _main(command, _publication_worker=True)


if __name__ == "__main__":
    raise SystemExit(main())
