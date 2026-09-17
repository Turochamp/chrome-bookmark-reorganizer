# Apply only through the extension, never by editing the store files

A signed-in Chrome profile under account storage keeps two stores and renders both merged:
the account store (`AccountBookmarks`), which sync rewrites from Google's servers, and
the device store (`Bookmarks`). An earlier version wrote the finished tree straight into
`Bookmarks` with Chrome closed. The profile moved to account storage along the way. Sync
restored the old tree into `AccountBookmarks`, and the bar showed every folder twice. So
the scripts only compute a target, and a small unpacked extension makes the changes
through `chrome.bookmarks`. Sync sees real moves, and the extension empties the device
store afterwards.

## Considered options

- **Write `AccountBookmarks` with sync paused.** Sync reconciles against the server copy
  when it resumes, so the edit does not last.
- **Keep a file-write mode for profiles without account storage.** It needs Chrome closed,
  a per-OS process check and restore commands, for a case the extension already handles.
  It was removed so there is one way to apply on every OS.

## Consequences

- There is no file-level rollback: a restored file would be overwritten by sync. Undo means
  building a plan from the backup.
- Users load an unpacked extension once, with Developer mode on.
