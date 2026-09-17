# No rules ship with the repo; setup is required

The limits the scripts enforce (top-level bar folders, depth, protected folders) and the
taste Claude plans by live in the user's gitignored `bookmark-rules.md`, written by
`/setup` from their own bookmarks and history. `verify-plan.py` stops when the file is
missing instead of falling back to defaults. The tool started as one person's
reorganizer, with their limits hardcoded and their own folders named in the
instructions. Any default would be someone else's taste applied without anyone choosing
it. A protected folder that silently isn't protected is also the costliest mistake this
tool can make.

## Considered options

- **Defaults with an optional override file.** Less friction on the first run, but the
  first plan would follow rules nobody chose.
- **Limits as command-line flags, taste in `CLAUDE.local.md`.** Protected folders would
  then be a request to Claude rather than a check.
