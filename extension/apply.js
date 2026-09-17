import { compare, inspect, MemoryBookmarks, reconcile } from "./reconcile.js";

const $ = (id) => document.getElementById(id);
let target;
let ready = false;

function show(el, ok, heading, lines = []) {
  el.innerHTML = "";
  const p = document.createElement("p");
  p.className = ok ? "ok" : "bad";
  p.textContent = heading;
  el.append(p);
  if (lines.length) {
    const pre = document.createElement("pre");
    pre.textContent = lines.join("\n");
    el.append(pre);
  }
}

async function check() {
  ready = false;
  $("apply").disabled = true;
  try {
    target = await (await fetch("target.json", { cache: "no-store" })).json();
  } catch (err) {
    show($("check"), false, "No readable target.json in the extension folder.", [String(err)]);
    return;
  }
  const resume = $("resume").checked;
  const problems = await inspect(chrome.bookmarks, target, { resume });
  if (problems.length) {
    show($("check"), false, "Chrome's bookmarks are not the ones target.json was built from. Nothing will be applied.", [
      ...problems,
      "",
      "Re-run read-profile.py, rekey or rebuild the plan, and apply-plan.py --target.",
    ]);
    show($("simulate"), false, "Skipped.");
    return;
  }
  show($("check"), true, `Matches the snapshot. target.json generated ${target.generated}.`);

  const memory = new MemoryBookmarks(await chrome.bookmarks.getTree());
  const stats = await reconcile(memory, target);
  const diffs = await compare(memory, target);
  if (diffs.length) {
    show($("simulate"), false, "The simulation does not reach the target. Nothing will be applied.", diffs);
    return;
  }
  show($("simulate"), true, "The simulation reaches the target exactly.", [JSON.stringify(stats, null, 1)]);
  ready = true;
  $("apply").disabled = !$("understand").checked;
}

$("understand").addEventListener("change", () => ($("apply").disabled = !(ready && $("understand").checked)));
$("resume").addEventListener("change", check);

$("apply").addEventListener("click", async () => {
  $("apply").disabled = true;
  $("understand").checked = false;
  show($("result"), true, "Applying… keep this tab open.");
  try {
    const stats = await reconcile(chrome.bookmarks, target);
    const diffs = await compare(chrome.bookmarks, target);
    if (diffs.length) show($("result"), false, "Applied, but Chrome does not match the target:", [JSON.stringify(stats), ...diffs]);
    else show($("result"), true, "Done. Chrome matches the target.", [JSON.stringify(stats, null, 1)]);
  } catch (err) {
    show($("result"), false, "Stopped on an error. Tick Resume to continue from here.", [String(err?.stack || err)]);
  }
});

check();
