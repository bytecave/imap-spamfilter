"""Read-only web dashboard for the imap-spamfilter.

Disabled by default. Enable it by configuring at least one dashboard
user (see below); the daemon then serves it on a fixed internal port
8080 and the orchestrator maps a host port. Reads the SQLite state DB
read-only and queries rspamd /stat. Intended for LAN access behind a
reverse proxy that terminates TLS.

Authentication uses a signed client-side session with a real login form.
Each session is bound to the active credential verifier, so password
changes revoke existing sessions while account-scope edits apply live.
Configure users one of two ways:

  * DASHBOARD_USERS - comma-separated `name:hash:scope` records, where hash is
    produced by `python dashboard.py` (an interactive hashing helper) and
    scope is `admin` or a pipe-separated account list.
    Preferred: supports multiple named accounts with per-user passwords.
  * DASHBOARD_USER + DASHBOARD_PASSWORD - a single legacy plaintext
    account. Still accepted so existing deployments keep working.

The signed-session secret is generated once into STATE_DIR and reused
across restarts so logins survive a redeploy.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
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

from filter import decode_rfc2047

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
SESSION_IDLE_S = 8 * 3600
SESSION_ABS_S = 24 * 3600
LOGIN_FAIL_LIMIT = 5
LOGIN_LOCKOUT_S = 60.0
LOGIN_IP_FAIL_LIMIT = 25
LOGIN_USER_FAIL_LIMIT = 10
LOGIN_GLOBAL_FAIL_LIMIT = 200
LOGIN_FAIL_STATE_MAX = 2048
LOGIN_USERNAME_MAX = 128
LOGIN_PASSWORD_MAX = 1024
LOGIN_REQUEST_MAX = 16 * 1024
_NEXT_OK = re.compile(r"^/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?$")
_DUMMY_VERIFIER = (
    f"pbkdf2${PBKDF2_ITERATIONS}${'00' * 16}${'00' * 32}"
)

log = logging.getLogger("dashboard")
app = Flask(__name__)
_login_lock = threading.Lock()
_login_fails: dict[tuple[str, str], list[float]] = {}


def _parse_proxy_networks(raw: str) -> tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            log.warning("invalid DASHBOARD_TRUSTED_PROXIES entry ignored: %s", value)
    return tuple(networks)


# Forwarding headers are ignored unless the direct peer is loopback or is
# explicitly configured. This keeps direct Netbird/WireGuard clients from
# spoofing throttle identities while supporting a local reverse proxy.
_TRUSTED_PROXY_NETWORKS = _parse_proxy_networks(
    os.environ.get("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1/32,::1/128")
)


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


def _write_private(path: Path, text: str) -> None:
    """Create or replace `path` with mode 0600 from the first byte.

    chmod-after-write leaves a world-readable window; open with 0o600.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _safe_next(dest: str | None) -> str:
    if dest and _NEXT_OK.fullmatch(dest):
        return dest
    return "/"


@dataclass(frozen=True)
class _User:
    name: str
    verifier: str                 # pbkdf2 hash, or 'plain:<pw>' (legacy env)
    admin: bool                   # admin sees every account
    accounts: frozenset[str]      # account names a non-admin may see


def _parse_user_line(raw: str, users: dict[str, "_User"]) -> None:
    """Parse one `username:verifier:scope` record. `scope` is 'admin'
    or a pipe/comma-separated list of account names. Missing scope is
    fail-closed (line skipped) — do not default to admin.
    The verifier is a pbkdf2 hash, which contains no ':'."""
    parts = raw.split(":")
    if len(parts) < 3:
        log.warning("dashboard user line ignored (missing scope): %s", raw.split(":", 1)[0])
        return
    name = parts[0].strip()
    verifier = parts[1].strip()
    scope = parts[2].strip()
    if not name or not verifier or not scope:
        log.warning("dashboard user line ignored (empty field): %s", name or "?")
        return
    if len(name) > LOGIN_USERNAME_MAX:
        log.warning(
            "dashboard user line ignored (username exceeds %d characters)",
            LOGIN_USERNAME_MAX,
        )
        return
    admin = scope.lower() == "admin"
    accounts = frozenset() if admin else frozenset(
        a.strip() for a in re.split(r"[|,]", scope) if a.strip())
    if not admin and not accounts:
        log.warning("dashboard user line ignored (empty account scope): %s", name)
        return
    users[name] = _User(name, verifier, admin, accounts)


def _load_users() -> dict[str, "_User"]:
    """Map username -> _User. Sources, lowest precedence first:
      1. state/dashboard_users - `username:hash:scope` per line,
         `#` comments and blank lines ignored;
      2. the DASHBOARD_USERS env var - comma-separated entries;
      3. the legacy DASHBOARD_USER + DASHBOARD_PASSWORD pair (admin).
    `scope` is 'admin' or a pipe-separated list of account names a
    non-admin user may see. A line with no scope is ignored. Re-read
    on every login so edits apply without a restart."""
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
        if (
            len(legacy_user) > LOGIN_USERNAME_MAX
            or len(legacy_pass) > LOGIN_PASSWORD_MAX
        ):
            log.warning(
                "legacy dashboard user ignored (credential exceeds login limits)"
            )
        else:
            users[legacy_user] = _User(
                legacy_user, "plain:" + legacy_pass, True, frozenset())
    return users


def _spend_auth_cost(password: str) -> None:
    """Perform the normal KDF work without accepting a credential."""
    candidate = password if len(password) <= LOGIN_PASSWORD_MAX else ""
    _verify_pbkdf2(_DUMMY_VERIFIER, candidate)


def _check_login(username: str, password: str) -> _User | None:
    u = _load_users().get(username)
    if u is None:
        # Spend comparable effort on an unknown user so response timing
        # does not reveal which usernames exist.
        _spend_auth_cost(password)
        return None
    if u.verifier.startswith("plain:"):
        # Preserve the legacy environment-variable account, but make it pay
        # the same PBKDF2 cost as hashed and unknown users.
        _spend_auth_cost(password)
        valid = hmac.compare_digest(
            u.verifier[len("plain:"):].encode(), password.encode())
    else:
        valid = _verify_pbkdf2(u.verifier, password)
    return u if valid else None


def _credential_version(user: _User) -> str:
    """Keyed verifier fingerprint stored in the signed session cookie.

    Keying is important for legacy plaintext verifiers: an unkeyed digest in
    a client-readable cookie would itself become an offline password oracle.
    """
    key = str(app.secret_key).encode()
    return hmac.new(key, user.verifier.encode(), hashlib.sha256).hexdigest()


def _current_scope() -> tuple[bool, frozenset[str]]:
    """(is_admin, allowed_account_names) for the logged-in user. An
    unknown session yields no access."""
    u = _load_users().get(session.get("user", ""))
    if u is None:
        return (False, frozenset())
    return (u.admin, u.accounts)


def _scope_clause(prefix: str = "AND", column: str = "account") -> tuple[str, list]:
    """SQL fragment + params restricting the `account` column to the
    current user's scope. Empty string for admins; '<prefix> 1=0' for
    a non-admin with no accounts. `column` lets callers qualify the
    name (e.g. `e.account`) when the query aliases `events`."""
    admin, accts = _current_scope()
    if admin:
        return ("", [])
    if not accts:
        return (f" {prefix} 1=0", [])
    return (f" {prefix} {column} IN ({','.join('?' * len(accts))})",
            list(accts))


def _load_secret() -> str:
    """Persist a signing secret in STATE_DIR so sessions survive restarts.
    Falls back to an ephemeral secret if the dir is not writable."""
    try:
        if SECRET_PATH.is_file():
            existing = SECRET_PATH.read_text().strip()
            if existing:
                try:
                    os.chmod(SECRET_PATH, 0o600)
                except OSError:
                    pass
                return existing
        fresh = secrets.token_hex(32)
        _write_private(SECRET_PATH, fresh + "\n")
        return fresh
    except OSError as ex:
        logging.getLogger("dashboard").warning(
            "could not persist session secret (%s); using an ephemeral one "
            "- logins will drop on restart", ex
        )
        return secrets.token_hex(32)


app.secret_key = _load_secret()
app.config.update(
    MAX_CONTENT_LENGTH=LOGIN_REQUEST_MAX,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_IDLE_S),
    SESSION_REFRESH_ON_EACH_REQUEST=True,
    # TLS is terminated upstream; only mark the cookie Secure when the
    # operator confirms the proxy forwards HTTPS to this app.
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_COOKIE_SECURE", "")
    .lower() in ("1", "true", "yes"),
)


@app.after_request
def _security_headers(resp: Response) -> Response:
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
        "form-action 'self'; base-uri 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _client_ip() -> str:
    remote = request.remote_addr or "unknown"
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        return remote
    if not any(remote_ip in network for network in _TRUSTED_PROXY_NETWORKS):
        return str(remote_ip)

    forwarded = request.headers.get("X-Forwarded-For") or ""
    if not forwarded:
        return str(remote_ip)
    try:
        chain = [
            ipaddress.ip_address(value.strip())
            for value in forwarded.split(",")
            if value.strip()
        ]
    except ValueError:
        return str(remote_ip)
    # Walk from the trusted direct peer towards the client. This prevents a
    # client-supplied leftmost value from winning when a proxy appends XFF.
    for address in reversed(chain):
        if not any(address in network for network in _TRUSTED_PROXY_NETWORKS):
            return str(address)
    return str(chain[0]) if chain else str(remote_ip)


def _login_key(ip: str, username: str) -> tuple[str, str]:
    return (ip[:64], username.casefold()[:LOGIN_USERNAME_MAX])


def _prune_login_failures(now: float) -> None:
    """Globally expire old failures and enforce a hard bucket bound.

    Caller must hold _login_lock.
    """
    for key, recorded in list(_login_fails.items()):
        recent = [t for t in recorded if now - t < LOGIN_LOCKOUT_S]
        if recent:
            _login_fails[key] = recent[-LOGIN_FAIL_LIMIT:]
        else:
            _login_fails.pop(key, None)
    overflow = len(_login_fails) - LOGIN_FAIL_STATE_MAX
    if overflow > 0:
        oldest = sorted(_login_fails, key=lambda key: _login_fails[key][-1])
        for key in oldest[:overflow]:
            _login_fails.pop(key, None)


def _login_blocked(ip: str, username: str) -> bool:
    key = _login_key(ip, username)
    now = time.monotonic()
    with _login_lock:
        _prune_login_failures(now)
        pair_count = len(_login_fails.get(key, ()))
        ip_count = sum(
            len(times) for (seen_ip, _), times in _login_fails.items()
            if seen_ip == key[0]
        )
        user_count = sum(
            len(times) for (_, seen_user), times in _login_fails.items()
            if seen_user == key[1]
        )
        global_count = sum(len(times) for times in _login_fails.values())
        return (
            pair_count >= LOGIN_FAIL_LIMIT
            or ip_count >= LOGIN_IP_FAIL_LIMIT
            or user_count >= LOGIN_USER_FAIL_LIMIT
            or global_count >= LOGIN_GLOBAL_FAIL_LIMIT
        )


def _record_login_failure(ip: str, username: str) -> None:
    key = _login_key(ip, username)
    now = time.monotonic()
    with _login_lock:
        _prune_login_failures(now)
        if key not in _login_fails and len(_login_fails) >= LOGIN_FAIL_STATE_MAX:
            oldest = min(_login_fails, key=lambda item: _login_fails[item][-1])
            _login_fails.pop(oldest, None)
        times = _login_fails.get(key, [])
        _login_fails[key] = (times + [now])[-LOGIN_FAIL_LIMIT:]


def _clear_login_failures(ip: str, username: str) -> None:
    key = _login_key(ip, username)
    with _login_lock:
        # A successful proof of the credential clears the account-wide
        # failures too, including attempts made from other addresses. Keep
        # unrelated failures from this IP so one valid login cannot erase an
        # address-wide brute-force history.
        for recorded_key in list(_login_fails):
            if recorded_key[1] == key[1]:
                _login_fails.pop(recorded_key, None)


def _requires_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        username = session.get("user", "")
        if not username:
            return redirect(url_for("login", next=request.path))
        user = _load_users().get(username)
        actual_version = str(session.get("credential_version") or "")
        if (
            user is None
            or not actual_version
            or not hmac.compare_digest(
                actual_version, _credential_version(user))
        ):
            session.clear()
            return redirect(url_for("login", next=request.path))
        now = int(time.time())
        issued = int(session.get("issued") or 0)
        last = int(session.get("last") or 0)
        if (now - issued) > SESSION_ABS_S or (now - last) > SESSION_IDLE_S:
            session.clear()
            return redirect(url_for("login", next=request.path))
        session["last"] = now
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


# Correlated subject lookup: events only store message_id. Prefer the
# newest messages.subject for that account+Message-Id.
_EVENTS_WITH_SUBJECT = (
    "SELECT e.ts, e.account, e.event, e.detail, e.message_id, "
    "(SELECT m.subject FROM messages m "
    " WHERE m.account = e.account AND e.message_id IS NOT NULL "
    "   AND m.message_id = e.message_id AND IFNULL(m.subject,'') != '' "
    " ORDER BY m.last_seen DESC LIMIT 1) AS subject "
    "FROM events e "
)


def _fmt_learn_detail(detail) -> str:
    """Shorten train-folder reasons: train_ham_folder -> train_ham."""
    text = "" if detail is None else str(detail)
    if text.endswith("_folder"):
        return text[:-7]
    return text


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
        from filter import _load_rspamd_password
        password = _load_rspamd_password()
    except Exception:  # noqa: BLE001
        password = os.environ.get("RSPAMD_PASSWORD", "").strip()
    try:
        r = requests.get(
            f"{RSPAMD_CONTROLLER_URL}/stat",
            headers={"Password": password},
            timeout=3,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


def _kpi(label: str, value, sub: str = "", cls: str = "") -> str:
    safe_cls = "".join(c for c in cls if c.isalnum() or c in "-_")
    sub_html = f'<div class="sub">{_h(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi {safe_cls}"><div class="label">{_h(label)}</div>'
        f'<div class="value">{_h(value)}</div>{sub_html}</div>'
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
nav form.logout { margin:0; display:inline; }
nav form.logout button { color:var(--nav-fg); background:transparent; border:0;
  font:inherit; font-weight:500; padding:0.35em 0.7em; border-radius:6px;
  cursor:pointer; opacity:0.78; }
nav form.logout button:hover { opacity:1; background:rgba(255,255,255,0.08); }
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
  <form method="post" action="/logout" class="logout">
    <button type="submit">Log out</button>
  </form>{% endif %}
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
<input id="u" name="username" autocomplete="username" autofocus required
 maxlength=""" + str(LOGIN_USERNAME_MAX) + """>
<label for="p">Password</label>
<input id="p" name="password" type="password"
 autocomplete="current-password" required
 maxlength=""" + str(LOGIN_PASSWORD_MAX) + """>
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
        ip = _client_ip()
        locked = _login_blocked(ip, username)
        invalid_size = (
            not username
            or len(username) > LOGIN_USERNAME_MAX
            or not password
            or len(password) > LOGIN_PASSWORD_MAX
        )
        user = None
        if locked:
            # The pre-KDF gate must be cheap. Performing the dummy PBKDF2 here
            # would let locked requests occupy every Waitress worker.
            error = "Invalid username or password."
        elif invalid_size:
            _spend_auth_cost(password)
            error = "Invalid username or password."
        else:
            user = _check_login(username, password)
        if user is not None:
            _clear_login_failures(ip, username)
            session.clear()
            now = int(time.time())
            session["user"] = username
            session["credential_version"] = _credential_version(user)
            session["issued"] = now
            session["last"] = now
            session.permanent = True
            dest = _safe_next(request.args.get("next"))
            return redirect(dest)
        if not locked:
            _record_login_failure(ip, username)
        if not error:
            error = "Invalid username or password."
    if session.get("user"):
        return redirect("/")
    return render_template_string(LOGIN, error=error)


@app.route("/logout", methods=["POST"])
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
    sc_e, sp_e = _scope_clause("AND", "e.account")
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
            _EVENTS_WITH_SUBJECT
            + "WHERE e.event IN ('learn_spam','learn_ham')" + sc_e
            + " ORDER BY e.ts DESC LIMIT 15",
            sp_e).fetchall()
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
        f'<td><span class="subj">{_h(decode_rfc2047(r["subject"]) or "-")}</span></td>'
        f'<td class="muted">{_h(_fmt_learn_detail(r["detail"]))}</td></tr>'
        for r in last_learns)
    learns_block = (
        '<div class="card"><h2>Recent learns</h2><div class="tw">'
        "<table><tr><th>When</th><th>Account</th><th>Kind</th>"
        "<th>Subject</th><th>Reason</th></tr>"
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
    sc, sp = _scope_clause("AND", "m.account")
    with _db() as c:
        rows = c.execute(
            f"""
            SELECT m.account, m.message_id, m.last_seen, m.received_at, m.our_score,
                   m.our_action, m.current_folder, m.sender, m.subject,
                   COALESCE(
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
             WHERE m.our_score IS NOT NULL {where}{sc}
             ORDER BY COALESCE(m.received_at, m.last_seen) DESC LIMIT 200
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
        f'<td><span class="subj">{_h(decode_rfc2047(r["subject"]) or "-")}</span></td></tr>'
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
            f'<td><span class="subj">{_h(decode_rfc2047(r["subject"]) or "-")}</span></td>'
            f'<td class="muted">{_h(_fmt_learn_detail(r["detail"]))}</td></tr>')
    return "".join(out)


@app.route("/learned")
@_requires_auth
def learned():
    sc, sp = _scope_clause("AND", "e.account")
    with _db() as c:
        rows = c.execute(
            _EVENTS_WITH_SUBJECT
            + "WHERE e.event IN ('learn_spam','learn_ham','learn_giveup',"
            "'learn_failed')" + sc + " ORDER BY e.ts DESC LIMIT 300",
            sp).fetchall()
    body = (
        '<div class="card"><div class="tw"><table>'
        "<tr><th>When</th><th>Age</th><th>Account</th><th>Event</th>"
        "<th>Subject</th><th>Detail</th></tr>"
        + (_event_table(rows)
           or '<tr><td colspan=6 class=muted>(no learn events yet)</td></tr>')
        + "</table></div></div>")
    return render(f"Learn events ({len(rows)} shown)", "learned", body)


@app.route("/events")
@_requires_auth
def events():
    sc, sp = _scope_clause("WHERE", "e.account")
    with _db() as c:
        rows = c.execute(
            _EVENTS_WITH_SUBJECT + sc + " ORDER BY e.ts DESC LIMIT 300",
            sp).fetchall()
    body = (
        '<div class="card"><div class="tw"><table>'
        "<tr><th>When</th><th>Age</th><th>Account</th><th>Event</th>"
        "<th>Subject</th><th>Detail</th></tr>"
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
            serve(
                app,
                host="0.0.0.0",
                port=DASHBOARD_PORT,
                threads=4,
                max_request_body_size=LOGIN_REQUEST_MAX,
            )
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
    if len(name) > LOGIN_USERNAME_MAX:
        raise SystemExit(
            f"username must be at most {LOGIN_USERNAME_MAX} characters")
    pw1 = getpass.getpass("password: ")
    pw2 = getpass.getpass("repeat:   ")
    if pw1 != pw2:
        raise SystemExit("passwords do not match")
    if not pw1:
        raise SystemExit("password must not be empty")
    if len(pw1) > LOGIN_PASSWORD_MAX:
        raise SystemExit(
            f"password must be at most {LOGIN_PASSWORD_MAX} characters")
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
        _write_private(USERS_FILE, "\n".join(lines) + "\n")
        print(f"\nsaved user '{name}' to {USERS_FILE}")
        print("no restart needed - the dashboard re-reads it on each login.")
    except OSError as ex:
        print(f"\ncould not write {USERS_FILE} ({ex}).")
        print("add this line to the DASHBOARD_USERS env var instead:\n")
        print(entry)
