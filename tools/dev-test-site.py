#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Serve login forms locally for testing the browser integration.

Real sites are a poor test surface: they need accounts, they rate-limit, they
change without warning, and few offer a one-time-code field you can reach. This
serves a handful of forms from tools/test-site/ instead.

Submitting a form echoes back exactly what the browser sent, so filled values
can be read off rather than inferred from whether a login appeared to work.

    tools/dev-test-site.py                 # http://localhost:8765/
    tools/dev-test-site.py --port 9000

Pair with an entry for the same host; tools/make-dev-database.py creates two.
"""

from __future__ import annotations

import argparse
import html
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

SITE_DIR = Path(__file__).resolve().parent / "test-site"
DEFAULT_PORT = 8765

# Values worth masking in the echo would defeat its purpose -- seeing the
# password is the point -- but flag which fields the extension filled.
INTERESTING = ("username", "password", "otp")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_POST(self) -> None:  # noqa: N802 - required name
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(body, keep_blank_values=True)

        self._send_html(self._render(fields))

        summary = ", ".join(
            f"{name}={values[0]!r}" for name, values in sorted(fields.items())
        )
        print(f"submitted: {summary or '(nothing)'}", flush=True)

    def _render(self, fields: dict[str, list[str]]) -> str:
        rows = []
        for name in INTERESTING:
            value = (fields.get(name) or [""])[0]
            shown = (
                f"<td>{html.escape(value)}</td>"
                if value
                else '<td class="empty">(not filled)</td>'
            )
            rows.append(f"<tr><th>{html.escape(name)}</th>{shown}</tr>")

        for name, values in sorted(fields.items()):
            if name in INTERESTING:
                continue
            rows.append(
                f"<tr><th>{html.escape(name)}</th>"
                f"<td>{html.escape(values[0])}</td></tr>"
            )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Submitted &mdash; Cipher Bridge test site</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<main>
<h1>Submitted</h1>
<p class="note">What the browser actually sent:</p>
<table class="submitted">{"".join(rows)}</table>
<p><a href="/index.html">Back</a></p>
</main>
</body>
</html>
"""

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Never cache: the point is to see the current submission.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        if self.command == "GET":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Quieter than the default, which logs every asset request.
        if self.command != "GET" or not self.path.endswith((".css", ".ico")):
            super().log_message(fmt, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="interface to listen on; loopback by default",
    )
    args = parser.parse_args()

    if not SITE_DIR.is_dir():
        print(f"missing site directory: {SITE_DIR}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    url = f"http://localhost:{args.port}/"

    print(f"serving {SITE_DIR}")
    print(f"open    {url}")
    print("\nCtrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
