// Make Chrome's live bookmark tree equal to target.json, written by apply-plan.py --target.
//
// No plan semantics here: the target is the finished tree. This module only walks it,
// moving, creating, updating and removing nodes through a chrome.bookmarks-shaped api,
// so it runs unchanged against Chrome and against MemoryBookmarks (dry run and tests).

const ROOT_TYPES = ["bookmarks-bar", "other", "mobile"];

function kind(node) {
  return node.url === undefined ? "folder" : "url";
}

// The three permanent folders of one store: syncing true is the account store, false the device store.
function storeRoots(tree, syncing) {
  const roots = {};
  for (const node of tree[0].children) {
    if (ROOT_TYPES.includes(node.folderType) && Boolean(node.syncing) === syncing) {
      roots[node.folderType] = node;
    }
  }
  return roots;
}

// [id, parentId, index, type, title, url] per non-root node, matching apply-plan.py's fingerprint.
function rows(roots) {
  const out = [];
  const visit = (node) => {
    (node.children || []).forEach((child, index) => {
      out.push([child.id, node.id, index, kind(child), child.title, child.url ?? null]);
      visit(child);
    });
  };
  for (const type of ROOT_TYPES) if (roots[type]) visit(roots[type]);
  return out;
}

function diffRows(expected, actual, limit = 10) {
  const problems = [];
  const live = new Map(actual.map((r) => [r[0], r]));
  for (const row of expected) {
    const got = live.get(row[0]);
    if (!got) problems.push(`node ${row[0]} "${row[4]}" is gone`);
    else if (JSON.stringify(got) !== JSON.stringify(row)) {
      problems.push(`node ${row[0]} changed: expected ${JSON.stringify(row)}, found ${JSON.stringify(got)}`);
    }
    live.delete(row[0]);
    if (problems.length >= limit) return problems;
  }
  for (const row of live.values()) {
    problems.push(`node ${row[0]} "${row[4]}" is new since the snapshot`);
    if (problems.length >= limit) break;
  }
  return problems;
}

function targetIds(target) {
  const ids = new Set();
  const visit = (node) => {
    if (node.id !== null) ids.add(node.id);
    (node.children || []).forEach(visit);
  };
  for (const type of ROOT_TYPES) visit(target.roots[type]);
  return ids;
}

// Is the live profile still the one target.json was built from?
export async function inspect(api, target, { resume = false } = {}) {
  if (target.format !== 2) {
    return [`target.json has format ${target.format}, this extension reads format 2; re-run apply-plan.py --target`];
  }
  const problems = [];
  const tree = await api.getTree();
  const store = storeRoots(tree, target.syncing);
  for (const type of ROOT_TYPES) {
    if (!store[type]) problems.push(`no ${target.syncing ? "account" : "device"} store "${type}" folder in Chrome`);
    else if (store[type].id !== target.roots[type].id) {
      problems.push(`"${type}" has id ${store[type].id} in Chrome but ${target.roots[type].id} in target.json; re-run read-profile.py`);
    }
  }
  if (problems.length) return problems;

  const live = rows(store);
  if (resume) {
    const byId = new Map(live.map((r) => [r[0], r]));
    const wantType = new Map(target.before.map((r) => [r[0], r[3]]));
    for (const id of targetIds(target)) {
      if (!wantType.has(id)) continue; // a permanent root
      const got = byId.get(id);
      if (!got || got[3] !== wantType.get(id)) problems.push(`node ${id} is missing or changed type`);
    }
  } else {
    problems.push(...diffRows(target.before, live));
  }
  if (target.syncing) {
    const device = rows(storeRoots(tree, false));
    if (resume) {
      const expected = new Set(target.clear_device.map((r) => r[0]));
      for (const row of device) {
        if (!expected.has(row[0])) problems.push(`device store: node ${row[0]} "${row[4]}" is new since the snapshot`);
      }
    } else {
      problems.push(...diffRows(target.clear_device, device).map((p) => `device store: ${p}`));
    }
  }
  return problems;
}

async function place(api, parentId, children, stats) {
  for (let index = 0; index < children.length; index++) {
    const want = children[index];
    let id = want.id;
    if (id === null) {
      const spec = { parentId, index, title: want.title };
      if (want.type === "url") spec.url = want.url;
      id = (await api.create(spec)).id;
      stats.created++;
    } else {
      const [live] = await api.get(id);
      // Everything left of index is already final, so a node in this folder sits at or
      // right of index and only ever moves left, where Chrome's move index is unambiguous.
      if (live.parentId !== parentId || live.index !== index) {
        await api.move(id, { parentId, index });
        stats.moved++;
      }
      const changes = {};
      if (live.title !== want.title) changes.title = want.title;
      if (want.type === "url" && live.url !== want.url) changes.url = want.url;
      if (Object.keys(changes).length) {
        await api.update(id, changes);
        stats.updated++;
      }
    }
    if (want.type === "folder") await place(api, id, want.children, stats);
  }
}

// Remove every non-root node under the roots that keep() rejects: bookmarks first, then
// folders deepest first. chrome.bookmarks.remove refuses a non-empty folder, which is
// the safety net against removing anything that still holds a wanted node.
async function prune(api, syncing, keep, stats, counter) {
  const tree = await api.getTree();
  const doomed = [];
  const visit = (node, depth) => {
    for (const child of node.children || []) {
      if (!keep(child.id)) doomed.push({ node: child, depth });
      visit(child, depth + 1);
    }
  };
  for (const root of Object.values(storeRoots(tree, syncing))) visit(root, 0);
  doomed.sort((a, b) => (kind(a.node) === kind(b.node) ? b.depth - a.depth : kind(a.node) === "url" ? -1 : 1));
  for (const { node } of doomed) {
    await api.remove(node.id);
    stats[counter]++;
  }
}

export async function reconcile(api, target) {
  const stats = { created: 0, moved: 0, updated: 0, removed: 0, clearedDevice: 0 };
  for (const type of ROOT_TYPES) {
    await place(api, target.roots[type].id, target.roots[type].children, stats);
  }
  // After place() every folder starts with exactly its target children, in order. Created
  // nodes only have ids in Chrome, so read the wanted ids off that prefix of the live tree.
  const wanted = new Set();
  const keepPrefix = (live, want) => {
    wanted.add(live.id);
    (want.children || []).forEach((w, i) => keepPrefix(live.children[i], w));
  };
  const store = storeRoots(await api.getTree(), target.syncing);
  for (const type of ROOT_TYPES) keepPrefix(store[type], target.roots[type]);
  await prune(api, target.syncing, (id) => wanted.has(id), stats, "removed");
  if (target.syncing) await prune(api, false, () => false, stats, "clearedDevice");
  return stats;
}

// Differences between the live tree and the target; empty means done.
export async function compare(api, target, limit = 20) {
  const diffs = [];
  const tree = await api.getTree();
  const store = storeRoots(tree, target.syncing);
  const walk = (live, want, path) => {
    const got = live.children || [];
    const exp = want.children || [];
    if (got.length !== exp.length) diffs.push(`${path}: ${got.length} children, expected ${exp.length}`);
    for (let i = 0; i < Math.min(got.length, exp.length) && diffs.length < limit; i++) {
      const g = got[i];
      const w = exp[i];
      const where = `${path}/${w.title}`;
      if ((w.id !== null && g.id !== w.id) || kind(g) !== w.type || g.title !== w.title || (w.type === "url" && g.url !== w.url)) {
        diffs.push(`${where}: found ${kind(g)} ${g.id} "${g.title}" ${g.url ?? ""}`);
      } else if (w.type === "folder") {
        walk(g, w, where);
      }
    }
  };
  for (const type of ROOT_TYPES) walk(store[type], target.roots[type], type);
  if (target.syncing) {
    const leftovers = rows(storeRoots(tree, false));
    if (leftovers.length) diffs.push(`device store still holds ${leftovers.length} node(s)`);
  }
  return diffs.slice(0, limit);
}

// In-memory stand-in for chrome.bookmarks with Chrome's move semantics, for dry runs and tests.
export class MemoryBookmarks {
  constructor(tree) {
    this.nodes = new Map();
    this.root = this.#adopt(structuredClone(tree[0]), undefined);
    this.nextId = Math.max(...[...this.nodes.keys()].map(Number)) + 1;
  }

  #adopt(node, parentId) {
    node.parentId = parentId;
    this.nodes.set(node.id, node);
    (node.children || []).forEach((child) => this.#adopt(child, node.id));
    return node;
  }

  #node(id) {
    const node = this.nodes.get(id);
    if (!node) throw new Error(`Can't find bookmark for id ${id}.`);
    return node;
  }

  #reindex(folder) {
    folder.children.forEach((child, i) => {
      child.index = i;
      child.parentId = folder.id;
    });
  }

  #view(node) {
    const copy = { ...node };
    if (node.children) copy.children = node.children.map((c) => this.#view(c));
    return copy;
  }

  async getTree() {
    this.#reindex(this.root);
    const fix = (n) => n.children && (this.#reindex(n), n.children.forEach(fix));
    fix(this.root);
    return [this.#view(this.root)];
  }

  async get(id) {
    const node = this.#node(id);
    const { children, ...rest } = node;
    return [rest];
  }

  async move(id, { parentId, index }) {
    const node = this.#node(id);
    const parent = this.#node(parentId);
    if (node.parentId === undefined || node.folderType) throw new Error("Can't modify the root bookmark folders.");
    if (!parent.children) throw new Error("Can't move to a bookmark, only a folder.");
    for (let p = parent; p; p = this.nodes.get(p.parentId)) {
      if (p === node) throw new Error("Can't move a folder into itself or a descendant.");
    }
    const old = this.#node(node.parentId);
    const oldIndex = old.children.indexOf(node);
    let at = index ?? parent.children.length;
    if (at > parent.children.length) throw new Error("Index out of bounds.");
    // BookmarkModel::Move: within one folder the index counts the node's own slot.
    if (old === parent && (at === oldIndex || at === oldIndex + 1)) return [node];
    if (old === parent && at > oldIndex) at--;
    old.children.splice(oldIndex, 1);
    this.#reindex(old);
    parent.children.splice(at, 0, node);
    this.#reindex(parent);
    return [node];
  }

  async create({ parentId, index, title, url }) {
    const parent = this.#node(parentId);
    const node = { id: String(this.nextId++), title: title ?? "", syncing: parent.syncing };
    if (url !== undefined) node.url = url;
    else node.children = [];
    const at = index ?? parent.children.length;
    if (at > parent.children.length) throw new Error("Index out of bounds.");
    parent.children.splice(at, 0, node);
    this.nodes.set(node.id, node);
    this.#reindex(parent);
    return node;
  }

  async update(id, { title, url }) {
    const node = this.#node(id);
    if (title !== undefined) node.title = title;
    if (url !== undefined) node.url = url;
    return node;
  }

  async remove(id) {
    const node = this.#node(id);
    if (node.folderType || node.parentId === undefined) throw new Error("Can't modify the root bookmark folders.");
    if (node.children && node.children.length) throw new Error("Can't remove non-empty folder (use recursive to force).");
    const parent = this.#node(node.parentId);
    parent.children.splice(parent.children.indexOf(node), 1);
    this.#reindex(parent);
    this.nodes.delete(id);
  }
}
