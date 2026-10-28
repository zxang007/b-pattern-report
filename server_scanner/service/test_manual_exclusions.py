import sqlite3
import tempfile
import unittest
from pathlib import Path
from manual_exclusions import four_h_activity

class ActivityTest(unittest.TestCase):
    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertIsNone(four_h_activity(root))
            self.assertFalse((root/'scanner.sqlite3').exists())
    def test_new_and_reappeared_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with sqlite3.connect(root/'scanner.sqlite3') as db:
                db.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,group_id TEXT,kind TEXT,seen_at TEXT); CREATE TABLE structures(group_id TEXT,symbol TEXT);')
                self.assertIsNone(four_h_activity(root))
                db.execute("INSERT INTO structures VALUES('a','MERLUSDT')")
                db.executemany('INSERT INTO events VALUES(?,?,?,?)',[(1,'a','baseline','initial'),(2,'a','updated','later')])
                db.commit()
                self.assertEqual(four_h_activity(root),{'generation':'initial','by_symbol':{}})
                db.execute("INSERT INTO events VALUES(3,'a','new','later')")
                db.execute("INSERT INTO events VALUES(4,'a','updated','later')")
                db.commit()
                self.assertEqual(four_h_activity(root)['by_symbol'],{'MERLUSDT':3})
                db.execute("INSERT INTO events VALUES(5,'a','reappeared','later')")
                db.commit()
                self.assertEqual(four_h_activity(root)['by_symbol'],{'MERLUSDT':5})
