"""Tests for the scripts: rules, profile paths, verify, apply and rekey.

A signed-in profile under account storage holds two stores: the device store (Bookmarks)
and the account store (AccountBookmarks). Chrome renders both. The pipeline once wrote and
verified only the device store, so a correct-looking apply sat next to the untouched
account tree and the bar showed old and new folders together. Several tests guard that.

    python -m unittest discover -s tests
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import bookmark_rules  # noqa: E402
import chrome_profile  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(SCRIPTS, name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply_plan = load_script("apply-plan.py")


def url(id, guid, name, href):
    return {"id": str(id), "guid": guid, "name": name, "type": "url", "url": href,
            "date_added": "13300000000000000", "date_last_used": "0"}


def folder(id, guid, name, children):
    return {"id": str(id), "guid": guid, "name": name, "type": "folder", "children": children,
            "date_added": "13300000000000000", "date_last_used": "0", "date_modified": "0"}


def bookmarks_file(bar, other=(), mobile=(), offset=0):
    """A Chrome Bookmarks file. offset shifts every id, as Chrome does for AccountBookmarks."""
    def shift(node):
        node = dict(node, id=str(int(node["id"]) + offset))
        if "children" in node:
            node["children"] = [shift(c) for c in node["children"]]
        return node

    data = {
        "roots": {
            "bookmark_bar": shift(folder(1, "00000000-0000-4000-a000-000000000002", "Bookmarks bar", list(bar))),
            "other": shift(folder(2, "00000000-0000-4000-a000-000000000003", "Other bookmarks", list(other))),
            "synced": shift(folder(3, "00000000-0000-4000-a000-000000000004", "Mobile bookmarks", list(mobile))),
        },
        "version": 1,
    }
    data["checksum"] = apply_plan.checksum(data)
    return data


def shared_folder():
    # deliberately unsorted: a folder the plan never touches keeps its order
    return folder(20, "g-shared", "Shared", [url(21, "g-z", "Zulu", "https://z.example/"),
                                             url(22, "g-y", "Yankee", "https://y.example/")])


def old_tree(offset=0):
    bar = [folder(10, "g-old", "Old", [url(11, "g-a", "Alpha", "https://a.example/"),
                                       url(12, "g-b", "Beta", "https://b.example/")])]
    return bookmarks_file(bar, mobile=[shared_folder()], offset=offset)


def new_tree(offset=0):
    bar = [folder(30, "g-new", "New", [url(11, "g-a", "Alpha", "https://a.example/"),
                                       url(12, "g-b", "Beta", "https://b.example/")])]
    return bookmarks_file(bar, mobile=[shared_folder()], offset=offset)


PLAN = "op\ttarget\tdest\tvalue\tnote\nMOVE\t{a}\tBAR/New\t\tAlpha\nMOVE\t{b}\tBAR/New\t\tBeta\n"
RULES = "---\nbar_max_folders: {folders}\nbar_max_depth: 2\nprotected: MOBILE/Shared\n---\n\n## Taste\n"


class Workspace:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.profile = os.path.join(self.dir, "profile")
        os.makedirs(self.profile)

    def write(self, relpath, content):
        path = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content, indent=3))
        return path

    def run(self, script, *args):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                              cwd=self.dir, capture_output=True, text=True, encoding="utf-8")


# ------------------------------------------------------------------------ rules


class Rules(unittest.TestCase):
    def test_front_block_is_read_and_prose_ignored(self):
        rules = bookmark_rules.parse_rules(
            "---\nbar_max_folders: 8\nbar_max_depth: 2\nprotected: MOBILE/Shared\n"
            "protected: OTHER/Kids, school\n---\n\nprotected: BAR/not a rule\n")
        self.assertEqual(rules, {"bar_max_folders": 8, "bar_max_depth": 2,
                                 "protected": ["MOBILE/Shared", "OTHER/Kids, school"]})

    def test_missing_limits_unknown_keys_and_bad_paths_are_refused(self):
        for text in ("bar_max_folders: 8\n",
                     "---\nbar_max_folders: 8\n---\n",
                     "---\nbar_max_folders: 8\nbar_max_depth: 2\nbar_colour: red\n---\n",
                     "---\nbar_max_folders: 0\nbar_max_depth: 2\n---\n",
                     "---\nbar_max_folders: 8\nbar_max_depth: 2\nprotected: Shared\n---\n"):
            with self.subTest(text=text), self.assertRaises(bookmark_rules.RulesError):
                bookmark_rules.parse_rules(text)

    def test_protection_covers_subfolders_but_not_namesakes(self):
        self.assertTrue(bookmark_rules.is_protected("MOBILE/Shared/Deep", ["MOBILE/Shared"]))
        self.assertFalse(bookmark_rules.is_protected("MOBILE/Shared stuff", ["MOBILE/Shared"]))

    def test_the_example_file_parses(self):
        bookmark_rules.load_rules(os.path.join(ROOT, "bookmark-rules.example.md"))


class ProfilePaths(unittest.TestCase):
    def test_user_data_dir_per_platform(self):
        home = os.path.join("H", "me")
        self.assertEqual(
            chrome_profile.user_data_dir("win32", {"LOCALAPPDATA": "L"}, home),
            os.path.join("L", "Google", "Chrome", "User Data"))
        self.assertEqual(
            chrome_profile.user_data_dir("darwin", {}, home),
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"))
        self.assertEqual(
            chrome_profile.user_data_dir("linux", {}, home),
            os.path.join(home, ".config", "google-chrome"))

    def test_a_path_is_used_as_is(self):
        self.assertEqual(chrome_profile.profile_dir("some/profile"), "some/profile")


# ----------------------------------------------------------------------- verify


class VerifyBeforeApply(unittest.TestCase):
    def workspace(self, rules=None, plan=PLAN.format(a=11, b=12)):
        ws = Workspace()
        ws.write("profile/Bookmarks", old_tree())
        result = ws.run("read-profile.py", "--profile", ws.profile, "--no-history")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ws.write("working/plan.tsv", plan)
        if rules is not None:
            ws.write("bookmark-rules.md", rules)
        return ws

    def test_missing_rules_stop_with_a_pointer_to_setup(self):
        ws = self.workspace()
        result = ws.run("verify-plan.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/setup", result.stdout + result.stderr)

    def test_plan_within_the_rules_passes(self):
        ws = self.workspace(RULES.format(folders=8))
        result = ws.run("verify-plan.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bar_limit_comes_from_the_rules(self):
        plan = PLAN.format(a=11, b=12).replace("BAR/New\t\tBeta", "BAR/Two\t\tBeta")
        ws = self.workspace(RULES.format(folders=1), plan)
        result = ws.run("verify-plan.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("2 top-level folders, budget is 1", result.stdout)

    def test_touching_a_protected_folder_fails(self):
        for line in ("MOVE\t21\tBAR/New\t\t\n", "ADD\t\tMOBILE/Shared\thttps://n.example/\tNew\n",
                     "RETITLE\t20\t\tRenamed\t\n"):
            with self.subTest(line=line):
                ws = self.workspace(RULES.format(folders=8), PLAN.format(a=11, b=12) + line)
                result = ws.run("verify-plan.py")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL  protected folders", result.stdout)


class PostVerifySeesTheAccountStore(unittest.TestCase):
    def test_new_device_tree_beside_old_account_tree_fails(self):
        """Apply wrote the device store, sync restored the account store."""
        ws = Workspace()
        snapshot = ws.write("snapshot.json", old_tree())
        plan = ws.write("plan.tsv", PLAN.format(a=11, b=12))
        ws.write("profile/Bookmarks", new_tree())
        ws.write("profile/AccountBookmarks", old_tree(offset=1000))

        result = ws.run("verify-plan.py", "--plan", plan, "--snapshot", snapshot,
                        "--post", "--profile", ws.profile)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("AccountBookmarks", result.stdout)
        self.assertIn("left in the device store", result.stdout)

    def test_account_store_matching_plan_and_empty_device_store_passes(self):
        ws = Workspace()
        snapshot = ws.write("snapshot.json", old_tree(offset=1000))
        plan = ws.write("plan.tsv", PLAN.format(a=1011, b=1012))
        ws.write("profile/Bookmarks", bookmarks_file([]))
        ws.write("profile/AccountBookmarks", new_tree(offset=1000))

        result = ws.run("verify-plan.py", "--plan", plan, "--snapshot", snapshot,
                        "--post", "--profile", ws.profile)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# ------------------------------------------------------------------------ apply


class ApplyWritesOnlyTheTarget(unittest.TestCase):
    def workspace(self):
        ws = Workspace()
        ws.write("working/snapshot.json", old_tree(offset=1000))
        ws.write("plan.tsv", PLAN.format(a=1011, b=1012))
        ws.write("profile/Bookmarks", bookmarks_file([]))
        ws.write("profile/AccountBookmarks", old_tree(offset=1000))
        return ws

    def test_there_is_no_file_write_mode(self):
        ws = self.workspace()
        before = open(os.path.join(ws.profile, "AccountBookmarks"), encoding="utf-8").read()

        result = ws.run("apply-plan.py", "--profile", ws.profile, "--plan", "plan.tsv")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--target", result.stderr)
        self.assertEqual(open(os.path.join(ws.profile, "AccountBookmarks"), encoding="utf-8").read(), before)

    def test_target_backs_up_both_stores_and_writes_format_2(self):
        ws = self.workspace()

        result = ws.run("apply-plan.py", "--profile", ws.profile, "--plan", "plan.tsv",
                        "--target", "extension/target.json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(ws.dir, "extension", "target.json"), encoding="utf-8") as fh:
            target = json.load(fh)
        self.assertEqual(target["format"], 2)
        self.assertTrue(target["syncing"])
        self.assertIn("clear_device", target)
        [stamp] = os.listdir(os.path.join(ws.dir, "working", "backup"))
        self.assertEqual(sorted(os.listdir(os.path.join(ws.dir, "working", "backup", stamp))),
                         ["AccountBookmarks", "Bookmarks"])


class ReadProfileReadsTheAccountStore(unittest.TestCase):
    def test_snapshot_comes_from_the_account_store(self):
        ws = Workspace()
        ws.write("profile/Bookmarks", bookmarks_file([]))
        ws.write("profile/AccountBookmarks", old_tree(offset=1000))

        result = ws.run("read-profile.py", "--profile", ws.profile, "--out", "working", "--no-history")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(ws.dir, "working", "manifest.tsv"), encoding="utf-8") as fh:
            ids = {line.split("\t")[2] for line in fh.read().splitlines()[1:]}
        self.assertEqual(ids, {"1011", "1012", "1021", "1022"})


class RekeyPlan(unittest.TestCase):
    def test_ids_follow_guids_into_the_new_snapshot(self):
        ws = Workspace()
        old = ws.write("old.json", old_tree())
        new = ws.write("new.json", old_tree(offset=1000))
        plan = ws.write("plan.tsv", "# keep me\n" + PLAN.format(a=11, b=12) + "MOVETREE\t20\tOTHER\t\t\n")

        result = ws.run("rekey-plan.py", "--plan", plan, "--from", old, "--to", new)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(plan, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, "# keep me\n" + PLAN.format(a=1011, b=1012) + "MOVETREE\t1020\tOTHER\t\t\n")


class TargetTree(unittest.TestCase):
    def test_untouched_folder_outside_the_bar_keeps_its_order(self):
        ws = Workspace()
        data = old_tree(offset=1000)
        applier = apply_plan.Applier(data)
        applier.apply(apply_plan.load_plan(ws.write("plan.tsv", PLAN.format(a=1011, b=1012))))
        applier.finalize()
        shared = data["roots"]["synced"]["children"][0]
        self.assertEqual([c["name"] for c in shared["children"]], ["Zulu", "Yankee"])
        self.assertEqual([c["name"] for c in data["roots"]["bookmark_bar"]["children"]], ["New"])


if __name__ == "__main__":
    unittest.main()
