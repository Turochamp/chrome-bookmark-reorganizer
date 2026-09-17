"""Turn a reviewed plan into the target tree the extension applies inside Chrome.

This script holds ZERO classification logic. Every judgement -- what goes where,
what gets dropped, what the bar looks like -- lives in the plan file. This is pure
mechanics: parse the plan, rebuild the tree, write it for the extension.

    python scripts/read-profile.py --profile Default          # snapshot first
    python scripts/apply-plan.py --profile Default --plan working/plan.tsv --dry-run
    python scripts/apply-plan.py --profile Default --plan working/plan.tsv --target extension/target.json

It never writes to the profile. --target backs up both stores to
working/backup/<UTC timestamp>/ and writes the resulting tree to the target path, for the
extension in extension/ to apply through Chrome's bookmarks API. Chrome may be running.
docs/adr/0001-apply-through-the-extension.md says why there is no file-write mode.

PLAN FORMAT (tab separated, UTF-8, header line "op<TAB>target<TAB>dest<TAB>value<TAB>note").
Blank lines and lines starting with # are ignored.

    MOVE       target=bookmark id   dest=folder path         move one bookmark
    MOVETREE   target=folder id     dest=folder path         move a folder + subtree
    DELETE     target=bookmark id                 note=why   remove a bookmark
    ADD                             dest=folder path  value=url  note=title
    RETITLE    target=node id                       value=new title
    REURL      target=bookmark id                   value=new url
    ORDER                           dest=parent folder path  value=comma-separated
                                                             child folder names

Folder paths start with a root token BAR, OTHER or MOBILE and continue with
/-separated folder names, e.g. "BAR", "BAR/Daily", "OTHER/Archive/2019".
Missing destination folders are created.

RESULTING TREE
  * surviving nodes keep id, guid, date_added, date_last_used and meta_info verbatim,
    so Chrome sync sees a move rather than a delete plus a create;
  * new nodes get max-existing-id+1 and a fresh uuid4 guid;
  * bookmarks sort alphabetically (case-insensitive) inside every folder, except
    bookmarks sitting directly on BAR, which take the order of their MOVE lines;
  * outside BAR, a folder whose contents the plan leaves unchanged keeps its order;
  * child folders follow their parent's ORDER directive, else alphabetical;
  * BAR puts loose bookmarks left of the folders, every other folder puts its
    subfolders first;
  * folders left empty are dropped recursively (never the three permanent roots);
  * date_modified is refreshed only on folders whose child list actually changed.

Applying the same plan twice produces the same tree: ADD skips a url already present
in the destination and DELETE of an already-absent id is a no-op.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

from chrome_profile import profile_dir, stores

ROOTS = ("bookmark_bar", "other", "synced")  # Chrome's JSON keys; "synced" is Mobile bookmarks
ROOT_TOKEN = {"BAR": "bookmark_bar", "OTHER": "other", "MOBILE": "synced"}
TOKEN_FOR_ROOT = {v: k for k, v in ROOT_TOKEN.items()}
FIELDS = ("op", "target", "dest", "value", "note")
OPS = ("MOVE", "MOVETREE", "DELETE", "ADD", "RETITLE", "REURL", "ORDER")
CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def fail(message):
    """Print a clear error and exit non-zero."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def chrome_now():
    """Now as Chrome time: microseconds since 1601-01-01 UTC, decimal string."""
    seconds = datetime.now(timezone.utc).timestamp() + CHROME_EPOCH_OFFSET
    return str(int(seconds * 1_000_000))


# --------------------------------------------------------------------------- checksum


def checksum(data):
    """Chrome's Bookmarks checksum: MD5 over a depth-first walk of the three roots.

    Per node: id as UTF-8, name as UTF-16LE, then either the literal "url" plus the
    url as UTF-8, or the literal "folder" followed by the node's children. The
    permanent roots are hashed as folder nodes themselves.
    """
    digest = hashlib.md5()

    def visit(node):
        digest.update(node["id"].encode("utf-8"))
        digest.update(node.get("name", "").encode("utf-16-le"))
        if node.get("type") == "url":
            digest.update(b"url")
            digest.update(node.get("url", "").encode("utf-8"))
        else:
            digest.update(b"folder")
            for child in node.get("children", []):
                visit(child)

    for root in ROOTS:
        node = data.get("roots", {}).get(root)
        if isinstance(node, dict) and "id" in node:
            visit(node)
    return digest.hexdigest()


# --------------------------------------------------------------------------- plan


def split_path(spec, lineno):
    """"BAR/Personal/House & Property" -> ("bookmark_bar", ["Personal", "House & Property"])."""
    if not spec:
        fail(f"plan line {lineno}: destination path is empty")
    parts = spec.split("/")
    token = parts[0].strip()
    if token not in ROOT_TOKEN:
        fail(
            f"plan line {lineno}: path {spec!r} must start with BAR, OTHER or MOBILE"
        )
    names = [p.strip() for p in parts[1:]]
    if any(not name for name in names):
        fail(f"plan line {lineno}: path {spec!r} has an empty folder name")
    return ROOT_TOKEN[token], names


def show_path(rootkey, names):
    return "/".join([TOKEN_FOR_ROOT[rootkey], *names])


REQUIRED = {
    "MOVE": ("target", "dest"),
    "MOVETREE": ("target", "dest"),
    "DELETE": ("target",),
    "ADD": ("dest", "value"),
    "RETITLE": ("target", "value"),
    "REURL": ("target", "value"),
    "ORDER": ("dest", "value"),
}


def load_plan(path):
    """Parse plan.tsv into an ordered list of op dicts."""
    if not os.path.exists(path):
        fail(f"no plan file at {path}")
    with open(path, encoding="utf-8-sig") as fh:
        raw_lines = fh.read().splitlines()

    ops = []
    header_seen = False
    for lineno, raw in enumerate(raw_lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) > len(FIELDS):
            fail(
                f"plan line {lineno}: {len(fields)} tab-separated fields, expected at "
                f"most {len(FIELDS)} ({'/'.join(FIELDS)}); a title or note contains a tab"
            )
        fields += [""] * (len(FIELDS) - len(fields))
        entry = {name: value.strip() for name, value in zip(FIELDS, fields)}
        entry["lineno"] = lineno

        if not header_seen and entry["op"].lower() == "op":
            header_seen = True
            continue
        header_seen = True

        entry["op"] = entry["op"].upper()
        if entry["op"] not in OPS:
            fail(
                f"plan line {lineno}: unknown op {entry['op']!r}; expected one of "
                + ", ".join(OPS)
            )
        for required in REQUIRED[entry["op"]]:
            if not entry[required]:
                fail(f"plan line {lineno}: {entry['op']} needs a {required}")
        if entry["op"] == "ADD" and entry["target"]:
            fail(f"plan line {lineno}: ADD must leave target empty")
        ops.append(entry)

    if not ops:
        fail(f"{path} contains no operations")
    return ops


# --------------------------------------------------------------------------- applier


class Applier:
    """Rewrites a loaded Bookmarks structure in place according to a plan."""

    def __init__(self, data):
        self.data = data
        self.roots = data["roots"]
        self.index = {}  # id -> node
        self.parent = {}  # id -> parent node
        self.orig_children = {}  # folder id -> [child id] as loaded
        self.root_ids = set()
        self.max_id = 0
        self.now = chrome_now()
        self.counts = Counter()
        self.deleted_ids = set()
        self.bar_order = []  # bookmark ids MOVEd straight onto BAR, in plan order
        self.order_directives = {}  # (rootkey, path tuple) -> (names, lineno)
        self.warnings = []
        self.created_ids = set()
        self._scan()

    # -- scaffolding -------------------------------------------------------

    def _scan(self):
        for rootkey in ROOTS:
            node = self.roots.get(rootkey)
            if not isinstance(node, dict) or "id" not in node:
                fail(f"profile has no usable {rootkey!r} root")
            self.root_ids.add(node["id"])
            self._register_tree(node, None)

    def _register_tree(self, node, parent):
        node_id = node.get("id")
        if not node_id:
            fail(f"node {node.get('name', '?')!r} has no id")
        if node_id in self.index:
            fail(f"duplicate node id {node_id} in the profile")
        self.index[node_id] = node
        self.parent[node_id] = parent
        try:
            self.max_id = max(self.max_id, int(node_id))
        except ValueError:
            fail(f"node id {node_id!r} is not numeric")
        if node.get("type") == "folder":
            children = node.setdefault("children", [])
            self.orig_children[node_id] = [c["id"] for c in children]
            for child in children:
                self._register_tree(child, node)

    def warn(self, message):
        self.warnings.append(message)

    def _next_id(self):
        self.max_id += 1
        self.created_ids.add(str(self.max_id))
        return str(self.max_id)

    def new_folder(self, name):
        return {
            "children": [],
            "date_added": self.now,
            "date_last_used": "0",
            "date_modified": self.now,
            "guid": str(uuid.uuid4()),
            "id": self._next_id(),
            "name": name,
            "type": "folder",
        }

    def new_bookmark(self, name, url):
        return {
            "date_added": self.now,
            "date_last_used": "0",
            "guid": str(uuid.uuid4()),
            "id": self._next_id(),
            "name": name,
            "type": "url",
            "url": url,
        }

    def detach(self, node):
        parent = self.parent.get(node["id"])
        if parent is not None:
            parent["children"] = [c for c in parent["children"] if c is not node]
        self.parent[node["id"]] = None

    def attach(self, node, folder):
        folder.setdefault("children", []).append(node)
        self.parent[node["id"]] = folder

    def is_ancestor(self, maybe_ancestor, node):
        seen = node
        while seen is not None:
            if seen is maybe_ancestor:
                return True
            seen = self.parent.get(seen["id"])
        return False

    def resolve(self, rootkey, names, lineno, create=True):
        """Walk/create a folder path. Folder names match exactly, else case-insensitively."""
        node = self.roots[rootkey]
        for depth, name in enumerate(names):
            children = node.get("children", [])
            match = next(
                (c for c in children if c.get("type") == "folder" and c.get("name") == name),
                None,
            )
            if match is None:
                match = next(
                    (
                        c
                        for c in children
                        if c.get("type") == "folder"
                        and c.get("name", "").casefold() == name.casefold()
                    ),
                    None,
                )
                if match is not None:
                    self.warn(
                        f"plan line {lineno}: folder {name!r} matched existing "
                        f"{match['name']!r} (case-insensitive) under "
                        f"{show_path(rootkey, names[:depth])}"
                    )
            if match is None:
                if not create:
                    return None
                match = self.new_folder(name)
                self.attach(match, node)
                self.index[match["id"]] = match
                self.counts["folders_created"] += 1
            node = match
        return node

    def target_node(self, entry, want=None):
        """Look up an op's target. Returns None when the op should be skipped."""
        node_id, op, lineno = entry["target"], entry["op"], entry["lineno"]
        if node_id in self.deleted_ids:
            return None
        node = self.index.get(node_id)
        if node is None:
            fail(
                f"plan line {lineno}: {op} target id {node_id} is not in the profile; "
                "the plan is stale -- re-run read-profile.py and rebuild it"
            )
        if node["id"] in self.root_ids:
            fail(f"plan line {lineno}: {op} cannot target the permanent root {node['name']!r}")
        if want and node.get("type") != want:
            kind = "folder" if node.get("type") == "folder" else "bookmark"
            fail(f"plan line {lineno}: {op} target id {node_id} is a {kind}, expected a {want}")
        return node

    # -- ops ---------------------------------------------------------------

    def apply(self, ops):
        by_op = {name: [o for o in ops if o["op"] == name] for name in OPS}

        for entry in by_op["DELETE"]:
            node = self.index.get(entry["target"])
            if node is None:
                self.warn(
                    f"plan line {entry['lineno']}: DELETE target {entry['target']} "
                    "already absent, skipped"
                )
                continue
            node = self.target_node(entry, want="url")
            self.detach(node)
            self.deleted_ids.add(node["id"])
            self.index.pop(node["id"], None)
            self.counts["deleted"] += 1

        for entry in by_op["RETITLE"]:
            node = self.target_node(entry)
            if node is None:
                continue
            if node.get("name") != entry["value"]:
                node["name"] = entry["value"]
                self.counts["retitled"] += 1

        for entry in by_op["REURL"]:
            node = self.target_node(entry, want="url")
            if node is None:
                continue
            if node.get("url") != entry["value"]:
                node["url"] = entry["value"]
                self.counts["reurled"] += 1

        for entry in by_op["MOVETREE"]:
            node = self.target_node(entry, want="folder")
            if node is None:
                continue
            rootkey, names = split_path(entry["dest"], entry["lineno"])
            dest = self.resolve(rootkey, names, entry["lineno"])
            if self.is_ancestor(node, dest):
                fail(
                    f"plan line {entry['lineno']}: MOVETREE would move folder "
                    f"{node['name']!r} into itself ({entry['dest']})"
                )
            if self.parent.get(node["id"]) is not dest:
                self.counts["subtrees"] += 1
            self.detach(node)
            self.attach(node, dest)

        for entry in by_op["MOVE"]:
            node = self.target_node(entry, want="url")
            if node is None:
                continue
            rootkey, names = split_path(entry["dest"], entry["lineno"])
            dest = self.resolve(rootkey, names, entry["lineno"])
            self.detach(node)
            self.attach(node, dest)
            self.counts["moved"] += 1
            if rootkey == "bookmark_bar" and not names:
                self.bar_order.append(node["id"])

        for entry in by_op["ADD"]:
            rootkey, names = split_path(entry["dest"], entry["lineno"])
            dest = self.resolve(rootkey, names, entry["lineno"])
            existing = next(
                (
                    c
                    for c in dest.get("children", [])
                    if c.get("type") == "url" and c.get("url") == entry["value"]
                ),
                None,
            )
            if existing is not None:
                self.warn(
                    f"plan line {entry['lineno']}: ADD skipped, {entry['value']} is "
                    f"already in {entry['dest']}"
                )
                self.counts["add_skipped"] += 1
                continue
            node = self.new_bookmark(entry["note"] or entry["value"], entry["value"])
            self.attach(node, dest)
            self.index[node["id"]] = node
            self.counts["added"] += 1
            if rootkey == "bookmark_bar" and not names:
                self.bar_order.append(node["id"])

        for entry in by_op["ORDER"]:
            rootkey, names = split_path(entry["dest"], entry["lineno"])
            key = (rootkey, tuple(names))
            if key in self.order_directives:
                first = self.order_directives[key][1]
                fail(
                    f"plan line {entry['lineno']}: a second ORDER for {entry['dest']} "
                    f"(the first is on line {first})"
                )
            wanted = [name.strip() for name in entry["value"].split(",") if name.strip()]
            if not wanted:
                fail(f"plan line {entry['lineno']}: ORDER lists no folder names")
            self.order_directives[key] = (wanted, entry["lineno"])

    # -- finalise ----------------------------------------------------------

    @staticmethod
    def sort_key(node):
        name = node.get("name", "")
        return (name.casefold(), name, node["id"])

    def finalize(self):
        """Order every folder, drop the empties, refresh date_modified where it changed."""
        for rootkey in ROOTS:
            self._normalize(self.roots[rootkey], rootkey, (), rootkey == "bookmark_bar")
        for (rootkey, path), (wanted, lineno) in self.order_directives.items():
            folder = self.resolve(rootkey, list(path), lineno, create=False)
            if folder is None:
                self.warn(
                    f"plan line {lineno}: ORDER refers to {show_path(rootkey, list(path))}, "
                    "which does not exist in the result"
                )
                continue
            present = {c.get("name", "").casefold() for c in folder.get("children", [])}
            for name in wanted:
                if name.casefold() not in present:
                    self.warn(
                        f"plan line {lineno}: ORDER names folder {name!r}, which is not "
                        f"under {show_path(rootkey, list(path))}"
                    )

    def _normalize(self, node, rootkey, path, is_bar_root):
        kept = []
        for child in node.get("children", []):
            if child.get("type") == "folder":
                self._normalize(child, rootkey, path + (child.get("name", ""),), False)
                if not child.get("children"):
                    self.counts["folders_dropped"] += 1
                    continue
            kept.append(child)

        original = self.orig_children.get(node["id"], [])
        untouched = (
            rootkey != "bookmark_bar"
            and (rootkey, path) not in self.order_directives
            and sorted(original) == sorted(c["id"] for c in kept)
        )
        if untouched:
            position = {node_id: i for i, node_id in enumerate(original)}
            node["children"] = sorted(kept, key=lambda c: position[c["id"]])
            return

        folders = [c for c in kept if c.get("type") == "folder"]
        bookmarks = [c for c in kept if c.get("type") != "folder"]

        if is_bar_root:
            rank = {node_id: i for i, node_id in enumerate(self.bar_order)}
            placed = sorted(
                (b for b in bookmarks if b["id"] in rank), key=lambda b: rank[b["id"]]
            )
            untouched = [b for b in bookmarks if b["id"] not in rank]
            bookmarks = untouched + placed
        else:
            bookmarks.sort(key=self.sort_key)

        directive = self.order_directives.get((rootkey, path))
        if directive:
            wanted = directive[0]
            rank = {}
            for i, name in enumerate(wanted):
                rank.setdefault(name.casefold(), i)
            folders.sort(
                key=lambda f: (rank.get(f.get("name", "").casefold(), len(wanted)),)
                + self.sort_key(f)
            )
        else:
            folders.sort(key=self.sort_key)

        node["children"] = (bookmarks + folders) if is_bar_root else (folders + bookmarks)

        new_ids = [c["id"] for c in node["children"]]
        if self.orig_children.get(node["id"]) != new_ids:
            node["date_modified"] = self.now


# --------------------------------------------------------------------------- reporting


def tree_lines(data):
    """Folder tree with per-folder counts, for --dry-run."""
    lines = []

    def visit(node, depth, label):
        children = node.get("children", [])
        bookmarks = sum(1 for c in children if c.get("type") != "folder")
        folders = [c for c in children if c.get("type") == "folder"]
        counts = f"{bookmarks} bookmark{'' if bookmarks == 1 else 's'}"
        if folders:
            counts += f", {len(folders)} folder{'' if len(folders) == 1 else 's'}"
        lines.append(f"{'   ' * depth}{label}  ({counts})")
        for folder in folders:
            visit(folder, depth + 1, folder.get("name", ""))

    for rootkey in ROOTS:
        visit(data["roots"][rootkey], 0, TOKEN_FOR_ROOT[rootkey])
    return lines


FOLDER_TYPE = {"bookmark_bar": "bookmarks-bar", "other": "other", "synced": "mobile"}


def fingerprint(data):
    """[id, parentId, index, type, title, url] for every non-root node, as chrome.bookmarks sees it."""
    rows = []

    def visit(node):
        for index, child in enumerate(node.get("children", [])):
            rows.append([child["id"], node["id"], index, child.get("type"),
                         child.get("name", ""), child.get("url")])
            visit(child)

    for rootkey in ROOTS:
        visit(data["roots"][rootkey])
    return rows


def target_document(data, before, created_ids, device_rows, account_storage):
    """The tree the extension reconciles Chrome to. Existing nodes by id, new ones with id null."""
    def emit(node):
        out = {
            "id": None if node["id"] in created_ids else node["id"],
            "type": node.get("type"),
            "title": node.get("name", ""),
        }
        if node.get("type") == "url":
            out["url"] = node.get("url", "")
        else:
            out["children"] = [emit(c) for c in node.get("children", [])]
        return out

    return {
        "format": 2,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "syncing": account_storage,  # the store to reconcile, as chrome.bookmarks names it
        "roots": {FOLDER_TYPE[k]: emit(data["roots"][k]) for k in ROOTS},
        "before": before,
        "clear_device": device_rows,
    }


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn a reviewed plan.tsv into the target tree the extension applies.",
        epilog="All judgement lives in the plan file; this script is pure mechanics.",
    )
    parser.add_argument(
        "--profile",
        default="Default",
        help="Chrome profile folder name, such as Default or 'Profile 1', or a path to "
        "a profile folder. Default: Default",
    )
    parser.add_argument(
        "--plan",
        default="working/plan.tsv",
        help="tab-separated plan file to apply (default: working/plan.tsv)",
    )
    parser.add_argument(
        "--snapshot",
        default="working/snapshot.json",
        help="snapshot written by read-profile.py; its checksum must still match the "
        "live profile, otherwise the plan is stale (default: working/snapshot.json)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="write nothing; print the resulting tree",
    )
    mode.add_argument(
        "--target",
        metavar="PATH",
        help="back up both stores, then write the resulting tree to PATH for the "
        "extension in extension/ to apply inside Chrome",
    )
    args = parser.parse_args(argv)

    account_path, device_path = stores(profile_dir(args.profile))
    account_storage = os.path.exists(account_path)
    bookmarks_path = account_path if account_storage else device_path
    if not os.path.exists(bookmarks_path):
        fail(f"no Bookmarks file at {bookmarks_path}")

    ops = load_plan(args.plan)

    with open(bookmarks_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if "roots" not in data:
        fail(f"{bookmarks_path} has no 'roots' key; this is not a Chrome Bookmarks file")

    live_sum = data.get("checksum", "")
    recomputed = checksum(data)
    if live_sum and live_sum != recomputed:
        fail(
            f"the live Bookmarks file's stored checksum ({live_sum}) does not match its "
            f"own contents ({recomputed}); Chrome may be writing it right now, retry in a moment"
        )

    if not os.path.exists(args.snapshot):
        fail(f"no snapshot at {args.snapshot}; run: python scripts/read-profile.py --profile {args.profile}")
    with open(args.snapshot, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    if snapshot.get("checksum") != live_sum:
        fail(
            f"the profile changed since the snapshot was taken "
            f"(snapshot {snapshot.get('checksum', '?')}, live {live_sum}). The plan is "
            f"stale. Re-run: python scripts/read-profile.py --profile {args.profile}, "
            "then rekey or rebuild the plan against the new manifest."
        )

    before = fingerprint(data)
    applier = Applier(data)
    applier.apply(ops)
    applier.finalize()
    data["checksum"] = checksum(data)

    for message in applier.warnings:
        print(f"warning: {message}", file=sys.stderr)

    device_rows = []
    if account_storage and os.path.exists(device_path):
        with open(device_path, encoding="utf-8") as fh:
            device_rows = fingerprint(json.load(fh))
        kept_urls = {row[5] for row in fingerprint(data) if row[3] == "url"}
        orphans = [row for row in device_rows if row[3] == "url" and row[5] not in kept_urls]
        if orphans:
            fail(
                f"{len(orphans)} bookmark(s) exist only in the device store {device_path} and "
                "the plan does not produce them, so clearing that store would lose them. "
                "Add them to the plan (ADD) first. First few: "
                + "; ".join(f"{r[4] or '(untitled)'} <{r[5]}>" for r in orphans[:5])
            )

    backup_dir = ""
    if args.target:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        backup_dir = os.path.join("working", "backup", stamp)
        os.makedirs(backup_dir, exist_ok=True)
        for source in (account_path, device_path):
            if os.path.exists(source):
                shutil.copy2(source, backup_dir)
        document = target_document(data, before, applier.created_ids, device_rows, account_storage)
        os.makedirs(os.path.dirname(os.path.abspath(args.target)), exist_ok=True)
        with open(args.target, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=1)
        print(f"target written to {args.target}; nothing was written to the profile")
        print(f"device store nodes to clear: {len(device_rows)}")
        print()
    else:
        print("DRY RUN -- nothing was backed up or written")
        print()
        for line in tree_lines(data):
            print(line)
        print()

    counts = applier.counts
    print(f"plan            {args.plan}  ({len(ops)} operations)")
    print(f"profile         {bookmarks_path}")
    print(f"moved           {counts['moved']}")
    print(f"subtrees moved  {counts['subtrees']}")
    print(f"deleted         {counts['deleted']}")
    print(f"added           {counts['added']}" + (f"  ({counts['add_skipped']} already present)" if counts["add_skipped"] else ""))
    print(f"retitled        {counts['retitled']}")
    print(f"reurled         {counts['reurled']}")
    print(f"folders created {counts['folders_created']}")
    print(f"folders dropped {counts['folders_dropped']}")
    print(f"backup          {os.path.abspath(backup_dir) if backup_dir else '(nothing written)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
