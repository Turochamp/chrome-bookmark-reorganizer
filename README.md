# Chrome Bookmark Reorganizer

A Claude Code project that reorganizes a Chrome profile's bookmarks from a plan you review.

Claude reads your bookmarks and how often you use them, then writes a plan: which bookmark
goes where, what gets merged, what the bookmarks bar looks like. You read the plan, a
script checks it, and a small Chrome extension applies it through Chrome's own bookmarks
API, so sync carries the result to your other devices.

The scripts and the extension make no decisions. Every judgment is a line in the plan,
and every line is yours to change before anything touches Chrome.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- Google Chrome 134 or later
- Python 3.10 or later, standard library only
- Windows or macOS. Linux should work but is untested.
- Node.js, only to run the extension's tests

## Quick start

```sh
git clone https://github.com/Turochamp/chrome-bookmark-reorganizer
cd chrome-bookmark-reorganizer
claude
```

Then ask Claude to reorganize your bookmarks. The first time, it runs setup (below) before
it plans anything.

On macOS, use `python3` wherever this README says `python`.

## Setup: your rules

Everyone wants a different bookmarks bar, so this repo ships no rules of its own. Run
`/setup` in Claude Code. It reads your profile and walks you through a few questions,
each with a suggestion drawn from your own bookmarks and history:

1. **Bar shape**: how many top-level folders, and how deep they may go
2. **Bar order**: alphabetical, by use, or an order you choose
3. **Protected folders**: folders the plan must never touch, such as one shared with
   someone else
4. **Untitled bar bookmarks**: keep icon-only buttons untitled
5. **Duplicates**: merge exact URL matches only, or near-duplicates too
6. **Cold material**: where bookmarks you rarely use should go
7. **Startup tabs**: optional advice, outside the plan

The answers land in `bookmark-rules.md` next to `CLAUDE.md`. The file is gitignored, so
your rules stay on your machine. `bookmark-rules.example.md` shows its shape. Its
front block holds the limits the scripts enforce, and the text below it holds your taste,
which Claude follows. Edit it by hand or run `/setup` again.

## How a run works

```sh
python scripts/read-profile.py --profile Default
# Claude writes working/plan.tsv; you review it
python scripts/verify-plan.py  --plan working/plan.tsv
python scripts/apply-plan.py   --profile Default --plan working/plan.tsv --target extension/target.json
# apply target.json with the extension (below)
python scripts/verify-plan.py  --plan working/plan.tsv --post --profile Default
```

Run the scripts from the repo root.

- **`read-profile.py`** copies the profile's bookmarks to `working/snapshot.json` and
  writes `working/manifest.tsv`: one row per bookmark with its folder, its visits and the
  number of days it was used in Chrome's retained history. It only reads, and Chrome may
  be running.
- **`verify-plan.py`** checks the plan before anything is written. Every bookmark is
  accounted for once, no ids are invented, the bar stays within your limits, no protected
  folder is touched and no folder is left empty. It prints the resulting tree. Nothing
  else enforces these checks, so always run it.
- **`apply-plan.py --target`** backs up the profile's bookmark files to
  `working/backup/<timestamp>/`. It then writes the finished tree to
  `extension/target.json`, with a fingerprint of the tree it started from. It writes
  nothing to the profile.
- **`verify-plan.py --post`** compares the live profile with the plan after the extension
  has run.
- **`rekey-plan.py`** carries a reviewed plan onto a fresh snapshot when Chrome has
  renumbered the bookmarks (matched by guid). Use it instead of rewriting a plan from scratch.

`--profile` takes a profile folder name. `Default` is the first profile. Other profiles
are named `Profile 1`, `Profile 2` and so on, and `chrome://version` shows the one you
are in under Profile Path. A full path works too.

| OS | Chrome profiles live in |
|---|---|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data\` |
| macOS | `~/Library/Application Support/Google/Chrome/` |
| Linux | `~/.config/google-chrome/` |

## Applying with the extension

1. Once: open `chrome://extensions`, turn on Developer mode, choose Load unpacked, pick
   the `extension/` folder and pin its icon.
2. Click the icon. The page checks that Chrome still holds the tree the target was built
   from, then runs the whole change in memory. Both checks must pass before Apply is enabled.
3. Tick the confirmation and click Apply. If it stops on an error, tick Resume and apply
   again.
4. Wait a few seconds, then run `verify-plan.py --post`.

Leave sync on the whole time.

## Why an extension, not a file edit

A signed-in profile keeps two bookmark files, and Chrome shows them merged:

- `AccountBookmarks`, the **account store**, syncs with your Google account.
- `Bookmarks`, the **device store**, stays on this machine.

Sync rewrites the account store from Google's servers. Editing it on disk is soon undone,
and editing the device store instead leaves the old tree beside the new one, with every
folder shown twice. The extension makes real moves through Chrome's bookmarks API, so
sync treats them as moves. It then empties the device store, so nothing appears twice.

`apply-plan.py` refuses to run when a bookmark exists only in the device store and the
plan does not keep it.

## Undo

There is no one-click undo, because sync would overwrite a restored file. The backup in
`working/backup/` holds both stores. To go back, build a plan from the backup, or import
the bookmarks in Chrome's bookmark manager.

## Privacy

`working/` holds a copy of all your bookmarks and a summary of your browsing history.
`extension/target.json` and `bookmark-rules.md` are personal too. All three are
gitignored. Don't commit them, and don't paste them into issues.

Claude reads the manifest while it plans, so your bookmark titles, URLs and visit counts go
to Anthropic's API as part of your Claude Code session.

## Plan file format

Tab-separated UTF-8, with the header row `op	target	dest	value	note`. Lines
starting with `#` are comments.

| op | target | dest | value | effect |
|---|---|---|---|---|
| MOVE | bookmark id | folder path | | move one bookmark |
| MOVETREE | folder id | folder path | | move a folder with everything under it |
| DELETE | bookmark id | | | remove a bookmark |
| ADD | | folder path | url | create a bookmark, title in `note` |
| RETITLE | node id | | new title | rename a bookmark or folder |
| REURL | bookmark id | | new url | repoint a bookmark |
| ORDER | | parent path | comma-separated folder names | left-to-right folder order |

Folder paths start with `BAR` (Bookmarks bar), `OTHER` (Other bookmarks) or `MOBILE`
(Mobile bookmarks), for example `BAR/Home/Garden`. Missing folders are created, and
folders left empty are removed. Bookmark ids, guids and dates survive a move, so sync
sees a move rather than a delete and a create.

You can write a plan without Claude. The scripts don't care who wrote it.

## Tests

```sh
python -m unittest discover -s tests
node --test tests/reconcile.test.mjs
```

To replay your own profile through the extension's logic without touching Chrome, point
`REPLAY_BACKUP` at the backup folder written together with `extension/target.json`:
`REPLAY_BACKUP=working/backup/<timestamp> node --test tests/reconcile.test.mjs`. It also
checks that your protected folders come through unchanged.

`docs/adr/` records the two decisions most likely to surprise you: why there is no
file-edit mode, and why the repo ships no rules.

## Vocabulary

`CONTEXT.md` defines the terms this repo uses: profile, account store, device store,
snapshot, manifest, plan, target and backup.

## License

MIT
