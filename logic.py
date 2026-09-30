"""
logic.py — shared business logic for OwlProxy Shop.
---------------------------------------------------------------
Used by BOTH bot.py (Telegram) and console.py (web panel), so that
money-affecting logic — crediting deposits, referral bonuses, stats —
lives in exactly one place. Self-contained: only depends on storage.py
and the Telegram HTTP API directly (no dependency on bot.py), which
avoids circular imports between bot.py and console.py.
"""

import os
import time
import html
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from storage import (
    db_read, db_update, log_event, stock_count, get_catalog, unit_stock_count,
    DB_USERS, DB_CATEGORIES, DB_SETTINGS, DB_DEPOSITS,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now(TZ).strftime(fmt)


def h(s) -> str:
    return html.escape(str(s), quote=True)


def fmt(n) -> str:
    s = f"{float(n):.2f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def get_lang(user: dict) -> str:
    return user.get("lang") or db_read(DB_SETTINGS).get("default_lang", "bn")


def tg_send(chat_id, text: str) -> None:
    """Minimal Telegram notifier — plain sendMessage with HTML parse mode."""
    if not BOT_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    except requests.RequestException:
        pass


def credit_deposit(uid: str, amount: float, currency: str):
    """Adds balance + pays referral bonus (once, within the validity window). Returns bonus info if paid."""
    def fn(users: dict):
        if uid not in users:
            return {}
        users[uid]["balance"] = round(users[uid].get("balance", 0) + amount, 2)
        users[uid]["total_deposited"] = round(users[uid].get("total_deposited", 0) + amount, 2)
        s = db_read(DB_SETTINGS)
        percent = float(s.get("referral_percent", 5))
        valid_days = float(s.get("referral_valid_days", 365))
        ref = users[uid].get("referred_by")
        if ref and not users[uid].get("ref_paid") and ref in users and percent > 0:
            age_days = (time.time() - float(users[uid].get("referred_at", 0))) / 86400
            if age_days <= valid_days:
                b = round(amount * percent / 100, 2)
                users[ref]["balance"] = round(users[ref].get("balance", 0) + b, 2)
                users[ref]["ref_earned"] = round(users[ref].get("ref_earned", 0) + b, 2)
                users[ref]["ref_count"] = users[ref].get("ref_count", 0) + 1
                users[uid]["ref_paid"] = True
                return {"ref": ref, "amount": b}
        return {}
    bonus_info = db_update(DB_USERS, fn) or {}
    if bonus_info.get("ref"):
        u = db_read(DB_USERS).get(bonus_info["ref"], {})
        lang = get_lang(u)
        tg_send(bonus_info["ref"], f"🎁 {'রেফারেল বোনাস' if lang == 'bn' else 'Referral bonus'} "
                f"<b>{fmt(bonus_info['amount'])} {h(currency)}</b> {'যোগ হয়েছে!' if lang == 'bn' else 'credited!'}")
    return bonus_info


def process_manual_deposit(dep_id: str, approve: bool, admin_id: str):
    """Approve/reject a pending (manual or gateway-fallback) deposit request. Idempotent."""
    def fn(deps: dict):
        if dep_id not in deps:
            return None
        if deps[dep_id]["status"] != "pending":
            return {"changed": False, "dep": deps[dep_id]}
        deps[dep_id]["status"] = "approved" if approve else "rejected"
        deps[dep_id]["handled_by"] = admin_id
        deps[dep_id]["handled_at"] = now_str()
        return {"changed": True, "dep": deps[dep_id]}

    r = db_update(DB_DEPOSITS, fn)
    if not r:
        return {"changed": False, "dep": None}
    if r["changed"]:
        dep = r["dep"]
        uid = str(dep["user"])
        u = db_read(DB_USERS).get(uid, {})
        lang = get_lang(u)
        currency = db_read(DB_SETTINGS).get("currency", "BDT")
        if approve:
            credit_deposit(uid, dep["amount"], currency)
            tg_send(uid, f"🎉 {'আপনার ডিপোজিট' if lang=='bn' else 'Your deposit of'} <b>{fmt(dep['amount'])} {h(currency)}</b> "
                         f"{'অনুমোদিত হয়েছে!' if lang=='bn' else 'has been approved!'}")
            log_event("DEPOSIT_OK", f"id={dep_id} user={uid} amount={dep['amount']} by={admin_id}")
        else:
            tg_send(uid, f"❌ {'আপনার ডিপোজিট রিকোয়েস্ট' if lang=='bn' else 'Your deposit request'} (#{dep_id}) "
                         f"{'বাতিল করা হয়েছে।' if lang=='bn' else 'was rejected.'}")
            log_event("DEPOSIT_REJECT", f"id={dep_id} user={uid} by={admin_id}")
    return r


def stats_text() -> str:
    users = db_read(DB_USERS)
    deps = db_read(DB_DEPOSITS)
    proxy_cats = db_read(DB_CATEGORIES)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    today = now_str("%Y-%m-%d")
    active = new_today = 0
    held = revenue = 0.0
    for u in users.values():
        if str(u.get("last_seen", "")).startswith(today):
            active += 1
        if str(u.get("joined", "")).startswith(today):
            new_today += 1
        held += u.get("balance", 0)
        revenue += u.get("total_spent", 0)
    pending = 0
    dep_total = 0.0
    for d in deps.values():
        if d["status"] == "pending":
            pending += 1
        if d["status"] == "approved":
            dep_total += d["amount"]
    out = (
        "📊 <b>পরিসংখ্যান</b>\n\n"
        f"👥 মোট ইউজার: <b>{len(users)}</b> (আজ নতুন: {new_today}, আজ অ্যাক্টিভ: {active})\n"
        f"💵 মোট বিক্রয়: <b>{fmt(revenue)} {cur}</b>\n"
        f"🏦 মোট ডিপোজিট: <b>{fmt(dep_total)} {cur}</b>\n"
        f"👛 ইউজারদের কাছে ব্যালেন্স: <b>{fmt(held)} {cur}</b>\n"
        f"⏳ অপেক্ষমান ডিপোজিট: <b>{pending}</b>\n\n<b>📦 প্রক্সি স্টক</b>\n"
    )
    for k, c in proxy_cats.items():
        out += f"• {h(c['name'])}: <b>{stock_count(k)}</b> ({fmt(c['price'])} {cur})\n"
    for group, label in (("vpn", "🛡️ VPN"), ("premium", "💎 Premium")):
        out += f"\n<b>{label} স্টক</b>\n"
        cats = get_catalog(group)
        if not cats:
            out += "  (খালি)\n"
        for ck, c in cats.items():
            for pk, p in c.get("products", {}).items():
                out += f"• {h(c['name'])} / {h(p['name'])}: <b>{unit_stock_count(group, ck, pk)}</b> ({fmt(p['price'])} {cur})\n"
    return out


def stats_summary() -> dict:
    """Plain-data version of stats_text(), for the web console's dashboard cards."""
    users = db_read(DB_USERS)
    deps = db_read(DB_DEPOSITS)
    today = now_str("%Y-%m-%d")
    active = new_today = 0
    held = revenue = 0.0
    for u in users.values():
        if str(u.get("last_seen", "")).startswith(today):
            active += 1
        if str(u.get("joined", "")).startswith(today):
            new_today += 1
        held += u.get("balance", 0)
        revenue += u.get("total_spent", 0)
    pending = 0
    dep_total = 0.0
    for d in deps.values():
        if d["status"] == "pending":
            pending += 1
        if d["status"] == "approved":
            dep_total += d["amount"]
    return {
        "total_users": len(users), "new_today": new_today, "active_today": active,
        "revenue": revenue, "deposits_total": dep_total, "balance_held": held,
        "pending_deposits": pending, "currency": db_read(DB_SETTINGS).get("currency", "BDT"),
    }
