# zen-to-firefox

Mirror Zen Browser pinned tabs, Essentials, spaces and folders into Firefox bookmarks,
so Firefox Sync carries them to Firefox for iOS / Android.

## Why

Zen stores pinned tabs in its session store (`zen-sessions.jsonlz4`), not in the bookmarks
database, and syncs them through a Zen-only "spaces" engine. Firefox for iOS ignores that
engine, so nothing shows up on the phone even though both are signed into the same Mozilla
account.

This tool rewrites that structure as ordinary bookmarks inside Zen's own `places.sqlite`.
Zen's normal bookmark sync then uploads them, and they appear on the phone under
**Bookmarks → Desktop Bookmarks → Bookmarks Toolbar → Zen**.

```
Bookmarks Toolbar/
  Zen/
    Essentials/          one bookmark per Essential tab
    <Space>/             one folder per space
      <Zen folder>/      nested like the sidebar
        <pinned tab>     bookmark = the tab's pinned URL
```

Each bookmark gets a GUID derived from its Zen identity, so re-running updates in place,
reorders, and removes bookmarks whose pin is gone (with Sync tombstones so the deletion
propagates). Nothing outside the `Zen` folder is touched.

## Usage

Zen holds `places.sqlite` locked while it runs, so it must be closed during the write.

```bash
python3 zen_to_firefox.py --dry-run          # show the tree, change nothing
python3 zen_to_firefox.py --quit-zen --relaunch
```

Then wait for the next sync (Zen syncs every ~10 minutes) or trigger one from
Settings → Sync → Sync now. Firefox on the phone picks it up on its next sync.

Options:

| Flag | Meaning |
|---|---|
| `--profile DIR` | Zen profile (default: the one `profiles.ini` marks as default) |
| `--parent toolbar\|menu\|unfiled\|mobile` | which bookmark root to write under (default `toolbar`) |
| `--root-title NAME` | top-level folder name (default `Zen`) |
| `--quit-zen` / `--relaunch` | close Zen gracefully before writing / reopen it after |
| `--watch` / `--interval SEC` | run forever; export whenever Zen is closed and pins changed (poll every SEC, default 3) |
| `--backup-dir DIR` | where `places.sqlite` is copied first (default `~/Library/Application Support/zen-to-firefox/backups`) |
| `--no-backup` | skip the backup |
| `--sessions FILE` / `--db FILE` | override the input/output files (useful for testing on copies) |

Only `http(s)` pins are exported. Empty Zen folders and placeholder tabs are skipped.

## Keeping it in sync

There is no live sync while Zen is open, because the database cannot be written then.
`--watch` makes it automatic anyway: it polls every 3 seconds and, whenever Zen is not
running and the pins changed since the last export, it exports. That fires at login
(before Zen starts) and a few seconds after every Zen quit, so the phone is always at most
one Zen restart behind.

The launchd agent in `launchd/` keeps the watcher alive (logs in
`~/Library/Logs/zen-to-firefox.log`, backups rotate to the newest 10):

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/eu.ngen.zen-to-firefox.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/eu.ngen.zen-to-firefox.plist
```

Check it: `launchctl print gui/$(id -u)/eu.ngen.zen-to-firefox | grep state`.
Remove it: `launchctl bootout gui/$(id -u)/eu.ngen.zen-to-firefox` and delete the plist.

## Restoring

Every run first copies `places.sqlite` to the backup directory. To undo, quit Zen and copy
the backup back over `~/Library/Application Support/zen/Profiles/<profile>/places.sqlite`
(delete any `places.sqlite-wal` next to it).

## Tests

```bash
python3 -m unittest discover -s tests
```
