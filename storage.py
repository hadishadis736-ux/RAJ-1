"""
storage.py — MongoDB Atlas-backed persistent storage for OwlProxy bot.
-----------------------------------------------------------------------
Replaces the old local-JSON-file storage (which got wiped on every
Render redeploy) with MongoDB Atlas, which is external and survives
redeploys/restarts.

Keeps the SAME function names/signatures the rest of bot.py already
calls (db_read, db_update, stock_count, take_proxies, add_proxies,
log_event, bootstrap) so bot.py needs only minimal changes.

Set the MONGODB_URI environment variable on Render, e.g.:
  mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority

Requires: pymongo (see requirements.txt)
"""

import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from pymongo import MongoClient

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DBNAME = os.environ.get("MONGODB_DBNAME", "owlproxybot")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI environment variable is not set. Add it in Render → Environment.")

_client = MongoClient(MONGODB_URI)
_db = _client[MONGODB_DBNAME]
_docs = _db["docs"]          # one document per logical "file": {_id: "users", data: {...}}
_proxies = _db["proxies"]    # one document per category: {_id: "owl_200mb", lines: [...]}

# Document identifiers — pass these into db_read()/db_update() exactly like the
# old file paths were passed. Names only matter as Mongo _id values now.
DB_USERS = "users"
DB_CATEGORIES = "categories"      # flat proxy categories (unchanged)
DB_SETTINGS = "settings"
DB_DEPOSITS = "deposits"
DB_CATALOG = "catalog"            # nested groups: vpn / premium -> categories -> products
DB_TEXTS = "texts"                # admin-editable UI copy, per language
DB_EMOJIS = "emojis"               # admin-uploaded Telegram Premium custom emoji

# All read-modify-write ops go through one process-wide lock so concurrent
# webhook requests (gunicorn threads) never race each other. This mirrors
# what the old fcntl file-lock guaranteed.
_lock = threading.RLock()


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now(TZ).strftime(fmt)


# =====================================================================
# Generic document storage (users / categories / settings / deposits)
# =====================================================================

def db_read(name: str) -> dict:
    with _lock:
        doc = _docs.find_one({"_id": name})
        return doc["data"] if doc and isinstance(doc.get("data"), dict) else {}


def db_update(name: str, fn):
    """Read-modify-write under a lock. fn(data) mutates data in place and returns a result."""
    with _lock:
        doc = _docs.find_one({"_id": name})
        data = doc["data"] if doc and isinstance(doc.get("data"), dict) else {}
        result = fn(data)
        _docs.update_one({"_id": name}, {"$set": {"data": data}}, upsert=True)
        return result


def write_raw(name: str, data: dict) -> None:
    with _lock:
        _docs.update_one({"_id": name}, {"$set": {"data": data}}, upsert=True)


# =====================================================================
# Proxy stock storage (one Mongo doc per category, holding a list of lines)
# =====================================================================

def clean_lines(raw: str):
    return [l.strip() for l in re.split(r"\r\n|\r|\n", raw) if l.strip() != ""]


def ensure_category_stock(key: str) -> None:
    """Create an empty stock document for a category if it doesn't exist yet."""
    with _lock:
        _proxies.update_one({"_id": key}, {"$setOnInsert": {"lines": []}}, upsert=True)


def delete_category_stock(key: str) -> None:
    with _lock:
        _proxies.delete_one({"_id": key})


def stock_count(key: str) -> int:
    with _lock:
        doc = _proxies.find_one({"_id": key})
        return len(doc["lines"]) if doc and isinstance(doc.get("lines"), list) else 0


def take_proxies(key: str, qty: int):
    """Take exactly qty proxies, or None (and touch nothing) if stock is insufficient."""
    with _lock:
        doc = _proxies.find_one({"_id": key})
        lines = doc["lines"] if doc and isinstance(doc.get("lines"), list) else []
        if len(lines) < qty:
            return None
        taken, remaining = lines[:qty], lines[qty:]
        _proxies.update_one({"_id": key}, {"$set": {"lines": remaining}}, upsert=True)
        return taken


def add_proxies(key: str, content: str):
    """Append proxies, removing blanks and duplicates. Returns (added, duplicates)."""
    with _lock:
        doc = _proxies.find_one({"_id": key})
        existing = doc["lines"] if doc and isinstance(doc.get("lines"), list) else []
        seen = set(existing)
        added = dupes = 0
        for l in clean_lines(content):
            if l in seen:
                dupes += 1
                continue
            seen.add(l)
            existing.append(l)
            added += 1
        _proxies.update_one({"_id": key}, {"$set": {"lines": existing}}, upsert=True)
        return added, dupes


# =====================================================================
# Unit stock — VPN / Premium accounts. Each stock LINE is one full,
# ready-to-deliver account (e.g. "email@x.com:password123"). Reuses the
# exact same lines-based storage as proxies, just under a "group:cat:prod"
# key instead of a bare category key.
# =====================================================================

def unit_key(group: str, cat: str, prod: str) -> str:
    return f"{group}:{cat}:{prod}"


def unit_stock_count(group: str, cat: str, prod: str) -> int:
    return stock_count(unit_key(group, cat, prod))


def unit_take_one(group: str, cat: str, prod: str):
    r = take_proxies(unit_key(group, cat, prod), 1)
    return r[0] if r else None


def unit_add_stock(group: str, cat: str, prod: str, content: str):
    ensure_category_stock(unit_key(group, cat, prod))
    return add_proxies(unit_key(group, cat, prod), content)


def unit_delete_stock(group: str, cat: str, prod: str) -> None:
    delete_category_stock(unit_key(group, cat, prod))


# =====================================================================
# Catalog — nested Group → Category → Product, used for "Buy VPN" and
# "Premium Buy". Stored as one document: {"vpn": {cat_key: {"name":...,
# "products": {prod_key: {"name":..., "price":...}}}}, "premium": {...}}
# =====================================================================

def get_catalog(group: str) -> dict:
    return db_read(DB_CATALOG).get(group, {})


def catalog_update(group: str, fn):
    def outer(data: dict):
        data.setdefault(group, {})
        return fn(data[group])
    return db_update(DB_CATALOG, outer)


def add_catalog_category(group: str, key: str, name: str):
    def fn(cats):
        cats[key] = {"name": name, "products": {}}
    catalog_update(group, fn)


def delete_catalog_category(group: str, cat_key: str):
    cats = get_catalog(group)
    prods = list((cats.get(cat_key) or {}).get("products", {}).keys())
    for p in prods:
        unit_delete_stock(group, cat_key, p)
    def fn(cats):
        cats.pop(cat_key, None)
    catalog_update(group, fn)


def add_catalog_product(group: str, cat_key: str, prod_key: str, name: str, price: float):
    def fn(cats):
        cats.setdefault(cat_key, {"name": cat_key, "products": {}})
        cats[cat_key].setdefault("products", {})
        cats[cat_key]["products"][prod_key] = {"name": name, "price": price}
    catalog_update(group, fn)
    ensure_category_stock(unit_key(group, cat_key, prod_key))


def delete_catalog_product(group: str, cat_key: str, prod_key: str):
    unit_delete_stock(group, cat_key, prod_key)
    def fn(cats):
        if cat_key in cats:
            cats[cat_key].get("products", {}).pop(prod_key, None)
    catalog_update(group, fn)


def set_catalog_product_price(group: str, cat_key: str, prod_key: str, price: float):
    def fn(cats):
        if cat_key in cats and prod_key in cats[cat_key].get("products", {}):
            cats[cat_key]["products"][prod_key]["price"] = price
    catalog_update(group, fn)


# =====================================================================
# Editable UI texts — admin can override any of these per language via
# an admin command; unset keys fall back to DEFAULT_TEXTS below.
# =====================================================================

DEFAULT_TEXTS = {
    "welcome": {
        "bn": "👋 স্বাগতম! নিচের মেনু থেকে একটি অপশন বেছে নিন:",
        "en": "👋 Welcome! Please choose an option from the menu below:",
    },
    "support_text": {
        "bn": "🎧 আপনার অর্ডার, পেমেন্ট, VPN, Proxy বা Premium নিয়ে সাহায্য দরকার?\n\nআমাদের সাপোর্ট টিম রেডি আছে। নিচের বাটনে চাপুন।",
        "en": "🎧 Need help with your order, payment, VPN, Proxy or Premium subscription?\n\nOur support team is ready to help. Tap the button below.",
    },
    "force_join_text": {
        "bn": "🔐 <b>অ্যাক্সেস সীমিত</b>\n\nবটটি ব্যবহার করতে নিচের চ্যানেল/গ্রুপে জয়েন করুন।",
        "en": "🔐 <b>Access Restricted</b>\n\nPlease join the channel/group below to use this bot.",
    },
    "deposit_prompt": {
        "bn": "💳 কত টাকা ডিপোজিট করতে চান লিখুন (সর্বনিম্ন {min}):",
        "en": "💳 Enter the amount you want to deposit (minimum {min}):",
    },
    "referral_text": {
        "bn": "🎁 <b>রেফার করে আয় করুন!</b>\n\nআপনার লিংকে কেউ জয়েন করে ডিপোজিট করলে আপনি পাবেন <b>{percent}%</b> কমিশন ({days} দিন মেয়াদ)।\n\n🔗 আপনার রেফারেল লিংক:\n{link}\n\n👥 মোট রেফার: {count}\n💵 মোট আয়: {earned} {currency}\n\n✅ ইউজারের ডিপোজিট অনুমোদিত হলেই কমিশন যোগ হবে।",
        "en": "🎁 <b>Earn by Referring!</b>\n\nWhen someone joins with your link and deposits, you earn <b>{percent}%</b> commission (valid {days} days).\n\n🔗 Your referral link:\n{link}\n\n👥 Total referrals: {count}\n💵 Total earned: {earned} {currency}\n\n✅ Commission is credited only after the referred user's deposit is approved.",
    },
}


def get_text(key: str, lang: str = "bn", **kwargs) -> str:
    texts = db_read(DB_TEXTS)
    entry = texts.get(key) or DEFAULT_TEXTS.get(key) or {}
    raw = entry.get(lang) or entry.get("bn") or entry.get("en") or key
    try:
        out = raw.format(**kwargs)
    except (KeyError, IndexError):
        out = raw
    return _expand_emojis(out)


def set_text(key: str, lang: str, value: str) -> None:
    def fn(t):
        t.setdefault(key, {})
        t[key][lang] = value
    db_update(DB_TEXTS, fn)


def list_text_keys():
    keys = set(DEFAULT_TEXTS.keys()) | set(db_read(DB_TEXTS).keys())
    return sorted(keys)


# =====================================================================
# Premium custom emoji — captured from a message the admin forwards/sends
# (Bot API can't create new custom emoji, only reference existing ones by
# ID). Stored as {key: {"id": "...", "fallback": "😀", "label": "..."}}.
# Referenced inside any editable text as {emoji:key}; get_text() expands
# it into Telegram's <tg-emoji emoji-id="..."> HTML tag automatically.
# =====================================================================

def list_emojis() -> dict:
    return db_read(DB_EMOJIS)


def add_emoji(key: str, emoji_id: str, fallback: str, label: str) -> None:
    def fn(e):
        e[key] = {"id": emoji_id, "fallback": fallback, "label": label}
    db_update(DB_EMOJIS, fn)


def delete_emoji(key: str) -> None:
    def fn(e):
        e.pop(key, None)
    db_update(DB_EMOJIS, fn)


def _expand_emojis(raw: str) -> str:
    emojis = db_read(DB_EMOJIS)
    def sub(m):
        key = m.group(1)
        e = emojis.get(key)
        if not e:
            return m.group(0)
        return f'<tg-emoji emoji-id="{e["id"]}">{e["fallback"]}</tg-emoji>'
    return re.sub(r"\{emoji:([a-zA-Z0-9_]+)\}", sub, raw)


# =====================================================================
# Logging — Render captures stdout in its Logs tab, so no file needed.
# =====================================================================

def log_event(kind: str, msg: str) -> None:
    print(f"[{now_str()}] {kind}: {msg}", flush=True)


# =====================================================================
# Bootstrap — create default documents the first time the bot runs.
# =====================================================================

def bootstrap():
    with _lock:
        if _docs.find_one({"_id": DB_SETTINGS}) is None:
            write_raw(DB_SETTINGS, {
                "currency": "BDT",
                "bot_status": "ON",
                "deposit_accounts": {},
                "support_link": "",
                "force_join": [],           # [{"chat": "@handle", "link": "https://t.me/handle", "label": "..."}]
                "referral_percent": 5,
                "referral_valid_days": 365,
                "default_lang": "bn",
                "uddoktapay": {"enabled": False, "api_key": "", "base_url": "https://sandbox.uddoktapay.com/"},
                "binance": {"enabled": False, "api_key": "", "api_secret": "", "currency": "USDT"},
            })
        if _docs.find_one({"_id": DB_USERS}) is None:
            write_raw(DB_USERS, {})
        if _docs.find_one({"_id": DB_DEPOSITS}) is None:
            write_raw(DB_DEPOSITS, {})
        if _docs.find_one({"_id": DB_CATEGORIES}) is None:
            write_raw(DB_CATEGORIES, {
                "owl_200mb": {"name": "OWL PROXY 200 MB", "price": 10.00},
                "standard": {"name": "OWL PROXY Standard", "price": 15.00},
            })
            for k in ("owl_200mb", "standard"):
                ensure_category_stock(k)
                add_proxies(k, "us_proxy_1:port:user:pass\n")
        if _docs.find_one({"_id": DB_CATALOG}) is None:
            write_raw(DB_CATALOG, {"vpn": {}, "premium": {}})
        if _docs.find_one({"_id": DB_TEXTS}) is None:
            write_raw(DB_TEXTS, {})
        if _docs.find_one({"_id": DB_EMOJIS}) is None:
            write_raw(DB_EMOJIS, {})
