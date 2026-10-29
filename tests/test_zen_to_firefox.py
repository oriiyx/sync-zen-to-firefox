import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zen_to_firefox as z  # noqa: E402

SCHEMA = """
CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, rev_host LONGVARCHAR,
  visit_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0 NOT NULL, typed INTEGER DEFAULT 0 NOT NULL,
  frecency INTEGER DEFAULT -1 NOT NULL, last_visit_date INTEGER, guid TEXT, foreign_count INTEGER DEFAULT 0 NOT NULL,
  url_hash INTEGER DEFAULT 0 NOT NULL, description TEXT, preview_image_url TEXT, site_name TEXT,
  origin_id INTEGER REFERENCES moz_origins(id), recalc_frecency INTEGER NOT NULL DEFAULT 0, alt_frecency INTEGER,
  recalc_alt_frecency INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX moz_places_guid_uniqueindex ON moz_places (guid);
CREATE TABLE moz_origins (id INTEGER PRIMARY KEY, prefix TEXT NOT NULL, host TEXT NOT NULL, frecency INTEGER NOT NULL,
  recalc_frecency INTEGER NOT NULL DEFAULT 0, alt_frecency INTEGER, recalc_alt_frecency INTEGER NOT NULL DEFAULT 0,
  block_until_ms INTEGER, block_pages_until_ms INTEGER, UNIQUE (host, prefix));
CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER DEFAULT NULL, parent INTEGER,
  position INTEGER, title LONGVARCHAR, keyword_id INTEGER, folder_type TEXT, dateAdded INTEGER, lastModified INTEGER,
  guid TEXT, syncStatus INTEGER NOT NULL DEFAULT 0, syncChangeCounter INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX moz_bookmarks_guid_uniqueindex ON moz_bookmarks (guid);
CREATE TABLE moz_bookmarks_deleted (guid TEXT PRIMARY KEY, dateRemoved INTEGER NOT NULL DEFAULT 0);
INSERT INTO moz_bookmarks (id,type,parent,position,title,guid,syncStatus,syncChangeCounter) VALUES
  (1,2,0,0,'','root________',2,0),(2,2,1,0,'menu','menu________',2,0),(3,2,1,1,'toolbar','toolbar_____',2,0),
  (4,2,1,2,'tags','tags________',2,0),(5,2,1,3,'unfiled','unfiled_____',2,0),(6,2,1,4,'mobile','mobile______',2,0);
"""

SPACE = "{11111111-1111-1111-1111-111111111111}"


def tab(sync_id, url, title=None, *, group=None, space=SPACE, essential=False, empty=False, pinned=True):
    return {
        "pinned": pinned, "zenSyncId": sync_id, "groupId": group, "zenWorkspace": None if essential else space,
        "zenEssential": essential, "zenIsEmpty": empty, "index": 1,
        "entries": [{"url": url, "title": title or url}] if url else [],
        "_zenPinnedInitialState": {"entry": {"url": url, "title": title or url}} if url else None,
    }


def folder(fid, name, prev, parent=None):
    return {"id": fid, "name": name, "parentId": parent, "prevSiblingInfo": prev, "workspaceId": SPACE, "pinned": True}


def session():
    return {
        "spaces": [{"uuid": SPACE, "name": "Work"}],
        "folders": [
            folder("f1", "Tools", {"type": "start", "id": None}),
            folder("f2", "Nested", {"type": "tab", "id": "t1"}, parent="f1"),
            folder("f3", "After tab", {"type": "tab", "id": "t3"}),
            folder("f4", "Empty folder", {"type": "group", "id": "f3"}),
        ],
        "tabs": [
            tab("e1", "https://claude.ai/code", "Claude", essential=True),
            tab("t1", "https://a.example/", "A", group="f1"),
            tab("t2", "https://b.example/x", "B", group="f2"),
            tab("t3", "https://C.Example", "C"),
            tab("t4", "https://d.example/", "D", group="f3"),
            tab("t5", "about:blank", empty=True, group="f4"),
            tab("t6", "https://not-pinned.example/", pinned=False),
            tab("t7", "moz-extension://abc/page.html", "ext"),
        ],
    }


def names(node):
    return [(c.title, names(c)) if c.is_folder else c.title for c in node.children]


class HelperTests(unittest.TestCase):
    def test_url_hash_matches_firefox(self):
        # values read from a real places.sqlite
        self.assertEqual(z.url_hash("https://github.com/"), 47357536345229)
        self.assertEqual(z.url_hash("place:sort=8&maxResults=10"), 268505095842199)
        self.assertEqual(z.url_hash("about:firefox"), 175532932156555)

    def test_origin_and_rev_host(self):
        self.assertEqual(z.origin_parts("https://u@www.x.com:8443/p?q"), ("https://", "www.x.com:8443"))
        self.assertEqual(z.origin_parts("about:firefox"), ("about:", "firefox"))
        self.assertEqual(z.rev_host("https://www.x.com:8443/p"), "moc.x.www.")
        self.assertEqual(z.rev_host("about:firefox"), ".")

    def test_normalize_url(self):
        self.assertEqual(z.normalize_url("HTTPS://GitHub.com"), "https://github.com/")
        self.assertEqual(z.normalize_url("http://u:p@Host.x:8080"), "http://u:p@host.x:8080/")

    def test_guid_is_valid_and_stable(self):
        g = z.stable_guid("zen:root")
        self.assertEqual(len(g), 12)
        self.assertRegex(g, r"^[A-Za-z0-9_-]{12}$")
        self.assertEqual(g, z.stable_guid("zen:root"))


class TreeTests(unittest.TestCase):
    def test_structure_and_order(self):
        tree = z.build_tree(session())
        self.assertEqual(
            names(tree),
            [
                ("Essentials", ["Claude"]),
                ("Work", [("Tools", ["A", ("Nested", ["B"])]), "C", ("After tab", ["D"])]),
            ],
        )
        c = tree.children[1].children[1]
        self.assertEqual(c.url, "https://c.example/")


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.executescript(SCHEMA)

    def rows(self):
        return self.conn.execute(
            "SELECT guid, type, parent, position, title, syncStatus, syncChangeCounter FROM moz_bookmarks WHERE id > 6"
        ).fetchall()

    def test_insert_then_idempotent(self):
        s1 = z.PlacesMirror(self.conn, ts=1).mirror(z.build_tree(session()), "toolbar_____")
        self.assertEqual((s1.inserted, s1.places_created), (11, 5))
        self.assertTrue(all(r[5] == z.SYNC_STATUS_NEW and r[6] >= 1 for r in self.rows()))
        toolbar = self.conn.execute("SELECT syncChangeCounter FROM moz_bookmarks WHERE guid='toolbar_____'").fetchone()[0]
        self.assertEqual(toolbar, 1)
        fc = self.conn.execute("SELECT foreign_count FROM moz_places WHERE url='https://a.example/'").fetchone()[0]
        self.assertEqual(fc, 1)
        s2 = z.PlacesMirror(self.conn, ts=2).mirror(z.build_tree(session()), "toolbar_____")
        self.assertEqual((s2.inserted, s2.updated, s2.removed, s2.unchanged), (0, 0, 0, 11))

    def test_remove_and_rename(self):
        z.PlacesMirror(self.conn, ts=1).mirror(z.build_tree(session()), "toolbar_____")
        self.conn.execute("UPDATE moz_bookmarks SET syncStatus=2, syncChangeCounter=0")
        sess = session()
        sess["tabs"] = [t for t in sess["tabs"] if t["zenSyncId"] != "t1"]  # Tools keeps only Nested/B
        sess["folders"][0]["name"] = "Toolz"
        st = z.PlacesMirror(self.conn, ts=2).mirror(z.build_tree(sess), "toolbar_____")
        self.assertEqual((st.removed, st.inserted), (1, 0))
        tomb = [g for (g,) in self.conn.execute("SELECT guid FROM moz_bookmarks_deleted")]
        self.assertEqual(tomb, [z.stable_guid("zen:tab:t1")])
        fc = self.conn.execute("SELECT foreign_count FROM moz_places WHERE url='https://a.example/'").fetchone()[0]
        self.assertEqual(fc, 0)
        tools = self.conn.execute(
            "SELECT title, syncChangeCounter FROM moz_bookmarks WHERE guid=?", (z.stable_guid("zen:folder:f1"),)
        ).fetchone()
        self.assertEqual(tools, ("Toolz", 2))  # rename + child removed
        nested = self.conn.execute("SELECT position FROM moz_bookmarks WHERE guid=?", (z.stable_guid("zen:folder:f2"),)).fetchone()
        self.assertEqual(nested[0], 0)

    def test_never_synced_items_leave_no_tombstone(self):
        z.PlacesMirror(self.conn, ts=1).mirror(z.build_tree(session()), "toolbar_____")
        sess = session()
        sess["tabs"] = [t for t in sess["tabs"] if t["zenSyncId"] != "t4"]  # empties 'After tab'
        st = z.PlacesMirror(self.conn, ts=2).mirror(z.build_tree(sess), "toolbar_____")
        self.assertEqual(st.removed, 2)  # bookmark + its now-empty folder
        self.assertEqual(self.conn.execute("SELECT count(*) FROM moz_bookmarks_deleted").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class WatchTests(unittest.TestCase):
    def test_exports_only_when_zen_closed_and_session_changed(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            sessions = Path(d) / "zen-sessions.json"
            sessions.write_text("{}")
            # Zen: running, running, quit, quit (unchanged), then stop the loop
            states = iter([True, True, True, False, False, False, False, False])
            exports = []
            sleeps = {"n": 0}

            def fake_sleep(_):
                sleeps["n"] += 1
                if sleeps["n"] > 12:
                    raise KeyboardInterrupt

            with mock.patch.object(z, "zen_is_running", side_effect=lambda: next(states, False)), \
                    mock.patch.object(z, "run_export", side_effect=lambda *a, **k: exports.append(a)), \
                    mock.patch.object(z.time, "sleep", fake_sleep):
                with self.assertRaises(KeyboardInterrupt):
                    z.watch(sessions, Path(d) / "db", "toolbar", "Zen", interval=1, backup_dir=None, settle=0)
            self.assertEqual(len(exports), 1)  # once after quit, not again while the file is unchanged
