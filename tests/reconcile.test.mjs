// node --test tests/
//
// The extension's reconcile logic against MemoryBookmarks. The last test is an opt-in replay
// of your own profile and is skipped unless REPLAY_BACKUP is set.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { compare, inspect, MemoryBookmarks, reconcile } from "../extension/reconcile.js";

const ROOTS = { bookmark_bar: "bookmarks-bar", other: "other", synced: "mobile" };

// A chrome.bookmarks.getTree() result from Bookmarks files, as Chrome shows a profile.
function chromeTree(stores) {
  const convert = (node, syncing, folderType) => {
    const out = { id: node.id, title: node.name ?? "", syncing };
    if (folderType) out.folderType = folderType;
    if (node.type === "url") out.url = node.url;
    else out.children = (node.children || []).map((c) => convert(c, syncing));
    return out;
  };
  const children = [];
  for (const { data, syncing } of stores) {
    for (const [key, type] of Object.entries(ROOTS)) children.push(convert(data.roots[key], syncing, type));
  }
  return [{ id: "0", title: "", children }];
}

const url = (id, name, href) => ({ id, name, type: "url", url: href });
const folder = (id, name, children) => ({ id, name, type: "folder", children });
const file = (bar, other = [], mobile = [], base = 0) => ({
  roots: {
    bookmark_bar: folder(String(base + 1), "Bookmarks bar", bar),
    other: folder(String(base + 2), "Other bookmarks", other),
    synced: folder(String(base + 3), "Mobile bookmarks", mobile),
  },
});

test("MemoryBookmarks.move follows Chrome's same-folder index rule", async () => {
  const api = new MemoryBookmarks(chromeTree([{ data: file([url("4", "A", "a:"), url("5", "B", "b:"), url("6", "C", "c:")]), syncing: false }]));
  await api.move("4", { parentId: "1", index: 2 }); // lands before C, not after it
  const [root] = await api.getTree();
  assert.deepEqual(root.children[0].children.map((c) => c.title), ["B", "A", "C"]);
});

test("reconcile reorders, creates, retitles and prunes, then clears the device store", async () => {
  const account = file(
    [folder("110", "Old", [url("111", "Beta", "https://b/"), url("112", "Alpha", "https://a/"), url("113", "Gone", "https://g/")])],
    [], [], 100);
  const local = file([folder("10", "New", [url("11", "Alpha", "https://a/")])]);
  const target = {
    format: 2,
    syncing: true,
    roots: {
      "bookmarks-bar": { id: "101", type: "folder", children: [
        { id: null, type: "folder", title: "New", children: [
          { id: "112", type: "url", title: "Alpha", url: "https://a/" },
          { id: "111", type: "url", title: "Beta 2", url: "https://b/" },
          { id: null, type: "url", title: "Created", url: "https://c/" },
        ] },
      ] },
      other: { id: "102", type: "folder", children: [] },
      mobile: { id: "103", type: "folder", children: [] },
    },
    before: [
      ["110", "101", 0, "folder", "Old", null],
      ["111", "110", 0, "url", "Beta", "https://b/"],
      ["112", "110", 1, "url", "Alpha", "https://a/"],
      ["113", "110", 2, "url", "Gone", "https://g/"],
    ],
    clear_device: [["10", "1", 0, "folder", "New", null], ["11", "10", 0, "url", "Alpha", "https://a/"]],
  };
  const api = new MemoryBookmarks(chromeTree([{ data: local, syncing: false }, { data: account, syncing: true }]));

  assert.deepEqual(await inspect(api, target), []);
  const stats = await reconcile(api, target);
  assert.deepEqual(await compare(api, target), []);
  assert.deepEqual(stats, { created: 2, moved: 2, updated: 1, removed: 2, clearedDevice: 2 });
});

test("an interrupted run can resume and still reaches the target", async () => {
  const account = file([folder("110", "Old", [url("111", "A", "https://a/"), url("112", "B", "https://b/")])], [], [], 100);
  const local = file([url("11", "A", "https://a/"), url("12", "B", "https://b/")]);
  const target = {
    format: 2,
    syncing: true,
    roots: {
      "bookmarks-bar": { id: "101", type: "folder", children: [
        { id: null, type: "folder", title: "New", children: [
          { id: "111", type: "url", title: "A", url: "https://a/" },
          { id: "112", type: "url", title: "B", url: "https://b/" },
        ] },
      ] },
      other: { id: "102", type: "folder", children: [] },
      mobile: { id: "103", type: "folder", children: [] },
    },
    before: [["110", "101", 0, "folder", "Old", null], ["111", "110", 0, "url", "A", "https://a/"], ["112", "110", 1, "url", "B", "https://b/"]],
    clear_device: [["11", "1", 0, "url", "A", "https://a/"], ["12", "1", 1, "url", "B", "https://b/"]],
  };
  const api = new MemoryBookmarks(chromeTree([{ data: local, syncing: false }, { data: account, syncing: true }]));
  // the first run got as far as creating the folder, moving one bookmark and clearing one local node
  const created = await api.create({ parentId: "101", index: 0, title: "New" });
  await api.move("111", { parentId: created.id, index: 0 });
  await api.remove("11");

  assert.notDeepEqual(await inspect(api, target), []);
  assert.deepEqual(await inspect(api, target, { resume: true }), []);
  await reconcile(api, target);
  assert.deepEqual(await compare(api, target), []);
});

test("inspect refuses a profile that changed since the snapshot", async () => {
  const account = file([url("104", "A", "https://a/")], [], [], 100);
  const target = {
    format: 2,
    syncing: true,
    roots: { "bookmarks-bar": { id: "101", children: [] }, other: { id: "102", children: [] }, mobile: { id: "103", children: [] } },
    before: [["104", "101", 0, "url", "A (renamed on the phone)", "https://a/"]],
    clear_device: [],
  };
  const api = new MemoryBookmarks(chromeTree([{ data: file([]), syncing: false }, { data: account, syncing: true }]));
  const problems = await inspect(api, target);
  assert.equal(problems.length, 1);
  assert.match(problems[0], /node 104 changed/);
});

test("inspect refuses a target.json in an older format", async () => {
  const api = new MemoryBookmarks(chromeTree([{ data: file([]), syncing: false }]));
  const problems = await inspect(api, { format: 1, syncing: false, roots: {}, before: [], clear_local: [] });
  assert.equal(problems.length, 1);
  assert.match(problems[0], /format 1/);
});

// Opt-in replay of your own profile: point REPLAY_BACKUP at the working/backup/<timestamp>
// folder that apply-plan.py --target wrote together with extension/target.json.
const backup = process.env.REPLAY_BACKUP;
const replay = backup && existsSync("extension/target.json");

// `protected: BAR/A/B` lines from the front block of bookmark-rules.md.
function protectedPaths(path = "bookmark-rules.md") {
  if (!existsSync(path)) return [];
  const [, front = ""] = readFileSync(path, "utf8").split(/^---\s*$/m);
  return front.split(/\r?\n/).map((l) => l.match(/^\s*protected\s*:\s*(.+?)\s*$/)).filter(Boolean).map((m) => m[1]);
}

test("your profile, replayed from a backup, reaches target.json and protected folders are untouched",
  { skip: !replay && "set REPLAY_BACKUP to a working/backup/<timestamp> folder" }, async () => {
  const read = (p) => JSON.parse(readFileSync(p, "utf8"));
  const target = read("extension/target.json");
  const stores = [];
  if (existsSync(`${backup}/Bookmarks`)) stores.push({ data: read(`${backup}/Bookmarks`), syncing: false });
  if (existsSync(`${backup}/AccountBookmarks`)) stores.push({ data: read(`${backup}/AccountBookmarks`), syncing: true });
  const api = new MemoryBookmarks(chromeTree(stores));

  const TYPE = { BAR: "bookmarks-bar", OTHER: "other", MOBILE: "mobile" };
  const subtree = (tree, path) => {
    const [token, ...names] = path.split("/");
    let node = tree[0].children.find((r) => Boolean(r.syncing) === target.syncing && r.folderType === TYPE[token]);
    for (const name of names) node = node?.children?.find((c) => c.children && c.title === name);
    const shape = (n) => ({ id: n.id, title: n.title, url: n.url, children: n.children?.map(shape) });
    return node && shape(node);
  };
  const guarded = protectedPaths();
  const treeBefore = await api.getTree();
  const before = guarded.map((p) => subtree(treeBefore, p));

  assert.deepEqual(await inspect(api, target), []);
  const stats = await reconcile(api, target);
  assert.deepEqual(await compare(api, target), []);

  const treeAfter = await api.getTree();
  guarded.forEach((p, i) => assert.deepEqual(subtree(treeAfter, p), before[i], `${p} changed`));
  console.log("replay:", stats, "protected:", guarded.length);
});
