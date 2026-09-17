"""Validate a bookmark reorganization plan before it is applied, and verify it after.

Structural invariants only. No classification logic, and never a write to a real
Chrome profile -- this script is read-only by design.

    python scripts/verify-plan.py
    python scripts/verify-plan.py --plan working/plan.tsv --rules bookmark-rules.md
    python scripts/verify-plan.py --plan working/plan.tsv --post --profile Default

Pre-apply (the default) reads working/manifest.tsv, working/snapshot.json and the
limits and protected folders in bookmark-rules.md, derives the tree the plan would
produce, runs eleven structural checks, and prints
the verdict, the failures with concrete examples, and a preview of the resulting
tree. Post-apply re-reads the live profile and diffs it against that derived tree.

Plan format -- tab separated, header op/target/dest/value/note, '#' and blank
lines ignored:

    MOVE      target=bookmark id   dest=destination folder path
    MOVETREE  target=folder id     dest=destination folder path (subtree moves whole)
    DELETE    target=bookmark id   note=reason
    ADD       dest=folder path     value=url            note=title
    RETITLE   target=node id       value=new title
    REURL     target=node id       value=new url
    ORDER     dest=parent path     value=comma-separated child folder names

Destination paths start with a root token BAR, OTHER or MOBILE, optionally
followed by /-separated folder names: "BAR/Personal/House & Property".

Ambiguities resolved here, stated so a reader can disagree with them:
  * Destination paths are FINAL paths. Folders named in a destination are created
    if they do not exist. A RETITLE of a folder is applied before paths are
    computed, so destinations must use the new name.
  * apply-plan.py drops every folder that ends up holding nothing, whether the
    plan emptied it or it was already empty in the snapshot, so this script
    drops them too when deriving the resulting tree. "No empty folders"
    therefore catches the one case apply cannot clean up for you: a folder the
    plan names as a destination but never fills.
  * Bookmarks the plan does not cover stay where they are while the resulting
    tree is derived, so the depth, top-level-count and empty-folder checks
    describe the tree an apply would really produce, not an idealised one.
  * MOVETREE reparents the folder itself: folder "News" moved to "BAR/Media"
    lands at "BAR/Media/News", and its own children one level below that.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from urllib.parse import urlsplit

from bookmark_rules import RulesError, is_protected, load_rules
from chrome_profile import profile_dir, stores

# Chrome's JSON keys; "synced" is Mobile bookmarks
ROOTS = (("bookmark_bar", "BAR"), ("other", "OTHER"), ("synced", "MOBILE"))
ROOT_TOKENS = ("BAR", "OTHER", "MOBILE")
EXAMPLES = 20


# --------------------------------------------------------------------------- tree

class Node:
    __slots__ = ("id", "type", "name", "url", "date_added", "children", "parent", "synthetic")

    def __init__(self, id, type, name, url="", date_added="", synthetic=False):
        self.id = id
        self.type = type
        self.name = name
        self.url = url
        self.date_added = date_added
        self.children = []
        self.parent = None
        self.synthetic = synthetic

    def add(self, child):
        child.parent = self
        self.children.append(child)

    def folders(self):
        return [c for c in self.children if c.type == "folder"]

    def urls(self):
        return [c for c in self.children if c.type == "url"]


def node_from_json(raw):
    node = Node(
        id=str(raw.get("id", "")),
        type=raw.get("type", "url"),
        name=raw.get("name", "") or "",
        url=raw.get("url", "") or "",
        date_added=str(raw.get("date_added", "")),
    )
    for child in raw.get("children", []) or []:
        node.add(node_from_json(child))
    return node


def load_tree(snapshot_path):
    """Return {root token: Node} plus {id: Node} for a Chrome Bookmarks file."""
    with open(snapshot_path, encoding="utf-8") as fh:
        data = json.load(fh)
    roots, index = {}, {}
    for key, token in ROOTS:
        raw = data.get("roots", {}).get(key)
        if not isinstance(raw, dict) or "type" not in raw:
            roots[token] = Node(id=f"root:{token}", type="folder", name=token)
            continue
        node = node_from_json(raw)
        node.name = token
        roots[token] = node
    for token, node in roots.items():
        for n in walk(node):
            if n is not node:
                index[n.id] = n
    return roots, index


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def path_key(node):
    """'BAR/A/B' for a folder, or the folder key holding a url node."""
    parts, cur = [], node
    while cur is not None:
        parts.append(cur.name)
        cur = cur.parent
    return "/".join(reversed(parts))


def folder_key(node):
    return path_key(node.parent) if node.type == "url" else path_key(node)


def subtree_urls(node):
    return [n for n in walk(node) if n.type == "url"]


def clone(node):
    copy = Node(node.id, node.type, node.name, node.url, node.date_added, node.synthetic)
    for child in node.children:
        copy.add(clone(child))
    return copy


def detach(node):
    if node.parent is not None:
        node.parent.children = [c for c in node.parent.children if c is not node]
        node.parent = None


# ----------------------------------------------------------------------- manifest

def load_manifest(path):
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\")
        return [dict(row) for row in reader]


# --------------------------------------------------------------------------- plan

class Op:
    __slots__ = ("line", "op", "target", "dest", "value", "note")

    def __init__(self, line, op, target, dest, value, note):
        self.line = line
        self.op = op
        self.target = target
        self.dest = dest
        self.value = value
        self.note = note

    def __str__(self):
        bits = [f"line {self.line}", self.op]
        if self.target:
            bits.append(f"target={self.target}")
        if self.dest:
            bits.append(f"dest={self.dest}")
        if self.value:
            bits.append(f"value={self.value}")
        return "  ".join(bits)


KNOWN_OPS = {"MOVE", "MOVETREE", "DELETE", "ADD", "RETITLE", "REURL", "ORDER"}


def load_plan(path):
    """Return (ops, parse_errors). Header line is skipped if present."""
    ops, errors = [], []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = [c.strip() for c in line.split("\t")]
            cols += [""] * (5 - len(cols))
            op = cols[0].upper()
            if lineno == 1 and op == "OP":
                continue
            if op not in KNOWN_OPS:
                errors.append(f"line {lineno}: unknown op {cols[0]!r}")
                continue
            ops.append(Op(lineno, op, cols[1], cols[2], cols[3], cols[4]))
    return ops, errors


def split_dest(dest):
    """('BAR', ['A','B']) for 'BAR/A/B'. Raises ValueError when malformed."""
    if not dest:
        raise ValueError("empty destination")
    parts = dest.split("/")
    if parts[0] not in ROOT_TOKENS:
        raise ValueError(f"root token must be one of {'/'.join(ROOT_TOKENS)}, got {parts[0]!r}")
    if any(p.strip() == "" for p in parts[1:]):
        raise ValueError("empty path segment")
    return parts[0], [p.strip() for p in parts[1:]]


# ----------------------------------------------------------------- derive the tree

def ensure_folder(roots, token, segments, counter):
    node = roots[token]
    for name in segments:
        match = next((f for f in node.folders() if f.name == name), None)
        if match is None:
            counter[0] += 1
            match = Node(id=f"new:f{counter[0]}", type="folder", name=name, synthetic=True)
            node.add(match)
        node = match
    return node


def build_result(source_roots, ops, problems):
    """Apply the plan to a copy of the snapshot tree. Returns (roots, added, deleted)."""
    roots = {token: clone(node) for token, node in source_roots.items()}
    index = {}
    for node in roots.values():
        for n in walk(node):
            if n is not node:
                index[n.id] = n
    counter = [0]
    added, deleted = [], []

    # 1. renames first, so destination paths are read against final folder names
    for op in ops:
        if op.op == "RETITLE" and op.target in index and op.value:
            index[op.target].name = op.value
        elif op.op == "REURL" and op.target in index and op.value:
            index[op.target].url = op.value

    # 2. deletions, so a MOVETREE carries only what survives
    dropped = set()
    for op in ops:
        if op.op == "DELETE" and op.target in index:
            node = index[op.target]
            detach(node)
            deleted.append(node)
            dropped.update(n.id for n in walk(node))

    # 3. moves, in plan order, so lazily created folders appear in plan order
    for op in ops:
        if op.op not in ("MOVE", "MOVETREE", "ADD"):
            continue
        try:
            token, segments = split_dest(op.dest)
        except ValueError:
            continue  # reported by the well-formed-destination check
        dest = ensure_folder(roots, token, segments, counter)
        if op.op in ("MOVE", "MOVETREE"):
            node = index.get(op.target)
            if node is None or node.id in dropped:
                continue  # unknown id, or already deleted -- reported by checks 1 and 2
            if dest is node or dest in list(walk(node)):
                problems.append(f"{op}: destination is inside the moved subtree")
                continue
            detach(node)
            dest.add(node)
        else:
            counter[0] += 1
            new = Node(id=f"new:b{counter[0]}", type="url", name=op.note,
                       url=op.value, synthetic=True)
            dest.add(new)
            added.append((new, dest))

    # 4. ordering
    declared = declared_folders(ops)
    for op in ops:
        if op.op != "ORDER":
            continue
        parent = find_folder(roots, op.dest)
        if parent is None:
            continue
        wanted = [n.strip() for n in op.value.split(",") if n.strip()]
        by_name = {f.name: f for f in parent.folders()}
        ordered = [by_name[n] for n in wanted if n in by_name]
        rest = [f for f in parent.folders() if f.name not in wanted]
        parent.children = parent.urls() + ordered + rest

    # 5. a folder the plan empties is assumed removed by the apply step;
    #    one that was already empty, or that the plan named and never filled, stays
    vacatable = {n.id for node in source_roots.values() for n in walk(node)
                 if n.type == "folder" and subtree_urls(n)}
    for node in roots.values():
        prune(node, node.name, declared, vacatable)
    return roots, added, deleted


def declared_folders(ops):
    """Every folder path the plan names explicitly, ancestors included."""
    out = set()
    for op in ops:
        dest = op.dest
        if op.op not in ("MOVE", "MOVETREE", "ADD", "ORDER") or not dest:
            continue
        try:
            token, segments = split_dest(dest)
        except ValueError:
            continue
        key = token
        out.add(key)
        for seg in segments:
            key = f"{key}/{seg}"
            out.add(key)
    return out


def prune(node, key, declared, vacatable):
    """Drop folders the plan emptied. Keep -- to be flagged -- the rest of the empties."""
    keep, kept_children = False, []
    for child in node.children:
        if child.type == "url":
            kept_children.append(child)
            keep = True
        elif prune(child, f"{key}/{child.name}", declared, vacatable):
            kept_children.append(child)
            keep = True
    node.children = kept_children
    if keep or key in declared:
        return True
    return False  # empty after the plan, and apply-plan.py drops empty folders


def find_folder(roots, dest):
    try:
        token, segments = split_dest(dest)
    except ValueError:
        return None
    node = roots.get(token)
    for name in segments:
        if node is None:
            return None
        node = next((f for f in node.folders() if f.name == name), None)
    return node


# ------------------------------------------------------------------------- checks

class Check:
    def __init__(self, num, name):
        self.num = num
        self.name = name
        self.failures = []
        self.warnings = []
        self.summary = ""

    @property
    def ok(self):
        return not self.failures


def label(node, manifest_by_id):
    row = manifest_by_id.get(node.id)
    title = node.name or (row or {}).get("title", "") or "(untitled)"
    where = folder_key(node) if node.parent is not None else (row or {}).get("path", "?")
    return f"{node.id:>7}  {title[:60]:60}  {where}"


def sample(items, n=EXAMPLES, render=str):
    """First n items rendered as strings, with a trailing '... and k more'."""
    shown = [render(i) for i in items[:n]]
    extra = len(items) - n
    if extra > 0:
        shown.append(f"... and {extra} more")
    return shown


def run_checks(manifest, source_roots, index, ops, result_roots, added, deleted, parse_errors, rules):
    manifest_by_id = {r["id"]: r for r in manifest}
    protected = rules["protected"]
    max_depth, max_top = rules["bar_max_depth"], rules["bar_max_folders"]
    bar_ids = [r["id"] for r in manifest if r["root"] == "BAR"
               and not is_protected("/".join(filter(None, (r["root"], r["path"]))), protected)]
    checks = []

    # 1 -- coverage of every BAR bookmark
    c = Check(1, "coverage: every unprotected BAR bookmark moved or deleted exactly once")
    cover = defaultdict(list)
    for op in ops:
        if op.op in ("MOVE", "DELETE") and op.target:
            cover[op.target].append(f"{op.op} line {op.line}")
        elif op.op == "MOVETREE" and op.target in index:
            folder = index[op.target]
            for bm in subtree_urls(folder):
                cover[bm.id].append(f"MOVETREE {folder.name!r} line {op.line}")
    uncovered = [i for i in bar_ids if i not in cover]
    doubled = [i for i in bar_ids if len(cover[i]) > 1]
    if uncovered:
        c.failures.append(f"{len(uncovered)} BAR bookmark(s) not covered by the plan:")
        for i in uncovered[:EXAMPLES]:
            c.failures.append("    " + manifest_line(manifest_by_id[i]))
        if len(uncovered) > EXAMPLES:
            c.failures.append(f"    ... and {len(uncovered) - EXAMPLES} more")
    if doubled:
        c.failures.append(f"{len(doubled)} BAR bookmark(s) covered more than once:")
        for i in doubled[:EXAMPLES]:
            c.failures.append("    " + manifest_line(manifest_by_id[i]))
            c.failures.append("        by " + "; ".join(cover[i]))
        if len(doubled) > EXAMPLES:
            c.failures.append(f"    ... and {len(doubled) - EXAMPLES} more")
    stale = [i for i in bar_ids if i not in index]
    if stale:
        c.warnings.append(f"{len(stale)} manifest id(s) absent from the snapshot -- "
                          "manifest and snapshot look out of step")
    c.summary = f"{len(bar_ids) - len(uncovered)}/{len(bar_ids)} covered"
    checks.append(c)

    # 2 -- no invented ids
    c = Check(2, "no invented ids: every plan target exists in the snapshot")
    for op in ops:
        if op.op == "ADD" or not op.target:
            continue
        if op.target not in index:
            c.failures.append(f"{op}: id not in snapshot")
    c.summary = f"{len(c.failures)} unknown id(s)"
    checks.append(c)

    # 3 -- target node types
    c = Check(3, "target types: MOVETREE hits folders, MOVE/DELETE/REURL hit bookmarks")
    want = {"MOVETREE": ("folder",), "MOVE": ("url",), "DELETE": ("url",),
            "REURL": ("url",), "RETITLE": ("url", "folder")}
    for op in ops:
        node = index.get(op.target)
        if node is None or op.op not in want:
            continue
        if node.type not in want[op.op]:
            c.failures.append(f"{op}: target is a {node.type}, expected "
                              f"{' or '.join(want[op.op])} ({node.name or node.url})")
    c.summary = f"{len(c.failures)} type mismatch(es)"
    checks.append(c)

    # 4 -- depth under BAR
    c = Check(4, f"depth: no bookmark more than {max_depth} folder levels under BAR")
    deep = []
    for bm in subtree_urls(result_roots["BAR"]):
        depth = len(folder_key(bm).split("/")) - 1
        if depth > max_depth:
            deep.append(bm)
    if deep:
        c.failures.append(f"{len(deep)} bookmark(s) land more than {max_depth} level(s) deep:")
        c.failures += ["    " + s for s in
                       sample(deep, render=lambda b: label(b, manifest_by_id))]
    c.summary = f"{len(deep)} too deep"
    checks.append(c)

    # 5 -- top-level folder budget
    c = Check(5, f"at most {max_top} top-level folders under BAR")
    tops = result_roots["BAR"].folders()
    if len(tops) > max_top:
        c.failures.append(f"{len(tops)} top-level folders, budget is {max_top}:")
        for f in tops:
            c.failures.append(f"    {f.name}  ({len(subtree_urls(f))} bookmarks)")
    c.summary = f"{len(tops)} top-level"
    checks.append(c)

    # 6 -- no empty folders
    c = Check(6, "no empty folders in the resulting tree")
    named = declared_folders(ops)
    empty, aside = [], []
    for token in ROOT_TOKENS:
        for node in walk(result_roots[token]):
            if node.type != "folder" or node.parent is None or subtree_urls(node):
                continue
            key = path_key(node)
            why = "named by the plan, nothing lands in it" if key in named \
                else "empty in the snapshot, plan leaves it standing"
            (empty if token == "BAR" or key in named else aside).append(f"{key}  ({why})")
    if empty:
        c.failures.append(f"{len(empty)} folder(s) end up empty:")
        c.failures += ["    " + p for p in sample(empty)]
    if aside:
        c.warnings.append(f"{len(aside)} empty folder(s) under OTHER/MOBILE the plan "
                          "never touches:")
        c.warnings += ["    " + p for p in sample(aside, 5)]
    c.summary = f"{len(empty)} empty"
    checks.append(c)

    # 7 -- ADD lines
    c = Check(7, "ADD lines carry a valid http(s) url, a title, and no duplicate")
    surviving = {}
    for token in ROOT_TOKENS:
        for bm in subtree_urls(result_roots[token]):
            surviving.setdefault(bm.url, []).append(bm)
    add_ops = [op for op in ops if op.op == "ADD"]
    seen_add = {}
    for op in add_ops:
        parts = urlsplit(op.value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            c.failures.append(f"{op}: not a well-formed http/https url")
        if not op.note.strip():
            c.failures.append(f"line {op.line} ADD {op.value}: empty title (note column)")
        clash = [b for b in surviving.get(op.value, []) if not b.synthetic]
        if clash:
            c.failures.append(f"line {op.line} ADD {op.value}: duplicates a surviving "
                              f"bookmark at {folder_key(clash[0])}")
        if op.value in seen_add:
            c.failures.append(f"line {op.line} ADD {op.value}: also added on line "
                              f"{seen_add[op.value]}")
        else:
            seen_add[op.value] = op.line
    c.summary = f"{len(add_ops)} ADD line(s)"
    checks.append(c)

    # 8 -- duplicate urls under BAR (warning only)
    c = Check(8, "duplicate urls among surviving BAR bookmarks")
    bar_urls = defaultdict(list)
    for bm in subtree_urls(result_roots["BAR"]):
        bar_urls[bm.url].append(bm)
    dupes = {u: v for u, v in bar_urls.items() if len(v) > 1 and u}
    if dupes:
        c.warnings.append(f"{len(dupes)} url(s) survive more than once under BAR:")
        c.warnings += ["    " + s for s in sample(
            sorted(dupes),
            render=lambda u: f"{u[:70]}  ->  " + ", ".join(folder_key(b) for b in dupes[u][:4]))]
    c.summary = f"{len(dupes)} duplicated url(s)"
    checks.append(c)

    # 9 -- ORDER directives
    c = Check(9, "ORDER directives name exactly the folders present at that parent")
    order_ops = [op for op in ops if op.op == "ORDER"]
    for op in order_ops:
        parent = find_folder(result_roots, op.dest)
        if parent is None:
            c.failures.append(f"{op}: parent folder does not exist in the resulting tree")
            continue
        actual = [f.name for f in parent.folders()]
        wanted = [n.strip() for n in op.value.split(",") if n.strip()]
        unknown = [n for n in wanted if n not in actual]
        missing = [n for n in actual if n not in wanted]
        dup = [n for n in set(wanted) if wanted.count(n) > 1]
        if unknown:
            c.failures.append(f"line {op.line} ORDER {op.dest}: names folders that do not "
                              f"exist there: {', '.join(unknown)}")
        if missing:
            c.failures.append(f"line {op.line} ORDER {op.dest}: does not name all children, "
                              f"missing: {', '.join(missing)}")
        if dup:
            c.failures.append(f"line {op.line} ORDER {op.dest}: repeats {', '.join(dup)}")
    c.summary = f"{len(order_ops)} ORDER line(s)"
    checks.append(c)

    # 10 -- destination paths well formed
    c = Check(10, "destination paths well formed")
    for op in ops:
        if op.op in ("MOVE", "MOVETREE", "ADD", "ORDER"):
            try:
                split_dest(op.dest)
            except ValueError as exc:
                c.failures.append(f"{op}: {exc}")
    for err in parse_errors:
        c.failures.append(err)
    c.summary = f"{len(c.failures)} malformed"
    checks.append(c)

    # 11 -- protected folders
    c = Check(11, "protected folders: the plan neither touches nor fills them")
    for path in protected:
        if find_folder(source_roots, path) is None:
            c.warnings.append(f"protected folder {path} is not in the snapshot")
    for op in ops:
        node = index.get(op.target) if op.target else None
        if node is not None:
            where = folder_key(node)
            if is_protected(where, protected):
                c.failures.append(f"{op}: target lies in protected {where}")
            elif node.type == "folder" and op.op in ("MOVETREE", "RETITLE") and any(
                    is_protected(p, [where]) for p in protected):
                c.failures.append(f"{op}: {where} holds a protected folder")
        if op.op in ("MOVE", "MOVETREE", "ADD", "ORDER") and op.dest:
            if is_protected(op.dest.rstrip("/"), protected):
                c.failures.append(f"{op}: destination lies in a protected folder")
    c.summary = f"{len(protected)} protected, {len(c.failures)} violation(s)"
    checks.append(c)

    return checks


def manifest_line(row):
    where = f"{row['root']}/{row['path']}" if row["path"] else f"{row['root']} (root)"
    return f"{row['id']:>7}  {(row['title'] or '(untitled)')[:60]:60}  {where}"


# ------------------------------------------------------------------------ reports

def print_tree(roots, ops):
    print()
    print("RESULTING TREE")
    bar = roots["BAR"]
    loose = len(bar.urls())
    print(f"  BAR  --  {len(subtree_urls(bar))} bookmarks, "
          f"{len(bar.folders())} top-level folders, {loose} loose at the root")
    _print_folder(bar, "  ")
    touched = declared_folders(ops)
    for token in ("OTHER", "MOBILE"):
        node = roots[token]
        total = len(subtree_urls(node))
        named = [f for f in walk(node)
                 if f.type == "folder" and f is not node and path_key(f) in touched]
        if total == 0 and not named:
            continue
        print(f"  {token}  --  {total} bookmarks"
              + (f", {len(named)} folder(s) named by the plan" if named else ""))
        for f in named:
            print(f"    {path_key(f)}  --  {len(subtree_urls(f))} bookmarks")


def _print_folder(node, indent):
    for folder in node.folders():
        direct = len(folder.urls())
        total = len(subtree_urls(folder))
        extra = f" ({total} incl. subfolders)" if total != direct else ""
        item = "item" if direct == 1 else "items"
        print(f"{indent}  {folder.name}  --  {direct} {item}{extra}")
        _print_folder(folder, indent + "  ")


def report(checks, tree_roots, ops):
    failed = [c for c in checks if not c.ok]
    warned = [c for c in checks if c.warnings]
    print()
    if failed:
        print(f"VERDICT  FAIL -- {len(failed)} of {len(checks)} checks failed. "
              "Do not apply this plan.")
    else:
        print(f"VERDICT  PASS -- all {len(checks)} checks passed."
              + ("  (see warnings)" if warned else ""))
    print()
    print("CHECKS")
    for c in checks:
        state = "FAIL" if not c.ok else ("WARN" if c.warnings else "PASS")
        print(f"  {c.num:>2}  {state}  {c.name}  [{c.summary}]")

    if failed:
        print()
        print("FAILURES")
        for c in failed:
            print(f"  -- check {c.num}: {c.name}")
            for line in c.failures:
                print("     " + line)
    if warned:
        print()
        print("WARNINGS")
        for c in warned:
            print(f"  -- check {c.num}: {c.name}")
            for line in c.warnings:
                print("     " + line)

    print_tree(tree_roots, ops)
    return 1 if failed else 0


# ---------------------------------------------------------------------- post mode

def post_verify(source_roots, index, ops, expected_roots, added, live_path, device_path=None):
    """Diff the live profile against the tree the plan should have produced.

    live_path is the store that was reorganized. device_path, given under account
    storage, is the device store Chrome renders beside it; anything in it is a mismatch.
    """
    live_roots, live_index = load_tree(live_path)
    device_leftovers = []
    if device_path and os.path.exists(device_path):
        device_roots, _ = load_tree(device_path)
        for token in ROOT_TOKENS:
            for bm in subtree_urls(device_roots[token]):
                device_leftovers.append(f"{bm.id:>7}  {bm.name or bm.url:50.50}  at {folder_key(bm)}")

    expected = {}
    for token in ROOT_TOKENS:
        for bm in subtree_urls(expected_roots[token]):
            if not bm.synthetic:
                expected[bm.id] = bm
    actual = {}
    for token in ROOT_TOKENS:
        for bm in subtree_urls(live_roots[token]):
            actual[bm.id] = bm

    misplaced, missing, unexpected, drifted = [], [], [], []
    for bid, node in expected.items():
        live = actual.get(bid)
        if live is None:
            missing.append(f"{bid:>7}  {node.name or node.url:60.60}  expected at {folder_key(node)}")
            continue
        if folder_key(live) != folder_key(node):
            misplaced.append(f"{bid:>7}  {node.name or node.url:50.50}\n"
                             f"          expected {folder_key(node)}\n"
                             f"          found    {folder_key(live)}")
        src = index.get(bid)
        if src is not None and src.date_added != live.date_added:
            drifted.append(f"{bid:>7}  date_added {src.date_added} -> {live.date_added}")

    add_urls = {}
    for node, dest in added:
        add_urls.setdefault(node.url, path_key(dest))
    for bid, node in actual.items():
        if bid in expected:
            continue
        if node.url in add_urls:
            continue
        origin = index.get(bid)
        where = f" (was {folder_key(origin)} in the snapshot)" if origin is not None else ""
        unexpected.append(f"{bid:>7}  {node.name or node.url:50.50}  at {folder_key(node)}{where}")

    not_added = []
    for node, dest in added:
        want = path_key(dest)
        hit = [b for b in actual.values() if b.url == node.url and folder_key(b) == want]
        if not hit:
            not_added.append(f"        {node.url[:60]}  expected at {want}")

    folder_drift = []
    for token in ROOT_TOKENS:
        for f in walk(live_roots[token]):
            if f.type != "folder" or f.parent is None:
                continue
            if not subtree_urls(f):
                folder_drift.append(f"empty folder left behind: {path_key(f)}")

    problems = [
        ("bookmarks in the wrong place", misplaced),
        ("bookmarks missing from the profile", missing),
        ("bookmarks unexpectedly present", unexpected),
        ("ADD lines not applied", not_added),
        ("nodes whose date_added changed", drifted),
        ("bookmarks left in the device store", device_leftovers),
    ]
    total = sum(len(v) for _, v in problems)
    print()
    if total:
        print(f"VERDICT  FAIL -- {total} mismatch(es) between the live profile and the plan.")
    else:
        print(f"VERDICT  PASS -- live profile matches the plan; "
              f"{len(expected)} surviving bookmark(s) kept their id and date_added.")
    print()
    print("POST-APPLY DIFF")
    print(f"  snapshot bookmarks   {sum(len(subtree_urls(r)) for r in source_roots.values())}")
    print(f"  expected surviving   {len(expected)} + {len(added)} added")
    print(f"  live bookmarks       {len(actual)}")
    for title, items in problems:
        print(f"  {title:36} {len(items)}")
    for title, items in problems:
        if not items:
            continue
        print()
        print(f"-- {title}")
        for line in sample(items):
            print("   " + line)
    if folder_drift:
        print()
        print("-- notes")
        for line in sample(folder_drift, 10):
            print("   " + line)
    return 1 if total else 0


# ----------------------------------------------------------------------------- main

def main():
    try:  # bookmark titles are not ASCII; never die on a console that cannot show them
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Read-only: never writes to a Chrome profile.")
    ap.add_argument("--plan", default="working/plan.tsv", help="plan file (default: %(default)s)")
    ap.add_argument("--manifest", default="working/manifest.tsv",
                    help="manifest from read-profile.py (default: %(default)s)")
    ap.add_argument("--snapshot", default="working/snapshot.json",
                    help="snapshot from read-profile.py (default: %(default)s)")
    ap.add_argument("--rules", default="bookmark-rules.md",
                    help="limits and protected folders, written by /setup (default: %(default)s)")
    ap.add_argument("--post", action="store_true",
                    help="post-apply mode: diff the live profile against snapshot+plan")
    ap.add_argument("--profile", default="Default",
                    help="Chrome profile folder name, or a path to one, to re-read in --post mode")
    args = ap.parse_args()

    for label_, path in (("plan", args.plan), ("snapshot", args.snapshot)):
        if not os.path.exists(path):
            sys.exit(f"no {label_} at {path}")

    source_roots, index = load_tree(args.snapshot)
    ops, parse_errors = load_plan(args.plan)
    problems = []
    result_roots, added, deleted = build_result(source_roots, ops, problems)

    print(f"plan       {args.plan}  ({len(ops)} directive(s))")
    print(f"snapshot   {args.snapshot}")

    if args.post:
        live, device = stores(profile_dir(args.profile))
        if not os.path.exists(live):
            live, device = device, None
        if not os.path.exists(live):
            sys.exit(f"no Bookmarks file at {live}")
        print(f"live       {live}  (read-only)")
        if device:
            print(f"device     {device}  (rendered beside the account store, must be empty)")
        sys.exit(post_verify(source_roots, index, ops, result_roots, added, live, device))

    try:
        rules = load_rules(args.rules)
    except RulesError as exc:
        sys.exit(f"error: {exc}")
    print(f"rules      {args.rules}")

    if not os.path.exists(args.manifest):
        sys.exit(f"no manifest at {args.manifest}")
    manifest = load_manifest(args.manifest)
    print(f"manifest   {args.manifest}  ({len(manifest)} bookmark(s))")

    checks = run_checks(manifest, source_roots, index, ops, result_roots,
                        added, deleted, parse_errors + problems, rules)
    sys.exit(report(checks, result_roots, ops))


if __name__ == "__main__":
    main()
