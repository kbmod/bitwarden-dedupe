#!/usr/bin/env python3
"""Find duplicate Bitwarden logins and move extras into a review folder.

Uses the official Bitwarden CLI (`bw`) so decryption happens locally the same
way the desktop app does. Passwords are never written to the report.

Default is dry-run. Pass --apply to actually move items.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

ITEM_TYPE_LOGIN = 1
DEFAULT_FOLDER = "Duplicates - Review"


class BwError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Duplicate detection (pure; unit-tested)
# ---------------------------------------------------------------------------


def normalize_host(uri: str | None) -> str | None:
    if not uri or not str(uri).strip():
        return None
    raw = str(uri).strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def item_hosts(item: dict[str, Any]) -> frozenset[str]:
    login = item.get("login") or {}
    hosts: set[str] = set()
    for entry in login.get("uris") or []:
        uri = entry.get("uri") if isinstance(entry, dict) else None
        host = normalize_host(uri)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def item_username(item: dict[str, Any]) -> str:
    login = item.get("login") or {}
    return (login.get("username") or "").strip()


def item_password(item: dict[str, Any]) -> str:
    login = item.get("login") or {}
    return login.get("password") or ""


def richness(item: dict[str, Any]) -> int:
    """Prefer the copy that has 2FA, extra URIs, notes, custom fields, etc."""
    login = item.get("login") or {}
    score = 0
    if login.get("totp"):
        score += 100
    if item.get("favorite"):
        score += 50
    if (item.get("notes") or "").strip():
        score += 10
    score += min(len(login.get("uris") or []), 10)
    score += min(len(item.get("fields") or []), 20)
    score += len(item.get("attachments") or []) * 25
    if login.get("password"):
        score += 5
    if login.get("username"):
        score += 2
    return score


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pick_keeper(items: list[dict[str, Any]], keep: str) -> dict[str, Any]:
    if keep == "oldest":
        return min(
            items,
            key=lambda item: (_parse_iso(item.get("creationDate")), -richness(item)),
        )
    if keep == "newest":
        return max(
            items,
            key=lambda item: (_parse_iso(item.get("revisionDate")), richness(item)),
        )
    return max(
        items,
        key=lambda item: (richness(item), _parse_iso(item.get("revisionDate"))),
    )


def group_duplicates(
    items: list[dict[str, Any]],
    strategy: str,
    include_empty_usernames: bool = False,
) -> list[list[dict[str, Any]]]:
    """Return groups of 2+ items that look like the same login."""
    logins = [i for i in items if i.get("type") == ITEM_TYPE_LOGIN and i.get("id")]
    by_id = {i["id"]: i for i in logins}
    ids = list(by_id)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    buckets: dict[tuple, list[str]] = defaultdict(list)

    for item in logins:
        username = item_username(item)
        name = (item.get("name") or "").strip().lower()
        hosts = item_hosts(item)
        password = item_password(item)

        if strategy == "host-user":
            if not username and not include_empty_usernames:
                continue
            user_key = username.lower()
            if not hosts:
                if name:
                    buckets[("nohost", user_key, name)].append(item["id"])
                continue
            for host in hosts:
                buckets[("host", user_key, host)].append(item["id"])
        elif strategy == "name-user":
            if not username and not include_empty_usernames:
                continue
            if not name:
                continue
            buckets[("name", username.lower(), name)].append(item["id"])
        elif strategy == "exact":
            if not username and not include_empty_usernames:
                continue
            buckets[
                (
                    "exact",
                    username.lower(),
                    name,
                    password,
                    tuple(sorted(hosts)),
                )
            ].append(item["id"])
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    for members in buckets.values():
        if len(members) < 2:
            continue
        first = members[0]
        for other in members[1:]:
            union(first, other)

    clustered: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item_id in ids:
        clustered[find(item_id)].append(by_id[item_id])

    groups = [g for g in clustered.values() if len(g) >= 2]
    groups.sort(key=lambda g: ((g[0].get("name") or "").lower(), g[0]["id"]))
    return groups


def public_item_view(item: dict[str, Any]) -> dict[str, Any]:
    """Safe summary: no password, totp, or card numbers."""
    login = item.get("login") or {}
    uris = []
    for entry in login.get("uris") or []:
        if isinstance(entry, dict) and entry.get("uri"):
            uris.append(entry["uri"])
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "username": item_username(item) or None,
        "uris": uris,
        "hosts": sorted(item_hosts(item)),
        "folderId": item.get("folderId"),
        "organizationId": item.get("organizationId"),
        "favorite": bool(item.get("favorite")),
        "hasTotp": bool(login.get("totp")),
        "hasPassword": bool(login.get("password")),
        "hasNotes": bool((item.get("notes") or "").strip()),
        "fieldCount": len(item.get("fields") or []),
        "attachmentCount": len(item.get("attachments") or []),
        "richness": richness(item),
        "revisionDate": item.get("revisionDate"),
        "creationDate": item.get("creationDate"),
    }


def plan_moves(
    groups: list[list[dict[str, Any]]],
    keep: str,
    move_groups: bool,
    skip_folder_id: str | None,
    skip_org: bool,
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for group in groups:
        usable = []
        for item in group:
            if skip_folder_id and item.get("folderId") == skip_folder_id:
                continue
            if skip_org and item.get("organizationId"):
                continue
            usable.append(item)
        if len(usable) < 2:
            continue
        keeper = pick_keeper(usable, keep)
        extras = [i for i in usable if i["id"] != keeper["id"]]
        to_move = usable if move_groups else extras
        planned.append(
            {
                "keeper": public_item_view(keeper),
                "move": [public_item_view(i) for i in to_move],
                "groupSize": len(usable),
            }
        )
    return planned


# ---------------------------------------------------------------------------
# Bitwarden CLI
# ---------------------------------------------------------------------------


def bw_cmd(args: list[str], session: str | None, stdin: str | None = None) -> str:
    bw = shutil.which("bw")
    if not bw:
        raise BwError(
            "Bitwarden CLI (`bw`) is not installed.\n"
            "Install it, then log in:\n"
            "  npm install -g @bitwarden/cli\n"
            "  # or: snap install bw\n"
            "  # or: https://bitwarden.com/help/cli/\n"
            "  bw login\n"
            "  export BW_SESSION=$(bw unlock --raw)"
        )
    env = os.environ.copy()
    if session:
        env["BW_SESSION"] = session
    try:
        proc = subprocess.run(
            [bw, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise BwError(f"Failed to run bw: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise BwError(f"bw {' '.join(args)} failed: {err}")
    return proc.stdout


def bw_json(args: list[str], session: str | None) -> Any:
    raw = bw_cmd(args, session).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BwError(f"bw {' '.join(args)} returned invalid JSON") from exc


def require_unlocked(session: str | None) -> None:
    status = bw_json(["status"], session)
    if not isinstance(status, dict):
        raise BwError("Could not read `bw status`.")
    state = status.get("status")
    if state == "unauthenticated":
        raise BwError("Not logged in. Run: bw login")
    if state == "locked":
        raise BwError(
            "Vault is locked. Unlock in this shell first:\n"
            "  export BW_SESSION=$(bw unlock --raw)"
        )
    if state != "unlocked":
        raise BwError(f"Unexpected vault status: {state}")


def load_items_from_export(path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise BwError("Export file must be a Bitwarden JSON object.")
    if data.get("encrypted"):
        raise BwError(
            "This export is encrypted. Export an unencrypted JSON file from "
            "Bitwarden, or use the CLI (`bw unlock`) instead of --from-export."
        )
    items = data.get("items") or []
    folders = data.get("folders") or []
    if not isinstance(items, list):
        raise BwError("Export file has no items array.")
    return items, folders if isinstance(folders, list) else []


def ensure_folder(name: str, folders: list[dict[str, Any]], session: str, apply: bool) -> str:
    for folder in folders:
        if (folder.get("name") or "") == name:
            return folder["id"]
    if not apply:
        return "dry-run-folder-id"
    encoded = bw_cmd(["encode"], session, stdin=json.dumps({"name": name})).strip()
    created = json.loads(bw_cmd(["create", "folder", encoded], session))
    return created["id"]


def move_item(item: dict[str, Any], folder_id: str, session: str) -> None:
    updated = dict(item)
    updated["folderId"] = folder_id
    encoded = bw_cmd(["encode"], session, stdin=json.dumps(updated)).strip()
    bw_cmd(["edit", "item", item["id"], encoded], session)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Find duplicate Bitwarden logins and move the extras into a folder "
            "so you can review and delete them. Dry-run unless --apply is set."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually move items. Without this flag, only print a plan.",
    )
    p.add_argument(
        "--strategy",
        choices=("host-user", "name-user", "exact"),
        default="host-user",
        help=(
            "How to decide two logins are the same. "
            "host-user: same website host + username (default). "
            "name-user: same item name + username. "
            "exact: same name, username, password, and hosts."
        ),
    )
    p.add_argument(
        "--keep",
        choices=("richest", "newest", "oldest"),
        default="richest",
        help="Which copy to leave in place (default: richest = 2FA/notes/URIs).",
    )
    p.add_argument(
        "--move-groups",
        action="store_true",
        help="Move every item in a duplicate group, including the keeper.",
    )
    p.add_argument(
        "--folder",
        default=DEFAULT_FOLDER,
        help=f'Review folder name (default: "{DEFAULT_FOLDER}").',
    )
    p.add_argument(
        "--include-empty-usernames",
        action="store_true",
        help="Also group logins that have no username.",
    )
    p.add_argument(
        "--skip-org",
        action="store_true",
        help="Ignore items that belong to an organization.",
    )
    p.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not run `bw sync` first.",
    )
    p.add_argument(
        "--from-export",
        metavar="FILE",
        help="Read an unencrypted Bitwarden JSON export instead of the CLI (report only).",
    )
    p.add_argument(
        "--report",
        metavar="FILE",
        help="Write a JSON report (no passwords) to this path.",
    )
    p.add_argument(
        "--undo",
        metavar="FILE",
        help="Restore folderIds from a previous --report file.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Seconds to wait between edits (default: 0.15).",
    )
    p.add_argument(
        "--session",
        default=os.environ.get("BW_SESSION"),
        help="Session key. Defaults to $BW_SESSION.",
    )
    return p


def print_plan(planned: list[dict[str, Any]]) -> None:
    if not planned:
        print("No duplicates found.")
        return
    move_count = sum(len(g["move"]) for g in planned)
    print(f"Found {len(planned)} duplicate group(s); {move_count} item(s) would be moved.\n")
    for i, group in enumerate(planned, 1):
        keeper = group["keeper"]
        print(f"Group {i}: {keeper.get('name') or '(unnamed)'}")
        print(
            f"  KEEP  {keeper['id']}  user={keeper.get('username') or '-'}  "
            f"hosts={','.join(keeper.get('hosts') or []) or '-'}  "
            f"richness={keeper['richness']}"
        )
        for item in group["move"]:
            tag = "MOVE*" if item["id"] == keeper["id"] else "MOVE "
            print(
                f"  {tag} {item['id']}  user={item.get('username') or '-'}  "
                f"hosts={','.join(item.get('hosts') or []) or '-'}  "
                f"richness={item['richness']}"
            )
        print()


def cmd_undo(args: argparse.Namespace) -> int:
    require_unlocked(args.session)
    with open(args.undo, encoding="utf-8") as fh:
        report = json.load(fh)
    moved = report.get("moved") or []
    if not moved:
        print("Report has no moved items.")
        return 0
    if not args.apply:
        print(f"Would restore {len(moved)} item(s) to their previous folders. Re-run with --apply.")
        for row in moved:
            print(f"  {row.get('id')}  {row.get('name')} -> {row.get('previousFolderId')}")
        return 0
    ok = 0
    failed = 0
    for row in moved:
        item_id = row["id"]
        try:
            item = bw_json(["get", "item", item_id], args.session)
            item["folderId"] = row.get("previousFolderId")
            encoded = bw_cmd(["encode"], args.session, stdin=json.dumps(item)).strip()
            bw_cmd(["edit", "item", item_id, encoded], args.session)
            ok += 1
            print(f"Restored {item_id} ({row.get('name')})")
        except BwError as exc:
            failed += 1
            print(f"FAILED {item_id}: {exc}", file=sys.stderr)
        time.sleep(args.delay)
    print(f"Restored {ok}, failed {failed}.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.undo:
        return cmd_undo(args)

    folders: list[dict[str, Any]]
    if args.from_export:
        items, folders = load_items_from_export(args.from_export)
        if args.apply:
            raise BwError("--from-export can only generate a report. Use the CLI (no --from-export) to move items.")
    else:
        require_unlocked(args.session)
        if not args.no_sync:
            print("Syncing vault…")
            bw_cmd(["sync"], args.session)
        items = bw_json(["list", "items"], args.session) or []
        folders = bw_json(["list", "folders"], args.session) or []

    if not isinstance(items, list):
        raise BwError("bw list items did not return a list.")

    existing = next((f for f in folders if f.get("name") == args.folder), None)
    skip_folder_id = existing["id"] if existing else None

    groups = group_duplicates(items, args.strategy, args.include_empty_usernames)
    planned = plan_moves(
        groups,
        keep=args.keep,
        move_groups=args.move_groups,
        skip_folder_id=skip_folder_id,
        skip_org=args.skip_org,
    )
    print_plan(planned)

    items_by_id = {i["id"]: i for i in items if i.get("id")}
    moved_rows: list[dict[str, Any]] = []

    if args.apply and planned:
        folder_id = ensure_folder(args.folder, folders, args.session, apply=True)
        print(f"Moving extras into folder {args.folder!r} ({folder_id})…")
        for group in planned:
            for summary in group["move"]:
                item = items_by_id[summary["id"]]
                previous = item.get("folderId")
                try:
                    move_item(item, folder_id, args.session)
                    moved_rows.append(
                        {
                            "id": item["id"],
                            "name": item.get("name"),
                            "previousFolderId": previous,
                            "keeperId": group["keeper"]["id"],
                        }
                    )
                    print(f"  moved {item['id']}  {item.get('name')}")
                except BwError as exc:
                    print(f"  FAILED {item['id']}: {exc}", file=sys.stderr)
                time.sleep(args.delay)
        print(f"Moved {len(moved_rows)} item(s). Review them in Bitwarden under {args.folder!r}.")
        print("Nothing was deleted. Remove extras yourself after checking.")
    elif planned:
        print("Dry-run only. Re-run with --apply to move extras into the review folder.")

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "strategy": args.strategy,
        "keep": args.keep,
        "moveGroups": args.move_groups,
        "folderName": args.folder,
        "applied": bool(args.apply),
        "groups": planned,
        "moved": moved_rows,
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
        print(f"Wrote report to {args.report}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BwError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
