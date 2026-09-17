# CLAUDE.md

Chrome bookmark reorganizer. `README.md` holds the commands, the plan format and the
runbook; `CONTEXT.md` holds the vocabulary. Use its terms: account store, device store,
snapshot, manifest, plan, target.

## Your job

Classification. Read `working/manifest.tsv`, decide a destination for every bookmark the
user wants reorganized, and write `working/plan.tsv`. The scripts and the extension
contain no classification logic and must stay that way: if you are about to put a folder
name or a domain list in code, put it in the plan instead.

## The user's rules

Read `bookmark-rules.md` before planning. The front block holds limits and protected
folders that `verify-plan.py` enforces; the text below it is the user's taste, and it
overrides any habit of yours.

If `bookmark-rules.md` does not exist, do not plan. Offer to run `/setup` first. Never
fall back to the example file's values.

## Fixed rules

- Run `verify-plan.py` before `apply-plan.py --target`, and `verify-plan.py --post` after
  the extension has run. Show the user the verdict, not just "done".
- Apply only through the extension. Never write `AccountBookmarks` or `Bookmarks`, and
  never tell the user to turn sync off. `docs/adr/0001-apply-through-the-extension.md`
  says why.
- Visit counts decide how finely to split a folder and how to order it. On their own they
  are never a reason to move, archive or delete a bookmark: the history window misses
  seasonal and typed-URL use.
- The user reviews the plan before `apply-plan.py --target`. Summarize what moves, what is
  deleted and what the bar will look like, and wait for their go-ahead.
- `working/`, `extension/target.json` and `bookmark-rules.md` are personal. Never commit
  them or quote them into files that are.
