import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from igdl import auth

SESSIONID = "1234567890%3AAbCdEfGhIj%3A12%3AAYeXyZ"


class CookieTextTests(unittest.TestCase):
    def test_raw_sessionid(self):
        self.assertEqual(auth.parse_cookie_text(f'  "{SESSIONID}" '), {"sessionid": SESSIONID})

    def test_cookie_header(self):
        cookies = auth.parse_cookie_text(f"Cookie: csrftoken=abc; ds_user_id=1234567890; sessionid={SESSIONID}")
        self.assertEqual(cookies, {"csrftoken": "abc", "ds_user_id": "1234567890", "sessionid": SESSIONID})

    def test_empty(self):
        self.assertEqual(auth.parse_cookie_text("   "), {})

    def test_user_id_from_sessionid(self):
        self.assertEqual(auth.user_id_from_sessionid(SESSIONID), "1234567890")
        self.assertEqual(auth.user_id_from_sessionid("1234567890:AbC:12"), "1234567890")
        self.assertIsNone(auth.user_id_from_sessionid("garbage"))


class CookieFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_netscape_format(self):
        path = self.write("cookies.txt", "\n".join([
            "# Netscape HTTP Cookie File",
            ".instagram.com\tTRUE\t/\tTRUE\t1999999999\tcsrftoken\tabc",
            f"#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\t{SESSIONID}",
            ".example.com\tTRUE\t/\tFALSE\t1999999999\tsessionid\tnot-this-one",
            "",
        ]))
        self.assertEqual(auth.load_cookie_file(path), {"csrftoken": "abc", "sessionid": SESSIONID})

    def test_json_formats(self):
        entries = [{"domain": ".instagram.com", "name": "sessionid", "value": SESSIONID},
                   {"domain": "www.example.com", "name": "other", "value": "x"}]
        self.assertEqual(auth.load_cookie_file(self.write("a.json", json.dumps(entries))), {"sessionid": SESSIONID})
        wrapped = json.dumps({"cookies": entries})
        self.assertEqual(auth.load_cookie_file(self.write("b.json", wrapped)), {"sessionid": SESSIONID})

    def test_without_login(self):
        path = self.write("cookies.txt", ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tabc\n")
        with self.assertRaises(auth.CookieImportError):
            auth.load_cookie_file(path)

    def test_missing_file(self):
        with self.assertRaises(auth.CookieImportError):
            auth.load_cookie_file(self.dir / "nope.txt")


class FirefoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "abcd.default-release"
        self.profile.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_db(self, rows):
        connection = sqlite3.connect(self.profile / "cookies.sqlite")
        connection.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT, name TEXT, "
                           "value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER)")
        connection.executemany("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, "
                               "lastAccessed) VALUES (?, ?, ?, ?, '/', 0, ?)", rows)
        connection.commit()
        connection.close()

    def test_picks_the_most_recently_used_login(self):
        self.make_db([
            ("", "sessionid", "old-session", ".instagram.com", 100),
            ("", "csrftoken", "old-csrf", ".instagram.com", 100),
            ("^userContextId=2", "sessionid", "new-session", ".instagram.com", 200),
            ("^userContextId=2", "csrftoken", "new-csrf", ".instagram.com", 200),
            ("", "sessionid", "unrelated", ".example.com", 300),
        ])
        cookies = auth.load_firefox_cookies([self.profile])
        self.assertEqual(cookies, {"sessionid": "new-session", "csrftoken": "new-csrf"})

    def test_not_logged_in(self):
        self.make_db([("", "csrftoken", "x", ".instagram.com", 100)])
        with self.assertRaises(auth.CookieImportError):
            auth.load_firefox_cookies([self.profile])

    def test_no_firefox(self):
        with self.assertRaises(auth.CookieImportError):
            auth.load_firefox_cookies([])


class SessionStoreTests(unittest.TestCase):
    def test_save_load_delete(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"IGDL_HOME": tmp}):
            self.assertIsNone(auth.load_session())
            auth.save_session({"sessionid": SESSIONID, "csrftoken": "abc"}, "me")
            self.assertEqual(auth.load_session(), ({"sessionid": SESSIONID, "csrftoken": "abc"}, "me"))
            self.assertTrue(auth.delete_session())
            self.assertIsNone(auth.load_session())
            self.assertFalse(auth.delete_session())


if __name__ == "__main__":
    unittest.main()
