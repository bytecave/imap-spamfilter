"""Read-only web dashboard for the imap-spamfilter.

Disabled by default. Set both DASHBOARD_USER and DASHBOARD_PASSWORD to
enable it (basic auth is mandatory). Listens on a fixed internal port
8080; the orchestrator maps a host port. Reads the SQLite state DB
read-only and queries rspamd /stat for Bayes counts. Intended for
LAN-only access behind a reverse proxy if TLS is wanted.
"""

from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, request, render_template_string, send_file
from markupsafe import escape
from waitress import serve

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
DB_PATH = STATE_DIR / "spamfilter.db"
# Project logo, shipped into the image next to this module (see Dockerfile).
FAVICON_PATH = Path(__file__).with_name("favicon.png")
RSPAMD_CONTROLLER_URL = os.environ.get(
    "RSPAMD_LEARN_URL", "http://spamfilter-rspamd:11334"
)

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
# Mirrors min_learns in rspamd/local.d/classifier-bayes.conf. A Bayes
# class scores nothing until it reaches this many learns.
BAYES_MIN_LEARNS = int(os.environ.get("BAYES_MIN_LEARNS", "200"))
# Container-internal listen port is fixed. The orchestrator decides
# what host port to map it to (e.g. 38080:8080 in compose / Unraid).
DASHBOARD_PORT = 8080

log = logging.getLogger("dashboard")
app = Flask(__name__)


# ----- helpers --------------------------------------------------------------


def _db() -> sqlite3.Connection:
    """Open a fresh read-only SQLite connection for this request."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _requires_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        # Constant-time compare on both fields, evaluated unconditionally,
        # so response timing does not leak how much of the credential
        # matched.
        user_ok = hmac.compare_digest((auth.username or "") if auth else "", DASHBOARD_USER)
        pass_ok = hmac.compare_digest((auth.password or "") if auth else "", DASHBOARD_PASSWORD)
        if not (auth and user_ok and pass_ok):
            return Response(
                "Authentication required.\n",
                401,
                {"WWW-Authenticate": 'Basic realm="spamfilter"'},
            )
        return view(*args, **kwargs)

    return wrapped


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


def _fmt_score(s):
    if s is None:
        return "-"
    try:
        f = float(s)
    except (TypeError, ValueError):
        return str(s)
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


# ----- error handling -------------------------------------------------------


@app.errorhandler(sqlite3.Error)
def _on_db_error(ex: sqlite3.Error) -> Response:
    """A locked DB, a missing table, or a mid-read failure should render a
    plain 503 instead of leaking a stack trace through Flask's default
    handler."""
    log.error("dashboard DB error: %s", ex)
    return Response(
        "Dashboard temporarily unavailable (state DB error).\n",
        503,
        mimetype="text/plain",
    )


# ----- templates ------------------------------------------------------------


BASE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{{ title }} - spamfilter</title>
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       margin: 0; background: #f4f4f4; color: #222; }
nav { background: #2b2b2b; color: #eee; padding: 0.6em 1em; }
nav a { color: #9cf; text-decoration: none; margin-right: 1em; font-weight: 500; }
nav a.active { color: #fff; }
main { padding: 1em 1.5em; max-width: 1400px; margin: 0 auto; }
h1 { font-size: 1.4em; margin: 0.2em 0 0.8em 0; }
h2 { font-size: 1.1em; margin: 1.2em 0 0.4em 0; color: #555; }
table { border-collapse: collapse; width: 100%; background: #fff;
        box-shadow: 0 1px 2px rgba(0,0,0,0.06); margin-bottom: 1.5em; }
th, td { padding: 5px 8px; border-bottom: 1px solid #eee; text-align: left;
         font-size: 12.5px; vertical-align: top; }
th { background: #ececec; font-weight: 600; }
tr:hover td { background: #fcfcfc; }
.kpi-row { display: flex; flex-wrap: wrap; gap: 0.8em; margin-bottom: 1em; }
.kpi { background: #fff; border: 1px solid #ddd; border-radius: 4px;
       padding: 0.6em 1em; min-width: 110px; flex: 0 0 auto; }
.kpi .label { font-size: 0.78em; color: #777; text-transform: uppercase; }
.kpi .value { font-size: 1.5em; font-weight: 600; color: #222; }
.kpi.warn .value { color: #c80; }
.kpi.bad .value { color: #c33; }
.score-high { color: #c33; font-weight: 600; }
.score-mid { color: #b87f00; }
.muted { color: #888; }
.subj { max-width: 36em; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; display: block; }
.pillok { background: #d8f5d8; color: #176317; padding: 1px 6px;
          border-radius: 8px; font-size: 0.85em; }
.pillbad { background: #fbd6d6; color: #8a1a1a; padding: 1px 6px;
           border-radius: 8px; font-size: 0.85em; }
.pillmid { background: #fff0c0; color: #7a5a00; padding: 1px 6px;
           border-radius: 8px; font-size: 0.85em; }
footer { font-size: 0.8em; color: #888; padding: 1em 1.5em; }
</style>
</head><body>
<nav>
  <a href="/" {% if active=='summary' %}class="active"{% endif %}>Summary</a>
  <a href="/messages" {% if active=='messages' %}class="active"{% endif %}>Messages</a>
  <a href="/learned" {% if active=='learned' %}class="active"{% endif %}>Learned</a>
  <a href="/events" {% if active=='events' %}class="active"{% endif %}>Events</a>
  <a href="/accounts" {% if active=='accounts' %}class="active"{% endif %}>Accounts</a>
</nav>
<main>
<h1>{{ title }}</h1>
{{ body|safe }}
</main>
<footer>imap-spamfilter dashboard, read-only.</footer>
</body></html>
"""


def render(title, active, body_html):
    return render_template_string(
        BASE, title=title, active=active, body=body_html
    )


# ----- routes ---------------------------------------------------------------


@app.route("/favicon.png")
@app.route("/favicon.ico")
def favicon():
    """Serve the project logo as the favicon. No auth - it is only the
    logo, and browsers request favicons without credentials."""
    return send_file(FAVICON_PATH, mimetype="image/png", max_age=86400)


@app.route("/")
@_requires_auth
def summary():
    now = int(time.time())
    day = 86400
    with _db() as c:
        scanned_24h = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='scan' AND ts>=?",
            (now - day,),
        ).fetchone()[0]
        scanned_7d = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='scan' AND ts>=?",
            (now - 7 * day,),
        ).fetchone()[0]
        moved_24h = c.execute(
            "SELECT COUNT(*) FROM events WHERE event LIKE 'moved%' AND ts>=?",
            (now - day,),
        ).fetchone()[0]
        learn_spam_24h = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='learn_spam' AND ts>=?",
            (now - day,),
        ).fetchone()[0]
        learn_ham_24h = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='learn_ham' AND ts>=?",
            (now - day,),
        ).fetchone()[0]
        learn_spam_total = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='learn_spam'"
        ).fetchone()[0]
        learn_ham_total = c.execute(
            "SELECT COUNT(*) FROM events WHERE event='learn_ham'"
        ).fetchone()[0]
        safe_modes = c.execute("SELECT account, scope, reason FROM safe_mode").fetchall()
        last_learns = c.execute(
            "SELECT ts, account, event, message_id, detail FROM events "
            "WHERE event IN ('learn_spam','learn_ham') ORDER BY ts DESC LIMIT 15"
        ).fetchall()

    stats = _rspamd_stats()
    rspamd_block = ""
    bayes_block = ""
    if stats:
        actions = stats.get("actions") or {}
        uptime_s = stats.get("uptime") or 0
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        uptime_str = f"{days}d {hours}h" if days else f"{hours}h"
        # /stat reports fuzzy_hashes as a {storage: count} map; older
        # builds return a plain int. Sum the map, pass an int through.
        fuzzy = stats.get("fuzzy_hashes")
        if isinstance(fuzzy, dict):
            fuzzy_total = sum(v for v in fuzzy.values() if isinstance(v, (int, float)))
        else:
            fuzzy_total = fuzzy if fuzzy is not None else "?"
        rspamd_block = f"""
<h2>rspamd lifetime totals (since rspamd start, uptime {uptime_str})</h2>
<div class="kpi-row">
  <div class="kpi"><div class="label">Scanned</div><div class="value">{stats.get('scanned', '?')}</div></div>
  <div class="kpi"><div class="label">Identified spam</div><div class="value">{stats.get('spam_count', '?')}</div></div>
  <div class="kpi"><div class="label">Identified ham</div><div class="value">{stats.get('ham_count', '?')}</div></div>
  <div class="kpi"><div class="label">Total learns</div><div class="value">{stats.get('total_learns', '?')}</div></div>
  <div class="kpi"><div class="label">Fuzzy hashes</div><div class="value">{fuzzy_total}</div></div>
  <div class="kpi"><div class="label">Connections</div><div class="value">{stats.get('connections', '?')}</div></div>
  <div class="kpi"><div class="label">Control conns</div><div class="value">{stats.get('control_connections', '?')}</div></div>
  <div class="kpi"><div class="label">Reject</div><div class="value">{actions.get('reject', 0)}</div></div>
  <div class="kpi"><div class="label">Add header</div><div class="value">{actions.get('add header', 0)}</div></div>
  <div class="kpi"><div class="label">Greylist</div><div class="value">{actions.get('greylist', 0)}</div></div>
  <div class="kpi"><div class="label">No action</div><div class="value">{actions.get('no action', 0)}</div></div>
</div>"""
        statfiles = (stats.get("statfiles") or []) if isinstance(stats, dict) else []
        rows = []
        learns_by_symbol = {}
        for sf in statfiles:
            # rspamd /stat field names vary by version: "revision" or
            # "learns" or "total" all crop up in the wild. Try them in
            # priority order; show 0 if none present.
            try:
                learns = int(
                    sf.get("revision")
                    or sf.get("learns")
                    or sf.get("total")
                    or 0
                )
            except (TypeError, ValueError):
                learns = 0
            symbol = sf.get("symbol", "?")
            learns_by_symbol[symbol] = learns
            if learns >= BAYES_MIN_LEARNS:
                status = '<span class="pillok">active</span>'
            else:
                status = (
                    f'<span class="pillmid">needs '
                    f'{BAYES_MIN_LEARNS - learns} more</span>'
                )
            rows.append(
                f"<tr><td>{_h(symbol)}</td>"
                f"<td>{_h(sf.get('users','?'))}</td>"
                f"<td>{learns}</td>"
                f"<td>{status}</td></tr>"
            )
        # Heavy spam/ham learn imbalance biases the classifier; flag it.
        spam_l = learns_by_symbol.get("BAYES_SPAM", 0)
        ham_l = learns_by_symbol.get("BAYES_HAM", 0)
        balance_note = ""
        if spam_l and ham_l:
            lo, hi = sorted((spam_l, ham_l))
            ratio = hi / lo
            skewed = ratio >= 3
            cls = "pillmid" if skewed else "pillok"
            direction = "ham-skewed" if ham_l > spam_l else "spam-skewed"
            label = f"{direction} {ratio:.1f}:1" if skewed else f"balanced {ratio:.1f}:1"
            balance_note = f'<p>Learn balance: <span class="{cls}">{label}</span>'
            if skewed:
                balance_note += (
                    " &mdash; heavy skew biases the classifier; "
                    "feed more of the lighter class"
                )
            balance_note += "</p>"
        bayes_block = (
            "<h2>rspamd Bayes</h2>"
            "<table><tr><th>Symbol</th><th>Users</th><th>Total learns</th>"
            f"<th>Status (min {BAYES_MIN_LEARNS})</th></tr>"
            + "".join(rows)
            + "</table>"
            + balance_note
        )

    safe_block = ""
    if safe_modes:
        rows = "".join(
            f"<tr><td>{_h(r['account'])}</td><td>{_h(r['scope'])}</td>"
            f"<td>{_h(r['reason'])}</td></tr>"
            for r in safe_modes
        )
        safe_block = (
            "<h2>Active safe-mode</h2>"
            "<table><tr><th>Account</th><th>Scope</th><th>Reason</th></tr>"
            + rows
            + "</table>"
        )
    else:
        safe_block = '<h2>Safe-mode</h2><p class="muted">No active safe-mode entries.</p>'

    learns_rows = "".join(
        f"<tr><td>{_fmt_ts(r['ts'])}</td><td>{_h(r['account'])}</td>"
        f"<td>{'<span class=pillbad>spam</span>' if r['event']=='learn_spam' else '<span class=pillok>ham</span>'}</td>"
        f"<td><code>{_h((r['message_id'] or '')[:60])}</code></td>"
        f"<td class=muted>{_h(r['detail'])}</td></tr>"
        for r in last_learns
    )
    learns_block = (
        "<h2>Recent learns (last 15)</h2>"
        "<table><tr><th>When</th><th>Account</th><th>Kind</th>"
        "<th>Message-Id</th><th>Reason</th></tr>"
        + (learns_rows or '<tr><td colspan=5 class=muted>(none yet)</td></tr>')
        + "</table>"
    )

    body = f"""
<div class="kpi-row">
  <div class="kpi"><div class="label">Scanned 24h</div><div class="value">{scanned_24h}</div></div>
  <div class="kpi"><div class="label">Scanned 7d</div><div class="value">{scanned_7d}</div></div>
  <div class="kpi"><div class="label">Moved 24h</div><div class="value">{moved_24h}</div></div>
  <div class="kpi"><div class="label">Spam learns 24h</div><div class="value">{learn_spam_24h}</div></div>
  <div class="kpi"><div class="label">Ham learns 24h</div><div class="value">{learn_ham_24h}</div></div>
  <div class="kpi"><div class="label">Spam learns total</div><div class="value">{learn_spam_total}</div></div>
  <div class="kpi"><div class="label">Ham learns total</div><div class="value">{learn_ham_total}</div></div>
</div>
{rspamd_block}
{bayes_block}
{safe_block}
{learns_block}
"""
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
    with _db() as c:
        rows = c.execute(
            f"""
            SELECT account, message_id, last_seen, our_score, our_action,
                   current_folder, sender, subject, learned_as
              FROM messages
             WHERE our_score IS NOT NULL {where}
             ORDER BY last_seen DESC LIMIT 200
            """,
        ).fetchall()
    body_rows = "".join(
        f'<tr><td>{_fmt_ts(r["last_seen"])}</td>'
        f'<td>{_h(r["account"])}</td>'
        f'<td class="{_score_class(r["our_score"])}">{_fmt_score(r["our_score"])}</td>'
        f'<td>{_h(r["our_action"] or "-")}</td>'
        f'<td>{_h(r["current_folder"] or "-")}</td>'
        f'<td>{_h(r["learned_as"] or "-")}</td>'
        f'<td><span class="muted">{_h((r["sender"] or "")[:50])}</span></td>'
        f'<td><span class="subj">{_h(r["subject"])}</span></td>'
        "</tr>"
        for r in rows
    )
    body = f"""
<p><a href="?band=all">all</a> |
   <a href="?band=spam">score >= 8</a> |
   <a href="?band=mid">score 4-8</a> |
   <a href="?band=low">score < 4</a></p>
<table>
<tr><th>When</th><th>Account</th><th>Score</th><th>Action</th>
    <th>Folder</th><th>Learn</th><th>Sender</th><th>Subject</th></tr>
{body_rows or '<tr><td colspan=8 class=muted>(no scored messages yet)</td></tr>'}
</table>
"""
    return render(f"Messages ({len(rows)} shown)", "messages", body)


@app.route("/learned")
@_requires_auth
def learned():
    with _db() as c:
        rows = c.execute(
            """
            SELECT ts, account, event, message_id, detail
              FROM events
             WHERE event IN ('learn_spam','learn_ham','learn_giveup','learn_failed')
             ORDER BY ts DESC LIMIT 300
            """
        ).fetchall()
    body_rows = "".join(
        f'<tr><td>{_fmt_ts(r["ts"])}</td>'
        f'<td>{_h(r["account"])}</td>'
        f'<td>{("<span class=pillbad>spam</span>" if r["event"]=="learn_spam" else "<span class=pillok>ham</span>" if r["event"]=="learn_ham" else "<span class=pillmid>"+_h(r["event"])+"</span>")}</td>'
        f'<td><code>{_h((r["message_id"] or "")[:80])}</code></td>'
        f'<td class="muted">{_h(r["detail"])}</td></tr>'
        for r in rows
    )
    body = f"""
<table>
<tr><th>When</th><th>Account</th><th>Event</th><th>Message-Id</th><th>Detail</th></tr>
{body_rows or '<tr><td colspan=5 class=muted>(no learn events yet)</td></tr>'}
</table>
"""
    return render(f"Learn events ({len(rows)} shown)", "learned", body)


@app.route("/events")
@_requires_auth
def events():
    with _db() as c:
        rows = c.execute(
            """
            SELECT ts, account, event, message_id, detail
              FROM events
             ORDER BY ts DESC LIMIT 300
            """
        ).fetchall()
    body_rows = "".join(
        f'<tr><td>{_fmt_ts(r["ts"])}</td>'
        f'<td>{_h(r["account"])}</td>'
        f'<td>{_h(r["event"])}</td>'
        f'<td><code>{_h((r["message_id"] or "")[:80])}</code></td>'
        f'<td class="muted">{_h(r["detail"])}</td></tr>'
        for r in rows
    )
    body = f"""
<table>
<tr><th>When</th><th>Account</th><th>Event</th><th>Message-Id</th><th>Detail</th></tr>
{body_rows or '<tr><td colspan=5 class=muted>(no events yet)</td></tr>'}
</table>
"""
    return render(f"All events ({len(rows)} shown)", "events", body)


@app.route("/accounts")
@_requires_auth
def accounts_view():
    now = int(time.time())
    day = 86400
    with _db() as c:
        accs = c.execute("SELECT DISTINCT account FROM events").fetchall()
        rows = []
        for a in accs:
            name = a["account"]
            last = c.execute(
                "SELECT MAX(ts) FROM events WHERE account=?", (name,)
            ).fetchone()[0]
            scans_24h = c.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND event='scan' AND ts>=?",
                (name, now - day),
            ).fetchone()[0]
            learns_24h = c.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND event LIKE 'learn_%' AND ts>=?",
                (name, now - day),
            ).fetchone()[0]
            spam_total = c.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND event='learn_spam'",
                (name,),
            ).fetchone()[0]
            ham_total = c.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND event='learn_ham'",
                (name,),
            ).fetchone()[0]
            failed = c.execute(
                "SELECT COUNT(*) FROM events WHERE account=? AND event='scan_failed' AND ts>=?",
                (name, now - day),
            ).fetchone()[0]
            safe = c.execute(
                "SELECT scope FROM safe_mode WHERE account=?", (name,)
            ).fetchall()
            safe_str = ",".join(s["scope"] for s in safe) or "-"
            rows.append(
                (name, last, scans_24h, learns_24h, spam_total, ham_total, failed, safe_str)
            )
    body_rows = "".join(
        f'<tr><td>{_h(n)}</td>'
        f'<td>{_fmt_ts(l)}</td>'
        f'<td>{s}</td>'
        f'<td>{lr}</td>'
        f'<td>{st}</td>'
        f'<td>{ht}</td>'
        f'<td class="{"pillbad" if f else ""}">{f}</td>'
        f'<td>{_h(sm)}</td></tr>'
        for (n, l, s, lr, st, ht, f, sm) in rows
    )
    body = f"""
<table>
<tr><th>Account</th><th>Last activity</th><th>Scans 24h</th>
    <th>Learns 24h</th><th>Spam learns total</th><th>Ham learns total</th>
    <th>Scan fails 24h</th><th>Safe-mode</th></tr>
{body_rows or '<tr><td colspan=8 class=muted>(no accounts seen yet)</td></tr>'}
</table>
"""
    return render("Accounts", "accounts", body)


# ----- entrypoint -----------------------------------------------------------


def start() -> None:
    """Spin up the dashboard. Caller decides whether to invoke (see
    filter.py main()); we only refuse if basic auth credentials are
    missing."""
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        logging.getLogger("dashboard").error(
            "DASHBOARD_USER/DASHBOARD_PASSWORD missing; refusing to start "
            "dashboard without basic auth"
        )
        return
    logging.getLogger("dashboard").info(
        "starting on 0.0.0.0:%d (basic auth as %r)", DASHBOARD_PORT, DASHBOARD_USER
    )

    def _serve():
        try:
            serve(app, host="0.0.0.0", port=DASHBOARD_PORT, threads=4)
        except Exception as ex:  # noqa: BLE001
            logging.getLogger("dashboard").error("crashed: %s", ex)

    threading.Thread(target=_serve, name="dashboard", daemon=True).start()
