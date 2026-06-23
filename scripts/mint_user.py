#!/usr/bin/env python3
"""
Mint an alpha user — add one opaque Bearer token to users.json and print the
ready-to-share onboarding links.

The backend keys auth on an opaque token per user (no signup/OAuth in alpha) and
loads users.json ONCE at startup, so after minting you must restart the app
container for the new token to work. See webapp/server.py:_load_users.

Usage (run on the host, against the data volume):
    python3 scripts/mint_user.py \
        --users /data/users.json \
        --server https://gotcha-app.duckdns.org \
        --user-id alice --name Alice \
        [--cap-minutes 120] [--glossary Feather,BookingPal,AML]

Then:  docker compose -f deploy/docker-compose.yml restart app
"""
import argparse
import json
import os
import secrets
import sys
from urllib.parse import urlencode, quote


def main():
    ap = argparse.ArgumentParser(description="Mint an alpha user token for Gotcha.")
    ap.add_argument("--users", default=os.environ.get("GOTCHA_USERS_FILE", "users.json"),
                    help="path to users.json (default: $GOTCHA_USERS_FILE or ./users.json)")
    ap.add_argument("--server", required=True,
                    help="public backend base URL, e.g. https://gotcha-app.duckdns.org")
    ap.add_argument("--user-id", required=True, help="stable storage namespace, e.g. alice")
    ap.add_argument("--name", required=True,
                    help='display name = the "you" speaker in transcripts, e.g. Alice')
    ap.add_argument("--cap-minutes", type=float, default=None,
                    help="per-user usage cap (default: backend GOTCHA_DEFAULT_CAP_MIN)")
    ap.add_argument("--glossary", default=None,
                    help="comma-separated domain terms to override the default glossary")
    ap.add_argument("--token", default=None,
                    help="use a specific token instead of a generated one (testing)")
    args = ap.parse_args()

    server = args.server.rstrip("/")

    # Load existing users (token -> record). Tolerate a missing/empty file.
    users = {}
    if os.path.exists(args.users) and os.path.getsize(args.users) > 0:
        with open(args.users, encoding="utf-8") as f:
            users = json.load(f)

    if any(u.get("user_id") == args.user_id for u in users.values()):
        sys.exit(f"✗ user_id {args.user_id!r} already exists in {args.users} — "
                 f"pick another or remove it first.")

    token = args.token or ("gk_" + secrets.token_urlsafe(24))
    record = {"user_id": args.user_id, "display_name": args.name}
    if args.cap_minutes is not None:
        record["cap_minutes"] = args.cap_minutes
    if args.glossary:
        record["glossary"] = [t.strip() for t in args.glossary.split(",") if t.strip()]

    users[token] = record

    # Write atomically so a crash can't truncate the live users file.
    tmp = args.users + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(args.users)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, args.users)

    connect = f"gotcha://connect?" + urlencode({"server": server, "token": token})
    download = f"{server}/download.html?token={quote(token)}"

    print(f"✓ Minted user {args.user_id!r} ({args.name}) → {args.users}\n")
    print(f"  token:         {token}")
    print(f"  download page: {download}")
    print(f"  deep link:     {connect}\n")
    print("Share the download page link (it has the token baked into the "
          "'Open in Gotcha' button).")
    print("⚠  Restart the backend so it picks up the new token:")
    print("     docker compose -f deploy/docker-compose.yml restart app")


if __name__ == "__main__":
    main()
