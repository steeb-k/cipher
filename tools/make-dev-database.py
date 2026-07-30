#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Create a throwaway database for browser-integration testing.

The upstream fixture tests/data/Test2Groups.kdbx contains groups but no
entries, so get-logins can never match anything against it. This builds a
database with entries whose URLs point at sites that are easy to visit, so the
whole fill path can actually be exercised.

Credentials here are fake and the file is disposable -- do not put anything
real in it.

    tools/make-dev-database.py                    # writes ./cipher-dev.kdbx
    tools/make-dev-database.py --output /tmp/x.kdbx --password hunter2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pykeepass import create_database

DEFAULT_PASSWORD = "cipher-dev"

# A well-known test secret, not a real one.
TOTP_SECRET = "JBSWY3DPEHPK3PXP"

# The local test site, which is the only place the TOTP fill path can actually
# be exercised: it needs a one-time-code field, and real sites will not give you
# one without an account. Two entries, so the credential picker has a choice.
LOCAL_SITE = "http://localhost:8765/login.html"

# (title, username, password, url)
ENTRIES = [
    ("Local Test Site", "alice", "dev-local-pw", LOCAL_SITE),
    ("Local Test Site (second account)", "bob", "dev-local-pw-2", LOCAL_SITE),
    ("GitHub", "octocat", "dev-github-pw", "https://github.com/login"),
    ("GitHub Gist", "octocat", "dev-gist-pw", "https://gist.github.com"),
    ("Example", "alice", "dev-example-pw", "https://example.com"),
    ("Hacker News", "pg", "dev-hn-pw", "https://news.ycombinator.com/login"),
    ("Wikipedia", "jimbo", "dev-wiki-pw", "https://en.wikipedia.org/w/index.php"),
    # No URL: must never be offered for any site.
    ("Unmatched Entry", "nobody", "dev-nourl-pw", None),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path.cwd() / "cipher-dev.kdbx",
    )
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    args = parser.parse_args()

    if args.output.exists() and not args.force:
        print(f"{args.output} already exists (use --force)", file=sys.stderr)
        return 1

    if args.output.exists():
        args.output.unlink()

    db = create_database(str(args.output), password=args.password)
    for title, username, password, url in ENTRIES:
        entry = db.add_entry(db.root_group, title, username, password, url=url or "")
        # Only the first local entry gets a one-time password, so the picker
        # shows one credential with TOTP and one without.
        if title == "Local Test Site":
            entry.otp = TOTP_SECRET
    db.save()

    print(f"wrote    {args.output}")
    print(f"password {args.password}")
    print(f"entries  {len(ENTRIES)}")
    for title, username, _, url in ENTRIES:
        print(f"   {title:16} {username:10} {url or '(no url)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
