"""Read a Chrome profile's bookmarks into a snapshot and a reviewable manifest.

Read-only. Chrome may be running.

    python scripts/read-profile.py --profile Default

Writes working/snapshot.json (verbatim copy of the store to reorganize) and
working/manifest.tsv (one line per bookmark, with usage from Chrome's retained history).

A signed-in profile under account storage keeps two stores: the account store
(AccountBookmarks), which syncs, and the device store (Bookmarks), which stays on this
machine. Chrome renders both. The account store is the one to reorganize, so it is read
whenever it exists.
"""
import argparse, csv, json, os, shutil, sqlite3, sys, tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from chrome_profile import profile_dir, stores

ROOTS = ("bookmark_bar", "other", "synced")  # Chrome's JSON keys; "synced" is Mobile bookmarks
ROOT_LABEL = {"bookmark_bar": "BAR", "other": "OTHER", "synced": "MOBILE"}
MANIFEST_FIELDS = ["root", "path", "id", "title", "url", "added", "visits", "days", "last",
                   "host_visits", "host_days"]


def chrome_time(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime(1601, 1, 1) + timedelta(microseconds=value)


def walk(node, path):
    """Yield (path, node) for every url node. path is the folder chain below the root."""
    if node.get("type") == "url":
        yield path, node
        return
    for child in node.get("children", []):
        child_path = path + [child["name"]] if child.get("type") == "folder" else path
        yield from walk(child, child_path)


def load_history(profile_path):
    """Return {url: (visits, days_active, last_visit)} for the retained history window."""
    src = os.path.join(profile_path, "History")
    if not os.path.exists(src):
        return {}, {}
    tmp = os.path.join(tempfile.mkdtemp(prefix="bm-hist-"), "History")
    shutil.copy2(src, tmp)
    try:
        con = sqlite3.connect(tmp)
        by_url, by_host = {}, defaultdict(lambda: [0, set()])
        days = defaultdict(set)
        for url, visit_time in con.execute(
            "select u.url, v.visit_time from visits v join urls u on u.id = v.url"
        ):
            when = chrome_time(visit_time)
            if when is not None:
                days[url].add(when.date())
        for url, visits, last in con.execute(
            "select url, visit_count, last_visit_time from urls"
        ):
            seen = days.get(url, set())
            by_url[url] = (visits, len(seen), chrome_time(last))
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            by_host[host][0] += visits
            by_host[host][1].update(seen)
        con.close()
        return by_url, {h: (v, len(d)) for h, (v, d) in by_host.items()}
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="Default",
                    help="Chrome profile folder name, such as Default or 'Profile 1', or a path to one")
    ap.add_argument("--out", default="working", help="output directory")
    ap.add_argument("--no-history", action="store_true", help="skip the 90-day usage join")
    args = ap.parse_args()

    profile_path = profile_dir(args.profile)
    bookmarks, device_store = stores(profile_path)
    if not os.path.exists(bookmarks):
        bookmarks = device_store
    if not os.path.exists(bookmarks):
        sys.exit(f"no Bookmarks file at {bookmarks}")

    os.makedirs(args.out, exist_ok=True)
    snapshot = os.path.join(args.out, "snapshot.json")
    shutil.copy2(bookmarks, snapshot)
    data = json.load(open(snapshot, encoding="utf-8"))

    by_url, by_host = ({}, {}) if args.no_history else load_history(profile_path)

    rows = []
    for root in ROOTS:
        node = data["roots"].get(root)
        if not isinstance(node, dict) or "type" not in node:
            continue
        for path, bm in walk(node, []):
            url = bm.get("url", "")
            visits, days, last = by_url.get(url, (0, 0, None))
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            host_visits, host_days = by_host.get(host, (0, 0))
            rows.append({
                "root": ROOT_LABEL[root],
                "path": "/".join(path),
                "id": bm["id"],
                "title": (bm.get("name") or "").replace("\t", " ").replace("\n", " "),
                "url": url,
                "added": (chrome_time(bm.get("date_added")) or datetime(1601, 1, 1)).date().isoformat(),
                "visits": visits,
                "days": days,
                "last": last.date().isoformat() if last else "",
                "host_visits": host_visits,
                "host_days": host_days,
            })

    manifest = os.path.join(args.out, "manifest.tsv")
    with open(manifest, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\")
        writer.writeheader()
        writer.writerows(rows)

    per_root = Counter(r["root"] for r in rows)
    folders = len({(r["root"], r["path"]) for r in rows})
    unused = sum(1 for r in rows if r["visits"] == 0)
    print(f"profile      {args.profile} ({data.get('roots', {}).get('bookmark_bar', {}).get('name', '?')})")
    print(f"store        {os.path.basename(bookmarks)}")
    print(f"snapshot     {snapshot}")
    print(f"manifest     {manifest}")
    print(f"bookmarks    {len(rows)}  " + "  ".join(f"{k}={v}" for k, v in sorted(per_root.items())))
    print(f"folders      {folders}")
    print(f"checksum     {data.get('checksum', '')[:16]}")
    if not args.no_history:
        print(f"unvisited    {unused} of {len(rows)} in the retained history window")


if __name__ == "__main__":
    main()
