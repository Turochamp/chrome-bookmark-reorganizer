"""Carry a reviewed plan's node ids from one snapshot to another, matched by guid.

Chrome gives a bookmark a new id when it moves between stores (for example when a
profile moves to account storage and its bookmarks land in the account store), but
the guid survives. This rewrites the target column in place and nothing else.

    python scripts/rekey-plan.py --plan working/plan.tsv --from old-snapshot.json --to working/snapshot.json
"""

import argparse
import json
import sys


def guid_index(path):
    """{id: guid} and {guid: id} over every node of a Bookmarks file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    by_id, by_guid = {}, {}

    def visit(node):
        by_id[node["id"]] = node["guid"]
        by_guid[node["guid"]] = node["id"]
        for child in node.get("children", []):
            visit(child)

    for root in ("bookmark_bar", "other", "synced"):
        visit(data["roots"][root])
    return by_id, by_guid


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--from", dest="old", required=True, help="snapshot the plan was written against")
    ap.add_argument("--to", dest="new", required=True, help="snapshot the plan should point into")
    args = ap.parse_args()

    old_ids, _ = guid_index(args.old)
    _, new_ids = guid_index(args.new)

    with open(args.plan, encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")

    rewritten, missing = 0, []
    for n, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) < 2 or line.lstrip().startswith("#") or not fields[1].strip():
            continue
        if fields[0].strip().lower() == "op":
            continue
        target = fields[1].strip()
        guid = old_ids.get(target)
        if guid is None or guid not in new_ids:
            missing.append(f"line {n + 1}: id {target}")
            continue
        fields[1] = new_ids[guid]
        lines[n] = "\t".join(fields)
        rewritten += 1

    if missing:
        print("error: no guid match, plan left unchanged:", file=sys.stderr)
        for item in missing[:20]:
            print(f"  {item}", file=sys.stderr)
        sys.exit(2)

    with open(args.plan, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))
    print(f"rekeyed {rewritten} target(s) in {args.plan}")


if __name__ == "__main__":
    main()
