#!/usr/bin/env python3
"""Mirror Zen Browser pinned tabs, Essentials, spaces and folders into Firefox bookmarks.

Zen keeps pinned tabs in its session store (zen-sessions.jsonlz4), not in the bookmarks
database, and syncs them with a Zen-only "spaces" engine that Firefox for iOS/Android
ignores.  This tool writes the same structure into Zen's own places.sqlite as ordinary
bookmarks, which Firefox Sync then carries to every device signed into the account.

Layout written under the chosen root (bookmarks toolbar by default):

    Zen/
      Essentials/            one bookmark per Essential tab
      <Space name>/          one folder per space
        <Zen folder>/        Zen folders (nested as in the sidebar)
          <pinned tab>       bookmark = the tab's pinned URL

Every item gets a GUID derived from its Zen identity, so re-running the tool updates
bookmarks in place (and removes ones whose pin is gone) instead of duplicating them.

Zen must be closed while the database is written; use --quit-zen / --relaunch.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "zen"
ZEN_APP = Path("/Applications/Zen.app")
DEFAULT_BACKUP_DIR = Path.home() / "Library" / "Application Support" / "zen-to-firefox" / "backups"

ROOT_GUIDS = {
    "toolbar": "toolbar_____",
    "menu": "menu________",
    "unfiled": "unfiled_____",
    "mobile": "mobile______",
}
TYPE_BOOKMARK = 1
TYPE_FOLDER = 2
SYNC_STATUS_NEW = 1
SYNC_STATUS_NORMAL = 2
TITLE_MAX = 4096
URL_MAX = 65536
BOOKMARKABLE_SCHEMES = {"http", "https"}

# ---------------------------------------------------------------------------
# mozlz4 (Firefox's "mozLz40\0" + LZ4 block) decoding, no third-party deps
# ---------------------------------------------------------------------------


def _lz4_block_decompress(src: bytes) -> bytes:
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i : i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst)


def read_mozlz4(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != b"mozLz40\0":
        raise ValueError(f"{path} is not a mozlz4 file")
    expected = struct.unpack("<I", raw[8:12])[0]
    data = _lz4_block_decompress(raw[12:])
    if len(data) != expected:
        raise ValueError(f"{path}: decompressed {len(data)} bytes, header says {expected}")
    return data


def load_session(path: Path) -> dict:
    """Parse zen-sessions.jsonlz4 (or an already-decoded .json copy of it)."""
    raw = path.read_bytes()
    if raw[:8] == b"mozLz40\0":
        raw = read_mozlz4(path)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Places helpers (mirror what toolkit/components/places does in C++/JS)
# ---------------------------------------------------------------------------

_GOLDEN_RATIO = 0x9E3779B9
_MASK32 = 0xFFFFFFFF
_MAX_CHARS_TO_HASH = 1500


def _moz_hash_string(s: str) -> int:
    """mozilla::HashString over a narrow (char) string; chars sign-extend like on macOS/x86."""
    h = 0
    for b in s.encode("utf-8"):
        v = b - 256 if b >= 128 else b
        h = ((((h << 5) | (h >> 27)) & _MASK32) ^ (v & _MASK32)) * _GOLDEN_RATIO & _MASK32
    return h


def url_hash(url: str) -> int:
    """moz_places.url_hash: 16 bits of scheme hash, then the hash of (at most 1500 chars of) the spec."""
    prefix = url.split(":", 1)[0]
    return ((_moz_hash_string(prefix) & 0xFFFF) << 32) + _moz_hash_string(url[:_MAX_CHARS_TO_HASH])


def rev_host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[::-1] + "."


def origin_parts(url: str) -> tuple[str, str]:
    """(prefix, host[:port]) the way moz_origins stores them: ('https://', 'www.example.com')."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*:(?://)?)", url)
    if not m:
        return "", ""
    prefix = m.group(1)
    rest = url[len(prefix) :]
    hostport = re.split(r"[/?#]", rest, maxsplit=1)[0]
    hostport = hostport.rsplit("@", 1)[-1]
    return prefix, hostport


def normalize_url(url: str) -> str:
    """Light normalisation towards the nsIURI spec Places stores: lower-case scheme/host, '/' path."""
    parts = urlsplit(url)
    userinfo, at, hostport = parts.netloc.rpartition("@")
    if hostport.startswith("["):
        end = hostport.find("]") + 1
        host, port = hostport[:end], hostport[end:]
    else:
        host, sep, port = hostport.partition(":")
        port = sep + port
    netloc = (userinfo + at if at else "") + host.lower() + port
    path = parts.path or ("/" if netloc else "")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))


def stable_guid(key: str) -> str:
    """12-char Places GUID ([A-Za-z0-9_-]) derived deterministically from a Zen identity."""
    return base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest()[:9]).decode("ascii")


def random_guid() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(9)).decode("ascii")


def now_us() -> int:
    return int(time.time() * 1_000_000)


# ---------------------------------------------------------------------------
# Zen session -> bookmark tree
# ---------------------------------------------------------------------------


@dataclass
class Node:
    key: str
    title: str
    url: str | None = None
    children: list["Node"] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.url is None

    @property
    def guid(self) -> str:
        return stable_guid(self.key)

    def count(self) -> tuple[int, int]:
        folders, bookmarks = (1, 0) if self.is_folder else (0, 1)
        for c in self.children:
            f, b = c.count()
            folders += f
            bookmarks += b
        return folders, bookmarks


def tab_target(tab: dict) -> tuple[str | None, str | None]:
    """URL/title a pinned tab should be bookmarked as: its pinned 'home' entry, else the current entry."""
    initial = (tab.get("_zenPinnedInitialState") or {}).get("entry") or {}
    if initial.get("url"):
        return initial["url"], initial.get("title") or None
    entries = tab.get("entries") or []
    if not entries:
        return None, None
    idx = tab.get("index", len(entries)) - 1
    idx = min(max(idx, 0), len(entries) - 1)
    entry = entries[idx]
    return entry.get("url"), entry.get("title") or None


def is_bookmarkable(url: str | None) -> bool:
    if not url or len(url) > URL_MAX:
        return False
    return url.split(":", 1)[0].lower() in BOOKMARKABLE_SCHEMES


def build_tree(session: dict, root_title: str = "Zen", essentials_title: str = "Essentials") -> Node:
    tabs = session.get("tabs") or []
    folders = [f for f in (session.get("folders") or []) if f.get("id")]
    spaces = session.get("spaces") or []

    folder_nodes = {f["id"]: Node(f"zen:folder:{f['id']}", (f.get("name") or "Folder").strip() or "Folder") for f in folders}
    space_nodes = {s["uuid"]: Node(f"zen:space:{s['uuid']}", (s.get("name") or "Space").strip() or "Space") for s in spaces if s.get("uuid")}
    essentials = Node("zen:essentials", essentials_title)
    unsorted = Node("zen:space:none", "Unsorted")

    # Ordered sequences of ("tab" | "group", id, node-or-None) per container, in sidebar order.
    seq: dict[tuple, list] = {}

    def container_of_tab(tab: dict) -> tuple:
        if tab.get("zenEssential"):
            return ("essentials",)
        gid = tab.get("groupId")
        if gid in folder_nodes:
            return ("folder", gid)
        ws = tab.get("zenWorkspace")
        return ("space", ws if ws in space_nodes else None)

    for tab in tabs:
        if not tab.get("pinned"):
            continue
        node = None
        if not tab.get("zenIsEmpty"):
            url, title = tab_target(tab)
            if is_bookmarkable(url):
                url = normalize_url(url)
                key = f"zen:tab:{tab.get('zenSyncId') or url}"
                node = Node(key, (title or url)[:TITLE_MAX], url)
        # keep a placeholder even for skipped tabs so folder ordering (prevSiblingInfo) still resolves
        seq.setdefault(container_of_tab(tab), []).append(("tab", tab.get("zenSyncId"), node))

    def container_of_folder(f: dict) -> tuple:
        pid = f.get("parentId")
        if pid in folder_nodes:
            return ("folder", pid)
        ws = f.get("workspaceId")
        return ("space", ws if ws in space_nodes else None)

    pending = list(folders)
    placed: set[str] = set()
    while pending:
        progress = False
        rest = []
        for f in pending:
            lst = seq.setdefault(container_of_folder(f), [])
            item = ("group", f["id"], folder_nodes[f["id"]])
            prev = f.get("prevSiblingInfo") or {}
            ptype, pid = prev.get("type"), prev.get("id")
            if ptype == "start" or not pid:
                lst.insert(0, item)
            else:
                pos = next((i for i, (_, sid, _) in enumerate(lst) if sid == pid), None)
                if pos is None and ptype == "group" and pid in folder_nodes and pid not in placed:
                    rest.append(f)  # wait until the sibling folder is placed
                    continue
                if pos is None:
                    lst.append(item)
                else:
                    lst.insert(pos + 1, item)
            placed.add(f["id"])
            progress = True
        if not progress:  # unresolved references (cycles / missing siblings): append in file order
            for f in rest:
                seq.setdefault(container_of_folder(f), []).append(("group", f["id"], folder_nodes[f["id"]]))
            break
        pending = rest

    def assemble(container: tuple, parent: Node) -> None:
        for kind, cid, node in seq.get(container, []):
            if node is None:
                continue
            if kind == "group":
                assemble(("folder", cid), node)
                if not node.children:
                    continue  # drop empty Zen folders
            parent.children.append(node)

    root = Node("zen:root", root_title)
    assemble(("essentials",), essentials)
    if essentials.children:
        root.children.append(essentials)
    for s in spaces:
        node = space_nodes.get(s.get("uuid"))
        if node is None:
            continue
        assemble(("space", s["uuid"]), node)
        if node.children:
            root.children.append(node)
    assemble(("space", None), unsorted)
    if unsorted.children:
        root.children.append(unsorted)
    return root


def print_tree(node: Node, indent: int = 0, out=sys.stdout) -> None:
    if node.is_folder:
        f, b = node.count()
        print(f"{'  ' * indent}{node.title}/  ({b} bookmarks)", file=out)
        for c in node.children:
            print_tree(c, indent + 1, out)
    else:
        print(f"{'  ' * indent}{node.title}  <{node.url}>", file=out)


# ---------------------------------------------------------------------------
# Writing the tree into places.sqlite
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    places_created: int = 0

    def __str__(self) -> str:
        return (
            f"inserted={self.inserted} updated={self.updated} removed={self.removed} "
            f"unchanged={self.unchanged} new_places={self.places_created}"
        )


class PlacesMirror:
    """Upserts a Node tree under a Places root folder, keeping Sync bookkeeping consistent."""

    def __init__(self, conn: sqlite3.Connection, ts: int | None = None):
        self.conn = conn
        self.ts = ts or now_us()
        self.stats = Stats()
        self.touched_parents: set[int] = set()

    # -- moz_places / moz_origins ------------------------------------------

    def ensure_place(self, url: str) -> int:
        h = url_hash(url)
        row = self.conn.execute("SELECT id FROM moz_places WHERE url_hash = ? AND url = ?", (h, url)).fetchone()
        if row:
            return row[0]
        prefix, host = origin_parts(url)
        orow = self.conn.execute("SELECT id FROM moz_origins WHERE prefix = ? AND host = ?", (prefix, host)).fetchone()
        if orow:
            origin_id = orow[0]
        else:
            origin_id = self.conn.execute(
                "INSERT INTO moz_origins (prefix, host, frecency, recalc_frecency, recalc_alt_frecency) VALUES (?, ?, 0, 1, 1)",
                (prefix, host),
            ).lastrowid
        guid = random_guid()
        while self.conn.execute("SELECT 1 FROM moz_places WHERE guid = ?", (guid,)).fetchone():
            guid = random_guid()
        place_id = self.conn.execute(
            "INSERT INTO moz_places (url, rev_host, hidden, typed, frecency, guid, foreign_count, url_hash, origin_id,"
            " recalc_frecency, recalc_alt_frecency) VALUES (?, ?, 0, 0, -1, ?, 0, ?, ?, 1, 1)",
            (url, rev_host(url), guid, h, origin_id),
        ).lastrowid
        self.stats.places_created += 1
        return place_id

    def _adjust_foreign_count(self, place_id: int | None, delta: int) -> None:
        if place_id is not None:
            self.conn.execute("UPDATE moz_places SET foreign_count = MAX(0, foreign_count + ?) WHERE id = ?", (delta, place_id))

    # -- moz_bookmarks -------------------------------------------------------

    def root_id(self, root_guid: str) -> int:
        row = self.conn.execute("SELECT id FROM moz_bookmarks WHERE guid = ?", (root_guid,)).fetchone()
        if not row:
            raise RuntimeError(f"bookmark root {root_guid!r} not found in places.sqlite")
        return row[0]

    def upsert(self, node: Node, parent_id: int, position: int) -> int:
        guid = node.guid
        item_type = TYPE_FOLDER if node.is_folder else TYPE_BOOKMARK
        fk = None if node.is_folder else self.ensure_place(node.url)
        title = node.title[:TITLE_MAX]
        row = self.conn.execute(
            "SELECT id, type, fk, parent, position, title FROM moz_bookmarks WHERE guid = ?", (guid,)
        ).fetchone()
        if row is None:
            item_id = self.conn.execute(
                "INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid,"
                " syncStatus, syncChangeCounter) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (item_type, fk, parent_id, position, title, self.ts, self.ts, guid, SYNC_STATUS_NEW),
            ).lastrowid
            self.conn.execute("DELETE FROM moz_bookmarks_deleted WHERE guid = ?", (guid,))
            self._adjust_foreign_count(fk, +1)
            self.touched_parents.add(parent_id)
            self.stats.inserted += 1
            return item_id

        item_id, old_type, old_fk, old_parent, old_pos, old_title = row
        if old_type != item_type:
            # Same Zen identity changed kind (should not happen); replace the row.
            self.delete_subtree(item_id)
            return self.upsert(node, parent_id, position)
        if (old_fk, old_parent, old_pos, old_title) == (fk, parent_id, position, title):
            self.stats.unchanged += 1
            return item_id
        self.conn.execute(
            "UPDATE moz_bookmarks SET fk = ?, parent = ?, position = ?, title = ?, lastModified = ?,"
            " syncChangeCounter = syncChangeCounter + 1 WHERE id = ?",
            (fk, parent_id, position, title, self.ts, item_id),
        )
        if old_fk != fk:
            self._adjust_foreign_count(old_fk, -1)
            self._adjust_foreign_count(fk, +1)
        if (old_parent, old_pos) != (parent_id, position):
            # Folder records carry their children's order, so a move re-uploads the folder(s).
            self.touched_parents.add(old_parent)
            self.touched_parents.add(parent_id)
        self.stats.updated += 1
        return item_id

    def delete_subtree(self, item_id: int) -> None:
        for (child_id,) in self.conn.execute("SELECT id FROM moz_bookmarks WHERE parent = ?", (item_id,)).fetchall():
            self.delete_subtree(child_id)
        row = self.conn.execute("SELECT guid, fk, parent, syncStatus FROM moz_bookmarks WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return
        guid, fk, parent, sync_status = row
        if sync_status != SYNC_STATUS_NEW:
            # The server knows this item: leave a tombstone so Sync deletes it everywhere.
            self.conn.execute(
                "INSERT OR REPLACE INTO moz_bookmarks_deleted (guid, dateRemoved) VALUES (?, ?)", (guid, self.ts)
            )
        self.conn.execute("DELETE FROM moz_bookmarks WHERE id = ?", (item_id,))
        self._adjust_foreign_count(fk, -1)
        self.touched_parents.add(parent)
        self.stats.removed += 1

    # -- tree level -----------------------------------------------------------

    def mirror(self, tree: Node, root_guid: str) -> Stats:
        parent_id = self.root_id(root_guid)
        existing = self.conn.execute("SELECT position FROM moz_bookmarks WHERE guid = ?", (tree.guid,)).fetchone()
        if existing:
            top_pos = existing[0]
        else:
            top_pos = self.conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM moz_bookmarks WHERE parent = ?", (parent_id,)
            ).fetchone()[0]

        # Pass 1: upsert every desired item (moves between folders are applied here).
        desired: dict[int, list[str]] = {}
        top_id = self.upsert(tree, parent_id, top_pos)
        self._upsert_children(tree, top_id, desired)

        # Pass 2: remove items under our tree that are no longer desired.
        for folder_id, guids in list(desired.items()):
            keep = set(guids)
            rows = self.conn.execute("SELECT id, guid FROM moz_bookmarks WHERE parent = ?", (folder_id,)).fetchall()
            for child_id, guid in rows:
                if guid not in keep:
                    self.delete_subtree(child_id)

        # Parents whose child list changed must be re-uploaded by Sync.
        for pid in self.touched_parents:
            self.conn.execute(
                "UPDATE moz_bookmarks SET lastModified = ?, syncChangeCounter = syncChangeCounter + 1 WHERE id = ?",
                (self.ts, pid),
            )
        return self.stats

    def _upsert_children(self, node: Node, node_id: int, desired: dict[int, list[str]]) -> None:
        desired[node_id] = []
        for pos, child in enumerate(node.children):
            child_id = self.upsert(child, node_id, pos)
            desired[node_id].append(child.guid)
            if child.is_folder:
                self._upsert_children(child, child_id, desired)


# ---------------------------------------------------------------------------
# Profile discovery, Zen process control, backups
# ---------------------------------------------------------------------------


def find_profile(app_support: Path = APP_SUPPORT) -> Path:
    ini_path = app_support / "profiles.ini"
    if not ini_path.exists():
        raise FileNotFoundError(f"{ini_path} not found; is Zen installed?")
    ini = configparser.RawConfigParser()
    ini.optionxform = str  # type: ignore[assignment]
    ini.read(ini_path, encoding="utf-8")
    # The [Install...] section names the profile the installed app actually launches.
    for section in ini.sections():
        if section.startswith("Install") and ini.has_option(section, "Default"):
            return app_support / ini.get(section, "Default")
    candidates = [s for s in ini.sections() if s.startswith("Profile")]
    for section in candidates:
        if ini.get(section, "Default", fallback="0") == "1":
            return app_support / ini.get(section, "Path")
    if candidates:
        return app_support / ini.get(candidates[0], "Path")
    raise RuntimeError("no profiles listed in profiles.ini")


def zen_is_running() -> bool:
    out = subprocess.run(["pgrep", "-fl", "Zen.app/Contents/MacOS/zen"], capture_output=True, text=True).stdout
    return any(re.search(r"Zen\.app/Contents/MacOS/zen(\s|$)", line) for line in out.splitlines())


def quit_zen(timeout: float = 60.0) -> None:
    if not zen_is_running():
        return
    subprocess.run(["osascript", "-e", 'tell application "Zen" to quit'], check=False)
    deadline = time.time() + timeout
    while zen_is_running():
        if time.time() > deadline:
            raise TimeoutError("Zen did not quit in time; close it manually and re-run")
        time.sleep(0.5)
    time.sleep(1.0)  # let it finish flushing the profile


def relaunch_zen() -> None:
    subprocess.run(["open", "-a", str(ZEN_APP)], check=False)


def backup_db(db: Path, backup_dir: Path, keep: int = 10) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"places-{stamp}.sqlite"
    shutil.copy2(db, dest)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    # keep only the newest `keep` backups
    backups = sorted(backup_dir.glob("places-*.sqlite"))
    for old in backups[:-keep] if keep > 0 else []:
        for f in backup_dir.glob(old.name + "*"):
            f.unlink(missing_ok=True)
    return dest


def open_places(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=2.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        conn.close()
        raise RuntimeError(f"cannot lock {db} ({e}); is Zen still running?") from e
    return conn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def run_export(sessions_path: Path, db_path: Path, parent: str, root_title: str, *, dry_run: bool = False,
               backup_dir: Path | None = None) -> Stats | None:
    """Build the tree from the session file and write it into places.sqlite. Caller ensures Zen is closed."""
    session = load_session(sessions_path)
    tree = build_tree(session, root_title=root_title)
    folders, bookmarks = tree.count()
    pinned = sum(1 for t in session.get("tabs") or [] if t.get("pinned"))
    log(f"Zen session: {len(session.get('spaces') or [])} spaces, {len(session.get('folders') or [])} folders, {pinned} pinned tabs")
    log(f"Bookmark tree: {folders - 1} folders, {bookmarks} bookmarks under {parent}/{root_title}")
    if dry_run:
        print_tree(tree)
        return None
    if backup_dir is not None:
        log(f"Backup: {backup_db(db_path, backup_dir)}")
    conn = open_places(db_path)
    try:
        stats = PlacesMirror(conn).mirror(tree, ROOT_GUIDS[parent])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    log(f"Done: {stats}")
    return stats


def watch(sessions_path: Path, db_path: Path, parent: str, root_title: str, *, interval: float,
          backup_dir: Path | None, settle: float = 3.0) -> None:
    """Run forever: whenever Zen is not running and the session file changed since the last export, export.

    Covers login (Zen not started yet) and every Zen quit (Zen rewrites the session file on shutdown).
    """
    log(f"Watching {sessions_path} every {interval:g}s; exporting whenever Zen is closed and pins changed")
    exported_mtime: float | None = None
    was_running = zen_is_running()
    while True:
        try:
            running = zen_is_running()
            if running != was_running:
                log("Zen started" if running else "Zen quit")
                was_running = running
            if not running and sessions_path.exists():
                mtime = sessions_path.stat().st_mtime
                if mtime != exported_mtime:
                    time.sleep(settle)  # let a just-quit Zen finish flushing its profile
                    if zen_is_running():
                        was_running = True
                        continue
                    run_export(sessions_path, db_path, parent, root_title, backup_dir=backup_dir)
                    exported_mtime = sessions_path.stat().st_mtime
        except Exception as e:  # keep the watcher alive; launchd would otherwise restart it in a loop
            log(f"error: {e}")
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--profile", type=Path, help="Zen profile directory (default: the one profiles.ini marks as default)")
    p.add_argument("--sessions", type=Path, help="override path to zen-sessions.jsonlz4")
    p.add_argument("--db", type=Path, help="override path to places.sqlite")
    p.add_argument("--parent", choices=sorted(ROOT_GUIDS), default="toolbar", help="bookmark root to write under (default: toolbar)")
    p.add_argument("--root-title", default="Zen", help="name of the top-level folder (default: Zen)")
    p.add_argument("--dry-run", action="store_true", help="print the tree that would be written and exit")
    p.add_argument("--quit-zen", action="store_true", help="quit Zen gracefully before writing")
    p.add_argument("--relaunch", action="store_true", help="relaunch Zen after writing")
    p.add_argument("--watch", action="store_true", help="run forever and export every time Zen is closed with changed pins")
    p.add_argument("--interval", type=float, default=3.0, help="poll interval in seconds for --watch (default: 3)")
    p.add_argument("--no-backup", action="store_true", help="skip the places.sqlite backup")
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    args = p.parse_args(argv)

    profile = args.profile or (None if (args.sessions and args.db) else find_profile())
    sessions_path = args.sessions or profile / "zen-sessions.jsonlz4"
    db_path = args.db or profile / "places.sqlite"
    backup_dir = None if args.no_backup else args.backup_dir

    if args.watch:
        if args.quit_zen or args.relaunch or args.dry_run:
            p.error("--watch cannot be combined with --quit-zen, --relaunch or --dry-run")
        watch(sessions_path, db_path, args.parent, args.root_title, interval=args.interval, backup_dir=backup_dir)
        return 0

    if not sessions_path.exists():
        print(f"error: {sessions_path} not found", file=sys.stderr)
        return 2
    if args.dry_run:
        run_export(sessions_path, db_path, args.parent, args.root_title, dry_run=True)
        return 0
    if not db_path.exists():
        print(f"error: {db_path} not found", file=sys.stderr)
        return 2

    if args.quit_zen:
        quit_zen()
    elif zen_is_running() and not args.db:
        print("error: Zen is running and holds places.sqlite locked. Close it or pass --quit-zen.", file=sys.stderr)
        return 3

    run_export(sessions_path, db_path, args.parent, args.root_title, backup_dir=backup_dir)
    if args.relaunch:
        relaunch_zen()
    return 0


if __name__ == "__main__":
    sys.exit(main())
