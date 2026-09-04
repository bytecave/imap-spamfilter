"""Dashboard auth, scope parsing, and redirect guards.

Run: STATE_DIR=/tmp/x python -m pytest test_dashboard.py
"""

import os
import sqlite3
import tempfile
import time
from urllib.parse import urlparse

import pytest

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="sf_dash_"))

import dashboard as d  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_login_state():
    d._login_fails.clear()
    yield
    d._login_fails.clear()


@pytest.fixture
def dashboard_db(tmp_path, monkeypatch):
    import filter as f

    db_path = tmp_path / "spamfilter.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(f.SCHEMA)
    now = int(time.time())
    sha = "shared-alpha-body"
    conn.executemany(
        """
        INSERT INTO messages(
            account, folder, uidvalidity, uid, message_id, body_sha256,
            first_seen, last_seen, current_folder, our_score, our_action,
            learned_as, learned_at, sender, subject, received_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "acct-alpha", "INBOX", 10, "alpha@id", sha, now, now,
                "INBOX", 9.0, "move", None, None, "alpha@sender",
                "ALPHA SUBJECT", now,
            ),
            (
                "acct-alpha", "Train-Ham", 11, "alpha-copy@id", sha,
                now, now, "Train-Ham", None, None, "ham", now,
                "alpha@sender", "ALPHA TRAIN COPY", now,
            ),
            (
                "acct-beta", "INBOX", 20, "beta@id", "beta-body",
                now, now, "INBOX", 5.0, "tag", "spam", now,
                "beta@sender", "BETA SUBJECT", now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO events(account, ts, message_id, event, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("acct-alpha", now, "alpha@id", "scan", "ALPHA SCAN DETAIL"),
            ("acct-alpha", now, "alpha@id", "learn_ham", "ALPHA LEARN DETAIL"),
            ("acct-beta", now, "beta@id", "scan", "BETA SCAN DETAIL"),
            ("acct-beta", now, "beta@id", "learn_spam", "BETA LEARN DETAIL"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO safe_mode(account, scope, entered_at, reason)
        VALUES (?, 'scan', ?, ?)
        """,
        [
            ("acct-alpha", now, "ALPHA SAFE REASON"),
            ("acct-beta", now, "BETA SAFE REASON"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(d, "DB_PATH", db_path)
    return db_path


def _install_user(monkeypatch, user):
    users = {user.name: user}
    monkeypatch.setattr(d, "_load_users", lambda: users)
    return users


def _authenticated_client(monkeypatch, user):
    _install_user(monkeypatch, user)
    client = d.app.test_client()
    now = int(time.time())
    with client.session_transaction() as sess:
        sess["user"] = user.name
        sess["credential_version"] = d._credential_version(user)
        sess["issued"] = now
        sess["last"] = now
    return client


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


def test_parse_user_line_rejects_username_login_cannot_accept():
    users: dict = {}
    d._parse_user_line(
        f"{'u' * (d.LOGIN_USERNAME_MAX + 1)}:h:admin",
        users,
    )
    assert users == {}


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
    monkeypatch.setattr(d, "_spend_auth_cost", lambda _password: None)
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
    monkeypatch.setattr(d, "_spend_auth_cost", lambda _password: None)
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


def test_client_ip_ignores_xff_from_untrusted_peer(monkeypatch):
    monkeypatch.setattr(
        d, "_TRUSTED_PROXY_NETWORKS",
        d._parse_proxy_networks("127.0.0.1/32,::1/128"),
    )
    with d.app.test_request_context(
        "/", headers={"X-Forwarded-For": "203.0.113.9"},
        environ_base={"REMOTE_ADDR": "100.64.0.8"},
    ):
        assert d._client_ip() == "100.64.0.8"


def test_client_ip_uses_rightmost_untrusted_hop_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(
        d, "_TRUSTED_PROXY_NETWORKS",
        d._parse_proxy_networks("127.0.0.1/32,10.0.0.0/8"),
    )
    with d.app.test_request_context(
        "/", headers={"X-Forwarded-For": "198.51.100.4, 100.64.0.9, 10.1.2.3"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert d._client_ip() == "100.64.0.9"


def test_login_failure_state_is_globally_pruned_and_bounded(monkeypatch):
    monkeypatch.setattr(d, "LOGIN_FAIL_STATE_MAX", 3)
    monkeypatch.setattr(d.time, "monotonic", lambda: 1000.0)
    d._login_fails[("stale-ip", "stale-user")] = [1.0]
    d._login_fails[("recent-ip", "recent-user")] = [999.0]

    for i in range(10):
        d._record_login_failure(f"100.64.0.{i}", f"user-{i}")

    assert ("stale-ip", "stale-user") not in d._login_fails
    assert len(d._login_fails) <= 3
    assert all(
        len(times) <= d.LOGIN_FAIL_LIMIT for times in d._login_fails.values())


def test_successful_login_clears_user_failures_from_all_ips():
    d._login_fails[("100.64.0.1", "alice")] = [1.0]
    d._login_fails[("100.64.0.2", "alice")] = [2.0]
    d._login_fails[("100.64.0.1", "bob")] = [3.0]

    d._clear_login_failures("100.64.0.1", "Alice")

    assert d._login_fails == {("100.64.0.1", "bob"): [3.0]}


def test_login_lockout_avoids_credential_check(monkeypatch):
    for _ in range(d.LOGIN_FAIL_LIMIT):
        d._record_login_failure("127.0.0.1", "alice")
    spent = []
    monkeypatch.setattr(d, "_spend_auth_cost", spent.append)
    monkeypatch.setattr(
        d, "_check_login",
        lambda *_args: pytest.fail("locked login reached credential check"),
    )

    resp = d.app.test_client().post(
        "/login", data={"username": "alice", "password": "guess"})

    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert spent == []


def test_login_rejects_oversized_fields_before_lookup(monkeypatch):
    spent = []
    monkeypatch.setattr(d, "_spend_auth_cost", spent.append)
    monkeypatch.setattr(
        d, "_check_login",
        lambda *_args: pytest.fail("oversized login reached credential check"),
    )

    resp = d.app.test_client().post(
        "/login",
        data={
            "username": "u" * (d.LOGIN_USERNAME_MAX + 1),
            "password": "p" * (d.LOGIN_PASSWORD_MAX + 1),
        },
    )

    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert len(spent) == 1
    assert all(len(user) <= d.LOGIN_USERNAME_MAX for _, user in d._login_fails)


def test_dummy_kdf_does_not_hash_an_oversized_password(monkeypatch):
    calls = []
    monkeypatch.setattr(
        d, "_verify_pbkdf2",
        lambda verifier, password: calls.append((verifier, password)) or False,
    )

    d._spend_auth_cost("p" * (d.LOGIN_PASSWORD_MAX + 1))

    assert calls == [(d._DUMMY_VERIFIER, "")]


def test_login_request_body_is_limited():
    resp = d.app.test_client().post(
        "/login",
        data={"username": "alice", "password": "x" * d.LOGIN_REQUEST_MAX},
    )
    assert resp.status_code == 413


def test_waitress_rejects_large_body_before_wsgi(monkeypatch):
    user = d._User("admin", "plain:pw", True, frozenset())
    served = {}

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(d, "_load_users", lambda: {"admin": user})
    monkeypatch.setattr(d.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        d,
        "serve",
        lambda _app, **kwargs: served.update(kwargs),
    )

    d.start()

    assert served["max_request_body_size"] == d.LOGIN_REQUEST_MAX


def test_legacy_login_performs_dummy_kdf(monkeypatch):
    legacy = d._User("legacy", "plain:pw", True, frozenset())
    monkeypatch.setattr(d, "_load_users", lambda: {"legacy": legacy})
    spent = []
    monkeypatch.setattr(d, "_spend_auth_cost", spent.append)

    assert d._check_login("legacy", "pw") == legacy
    assert d._check_login("legacy", "wrong") is None
    assert d._check_login("missing", "guess") is None
    assert spent == ["pw", "wrong", "guess"]


@pytest.mark.parametrize("route", ["/", "/messages", "/learned", "/events", "/accounts"])
def test_scoped_user_cannot_see_other_account_on_any_page(
        route, dashboard_db, monkeypatch):
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    client = _authenticated_client(monkeypatch, user)
    monkeypatch.setattr(
        d, "_rspamd_stats",
        lambda: pytest.fail("scoped user queried system-wide Rspamd stats"),
    )

    resp = client.get(route)

    assert resp.status_code == 200
    assert b"acct-alpha" in resp.data
    assert b"acct-beta" not in resp.data
    assert b"BETA SUBJECT" not in resp.data
    assert b"BETA LEARN DETAIL" not in resp.data
    assert b"BETA SAFE REASON" not in resp.data


def test_messages_route_uses_sibling_learning_query(dashboard_db, monkeypatch):
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    client = _authenticated_client(monkeypatch, user)

    resp = client.get("/messages")

    assert resp.status_code == 200
    assert b"ALPHA SUBJECT" in resp.data
    assert b">ham<" in resp.data


def test_messages_sort_by_score(dashboard_db, monkeypatch):
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    resp = client.get("/messages")
    assert b'sort=score' in resp.data
    assert b'dir=desc' in resp.data
    desc = client.get("/messages?sort=score&dir=desc")
    assert desc.status_code == 200
    html = desc.data.decode()
    assert html.index("ALPHA SUBJECT") < html.index("BETA SUBJECT")
    assert 'dir=asc' in html
    asc = client.get("/messages?sort=score&dir=asc")
    html = asc.data.decode()
    assert html.index("BETA SUBJECT") < html.index("ALPHA SUBJECT")
    assert 'dir=desc' in html


def test_learned_sort_by_event(dashboard_db, monkeypatch):
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    resp = client.get("/learned")
    assert b'sort=event' in resp.data
    assert b'dir=asc' in resp.data
    asc = client.get("/learned?sort=event&dir=asc")
    assert asc.status_code == 200
    html = asc.data.decode()
    assert html.index("ALPHA SUBJECT") < html.index("BETA SUBJECT")
    desc = client.get("/learned?sort=event&dir=desc")
    html = desc.data.decode()
    assert html.index("BETA SUBJECT") < html.index("ALPHA SUBJECT")


def test_rspamd_stats_are_admin_only(dashboard_db, monkeypatch):
    calls = []

    def stats():
        calls.append(True)
        return {
            "scanned": 424242,
            "uptime": 3600,
            "actions": {},
            "statfiles": [],
        }

    monkeypatch.setattr(d, "_rspamd_stats", stats)
    admin = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, admin)

    resp = client.get("/")

    assert resp.status_code == 200
    assert b"424242" in resp.data
    assert b"rspamd lifetime totals" in resp.data
    assert calls == [True]


def test_security_headers_and_secure_cookie(monkeypatch):
    user = d._User("admin", "plain:stable", True, frozenset())
    monkeypatch.setattr(d, "_check_login", lambda *_args: user)
    monkeypatch.setattr(d, "_login_blocked", lambda *_args: False)
    monkeypatch.setitem(d.app.config, "SESSION_COOKIE_SECURE", True)

    resp = d.app.test_client().post(
        "/login", data={"username": "admin", "password": "pw"})

    assert resp.status_code == 302
    csp = resp.headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'none'")
    assert "script-src 'self'" in csp
    assert "script-src 'unsafe-inline'" not in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie


def test_password_change_revokes_existing_session(dashboard_db, monkeypatch):
    original = d._User("viewer", "plain:old", False, frozenset({"acct-alpha"}))
    users = _install_user(monkeypatch, original)
    client = d.app.test_client()
    now = int(time.time())
    with client.session_transaction() as sess:
        sess["user"] = original.name
        sess["credential_version"] = d._credential_version(original)
        sess["issued"] = now
        sess["last"] = now
    users["viewer"] = d._User(
        "viewer", "plain:new", False, frozenset({"acct-alpha"}))

    resp = client.get("/messages", follow_redirects=False)

    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/login"
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_scope_change_applies_without_revoking_session(dashboard_db, monkeypatch):
    original = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    users = _install_user(monkeypatch, original)
    client = d.app.test_client()
    now = int(time.time())
    with client.session_transaction() as sess:
        sess["user"] = original.name
        sess["credential_version"] = d._credential_version(original)
        sess["issued"] = now
        sess["last"] = now
    users["viewer"] = d._User(
        "viewer", "plain:stable", False, frozenset({"acct-beta"}))

    resp = client.get("/messages")

    assert resp.status_code == 200
    assert b"acct-beta" in resp.data
    assert b"acct-alpha" not in resp.data


@pytest.mark.parametrize(
    ("issued_delta", "last_delta"),
    [
        (d.SESSION_ABS_S + 1, 0),
        (0, d.SESSION_IDLE_S + 1),
    ],
)
def test_expired_session_is_cleared(
        issued_delta, last_delta, dashboard_db, monkeypatch):
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    _install_user(monkeypatch, user)
    client = d.app.test_client()
    now = int(time.time())
    with client.session_transaction() as sess:
        sess["user"] = user.name
        sess["credential_version"] = d._credential_version(user)
        sess["issued"] = now - issued_delta
        sess["last"] = now - last_delta

    resp = client.get("/messages", follow_redirects=False)

    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path == "/login"
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_database_error_returns_hardened_503(dashboard_db, tmp_path, monkeypatch):
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    client = _authenticated_client(monkeypatch, user)
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "missing" / "spamfilter.db")

    resp = client.get("/messages")

    assert resp.status_code == 503
    assert resp.mimetype == "text/plain"
    assert b"state DB error" in resp.data
    assert resp.headers["Cache-Control"] == "no-store"


def _list_yaml(tmp_path):
    path = tmp_path / "accounts.yml"
    path.write_text(
        "list_domains:\n"
        "  - domain: rjmetalfab.com\n"
        "    type: company\n"
        "accounts:\n"
        "  - name: a\n"
        "    imap_host: h\n"
        "    user: u@rjmetalfab.com\n"
        "    password: x\n"
        "    actual_name: Rich Eizenhoefer\n"
    )
    return path


def test_admin_list_page_and_js(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    resp = client.get("/lists/domains")
    assert resp.status_code == 200
    assert b"/lists.js" in resp.data
    assert b"list-body-hl" in resp.data
    assert b"rjmetalfab.com" in resp.data
    resp = client.get("/lists/users")
    assert resp.status_code == 200
    assert b"/lists.js" in resp.data
    assert b"Rich Eizenhoefer" in resp.data
    js = client.get("/lists.js")
    assert js.status_code == 200
    assert b"list-editor" in js.data
    resp = client.get("/")
    assert b"Domain lists" in resp.data
    assert b"User lists" in resp.data


def test_non_admin_lists_forbidden(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    client = _authenticated_client(monkeypatch, user)
    assert client.get("/lists/domains").status_code == 403
    assert client.get("/lists/users").status_code == 403
    assert b"Domain lists" not in client.get("/").data


def test_list_post_requires_csrf(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    resp = client.post(
        "/lists/domains",
        data={"scope": "rjmetalfab.com", "kind": "allow", "body": "a@x.com\n"},
    )
    assert resp.status_code == 400
    import filter as f
    f.DB_PATH = d.DB_PATH
    db = f.Db("_dashboard")
    assert db.list_get("domain", "rjmetalfab.com", "allow") == []
    db.close()


def test_list_post_invalid_line_does_not_persist(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    client.get("/lists/domains")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    resp = client.post(
        "/lists/domains",
        data={
            "csrf_token": token,
            "scope": "rjmetalfab.com",
            "kind": "allow",
            "body": "a @x.com\n",
        },
    )
    assert resp.status_code == 400
    assert b"whitespace" in resp.data
    import filter as f
    f.DB_PATH = d.DB_PATH
    db = f.Db("_dashboard")
    assert db.list_get("domain", "rjmetalfab.com", "allow") == []
    db.close()


def test_list_post_person_rejects_domain_pattern(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    client.get("/lists/users")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    resp = client.post(
        "/lists/users",
        data={
            "csrf_token": token,
            "scope": "Rich Eizenhoefer",
            "kind": "allow",
            "body": "@x.com\n",
        },
    )
    assert resp.status_code == 400
    assert b"person lists" in resp.data or b"whole-domain" in resp.data


def test_list_post_domain_normalizes_bare_host(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    client.get("/lists/domains?scope=rjmetalfab.com&kind=allow")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    resp = client.post(
        "/lists/domains",
        data={
            "csrf_token": token,
            "scope": "rjmetalfab.com",
            "kind": "allow",
            "body": "x.com\n",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    import filter as f
    f.DB_PATH = d.DB_PATH
    db = f.Db("_dashboard")
    assert db.list_get("domain", "rjmetalfab.com", "allow") == ["@x.com"]
    db.close()


def test_list_post_allow_flips_block_sibling(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    import filter as f
    f.DB_PATH = d.DB_PATH
    db = f.Db("_dashboard")
    with db.tx():
        db.list_upsert_address(
            "domain", "rjmetalfab.com", "block",
            f.ParsedPattern("a@x.com", "address"),
            source="dashboard", max_entries=1000,
        )
    db.close()
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    client.get("/lists/domains?scope=rjmetalfab.com&kind=allow")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    resp = client.post(
        "/lists/domains",
        data={
            "csrf_token": token,
            "scope": "rjmetalfab.com",
            "kind": "allow",
            "body": "a@x.com\n",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db = f.Db("_dashboard")
    assert db.list_get("domain", "rjmetalfab.com", "allow") == ["a@x.com"]
    assert db.list_get("domain", "rjmetalfab.com", "block") == []
    db.close()


def test_list_post_unknown_scope_404(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("admin", "plain:stable", True, frozenset())
    client = _authenticated_client(monkeypatch, user)
    client.get("/lists/domains")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    resp = client.post(
        "/lists/domains",
        data={
            "csrf_token": token,
            "scope": "not-a-domain.com",
            "kind": "allow",
            "body": "a@x.com\n",
        },
    )
    assert resp.status_code == 404


def test_non_admin_list_post_403(dashboard_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d, "CONFIG_PATH", _list_yaml(tmp_path))
    user = d._User("viewer", "plain:stable", False, frozenset({"acct-alpha"}))
    client = _authenticated_client(monkeypatch, user)
    resp = client.post(
        "/lists/domains",
        data={"scope": "rjmetalfab.com", "kind": "allow", "body": "a@x.com\n"},
    )
    assert resp.status_code == 403
