---
name: setup
description: Write or revise bookmark-rules.md from the user's own bookmarks and history. Use before the first plan, when bookmark-rules.md is missing, or when the user wants to change their bar limits, protected folders or taste.
---

# Setup: the user's bookmark rules

The output is `bookmark-rules.md` at the repo root, in the shape of
`bookmark-rules.example.md`: a front block the scripts enforce, then prose you follow when
planning. Every suggestion you make comes from this profile's data, never from the example's
values.

## 1. Pick the profile

If `bookmark-rules.md` exists, read it and ask which topics to revisit; keep the rest.

Chrome's `Local State` file, in the User Data folder listed in `README.md`, maps profile
folders to names under `profile.info_cache`. Show the user the names and ask which one.
Skip the question when there is only `Default`.

## 2. Read the profile

```sh
python scripts/read-profile.py --profile "<folder>"
```

Then compute these facts from `working/manifest.tsv` with a short script. Do not read the
manifest into the conversation; it can hold thousands of rows.

- bookmarks per root (BAR, OTHER, MOBILE) and in total
- each top-level BAR folder: bookmark count, summed `visits`, summed `days`
- deepest folder level under BAR
- untitled bookmarks sitting directly on BAR
- exact duplicate URLs, and near-duplicates (same host and path, ignoring scheme, `www.`,
  query and trailing slash)
- bookmarks with `visits` 0, and how many of those were `added` over a year ago
- folders under MOBILE, and folders anywhere with no visits in the window
- hosts with the highest `host_days`, which show what is used daily

## 3. Ask

One round, every question numbered, each with the relevant facts and your recommendation.
Wait for the answers; ask a follow-up round only for answers that open a new question.

1. **Bar shape**: how many top-level folders, and how deep. Base the suggestion on how many
   distinct groups the high-`host_days` sites fall into, not on today's folder count.
2. **Bar order**: alphabetical, by use, or a fixed order the user names.
3. **Protected folders**: offer MOBILE folders, folders that look shared or belong to
   someone else, and anything the user names. Paths start with BAR, OTHER or MOBILE.
4. **Untitled bar bookmarks**: only if there are any. Keep them as icon-only buttons?
5. **Duplicates**: merge exact URL matches only, or near-duplicates too. Give both counts.
6. **Cold material**: where rarely used bookmarks go (Other bookmarks, an archive folder,
   or deletion after review). Give the unvisited count, and say that no visits in the
   window is not proof of disuse.
7. **Startup tabs** (optional): suggest the two or three highest-`host_days` sites as
   pages to open on startup. Say this is set in Chrome's settings, not by the plan.

## 4. Write

Write `bookmark-rules.md`:

- the front block with `bar_max_folders`, `bar_max_depth` and one `protected:` line per
  protected folder
- one `##` section per answered topic, in the user's words where they gave them

Check that the front block parses:

```sh
python -c "import sys; sys.path.insert(0, 'scripts'); import bookmark_rules; print(bookmark_rules.load_rules())"
```

Show the user the file's sections in a few lines and stop. Start planning only when
they ask for it.
