# Chrome Bookmark Reorganizer

Reorganizes one Chrome profile's bookmarks from a reviewed plan, so the judgment about
where things belong stays in data a person can read and the tooling only carries it out.

## Language

### Where bookmarks live

**Profile**:
One Chrome profile directory, such as `Default` or `Profile 1`, holding its own bookmarks and history.
_Avoid_: account, user

**Account store**:
The profile's `AccountBookmarks` file, the bookmarks that sync with the signed-in Google account.
_Avoid_: synced store, sync file

**Device store**:
The profile's `Bookmarks` file, bookmarks kept only on this machine. Chrome shows it merged with the account store.
_Avoid_: local store, local bookmarks, device-only store

**Account storage**:
The state of a profile that has an account store beside its device store.
_Avoid_: sync mode, synced profile

**Bookmarks bar**, **Other bookmarks**, **Mobile bookmarks**:
The three permanent root folders of a store, written `BAR`, `OTHER` and `MOBILE` in a plan.
_Avoid_: "synced" for Mobile bookmarks

### The pipeline's artifacts

**Snapshot**:
A verbatim copy of the store being reorganized, taken before planning; everything after it is checked against it.

**Manifest**:
One row per bookmark in the snapshot, with its folder path and recent usage, the input to planning.

**Plan**:
The reviewed list of operations (move, delete, add, retitle, reurl, order) that turns the snapshot into the wanted tree.
_Avoid_: rules, mapping

**Target**:
The finished tree a plan produces, plus the fingerprint of the snapshot it starts from, handed to the extension to apply.
_Avoid_: output, result file

**Backup**:
Copies of both stores taken just before a target is produced.
