"""
console.py — Web admin console for OwlProxy Shop.
---------------------------------------------------------------
A password-protected web panel (separate from the Telegram bot's own
inline-button admin panel) that lets the owner manage everything from
a browser: stats, users, deposits, proxy/VPN/Premium catalog, settings
(support link, force-join, referral, payment gateways, deposit
numbers), editable UI texts, and broadcast.

Mounted into the main Flask app (see bot.py) as a Blueprint at /console.
Self-contained: only depends on storage.py and logic.py, never on
bot.py, so there is no circular import.

REQUIRED environment variables:
  CONSOLE_PASSWORD  — the login password. If unset, the console refuses
                       every login attempt (safer default than an open door).
  SESSION_SECRET    — signs the login session cookie. If unset, a random
                       one is generated at each process start, which logs
                       everyone out on every redeploy (still safe, just
                       inconvenient) — set a fixed random string to avoid that.
"""

import os
import functools
from datetime import datetime

from flask import Blueprint, request, session, redirect, url_for, Response

from storage import (
    db_read, db_update, log_event,
    stock_count, add_proxies, ensure_category_stock, delete_category_stock,
    unit_stock_count, unit_add_stock, unit_delete_stock,
    get_catalog, add_catalog_category, delete_catalog_category,
    add_catalog_product, delete_catalog_product, set_catalog_product_price,
    get_text, set_text, list_text_keys,
    list_emojis, add_emoji, delete_emoji,
    DB_USERS, DB_CATEGORIES, DB_SETTINGS, DB_DEPOSITS,
)
from logic import fmt, h, credit_deposit, process_manual_deposit, stats_summary, tg_send

CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")

console_bp = Blueprint("console", __name__, url_prefix="/console")


# =====================================================================
# Auth
# =====================================================================

def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not session.get("console_in"):
            return redirect(url_for("console.login"))
        return view(*a, **kw)
    return wrapped


@console_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if not CONSOLE_PASSWORD:
            error = "CONSOLE_PASSWORD এনভায়রনমেন্ট ভ্যারিয়েবল সেট করা নেই — অ্যাডমিন লগইন বন্ধ আছে।"
        elif request.form.get("password", "") == CONSOLE_PASSWORD:
            session["console_in"] = True
            return redirect(url_for("console.dashboard"))
        else:
            error = "ভুল পাসওয়ার্ড।"
    body = f"""
    <div class="login-box">
      <h1>🛠️ OwlProxy Console</h1>
      {'<p class="err">' + error + '</p>' if error else ''}
      <form method="post">
        <input type="password" name="password" placeholder="পাসওয়ার্ড" autofocus required>
        <button type="submit">লগইন</button>
      </form>
    </div>
    """
    return Response(_wrap("Login", body, bare=True), mimetype="text/html")


@console_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("console.login"))


# =====================================================================
# Layout helpers
# =====================================================================

NAV = [
    ("dashboard", "📊 Dashboard"),
    ("users", "👥 Users"),
    ("deposits", "⏳ Deposits"),
    ("proxy", "🌐 Proxy"),
    ("vpn", "🛡️ VPN"),
    ("premium", "💎 Premium"),
    ("settings", "⚙️ Settings"),
    ("texts", "📝 Texts"),
    ("emoji", "🎨 Emoji"),
    ("broadcast", "📢 Broadcast"),
]

CSS = """
<style>
  :root{--bg:#0f1115;--panel:#171a21;--border:#262a33;--text:#e8e8ea;--muted:#93989f;
        --accent:#5b8def;--good:#3ecf8e;--bad:#f2545b;--warn:#f2b705;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);}
  a{color:var(--accent);text-decoration:none;}
  .shell{display:flex;min-height:100vh;}
  nav{width:200px;background:var(--panel);border-right:1px solid var(--border);
      padding:20px 0;flex-shrink:0;}
  nav .brand{padding:0 20px 16px;font-weight:700;font-size:15px;border-bottom:1px solid var(--border);margin-bottom:8px;}
  nav a{display:block;padding:10px 20px;color:var(--muted);font-size:14px;}
  nav a.active{color:var(--text);background:#1e222b;border-right:2px solid var(--accent);}
  nav a:hover{color:var(--text);}
  main{flex:1;padding:28px 32px;max-width:1100px;}
  h1{font-size:20px;margin:0 0 20px;}
  h2{font-size:15px;color:var(--muted);margin:28px 0 10px;text-transform:uppercase;letter-spacing:.04em;}
  .cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:10px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;
        padding:16px 18px;min-width:150px;}
  .card .num{font-size:22px;font-weight:700;}
  .card .lbl{font-size:12px;color:var(--muted);margin-top:2px;}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);
        border-radius:10px;overflow:hidden;font-size:13px;}
  th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;}
  tr:last-child td{border-bottom:none;}
  .pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;}
  .pill.on{background:rgba(62,207,142,.15);color:var(--good);}
  .pill.off{background:rgba(242,84,91,.15);color:var(--bad);}
  form.inline{display:inline;}
  input,select,textarea{background:#0f1115;border:1px solid var(--border);color:var(--text);
       padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit;}
  textarea{width:100%;min-height:100px;}
  button, input[type=submit]{background:var(--accent);border:none;color:#fff;padding:7px 14px;
       border-radius:6px;font-size:13px;cursor:pointer;font-weight:600;}
  button.bad{background:var(--bad);}
  button.good{background:var(--good);}
  button.small{padding:4px 9px;font-size:12px;}
  .box{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;margin-bottom:16px;}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;}
  .muted{color:var(--muted);font-size:12px;}
  .flash{background:rgba(91,141,239,.15);border:1px solid var(--accent);color:var(--text);
       padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:13px;}
  .login-box{max-width:340px;margin:120px auto;background:var(--panel);border:1px solid var(--border);
       border-radius:12px;padding:32px;text-align:center;}
  .login-box input{width:100%;margin-bottom:12px;}
  .login-box button{width:100%;}
  .err{color:var(--bad);font-size:13px;}
  code{background:#0f1115;padding:1px 5px;border-radius:4px;font-size:12px;}
</style>
"""


def _wrap(title: str, body: str, active: str = "", bare: bool = False) -> str:
    if bare:
        return f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>{CSS}</head><body>{body}</body></html>"
    flash = session.pop("flash", None)
    nav_html = "".join(f'<a href="{url_for("console." + key)}" class="{"active" if key == active else ""}">{label}</a>' for key, label in NAV)
    nav_html += f'<a href="{url_for("console.logout")}" style="margin-top:16px;">🚪 Logout</a>'
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} · OwlProxy Console</title>{CSS}</head><body>
    <div class="shell">
      <nav><div class="brand">🛠️ OwlProxy</div>{nav_html}</div>
      <main>
        <h1>{title}</h1>
        {'<div class="flash">' + flash + '</div>' if flash else ''}
        {body}
      </main>
    </div>
    </body></html>"""


def _flash(msg: str):
    session["flash"] = msg


def _page(title, body, active):
    return Response(_wrap(title, body, active), mimetype="text/html")


# =====================================================================
# Dashboard
# =====================================================================

@console_bp.route("/")
@login_required
def index():
    return redirect(url_for("console.dashboard"))


@console_bp.route("/dashboard")
@login_required
def dashboard():
    s = stats_summary()
    cur = s["currency"]
    cards = f"""
    <div class="cards">
      <div class="card"><div class="num">{s['total_users']}</div><div class="lbl">Total Users</div></div>
      <div class="card"><div class="num">{s['new_today']}</div><div class="lbl">New Today</div></div>
      <div class="card"><div class="num">{s['active_today']}</div><div class="lbl">Active Today</div></div>
      <div class="card"><div class="num">{fmt(s['revenue'])} {cur}</div><div class="lbl">Total Revenue</div></div>
      <div class="card"><div class="num">{fmt(s['deposits_total'])} {cur}</div><div class="lbl">Total Deposits</div></div>
      <div class="card"><div class="num">{fmt(s['balance_held'])} {cur}</div><div class="lbl">User Balances Held</div></div>
      <div class="card"><div class="num">{s['pending_deposits']}</div><div class="lbl">Pending Deposits</div></div>
    </div>
    <p class="muted">⏳ Pending deposits need action — see the <a href="{url_for('console.deposits')}">Deposits</a> tab.</p>
    """
    return _page("Dashboard", cards, "dashboard")


# =====================================================================
# Users
# =====================================================================

@console_bp.route("/users")
@login_required
def users():
    q = request.args.get("q", "").strip().lower()
    all_users = db_read(DB_USERS)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    rows = ""
    for uid, u in all_users.items():
        if q and q not in uid.lower() and q not in str(u.get("username", "")).lower() and q not in str(u.get("tg_username", "")).lower():
            continue
        banned = u.get("banned")
        rows += f"""<tr>
          <td><code>{h(uid)}</code></td>
          <td>{h(u.get('username',''))}{(' @' + h(u['tg_username'])) if u.get('tg_username') else ''}</td>
          <td>{fmt(u.get('balance',0))} {cur}</td>
          <td>{fmt(u.get('total_spent',0))} {cur}</td>
          <td>{fmt(u.get('total_deposited',0))} {cur}</td>
          <td><span class="pill {'off' if banned else 'on'}">{'Banned' if banned else 'Active'}</span></td>
          <td>
            <form class="inline" method="post" action="{url_for('console.user_balance', uid=uid)}">
              <input type="number" step="0.01" name="amount" placeholder="±amount" style="width:90px" required>
              <button class="small" type="submit">Apply</button>
            </form>
            <form class="inline" method="post" action="{url_for('console.user_ban', uid=uid)}">
              <button class="small {'good' if banned else 'bad'}" type="submit">{'Unban' if banned else 'Ban'}</button>
            </form>
          </td>
        </tr>"""
    body = f"""
    <form method="get" class="row"><input name="q" value="{h(q)}" placeholder="Search id / name / @username"><button type="submit">Search</button></form>
    <table><tr><th>ID</th><th>Name</th><th>Balance</th><th>Spent</th><th>Deposited</th><th>Status</th><th>Actions</th></tr>
    {rows or '<tr><td colspan="7" class="muted">No users found.</td></tr>'}
    </table>
    """
    return _page("Users", body, "users")


@console_bp.route("/users/<uid>/balance", methods=["POST"])
@login_required
def user_balance(uid):
    try:
        delta = round(float(request.form.get("amount", "0")), 2)
    except ValueError:
        delta = 0
    if delta:
        def fn(u):
            if uid in u:
                u[uid]["balance"] = round(u[uid].get("balance", 0) + delta, 2)
        db_update(DB_USERS, fn)
        log_event("CONSOLE_BALANCE", f"user={uid} delta={delta}")
        u = db_read(DB_USERS).get(uid, {})
        cur = db_read(DB_SETTINGS).get("currency", "BDT")
        if delta > 0:
            tg_send(uid, f"💰 আপনার ব্যালেন্সে <b>{fmt(delta)} {cur}</b> যোগ করা হয়েছে।")
        _flash(f"Balance updated for {uid}.")
    return redirect(url_for("console.users"))


@console_bp.route("/users/<uid>/ban", methods=["POST"])
@login_required
def user_ban(uid):
    def fn(u):
        if uid in u:
            u[uid]["banned"] = not u[uid].get("banned", False)
    db_update(DB_USERS, fn)
    _flash(f"Toggled ban for {uid}.")
    return redirect(url_for("console.users"))


# =====================================================================
# Deposits
# =====================================================================

@console_bp.route("/deposits")
@login_required
def deposits():
    deps = db_read(DB_DEPOSITS)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    rows = ""
    for dep_id in reversed(list(deps.keys())):
        d = deps[dep_id]
        actions = (f"""<form class="inline" method="post" action="{url_for('console.deposit_action', dep_id=dep_id, action='approve')}"><button class="small good">Approve</button></form>
                      <form class="inline" method="post" action="{url_for('console.deposit_action', dep_id=dep_id, action='reject')}"><button class="small bad">Reject</button></form>"""
                   if d["status"] == "pending" else f"<span class='muted'>{d.get('handled_by','-')}</span>")
        rows += f"""<tr>
          <td>#{h(dep_id)}</td><td><code>{h(d['user'])}</code></td>
          <td>{fmt(d['amount'])} {h(d.get('currency', cur))}</td>
          <td>{h((d.get('method') or '-').upper())}</td>
          <td><code>{h(d.get('trx','-'))}</code></td>
          <td><span class="pill {'on' if d['status']=='approved' else ('off' if d['status']=='rejected' else '')}">{h(d['status'])}</span></td>
          <td>{h(d.get('created','-'))}</td>
          <td>{actions}</td>
        </tr>"""
    body = f"""<table><tr><th>ID</th><th>User</th><th>Amount</th><th>Method</th><th>TrxID</th><th>Status</th><th>Created</th><th>Actions</th></tr>
    {rows or '<tr><td colspan="8" class="muted">No deposits yet.</td></tr>'}</table>"""
    return _page("Deposits", body, "deposits")


@console_bp.route("/deposits/<dep_id>/<action>", methods=["POST"])
@login_required
def deposit_action(dep_id, action):
    r = process_manual_deposit(dep_id, action == "approve", "console")
    if r.get("dep"):
        _flash(f"Deposit #{dep_id} {'approved' if action == 'approve' else 'rejected'}.")
    else:
        _flash(f"Deposit #{dep_id} not found.")
    return redirect(url_for("console.deposits"))


# =====================================================================
# Proxy catalog (flat: category → qty)
# =====================================================================

@console_bp.route("/proxy")
@login_required
def proxy():
    cats = db_read(DB_CATEGORIES)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    rows = ""
    for k, c in cats.items():
        rows += f"""<tr>
          <td>{h(c['name'])}</td><td><code>{h(k)}</code></td>
          <td>
            <form class="inline" method="post" action="{url_for('console.proxy_price', key=k)}">
              <input type="number" step="0.01" name="price" value="{c['price']}" style="width:80px" required>
              <button class="small" type="submit">Save</button>
            </form>
          </td>
          <td>{stock_count(k)}</td>
          <td>
            <form class="inline" method="post" action="{url_for('console.proxy_delete', key=k)}"
                  onsubmit="return confirm('Delete {h(c['name'])} and all its stock?');">
              <button class="small bad" type="submit">Delete</button>
            </form>
          </td>
        </tr>
        <tr><td colspan="5">
          <form method="post" action="{url_for('console.proxy_stock', key=k)}">
            <textarea name="content" placeholder="One proxy per line — pasted lines are appended, duplicates skipped"></textarea>
            <button type="submit">Upload stock</button>
          </form>
        </td></tr>"""
    body = f"""
    <div class="box">
      <form method="post" action="{url_for('console.proxy_new')}" class="row">
        <input name="name" placeholder="Category name" required>
        <input type="number" step="0.01" name="price" placeholder="Price" required>
        <button type="submit">+ Add category</button>
      </form>
    </div>
    <table><tr><th>Name</th><th>Key</th><th>Price</th><th>Stock</th><th></th></tr>{rows or '<tr><td colspan="5" class="muted">No categories yet.</td></tr>'}</table>
    """
    return _page("Proxy Categories", body, "proxy")


@console_bp.route("/proxy/new", methods=["POST"])
@login_required
def proxy_new():
    import re
    name = request.form.get("name", "").strip()
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    if not name or price <= 0:
        _flash("Name and a positive price are required.")
        return redirect(url_for("console.proxy"))
    key = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:30] or "cat"
    cats = db_read(DB_CATEGORIES)
    base, i = key, 2
    while key in cats:
        key = f"{base[:27]}_{i}"
        i += 1
    db_update(DB_CATEGORIES, lambda c: c.update({key: {"name": name, "price": price}}))
    ensure_category_stock(key)
    log_event("CONSOLE_CAT_ADD", key)
    _flash(f"Category '{name}' created.")
    return redirect(url_for("console.proxy"))


@console_bp.route("/proxy/<key>/price", methods=["POST"])
@login_required
def proxy_price(key):
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    if price > 0:
        def fn(c):
            if key in c:
                c[key]["price"] = price
        db_update(DB_CATEGORIES, fn)
        _flash("Price updated.")
    return redirect(url_for("console.proxy"))


@console_bp.route("/proxy/<key>/stock", methods=["POST"])
@login_required
def proxy_stock(key):
    content = request.form.get("content", "")
    added, dupes = add_proxies(key, content)
    log_event("CONSOLE_UPLOAD", f"cat={key} added={added} dupes={dupes}")
    _flash(f"{added} added, {dupes} duplicates skipped.")
    return redirect(url_for("console.proxy"))


@console_bp.route("/proxy/<key>/delete", methods=["POST"])
@login_required
def proxy_delete(key):
    db_update(DB_CATEGORIES, lambda c: c.pop(key, None))
    delete_category_stock(key)
    log_event("CONSOLE_CAT_DELETE", key)
    _flash("Category deleted.")
    return redirect(url_for("console.proxy"))


# =====================================================================
# VPN / Premium catalog (nested: group → category → product)
# =====================================================================

def _catalog_page(group: str, title: str):
    import re
    cats = get_catalog(group)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    sections = ""
    for ck, c in cats.items():
        prod_rows = ""
        for pk, p in c.get("products", {}).items():
            prod_rows += f"""<tr>
              <td>{h(p['name'])}</td><td><code>{h(pk)}</code></td>
              <td>
                <form class="inline" method="post" action="{url_for('console.catalog_price', group=group, cat=ck, prod=pk)}">
                  <input type="number" step="0.01" name="price" value="{p['price']}" style="width:80px" required>
                  <button class="small" type="submit">Save</button>
                </form>
              </td>
              <td>{unit_stock_count(group, ck, pk)}</td>
              <td>
                <form class="inline" method="post" action="{url_for('console.catalog_prod_delete', group=group, cat=ck, prod=pk)}"
                      onsubmit="return confirm('Delete this product and its stock?');">
                  <button class="small bad" type="submit">Delete</button>
                </form>
              </td>
            </tr>
            <tr><td colspan="5">
              <form method="post" action="{url_for('console.catalog_stock', group=group, cat=ck, prod=pk)}">
                <textarea name="content" placeholder="One account per line, e.g. email:password — appended, duplicates skipped"></textarea>
                <button type="submit">Upload stock</button>
              </form>
            </td></tr>"""
        sections += f"""
        <div class="box">
          <div class="row"><b>🗂 {h(c['name'])}</b> <span class="muted">({ck})</span>
            <form class="inline" method="post" action="{url_for('console.catalog_cat_delete', group=group, cat=ck)}"
                  onsubmit="return confirm('Delete this whole category?');" style="margin-left:auto">
              <button class="small bad" type="submit">Delete category</button>
            </form>
          </div>
          <table><tr><th>Product</th><th>Key</th><th>Price</th><th>Stock</th><th></th></tr>
          {prod_rows or '<tr><td colspan="5" class="muted">No products yet.</td></tr>'}</table>
          <form method="post" action="{url_for('console.catalog_prod_new', group=group, cat=ck)}" class="row" style="margin-top:10px">
            <input name="name" placeholder="Product name" required>
            <input type="number" step="0.01" name="price" placeholder="Price" required>
            <button type="submit">+ Add product</button>
          </form>
        </div>"""
    body = f"""
    <div class="box">
      <form method="post" action="{url_for('console.catalog_cat_new', group=group)}" class="row">
        <input name="name" placeholder="Category name (e.g. 7 Days VPN)" required>
        <button type="submit">+ Add category</button>
      </form>
    </div>
    {sections or '<p class="muted">No categories yet.</p>'}
    """
    return _page(title, body, group)


@console_bp.route("/vpn")
@login_required
def vpn():
    return _catalog_page("vpn", "VPN Catalog")


@console_bp.route("/premium")
@login_required
def premium():
    return _catalog_page("premium", "Premium Catalog")


@console_bp.route("/catalog/<group>/new", methods=["POST"])
@login_required
def catalog_cat_new(group):
    import re
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for(f"console.{group}"))
    key = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:30] or "cat"
    cats = get_catalog(group)
    base, i = key, 2
    while key in cats:
        key = f"{base[:27]}_{i}"
        i += 1
    add_catalog_category(group, key, name)
    log_event("CONSOLE_CATALOG_CAT_ADD", f"{group}/{key}")
    _flash(f"Category '{name}' created.")
    return redirect(url_for(f"console.{group}"))


@console_bp.route("/catalog/<group>/<cat>/delete", methods=["POST"])
@login_required
def catalog_cat_delete(group, cat):
    delete_catalog_category(group, cat)
    _flash("Category deleted.")
    return redirect(url_for(f"console.{group}"))


@console_bp.route("/catalog/<group>/<cat>/new", methods=["POST"])
@login_required
def catalog_prod_new(group, cat):
    import re
    name = request.form.get("name", "").strip()
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    if not name or price <= 0:
        _flash("Name and a positive price are required.")
        return redirect(url_for(f"console.{group}"))
    prod_key = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:30] or "prod"
    base, i = prod_key, 2
    while prod_key in get_catalog(group).get(cat, {}).get("products", {}):
        prod_key = f"{base[:27]}_{i}"
        i += 1
    add_catalog_product(group, cat, prod_key, name, price)
    log_event("CONSOLE_CATALOG_PROD_ADD", f"{group}/{cat}/{prod_key}")
    _flash(f"Product '{name}' created.")
    return redirect(url_for(f"console.{group}"))


@console_bp.route("/catalog/<group>/<cat>/<prod>/price", methods=["POST"])
@login_required
def catalog_price(group, cat, prod):
    try:
        price = round(float(request.form.get("price", "0")), 2)
    except ValueError:
        price = 0
    if price > 0:
        set_catalog_product_price(group, cat, prod, price)
        _flash("Price updated.")
    return redirect(url_for(f"console.{group}"))


@console_bp.route("/catalog/<group>/<cat>/<prod>/stock", methods=["POST"])
@login_required
def catalog_stock(group, cat, prod):
    content = request.form.get("content", "")
    added, dupes = unit_add_stock(group, cat, prod, content)
    log_event("CONSOLE_CATALOG_UPLOAD", f"{group}/{cat}/{prod} added={added} dupes={dupes}")
    _flash(f"{added} added, {dupes} duplicates skipped.")
    return redirect(url_for(f"console.{group}"))


@console_bp.route("/catalog/<group>/<cat>/<prod>/delete", methods=["POST"])
@login_required
def catalog_prod_delete(group, cat, prod):
    delete_catalog_product(group, cat, prod)
    _flash("Product deleted.")
    return redirect(url_for(f"console.{group}"))


# =====================================================================
# Settings
# =====================================================================

@console_bp.route("/settings")
@login_required
def settings():
    s = db_read(DB_SETTINGS)
    st = s.get("bot_status", "ON")
    accounts = s.get("deposit_accounts", {})
    fj = s.get("force_join", [])
    up = s.get("uddoktapay", {})
    bn = s.get("binance", {})

    accounts_rows = "".join(f"""<tr><td>{h(m.upper())}</td><td>{h(n)}</td>
        <td><form class="inline" method="post" action="{url_for('console.settings_pay_delete', method=m)}"><button class="small bad">Delete</button></form></td></tr>"""
        for m, n in accounts.items())
    fj_rows = "".join(f"""<tr><td>{h(e['chat'])}</td><td><a href="{h(e['link'])}" target="_blank">{h(e['link'])}</a></td>
        <td><form class="inline" method="post" action="{url_for('console.settings_fj_delete', idx=i)}"><button class="small bad">Delete</button></form></td></tr>"""
        for i, e in enumerate(fj))

    body = f"""
    <div class="box">
      <h2 style="margin-top:0">Bot Status</h2>
      <form method="post" action="{url_for('console.settings_toggle')}">
        Current: <span class="pill {'on' if st=='ON' else 'off'}">{st}</span>
        <button type="submit" class="{'bad' if st=='ON' else 'good'}">{'Turn OFF' if st=='ON' else 'Turn ON'}</button>
      </form>
    </div>

    <div class="box">
      <h2 style="margin-top:0">Currency &amp; Support</h2>
      <form method="post" action="{url_for('console.settings_general')}" class="row">
        Currency <input name="currency" value="{h(s.get('currency','BDT'))}" style="width:70px">
        Support link <input name="support_link" value="{h(s.get('support_link',''))}" style="width:260px" placeholder="https://t.me/yourusername">
        <button type="submit">Save</button>
      </form>
    </div>

    <div class="box">
      <h2 style="margin-top:0">Referral</h2>
      <form method="post" action="{url_for('console.settings_referral')}" class="row">
        Commission % <input type="number" step="0.1" name="percent" value="{s.get('referral_percent',5)}" style="width:70px">
        Valid days <input type="number" name="days" value="{int(s.get('referral_valid_days',365))}" style="width:90px">
        <button type="submit">Save</button>
      </form>
    </div>

    <div class="box">
      <h2 style="margin-top:0">Deposit Numbers (manual payment)</h2>
      <table><tr><th>Method</th><th>Number</th><th></th></tr>{accounts_rows or '<tr><td colspan="3" class="muted">None set.</td></tr>'}</table>
      <form method="post" action="{url_for('console.settings_pay_new')}" class="row" style="margin-top:10px">
        <input name="method" placeholder="e.g. bkash, nagad, binance" required>
        <input name="number" placeholder="Number / address" required>
        <button type="submit">+ Add</button>
      </form>
    </div>

    <div class="box">
      <h2 style="margin-top:0">Force-Join Channels/Groups</h2>
      <table><tr><th>Chat</th><th>Link</th><th></th></tr>{fj_rows or '<tr><td colspan="3" class="muted">None — force-join disabled.</td></tr>'}</table>
      <form method="post" action="{url_for('console.settings_fj_new')}" class="row" style="margin-top:10px">
        <input name="chat" placeholder="@channelusername or -100..." required>
        <input name="link" placeholder="https://t.me/..." required>
        <button type="submit">+ Add</button>
      </form>
      <p class="muted">⚠️ The bot must be an admin in each channel/group to check membership.</p>
    </div>

    <div class="box">
      <h2 style="margin-top:0">UddoktaPay Gateway</h2>
      <form method="post" action="{url_for('console.settings_uddoktapay')}" class="row">
        <label><input type="checkbox" name="enabled" {'checked' if up.get('enabled', True) else ''}> Enabled</label>
        <input name="api_key" value="{h(up.get('api_key',''))}" placeholder="API Key" style="width:220px">
        <input name="base_url" value="{h(up.get('base_url','https://sandbox.uddoktapay.com/'))}" placeholder="Base URL" style="width:260px">
        <button type="submit">Save</button>
      </form>
    </div>

    <div class="box">
      <h2 style="margin-top:0">Binance Pay Gateway</h2>
      <form method="post" action="{url_for('console.settings_binance')}" class="row">
        <label><input type="checkbox" name="enabled" {'checked' if bn.get('enabled', False) else ''}> Enabled</label>
        <input name="api_key" value="{h(bn.get('api_key',''))}" placeholder="API Key" style="width:220px">
        <input name="api_secret" value="{h(bn.get('api_secret',''))}" placeholder="API Secret" style="width:220px">
        <select name="currency">
          <option value="USDT" {'selected' if bn.get('currency')=='USDT' else ''}>USDT</option>
          <option value="BUSD" {'selected' if bn.get('currency')=='BUSD' else ''}>BUSD</option>
        </select>
        <button type="submit">Save</button>
      </form>
    </div>
    """
    return _page("Settings", body, "settings")


@console_bp.route("/settings/toggle", methods=["POST"])
@login_required
def settings_toggle():
    def fn(s):
        s["bot_status"] = "OFF" if s.get("bot_status", "ON") == "ON" else "ON"
    db_update(DB_SETTINGS, fn)
    _flash("Bot status toggled.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/general", methods=["POST"])
@login_required
def settings_general():
    cur = request.form.get("currency", "BDT").strip() or "BDT"
    link = request.form.get("support_link", "").strip()
    db_update(DB_SETTINGS, lambda s: s.update({"currency": cur, "support_link": link}))
    _flash("Saved.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/referral", methods=["POST"])
@login_required
def settings_referral():
    try:
        percent = float(request.form.get("percent", "5"))
        days = float(request.form.get("days", "365"))
    except ValueError:
        percent, days = 5, 365
    db_update(DB_SETTINGS, lambda s: s.update({"referral_percent": percent, "referral_valid_days": days}))
    _flash("Saved.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/pay/new", methods=["POST"])
@login_required
def settings_pay_new():
    import re
    method = re.sub(r"[^a-zA-Z0-9]", "", request.form.get("method", "")).lower()
    number = request.form.get("number", "").strip()
    if method and number:
        def fn(s):
            s.setdefault("deposit_accounts", {})[method] = number
        db_update(DB_SETTINGS, fn)
        _flash("Added.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/pay/<method>/delete", methods=["POST"])
@login_required
def settings_pay_delete(method):
    def fn(s):
        s.get("deposit_accounts", {}).pop(method, None)
    db_update(DB_SETTINGS, fn)
    _flash("Deleted.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/fj/new", methods=["POST"])
@login_required
def settings_fj_new():
    chat = request.form.get("chat", "").strip()
    link = request.form.get("link", "").strip()
    if chat and link:
        def fn(s):
            s.setdefault("force_join", []).append({"chat": chat, "link": link})
        db_update(DB_SETTINGS, fn)
        _flash("Added.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/fj/<int:idx>/delete", methods=["POST"])
@login_required
def settings_fj_delete(idx):
    def fn(s):
        entries = s.get("force_join", [])
        if 0 <= idx < len(entries):
            entries.pop(idx)
    db_update(DB_SETTINGS, fn)
    _flash("Deleted.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/uddoktapay", methods=["POST"])
@login_required
def settings_uddoktapay():
    def fn(s):
        s.setdefault("uddoktapay", {})
        s["uddoktapay"]["enabled"] = request.form.get("enabled") == "on"
        s["uddoktapay"]["api_key"] = request.form.get("api_key", "").strip()
        s["uddoktapay"]["base_url"] = request.form.get("base_url", "").strip()
    db_update(DB_SETTINGS, fn)
    _flash("Saved.")
    return redirect(url_for("console.settings"))


@console_bp.route("/settings/binance", methods=["POST"])
@login_required
def settings_binance():
    def fn(s):
        s.setdefault("binance", {})
        s["binance"]["enabled"] = request.form.get("enabled") == "on"
        s["binance"]["api_key"] = request.form.get("api_key", "").strip()
        s["binance"]["api_secret"] = request.form.get("api_secret", "").strip()
        s["binance"]["currency"] = request.form.get("currency", "USDT")
    db_update(DB_SETTINGS, fn)
    _flash("Saved.")
    return redirect(url_for("console.settings"))


# =====================================================================
# Texts
# =====================================================================

@console_bp.route("/texts")
@login_required
def texts():
    keys = list_text_keys()
    sections = ""
    for k in keys:
        bn_val = get_text(k, "bn")
        en_val = get_text(k, "en")
        sections += f"""
        <div class="box">
          <b>{h(k)}</b>
          <form method="post" action="{url_for('console.texts_save', key=k)}">
            <p class="muted">বাংলা</p>
            <textarea name="bn">{h(bn_val)}</textarea>
            <p class="muted">English</p>
            <textarea name="en">{h(en_val)}</textarea>
            <button type="submit">Save</button>
          </form>
        </div>"""
    return _page("Texts", sections, "texts")


@console_bp.route("/texts/<key>/save", methods=["POST"])
@login_required
def texts_save(key):
    set_text(key, "bn", request.form.get("bn", ""))
    set_text(key, "en", request.form.get("en", ""))
    _flash(f"'{key}' saved.")
    return redirect(url_for("console.texts"))


# =====================================================================
# Premium Emoji
# =====================================================================

@console_bp.route("/emoji")
@login_required
def emoji():
    items = list_emojis()
    rows = ""
    for k, e in items.items():
        rows += f"""<tr>
          <td>{h(e.get('label', k))}</td><td><code>{h(k)}</code></td>
          <td>{h(e.get('fallback',''))}</td><td><code>{h(e.get('id',''))}</code></td>
          <td><code>{{{{emoji:{h(k)}}}}}</code></td>
          <td><form class="inline" method="post" action="{url_for('console.emoji_delete', key=k)}"><button class="small bad">Delete</button></form></td>
        </tr>"""
    body = f"""
    <p class="muted">Bot API can't create new custom emoji — a bot can only <i>reference</i> one that already
    exists, by its numeric ID. Get the ID by forwarding a message containing the emoji to your bot in Telegram
    (the bot's inline admin panel → Settings → Premium Emoji captures it automatically), then paste that ID here,
    or just add it straight from Telegram's admin panel.</p>
    <div class="box">
      <form method="post" action="{url_for('console.emoji_new')}" class="row">
        <input name="label" placeholder="Label (e.g. fire)" required>
        <input name="key" placeholder="key (a-z0-9_, blank = auto from label)">
        <input name="emoji_id" placeholder="Custom emoji ID (numeric)" required>
        <input name="fallback" placeholder="Fallback emoji, e.g. 🔥" required style="width:80px">
        <button type="submit">+ Add</button>
      </form>
    </div>
    <table><tr><th>Label</th><th>Key</th><th>Fallback</th><th>Emoji ID</th><th>Use in text as</th><th></th></tr>
    {rows or '<tr><td colspan="6" class="muted">No custom emoji added yet.</td></tr>'}</table>
    """
    return _page("Premium Emoji", body, "emoji")


@console_bp.route("/emoji/new", methods=["POST"])
@login_required
def emoji_new():
    import re
    label = request.form.get("label", "").strip()
    emoji_id = request.form.get("emoji_id", "").strip()
    fallback = request.form.get("fallback", "").strip()
    key = request.form.get("key", "").strip()
    if not (label and emoji_id and fallback):
        _flash("Label, emoji ID and fallback are all required.")
        return redirect(url_for("console.emoji"))
    if not key:
        key = re.sub(r"[^a-zA-Z0-9_]+", "_", label).strip("_").lower()[:30] or "emoji"
    add_emoji(key, emoji_id, fallback, label)
    log_event("CONSOLE_EMOJI_ADD", f"{key} id={emoji_id}")
    _flash(f"Emoji '{label}' added.")
    return redirect(url_for("console.emoji"))


@console_bp.route("/emoji/<key>/delete", methods=["POST"])
@login_required
def emoji_delete(key):
    delete_emoji(key)
    _flash("Deleted.")
    return redirect(url_for("console.emoji"))


# =====================================================================
# Broadcast
# =====================================================================

@console_bp.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast():
    result = ""
    if request.method == "POST":
        text = request.form.get("message", "").strip()
        if text:
            all_users = db_read(DB_USERS)
            sent = failed = 0
            for uid, u in all_users.items():
                if not str(uid).isdigit() or u.get("banned"):
                    continue
                try:
                    tg_send(uid, text)
                    sent += 1
                except Exception:
                    failed += 1
            log_event("CONSOLE_BROADCAST", f"sent={sent} failed={failed}")
            result = f"<p class='muted'>Sent: {sent} · Failed: {failed}</p>"
    body = f"""
    <form method="post">
      <textarea name="message" placeholder="HTML formatting supported (e.g. &lt;b&gt;bold&lt;/b&gt;)" style="min-height:160px"></textarea>
      <p><button type="submit">Send to all users</button></p>
    </form>
    {result}
    """
    return _page("Broadcast", body, "broadcast")
