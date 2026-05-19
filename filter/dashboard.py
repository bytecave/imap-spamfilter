"""Read-only web dashboard for the imap-spamfilter.

Disabled by default. Enable it by configuring at least one dashboard
user (see below); the daemon then serves it on a fixed internal port
8080 and the orchestrator maps a host port. Reads the SQLite state DB
read-only and queries rspamd /stat. Intended for LAN access behind a
reverse proxy that terminates TLS.

Authentication is a server-side session with a real login form.
Configure users one of two ways:

  * DASHBOARD_USERS - comma-separated `name:hash` pairs, where hash is
    produced by `python dashboard.py` (an interactive hashing helper).
    Preferred: supports multiple named accounts with per-user passwords.
  * DASHBOARD_USER + DASHBOARD_PASSWORD - a single legacy plaintext
    account. Still accepted so existing deployments keep working.

The signed-session secret is generated once into STATE_DIR and reused
across restarts so logins survive a redeploy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

import requests
from flask import (
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import escape
from waitress import serve

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
DB_PATH = STATE_DIR / "spamfilter.db"
SECRET_PATH = STATE_DIR / "dashboard_secret"
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/accounts.yml"))
RSPAMD_CONTROLLER_URL = os.environ.get(
    "RSPAMD_LEARN_URL", "http://spamfilter-rspamd:11334"
)
# Project logo, shipped into the image next to this module (see Dockerfile).
FAVICON_PATH = Path(__file__).with_name("favicon.png")
# Mirrors min_learns in rspamd/local.d/classifier-bayes.conf. A Bayes
# class scores nothing until it reaches this many learns.
BAYES_MIN_LEARNS = int(os.environ.get("BAYES_MIN_LEARNS", "200"))
# Container-internal listen port is fixed; the orchestrator maps a host port.
DASHBOARD_PORT = 8080
PBKDF2_ITERATIONS = 600_000

log = logging.getLogger("dashboard")
app = Flask(__name__)


# ----- auth -----------------------------------------------------------------


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-describing pbkdf2 hash string for `password`."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_pbkdf2(stored: str, password: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


USERS_FILE = STATE_DIR / "dashboard_users"


@dataclass(frozen=True)
class _User:
    name: str
    verifier: str                 # pbkdf2 hash, or 'plain:<pw>' (legacy env)
    admin: bool                   # admin sees every account
    accounts: frozenset[str]      # account names a non-admin may see


def _parse_user_line(raw: str, users: dict[str, "_User"]) -> None:
    """Parse one `username:verifier[:scope]` record. `scope` is 'admin'
    or a pipe/comma-separated list of account names; absent = admin.
    The verifier is a pbkdf2 hash, which contains no ':'."""
    parts = raw.split(":")
    if len(parts) < 2:
        return
    name = parts[0].strip()
    verifier = parts[1].strip()
    scope = (parts[2].strip() if len(parts) >= 3 and parts[2].strip()
             else "admin")
    if not name or not verifier:
        return
    admin = scope.lower() == "admin"
    accounts = frozenset() if admin else frozenset(
        a.strip() for a in re.split(r"[|,]", scope) if a.strip())
    users[name] = _User(name, verifier, admin, accounts)


def _load_users() -> dict[str, "_User"]:
    """Map username -> _User. Sources, lowest precedence first:
      1. state/dashboard_users - `username:hash[:scope]` per line,
         `#` comments and blank lines ignored;
      2. the DASHBOARD_USERS env var - comma-separated entries;
      3. the legacy DASHBOARD_USER + DASHBOARD_PASSWORD pair (admin).
    `scope` is 'admin' or a pipe-separated list of account names a
    non-admin user may see. Re-read on every login so edits apply
    without a restart."""
    users: dict[str, _User] = {}
    try:
        if USERS_FILE.is_file():
            for line in USERS_FILE.read_text().splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    _parse_user_line(s, users)
    except OSError as ex:
        logging.getLogger("dashboard").warning(
            "could not read %s: %s", USERS_FILE, ex)
    for entry in os.environ.get("DASHBOARD_USERS", "").split(","):
        if entry.strip():
            _parse_user_line(entry.strip(), users)
    legacy_user = os.environ.get("DASHBOARD_USER", "").strip()
    legacy_pass = os.environ.get("DASHBOARD_PASSWORD", "")
    if legacy_user and legacy_pass and legacy_user not in users:
        users[legacy_user] = _User(
            legacy_user, "plain:" + legacy_pass, True, frozenset())
    return users


def _check_login(username: str, password: str) -> bool:
    u = _load_users().get(username)
    if u is None:
        # Spend comparable effort on an unknown user so response timing
        # does not reveal which usernames exist.
        _verify_pbkdf2(f"pbkdf2${PBKDF2_ITERATIONS}$00$00", password)
        return False
    if u.verifier.startswith("plain:"):
        return hmac.compare_digest(u.verifier[len("plain:"):], password)
    return _verify_pbkdf2(u.verifier, password)


def _current_scope() -> tuple[bool, frozenset[str]]:
    """(is_admin, allowed_account_names) for the logged-in user. An
    unknown session yields no access."""
    u = _load_users().get(session.get("user", ""))
    if u is None:
        return (False, frozenset())
    return (u.admin, u.accounts)


def _scope_clause(prefix: str = "AND") -> tuple[str, list]:
    """SQL fragment + params restricting the `account` column to the
    current user's scope. Empty string for admins; '<prefix> 1=0' for
    a non-admin with no accounts."""
    admin, accts = _current_scope()
    if admin:
        return ("", [])
    if not accts:
        return (f" {prefix} 1=0", [])
    return (f" {prefix} account IN ({','.join('?' * len(accts))})",
            list(accts))


def _load_secret() -> str:
    """Persist a signing secret in STATE_DIR so sessions survive restarts.
    Falls back to an ephemeral secret if the dir is not writable."""
    try:
        if SECRET_PATH.is_file():
            existing = SECRET_PATH.read_text().strip()
            if existing:
                return existing
        fresh = secrets.token_hex(32)
        SECRET_PATH.write_text(fresh)
        SECRET_PATH.chmod(0o600)
        return fresh
    except OSError as ex:
        logging.getLogger("dashboard").warning(
            "could not persist session secret (%s); using an ephemeral one "
            "- logins will drop on restart", ex
        )
        return secrets.token_hex(32)


app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # TLS is terminated upstream; only mark the cookie Secure when the
    # operator confirms the proxy forwards HTTPS to this app.
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_COOKIE_SECURE", "")
    .lower() in ("1", "true", "yes"),
)


def _requires_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


# ----- helpers --------------------------------------------------------------


def _db() -> sqlite3.Connection:
    """Open a fresh read-only SQLite connection for this request."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _h(value) -> str:
    """HTML-escape a value for safe interpolation into the f-string page
    bodies below. None renders as an empty string. Mail-derived values
    (subject, sender, Message-Id, ...) are attacker-controlled, so every
    such interpolation must pass through here."""
    return str(escape("" if value is None else value))


def _fmt_ts(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))


def _ago(ts):
    """Short relative time, e.g. '3m', '5h', '2d'."""
    if not ts:
        return "-"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 90:
        return f"{delta}s"
    if delta < 5400:
        return f"{delta // 60}m"
    if delta < 172800:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _fmt_score(s):
    if s is None:
        return "-"
    try:
        f = float(s)
    except (TypeError, ValueError):
        return _h(s)
    return f"{f:+.2f}"


def _score_class(s):
    if s is None:
        return ""
    try:
        f = float(s)
    except (TypeError, ValueError):
        return ""
    if f >= 8:
        return "score-high"
    if f >= 4:
        return "score-mid"
    return ""


def _bar(value: float, maximum: float, cls: str = "ok") -> str:
    """A single-segment CSS bar, width proportional to value/maximum."""
    pct = 0.0 if maximum <= 0 else min(100.0, value / maximum * 100.0)
    return (
        f'<div class="bar"><span class="seg seg-{cls}" '
        f'style="width:{pct:.1f}%"></span></div>'
    )


def _split_bar(left: float, right: float) -> str:
    """A two-segment stacked bar (spam vs ham), each sized by share."""
    total = left + right
    lpct = 50.0 if total <= 0 else left / total * 100.0
    return (
        f'<div class="bar bar-split">'
        f'<span class="seg seg-bad" style="width:{lpct:.1f}%"></span>'
        f'<span class="seg seg-ok" style="width:{100 - lpct:.1f}%"></span>'
        f"</div>"
    )


def _sparkline(values: list[int], width: int = 168, height: int = 38) -> str:
    """Inline-SVG sparkline; no JS, no charting library."""
    if not values or max(values) == 0:
        return '<span class="muted">no activity</span>'
    peak = max(values)
    n = len(values)
    step = width / max(1, n - 1)
    pad = 3
    pts = " ".join(
        f"{i * step:.1f},"
        f"{height - pad - (v / peak) * (height - 2 * pad):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" preserveAspectRatio="none">'
        f'<polyline points="{pts}"/></svg>'
    )


def _rspamd_stats() -> dict | None:
    try:
        r = requests.get(
            f"{RSPAMD_CONTROLLER_URL}/stat",
            headers={"Password": os.environ.get("RSPAMD_PASSWORD", "")},
            timeout=3,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


def _kpi(label: str, value, sub: str = "", cls: str = "") -> str:
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi {cls}"><div class="label">{label}</div>'
        f'<div class="value">{value}</div>{sub_html}</div>'
    )


def _kind_badge(event: str) -> str:
    if event == "learn_spam":
        return '<span class="pill pill-bad">spam</span>'
    if event == "learn_ham":
        return '<span class="pill pill-ok">ham</span>'
    return _h(event)


def _fail_cell(count: int) -> str:
    if count:
        return f'<span class="pill pill-warn">{count}</span>'
    return "0"


# ----- error handling -------------------------------------------------------


@app.errorhandler(sqlite3.Error)
def _on_db_error(ex: sqlite3.Error) -> Response:
    """A locked DB, a missing table, or a mid-read failure renders a plain
    503 instead of leaking a stack trace through Flask's default handler."""
    log.error("dashboard DB error: %s", ex)
    return Response(
        "Dashboard temporarily unavailable (state DB error).\n",
        503,
        mimetype="text/plain",
    )


# ----- templates ------------------------------------------------------------


STYLE = """
:root {
  --bg:#f5f6f8; --surface:#ffffff; --surface-2:#f0f2f5; --text:#1f2328;
  --muted:#6b7280; --border:#e3e6ea; --accent:#2563eb;
  --nav-bg:#1f2328; --nav-fg:#e6e8eb; --nav-active:#5b9bff;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#b45309; --warn-bg:#fef3c7;
  --bad:#b91c1c; --bad-bg:#fee2e2; --shadow:0 1px 3px rgba(0,0,0,0.07);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0f1115; --surface:#1a1d23; --surface-2:#22262e; --text:#e6e8eb;
    --muted:#9aa1ab; --border:#2d323b; --accent:#5b9bff;
    --nav-bg:#16181d; --nav-fg:#e6e8eb; --nav-active:#7fb0ff;
    --ok:#4ade80; --ok-bg:#0f2e1c; --warn:#fbbf24; --warn-bg:#3a2c0a;
    --bad:#f87171; --bad-bg:#3a1414; --shadow:0 1px 3px rgba(0,0,0,0.4);
  }
}
* { box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  margin:0; background:var(--bg); color:var(--text);
  font-size:14px; line-height:1.45; }
a { color:var(--accent); }
nav { background:var(--nav-bg); color:var(--nav-fg); padding:0.55em 1em;
  display:flex; flex-wrap:wrap; align-items:center; gap:0.3em;
  position:sticky; top:0; z-index:10; }
nav .brand { display:flex; align-items:center; gap:0.5em; font-weight:600;
  margin-right:0.8em; }
nav .brand img { width:22px; height:22px; border-radius:4px; }
nav a { color:var(--nav-fg); text-decoration:none; padding:0.35em 0.7em;
  border-radius:6px; font-weight:500; opacity:0.78; }
nav a:hover { opacity:1; background:rgba(255,255,255,0.08); }
nav a.active { opacity:1; background:rgba(255,255,255,0.14);
  color:var(--nav-active); }
nav .spacer { flex:1 1 auto; }
nav .who { opacity:0.7; font-size:0.85em; padding:0.35em 0.5em; }
main { padding:1.1em 1.3em; max-width:1320px; margin:0 auto; }
h1 { font-size:1.35em; margin:0.1em 0 0.7em; }
h2 { font-size:1.02em; margin:0 0 0.6em; color:var(--muted);
  text-transform:uppercase; letter-spacing:0.04em; font-weight:600; }
.card { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:1em 1.1em; margin-bottom:1.1em;
  box-shadow:var(--shadow); }
.kpi-row { display:grid; gap:0.7em; margin:0 0 0.4em;
  grid-template-columns:repeat(auto-fill,minmax(148px,1fr)); }
.kpi { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:0.65em 0.85em; box-shadow:var(--shadow); }
.kpi .label { font-size:0.72em; color:var(--muted); text-transform:uppercase;
  letter-spacing:0.04em; }
.kpi .value { font-size:1.6em; font-weight:650; font-variant-numeric:tabular-nums; }
.kpi .sub { font-size:0.78em; color:var(--muted); }
.kpi.warn .value { color:var(--warn); }
.kpi.bad .value { color:var(--bad); }
.kpi.ok .value { color:var(--ok); }
.banner { border-radius:10px; padding:0.7em 1em; margin-bottom:1.1em;
  font-weight:550; border:1px solid; }
.banner.ok { background:var(--ok-bg); border-color:var(--ok); color:var(--ok); }
.banner.warn { background:var(--warn-bg); border-color:var(--warn);
  color:var(--warn); }
.tw { overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { border-collapse:collapse; width:100%; }
th,td { padding:0.5em 0.7em; border-bottom:1px solid var(--border);
  text-align:left; vertical-align:top; white-space:nowrap; }
th { font-size:0.74em; text-transform:uppercase; letter-spacing:0.03em;
  color:var(--muted); }
tbody tr:hover td { background:var(--surface-2); }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
.muted { color:var(--muted); }
.subj { max-width:34em; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; display:block; }
code { font-size:0.92em; word-break:break-all; }
.pill { padding:0.1em 0.5em; border-radius:999px; font-size:0.8em;
  font-weight:600; white-space:nowrap; }
.pill-ok { background:var(--ok-bg); color:var(--ok); }
.pill-bad { background:var(--bad-bg); color:var(--bad); }
.pill-warn { background:var(--warn-bg); color:var(--warn); }
.score-high { color:var(--bad); font-weight:650; }
.score-mid { color:var(--warn); }
.bar { background:var(--surface-2); border-radius:999px; height:9px;
  width:100%; min-width:90px; overflow:hidden; display:flex; }
.bar-split { background:transparent; }
.seg { height:100%; display:block; }
.seg-ok { background:var(--ok); }
.seg-bad { background:var(--bad); }
.seg-warn { background:var(--warn); }
svg.spark { display:block; }
svg.spark polyline { fill:none; stroke:var(--accent); stroke-width:2;
  stroke-linejoin:round; stroke-linecap:round; }
.filterbar { display:flex; flex-wrap:wrap; gap:0.4em; margin-bottom:0.8em; }
.filterbar a { text-decoration:none; padding:0.3em 0.75em; border-radius:999px;
  border:1px solid var(--border); color:var(--text); font-size:0.85em; }
.filterbar a.active { background:var(--accent); border-color:var(--accent);
  color:#fff; }
footer { font-size:0.78em; color:var(--muted); padding:1em 1.3em;
  text-align:center; }
.login-wrap { max-width:320px; margin:8vh auto; padding:0 1em; }
.login-wrap .card { padding:1.4em; }
.login-wrap h1 { text-align:center; }
.login-wrap label { display:block; font-size:0.8em; color:var(--muted);
  margin:0.6em 0 0.2em; }
.login-wrap input { width:100%; padding:0.55em 0.65em; border-radius:7px;
  border:1px solid var(--border); background:var(--bg); color:var(--text);
  font-size:1em; }
.login-wrap button { width:100%; margin-top:1.1em; padding:0.6em;
  border:0; border-radius:7px; background:var(--accent); color:#fff;
  font-size:1em; font-weight:600; cursor:pointer; }
.login-err { background:var(--bad-bg); color:var(--bad); border-radius:7px;
  padding:0.5em 0.7em; font-size:0.88em; margin-top:0.8em; }
@media (max-width:640px) {
  main { padding:0.9em 0.8em; }
  nav { padding:0.5em 0.6em; }
  .subj { max-width:60vw; }
}
"""

BASE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} - spamfilter</title>
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<style>""" + STYLE + """</style>
</head><body>
<nav>
  <span class="brand"><img src="/favicon.png" alt=""> spamfilter</span>
  <a href="/" {% if active=='summary' %}class="active"{% endif %}>Summary</a>
  <a href="/messages" {% if active=='messages' %}class="active"{% endif %}>Messages</a>
  <a href="/learned" {% if active=='learned' %}class="active"{% endif %}>Learned</a>
  <a href="/events" {% if active=='events' %}class="active"{% endif %}>Events</a>
  <a href="/accounts" {% if active=='accounts' %}class="active"{% endif %}>Accounts</a>
  <span class="spacer"></span>
  {% if user %}<span class="who">{{ user }}{% if is_admin %} &middot; admin{% endif %}</span>
  <a href="/logout">Log out</a>{% endif %}
</nav>
<main>
<h1>{{ title }}</h1>
{{ body|safe }}
</main>
<footer>imap-spamfilter dashboard &middot; read-only</footer>
</body></html>
"""

LOGIN = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - spamfilter</title>
<link rel="icon" type="image/png" href="/favicon.png">
<style>""" + STYLE + """</style>
</head><body>
<div class="login-wrap">
<h1><img src="/favicon.png" alt="" style="width:30px;vertical-align:-7px">
 spamfilter</h1>
<div class="card">
<form method="post">
<label for="u">Username</label>
<input id="u" name="username" autocomplete="username" autofocus required>
<label for="p">Password</label>
<input id="p" name="password" type="password"
 autocomplete="current-password" required>
<button type="submit">Sign in</button>
{% if error %}<div class="login-err">{{ error }}</div>{% endif %}
</form>
</div>
</div>
</body></html>
"""


def render(title, active, body_html):
    return render_template_string(
        BASE, title=title, active=active, body=body_html,
        user=session.get("user"), is_admin=_current_scope()[0],
    )


# ----- routes ---------------------------------------------------------------


@app.route("/favicon.png")
@app.route("/favicon.ico")
def favicon():
    """Serve the project logo as the favicon. No auth - it is only the
    logo, and browsers request favicons without credentials."""
    return send_file(FAVICON_PATH, mimetype="image/png", max_age=86400)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if _check_login(username, password):
            session.clear()
            session["user"] = username
            dest = request.args.get("next", "/")
            # Only allow same-site relative redirects.
            if not dest.startswith("/") or dest.startswith("//"):
                dest = "/"
            return redirect(dest)
        error = "Invalid username or password."
    if session.get("user"):
        return redirect("/")
    return render_template_string(LOGIN, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@_requires_auth
def summary():
    now = int(time.time())
    day = 86400
    admin, _accts = _current_scope()
    # sc: AND-fragment for queries that already have a WHERE; scw: the
    # WHERE-style fragment for the account-less safe_mode query.
    sc, sp = _scope_clause("AND")
    scw, scwp = _scope_clause("WHERE")
    with _db() as c:
        def one(sql, params=()):
            return c.execute(sql + sc, (*params, *sp)).fetchone()[0]

        scanned_24h = one(
            "SELECT COUNT(*) FROM events WHERE event='scan' AND ts>=?",
            (now - day,))
        scanned_7d = one(
            "SELECT COUNT(*) FROM events WHERE event='scan' AND ts>=?",
            (now - 7 * day,))
        moved_24h = one(
            "SELECT COUNT(*) FROM events WHERE event LIKE 'moved%' AND ts>=?",
            (now - day,))
        scan_fail_24h = one(
            "SELECT COUNT(*) FROM events WHERE event='scan_failed' AND ts>=?",
            (now - day,))
        learn_fail_24h = one(
            "SELECT COUNT(*) FROM events WHERE event IN "
            "('learn_failed','learn_giveup') AND ts>=?", (now - day,))
        learn_spam_24h = one(
            "SELECT COUNT(*) FROM events WHERE event='learn_spam' AND ts>=?",
            (now - day,))
        learn_ham_24h = one(
            "SELECT COUNT(*) FROM events WHERE event='learn_ham' AND ts>=?",
            (now - day,))
        learn_spam_total = one(
            "SELECT COUNT(*) FROM events WHERE event='learn_spam'")
        learn_ham_total = one(
            "SELECT COUNT(*) FROM events WHERE event='learn_ham'")
        safe_modes = c.execute(
            "SELECT account, scope, reason FROM safe_mode" + scw,
            scwp).fetchall()
        last_learns = c.execute(
            "SELECT ts, account, event, message_id, detail FROM events "
            "WHERE event IN ('learn_spam','learn_ham')" + sc
            + " ORDER BY ts DESC LIMIT 15", sp).fetchall()
        scan_by_day = dict(c.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d, "
            "COUNT(*) FROM events WHERE event='scan' AND ts>=?" + sc
            + " GROUP BY d", (now - 14 * day, *sp)).fetchall())

    # Health banner -----------------------------------------------------
    problems = []
    if scan_fail_24h:
        problems.append(f"{scan_fail_24h} scan failure(s)")
    if learn_fail_24h:
        problems.append(f"{learn_fail_24h} learn failure(s)")
    if safe_modes:
        problems.append(f"{len(safe_modes)} account(s) in safe-mode")
    if problems:
        banner = (f'<div class="banner warn">Attention: '
                  f'{_h(", ".join(problems))} in the last 24h.</div>')
    else:
        banner = ('<div class="banner ok">All healthy - '
                  'no scan or learn failures, no safe-mode.</div>')

    # 14-day scan sparkline --------------------------------------------
    series = []
    for i in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * day))
        series.append(int(scan_by_day.get(d, 0)))
    catch_rate = f"{moved_24h / scanned_24h * 100:.0f}%" if scanned_24h else "-"

    kpis = "".join([
        _kpi("Scanned 24h", scanned_24h),
        _kpi("Scanned 7d", scanned_7d),
        _kpi("Moved 24h", moved_24h, sub=f"{catch_rate} of scanned"),
        _kpi("Scan fails 24h", scan_fail_24h,
             cls="bad" if scan_fail_24h else ""),
        _kpi("Spam learns 24h", learn_spam_24h),
        _kpi("Ham learns 24h", learn_ham_24h),
        _kpi("Spam learns total", learn_spam_total),
        _kpi("Ham learns total", learn_ham_total),
    ])

    trend = (
        f'<div class="card"><h2>Scans per day (14d)</h2>'
        f'{_sparkline(series)}'
        f'<div class="muted" style="font-size:0.8em;margin-top:0.3em">'
        f'{series[0]} &rarr; {series[-1]} per day</div></div>'
    )

    # rspamd lifetime totals and the Bayes table are system-wide
    # aggregates - admin only. Non-admins get just their scoped views.
    rspamd_block = ""
    bayes_block = ""
    stats = _rspamd_stats() if admin else None
    if admin and stats:
        actions = stats.get("actions") or {}
        uptime_s = stats.get("uptime") or 0
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        uptime_str = f"{days}d {hours}h" if days else f"{hours}h"
        fuzzy = stats.get("fuzzy_hashes")
        if isinstance(fuzzy, dict):
            fuzzy_total = sum(
                v for v in fuzzy.values() if isinstance(v, (int, float)))
        else:
            fuzzy_total = fuzzy if fuzzy is not None else "?"
        rspamd_kpis = "".join([
            _kpi("Scanned", stats.get("scanned", "?")),
            _kpi("Identified spam", stats.get("spam_count", "?")),
            _kpi("Identified ham", stats.get("ham_count", "?")),
            _kpi("Total learns", stats.get("total_learns", "?")),
            _kpi("Fuzzy hashes", fuzzy_total),
            _kpi("Connections", stats.get("connections", "?")),
            _kpi("Reject", actions.get("reject", 0)),
            _kpi("Add header", actions.get("add header", 0)),
            _kpi("Greylist", actions.get("greylist", 0)),
            _kpi("No action", actions.get("no action", 0)),
        ])
        rspamd_block = (
            f'<div class="card"><h2>rspamd lifetime totals '
            f'(uptime {uptime_str})</h2>'
            f'<div class="kpi-row">{rspamd_kpis}</div></div>'
        )

        statfiles = stats.get("statfiles") or []
        rows = []
        learns_by_symbol = {}
        for sf in statfiles:
            try:
                learns = int(sf.get("revision") or sf.get("learns")
                             or sf.get("total") or 0)
            except (TypeError, ValueError):
                learns = 0
            symbol = sf.get("symbol", "?")
            learns_by_symbol[symbol] = learns
            if learns >= BAYES_MIN_LEARNS:
                status = '<span class="pill pill-ok">active</span>'
            else:
                status = (f'<span class="pill pill-warn">needs '
                          f'{BAYES_MIN_LEARNS - learns} more</span>')
            bar_cls = "ok" if learns >= BAYES_MIN_LEARNS else "warn"
            rows.append(
                f"<tr><td>{_h(symbol)}</td>"
                f'<td class="num">{_h(sf.get("users", "?"))}</td>'
                f'<td class="num">{learns}</td>'
                f"<td>{_bar(learns, BAYES_MIN_LEARNS, bar_cls)}</td>"
                f"<td>{status}</td></tr>")
        spam_l = learns_by_symbol.get("BAYES_SPAM", 0)
        ham_l = learns_by_symbol.get("BAYES_HAM", 0)
        balance = ""
        if spam_l and ham_l:
            lo, hi = sorted((spam_l, ham_l))
            ratio = hi / lo
            skewed = ratio >= 3
            direction = "ham" if ham_l > spam_l else "spam"
            note = (f'{direction}-skewed {ratio:.1f}:1 - feed more of the '
                    f'lighter class') if skewed else f"balanced {ratio:.1f}:1"
            balance = (
                f'<div style="margin-top:0.7em">'
                f'<div class="muted" style="font-size:0.8em">Learn balance '
                f'(spam vs ham): {_h(note)}</div>{_split_bar(spam_l, ham_l)}'
                f"</div>")
        bayes_block = (
            f'<div class="card"><h2>rspamd Bayes</h2><div class="tw">'
            f"<table><tr><th>Symbol</th><th class=num>Users</th>"
            f"<th class=num>Learns</th><th>Progress (min "
            f"{BAYES_MIN_LEARNS})</th><th>Status</th></tr>"
            + "".join(rows) + f"</table></div>{balance}</div>")
    elif admin:
        rspamd_block = (
            '<div class="card"><h2>rspamd</h2>'
            '<p class="muted">rspamd controller unreachable - lifetime '
            'totals and Bayes stats unavailable.</p></div>')

    # safe-mode ---------------------------------------------------------
    if safe_modes:
        sm_rows = "".join(
            f"<tr><td>{_h(r['account'])}</td><td>{_h(r['scope'])}</td>"
            f"<td>{_h(r['reason'])}</td></tr>" for r in safe_modes)
        safe_block = (
            '<div class="card"><h2>Active safe-mode</h2><div class="tw">'
            "<table><tr><th>Account</th><th>Scope</th><th>Reason</th></tr>"
            + sm_rows + "</table></div></div>")
    else:
        safe_block = ""

    # recent learns -----------------------------------------------------
    learn_rows = "".join(
        f"<tr><td>{_fmt_ts(r['ts'])}</td><td>{_h(r['account'])}</td>"
        f"<td>{_kind_badge(r['event'])}</td>"
        f"<td><code>{_h((r['message_id'] or '')[:60])}</code></td>"
        f'<td class="muted">{_h(r["detail"])}</td></tr>'
        for r in last_learns)
    learns_block = (
        '<div class="card"><h2>Recent learns</h2><div class="tw">'
        "<table><tr><th>When</th><th>Account</th><th>Kind</th>"
        "<th>Message-Id</th><th>Reason</th></tr>"
        + (learn_rows or '<tr><td colspan=5 class=muted>(none yet)</td></tr>')
        + "</table></div></div>")

    body = (
        banner
        + f'<div class="kpi-row">{kpis}</div>'
        + trend + rspamd_block + bayes_block + safe_block + learns_block
    )
    return render("Summary", "summary", body)


@app.route("/messages")
@_requires_auth
def messages():
    band = request.args.get("band", "all")
    where = ""
    if band == "spam":
        where = "AND our_score >= 8"
    elif band == "mid":
        where = "AND our_score BETWEEN 4 AND 8"
    elif band == "low":
        where = "AND our_score < 4"
    sc, sp = _scope_clause("AND")
    with _db() as c:
        rows = c.execute(
            f"""
            SELECT account, message_id, last_seen, received_at, our_score,
                   our_action, current_folder, sender, subject, learned_as
              FROM messages
             WHERE our_score IS NOT NULL {where}{sc}
             ORDER BY COALESCE(received_at, last_seen) DESC LIMIT 200
            """, sp).fetchall()
    body_rows = "".join(
        f'<tr><td>{_fmt_ts(r["received_at"] or r["last_seen"])}</td>'
        f'<td>{_h(r["account"])}</td>'
        f'<td class="num {_score_class(r["our_score"])}">'
        f'{_fmt_score(r["our_score"])}</td>'
        f'<td>{_h(r["our_action"] or "-")}</td>'
        f'<td>{_h(r["current_folder"] or "-")}</td>'
        f'<td>{_h(r["learned_as"] or "-")}</td>'
        f'<td><span class="muted">{_h((r["sender"] or "")[:50])}</span></td>'
        f'<td><span class="subj">{_h(r["subject"])}</span></td></tr>'
        for r in rows)
    bands = [("all", "all"), ("spam", "score >= 8"),
             ("mid", "score 4-8"), ("low", "score < 4")]
    filt = "".join(
        f'<a href="?band={b}" class="{"active" if band==b else ""}">'
        f"{_h(lbl)}</a>" for b, lbl in bands)
    body = (
        f'<div class="filterbar">{filt}</div>'
        '<div class="card"><div class="tw"><table>'
        "<tr><th>When</th><th>Account</th><th class=num>Score</th>"
        "<th>Action</th><th>Folder</th><th>Learn</th><th>Sender</th>"
        "<th>Subject</th></tr>"
        + (body_rows
           or '<tr><td colspan=8 class=muted>(no scored messages yet)</td></tr>')
        + "</table></div></div>")
    return render(f"Messages ({len(rows)} shown)", "messages", body)


def _event_table(rows) -> str:
    out = []
    for r in rows:
        ev = r["event"]
        if ev == "learn_spam":
            badge = '<span class="pill pill-bad">spam</span>'
        elif ev == "learn_ham":
            badge = '<span class="pill pill-ok">ham</span>'
        elif ev in ("learn_failed", "learn_giveup", "scan_failed"):
            badge = f'<span class="pill pill-warn">{_h(ev)}</span>'
        else:
            badge = _h(ev)
        out.append(
            f'<tr><td>{_fmt_ts(r["ts"])}</td>'
            f'<td class="muted">{_ago(r["ts"])}</td>'
            f'<td>{_h(r["account"])}</td><td>{badge}</td>'
            f'<td><code>{_h((r["message_id"] or "")[:80])}</code></td>'
            f'<td class="muted">{_h(r["detail"])}</td></tr>')
    return "".join(out)


@app.route("/learned")
@_requires_auth
def learned():
    sc, sp = _scope_clause("AND")
    with _db() as c:
        rows = c.execute(
            "SELECT ts, account, event, message_id, detail FROM events "
            "WHERE event IN ('learn_spam','learn_ham','learn_giveup',"
            "'learn_failed')" + sc + " ORDER BY ts DESC LIMIT 300",
            sp).fetchall()
    body = (
        '<div class="card"><div class="tw"><table>'
        "<tr><th>When</th><th>Age</th><th>Account</th><th>Event</th>"
        "<th>Message-Id</th><th>Detail</th></tr>"
        + (_event_table(rows)
           or '<tr><td colspan=6 class=muted>(no learn events yet)</td></tr>')
        + "</table></div></div>")
    return render(f"Learn events ({len(rows)} shown)", "learned", body)


@app.route("/events")
@_requires_auth
def events():
    sc, sp = _scope_clause("WHERE")
    with _db() as c:
        rows = c.execute(
            "SELECT ts, account, event, message_id, detail FROM events"
            + sc + " ORDER BY ts DESC LIMIT 300", sp).fetchall()
    body = (
        '<div class="card"><div class="tw"><table>'
        "<tr><th>When</th><th>Age</th><th>Account</th><th>Event</th>"
        "<th>Message-Id</th><th>Detail</th></tr>"
        + (_event_table(rows)
           or '<tr><td colspan=6 class=muted>(no events yet)</td></tr>')
        + "</table></div></div>")
    return render(f"All events ({len(rows)} shown)", "events", body)


@app.route("/accounts")
@_requires_auth
def accounts_view():
    now = int(time.time())
    day = 86400
    with _db() as c:
        def grouped(sql, params=()):
            return {r[0]: r[1] for r in c.execute(sql, params).fetchall()}

        last = grouped("SELECT account, MAX(ts) FROM events GROUP BY account")
        scans = grouped(
            "SELECT account, COUNT(*) FROM events WHERE event='scan' "
            "AND ts>=? GROUP BY account", (now - day,))
        learns = grouped(
            "SELECT account, COUNT(*) FROM events WHERE event LIKE 'learn_%' "
            "AND ts>=? GROUP BY account", (now - day,))
        spam_total = grouped(
            "SELECT account, COUNT(*) FROM events WHERE event='learn_spam' "
            "GROUP BY account")
        ham_total = grouped(
            "SELECT account, COUNT(*) FROM events WHERE event='learn_ham' "
            "GROUP BY account")
        fails = grouped(
            "SELECT account, COUNT(*) FROM events WHERE event='scan_failed' "
            "AND ts>=? GROUP BY account", (now - day,))
        safe: dict[str, list[str]] = {}
        for r in c.execute("SELECT account, scope FROM safe_mode"):
            safe.setdefault(r[0], []).append(r[1])

    admin, accts = _current_scope()
    names = sorted(set(last) | set(safe))
    if not admin:
        names = [n for n in names if n in accts]
    body_rows = "".join(
        f"<tr><td>{_h(n)}</td>"
        f'<td>{_fmt_ts(last.get(n))}</td>'
        f'<td class="muted">{_ago(last.get(n))}</td>'
        f'<td class="num">{scans.get(n, 0)}</td>'
        f'<td class="num">{learns.get(n, 0)}</td>'
        f'<td class="num">{spam_total.get(n, 0)}</td>'
        f'<td class="num">{ham_total.get(n, 0)}</td>'
        f'<td class="num">{_fail_cell(fails.get(n, 0))}</td>'
        f'<td>{_h(",".join(safe.get(n, [])) or "-")}</td></tr>'
        for n in names)
    body = (
        '<div class="card"><div class="tw"><table>'
        "<tr><th>Account</th><th>Last activity</th><th>Age</th>"
        "<th class=num>Scans 24h</th><th class=num>Learns 24h</th>"
        "<th class=num>Spam total</th><th class=num>Ham total</th>"
        "<th class=num>Scan fails 24h</th><th>Safe-mode</th></tr>"
        + (body_rows
           or '<tr><td colspan=9 class=muted>(no accounts seen yet)</td></tr>')
        + "</table></div></div>")
    return render("Accounts", "accounts", body)


# ----- entrypoint -----------------------------------------------------------


def start() -> None:
    """Spin up the dashboard. Caller decides whether to invoke (see
    filter.py main()); we refuse only if no users are configured."""
    users = _load_users()
    if not users:
        logging.getLogger("dashboard").error(
            "no dashboard users configured (add to %s, or set "
            "DASHBOARD_USERS / DASHBOARD_USER+DASHBOARD_PASSWORD); "
            "refusing to start", USERS_FILE
        )
        return
    logging.getLogger("dashboard").info(
        "starting on 0.0.0.0:%d (%d user(s): %s)",
        DASHBOARD_PORT, len(users), ", ".join(sorted(users)),
    )

    def _serve():
        try:
            serve(app, host="0.0.0.0", port=DASHBOARD_PORT, threads=4)
        except Exception as ex:  # noqa: BLE001
            logging.getLogger("dashboard").error("crashed: %s", ex)

    threading.Thread(target=_serve, name="dashboard", daemon=True).start()


def _known_accounts() -> set[str] | None:
    """Account names declared in accounts.yml, or None if the file
    cannot be read or parsed. Used by the user-add helper to list and
    validate scopes so a typo'd account name is caught immediately."""
    try:
        import yaml

        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        names = {
            str(a["name"]).strip()
            for a in (raw.get("accounts") or [])
            if isinstance(a, dict) and a.get("name")
        }
        return names or None
    except Exception:  # noqa: BLE001 - any failure: skip validation
        return None


if __name__ == "__main__":
    # Interactive helper: add (or update) a dashboard user. Writes the
    # state/dashboard_users file directly when it can; otherwise prints
    # a line for the DASHBOARD_USERS env var.
    import getpass

    name = input("username: ").strip()
    if not name or ":" in name or "," in name or name.startswith("#"):
        raise SystemExit(
            "username must be non-empty with no ':' ',' or leading '#'")
    pw1 = getpass.getpass("password: ")
    pw2 = getpass.getpass("repeat:   ")
    if pw1 != pw2:
        raise SystemExit("passwords do not match")
    if not pw1:
        raise SystemExit("password must not be empty")
    known = _known_accounts()
    if known:
        print("known accounts:", ", ".join(sorted(known)))
    raw_scope = input(
        "access scope - 'admin' for everything, or the account name(s) "
        "this user may see (comma-separated) [admin]: ").strip() or "admin"
    if raw_scope.lower() == "admin":
        scope = "admin"
    else:
        wanted = [a.strip() for a in re.split(r"[|,]", raw_scope) if a.strip()]
        if known:
            unknown = sorted(a for a in wanted if a not in known)
            if unknown:
                raise SystemExit(
                    f"unknown account(s): {', '.join(unknown)} - not in "
                    f"{CONFIG_PATH}. Known: {', '.join(sorted(known))}")
        # Normalise to pipe-separated; pipe is the on-disk separator so
        # the value survives the comma-delimited DASHBOARD_USERS env too.
        scope = "|".join(wanted) or "admin"
    entry = f"{name}:{_hash_password(pw1)}:{scope}"
    try:
        lines = []
        if USERS_FILE.exists():
            for ln in USERS_FILE.read_text().splitlines():
                s = ln.strip()
                if (s and not s.startswith("#")
                        and s.split(":", 1)[0].strip() == name):
                    continue  # replace any existing entry for this user
                lines.append(ln)
        lines.append(entry)
        USERS_FILE.write_text("\n".join(lines) + "\n")
        USERS_FILE.chmod(0o600)
        print(f"\nsaved user '{name}' to {USERS_FILE}")
        print("no restart needed - the dashboard re-reads it on each login.")
    except OSError as ex:
        print(f"\ncould not write {USERS_FILE} ({ex}).")
        print("add this line to the DASHBOARD_USERS env var instead:\n")
        print(entry)
