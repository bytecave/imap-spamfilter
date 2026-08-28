"""Dashboard auth, scope parsing, and redirect guards.

Run: STATE_DIR=/tmp/x python -m pytest test_dashboard.py
"""

import os
import tempfile
from urllib.parse import urlparse

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_dash_"))

import dashboard as d  # noqa: E402


def test_parse_user_line_missing_scope_is_ignored():
    users: dict = {}
    d._parse_user_line("alice:somehash", users)
    assert users == {}


def test_parse_user_line_admin_and_scoped():
    users: dict = {}
    d._parse_user_line("alice:h:admin", users)
    d._parse_user_line("bob:h:acct1|acct2", users)
    assert users["alice"].admin is True
    assert users["alice"].accounts == frozenset()
    assert users["bob"].admin is False
    assert users["bob"].accounts == frozenset({"acct1", "acct2"})


def test_kpi_escapes_html():
    html = d._kpi("x", "<script>alert(1)</script>", sub="<img>")
    assert "<script>" not in html
    assert "<img>" not in html
    assert "&lt;script&gt;" in html


def test_safe_next_rejects_open_redirect():
    assert d._safe_next("//evil.example") == "/"
    assert d._safe_next("https://evil.example") == "/"
    assert d._safe_next("/messages") == "/messages"
    assert d._safe_next("/") == "/"
    assert d._safe_next("/messages?x=1") == "/"


def _login_client(monkeypatch, next_url: str):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    d._login_fails.clear()
    client = d.app.test_client()
    return client.post(
        "/login",
        query_string={"next": next_url},
        data={"username": "admin", "password": "pw"},
        follow_redirects=False,
    )


def test_login_next_open_redirect_goes_home(monkeypatch):
    resp = _login_client(monkeypatch, "//evil.example")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/"


def test_login_next_absolute_url_goes_home(monkeypatch):
    resp = _login_client(monkeypatch, "https://evil.example")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/"


def test_login_next_messages_allowed(monkeypatch):
    resp = _login_client(monkeypatch, "/messages")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/messages"


def test_get_logout_does_not_clear_session(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    d._login_fails.clear()
    client = d.app.test_client()
    login = client.post(
        "/login",
        data={"username": "admin", "password": "pw"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 405
    with client.session_transaction() as sess:
        assert sess.get("user") == "admin"


def test_fmt_learn_detail_strips_folder_suffix():
    assert d._fmt_learn_detail("train_ham_folder") == "train_ham"
    assert d._fmt_learn_detail("train_spam_folder") == "train_spam"
    assert d._fmt_learn_detail("user revert") == "user revert"
    assert d._fmt_learn_detail(None) == ""


def test_event_table_shows_subject_not_message_id():
    rows = [{
        "ts": 1_700_000_000,
        "account": "acct",
        "event": "learn_ham",
        "message_id": "secret-mid@example.com",
        "detail": "train_ham_folder",
        "subject": "Invoice from Amazon",
    }]
    html = d._event_table(rows)
    assert "Invoice from Amazon" in html
    assert "secret-mid@example.com" not in html
    assert "train_ham_folder" not in html
    assert "train_ham" in html
    assert "Message-Id" not in html


def test_decode_rfc2047_cleans_encoded_subjects():
    q = "=?UTF-8?Q?=F0=9F=94=94_Reminder:_My_Life_With_the_Walter_Boys_is_back_?="
    out_q = d.decode_rfc2047(q)
    assert "Reminder" in out_q
    assert "Walter Boys" in out_q
    assert "=?" not in out_q

    b64 = "=?utf-8?B?U2VlIHdoYXTigJlzIGNvbWluZyBmb3IgQUFkdmFudGFnZcKuIG1lbWJlcnM=?="
    out_b = d.decode_rfc2047(b64)
    assert "coming" in out_b.lower()
    assert "=?" not in out_b

    truncated = (
        "=?us-ascii?Q?Rich_Writes_Contact:_Mutual_benefit:_featured_p?= "
        "=?us-ascii?]"
    )
    out_t = d.decode_rfc2047(truncated)
    assert "Rich Writes Contact" in out_t
    assert "=?" not in out_t


def test_event_table_decodes_encoded_subject():
    rows = [{
        "ts": 1_700_000_000,
        "account": "acct",
        "event": "learn_spam",
        "message_id": "mid",
        "detail": "x",
        "subject": "=?utf-8?Q?Hello_World?=",
    }]
    html = d._event_table(rows)
    assert "Hello World" in html
    assert "=?utf-8" not in html


def test_messages_learn_fills_from_sha256_sibling(tmp_path):
    """Inbox scored row with empty learned_as picks up a Train-* sibling."""
    import sqlite3

    import filter as f

    db_path = tmp_path / "spamfilter.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(f.SCHEMA)
    f._migrate(conn)
    sha = "abc" * 21 + "ab"  # 64 hex-like chars not required; any shared string
    conn.execute(
        """
        INSERT INTO messages(
            account, folder, uidvalidity, uid, message_id, body_sha256,
            first_seen, last_seen, current_folder, our_score, learned_as, learned_at
        ) VALUES
        ('a','INBOX',1,10,'mid@x',?,1,2,'INBOX',9.0,NULL,NULL),
        ('a','Junk/Train-Ham',1,3,'mid@x',?,1,3,'Junk/Train-Ham',NULL,'ham',3)
        """,
        (sha, sha),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT COALESCE(
                 NULLIF(m.learned_as, ''),
                 (SELECT s.learned_as FROM messages s
                   WHERE s.account = m.account
                     AND m.body_sha256 IS NOT NULL AND m.body_sha256 != ''
                     AND s.body_sha256 = m.body_sha256
                     AND s.learned_as IS NOT NULL AND s.learned_as != ''
                   ORDER BY CASE WHEN s.learned_as IN ('ham','spam') THEN 0 ELSE 1 END,
                            IFNULL(s.learned_at, 0) DESC
                   LIMIT 1)
               ) AS learned_as
          FROM messages m
         WHERE m.folder='INBOX'
        """
    ).fetchone()
    conn.close()
    assert row["learned_as"] == "ham"
