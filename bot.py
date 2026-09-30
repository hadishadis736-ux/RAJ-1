#!/usr/bin/env python3
"""
OwlProxy Shop — Multi-Category Edition (Python / Flask)
---------------------------------------------------------------
Sells VPN accounts, Proxies and Premium subscriptions through a single
Telegram bot. Persistent storage in MongoDB Atlas (see storage.py) so
data survives Render/Railway redeploys and restarts.

Everything configurable (support link, force-join list, referral %,
payment gateways, deposit numbers, UI texts, VPN/Premium categories
and products) is managed entirely through inline BUTTONS inside the
admin dashboard — no slash commands needed at all for day-to-day use.
A few power-user slash commands still exist (/addbalance, /ban,
/userinfo, /stats) for quick one-line admin actions.

Run with:  gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT bot:app
"""

import os
import re
import html
import time
import hmac
import hashlib
import tempfile
import binascii
import random
import string
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, abort, Response

import storage
from storage import (
    db_read, db_update, log_event, clean_lines,
    stock_count, take_proxies, add_proxies,
    ensure_category_stock, delete_category_stock,
    unit_stock_count, unit_take_one, unit_add_stock, unit_delete_stock,
    get_catalog, add_catalog_category, delete_catalog_category,
    add_catalog_product, delete_catalog_product, set_catalog_product_price,
    get_text, set_text, list_text_keys,
    list_emojis, add_emoji, delete_emoji,
    DB_USERS, DB_CATEGORIES, DB_SETTINGS, DB_DEPOSITS,
)
from logic import credit_deposit, process_manual_deposit, stats_text

# =====================================================================
# 1. CONFIGURATION
# =====================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_IDS = [x.strip() for x in os.environ.get("OWNER_IDS", os.environ.get("ADMIN_IDS", "")).split(",") if x.strip()]

# The bot's own public URL — needed to build UddoktaPay redirect/cancel/webhook
# URLs. Render sets RENDER_EXTERNAL_URL automatically; on Railway (or anywhere
# else) set PUBLIC_BASE_URL yourself, e.g. https://your-app.up.railway.app
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL")
                    or os.environ.get("RENDER_EXTERNAL_URL")
                    or "").rstrip("/")

NOTIFICATION_CHANNEL = os.environ.get("NOTIFICATION_CHANNEL", "")

# UddoktaPay — env vars are the fallback; the admin panel's Gateway screen
# overrides them and is what's actually used if set. The public sandbox key
# below works out of the box for testing (see https://uddoktapay.readme.io).
UDDOKTAPAY_API_KEY_ENV = os.environ.get("UDDOKTAPAY_API_KEY", "982d381360a69d419689740d9f2e26ce36fb7a50")
UDDOKTAPAY_BASE_URL_ENV = os.environ.get("UDDOKTAPAY_BASE_URL", "https://sandbox.uddoktapay.com/")

MIN_DEPOSIT = float(os.environ.get("MIN_DEPOSIT", "20"))
MAX_ORDER_QTY = int(os.environ.get("MAX_ORDER_QTY", "500"))
INLINE_DELIVERY_LIMIT = int(os.environ.get("INLINE_DELIVERY_LIMIT", "10"))
LOW_STOCK_ALERT = int(os.environ.get("LOW_STOCK_ALERT", "5"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "10"))
KEEP_ORDERS = int(os.environ.get("KEEP_ORDERS", "50"))
MEMBERS_PER_PAGE = int(os.environ.get("MEMBERS_PER_PAGE", "10"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET") or binascii.hexlify(os.urandom(24)).decode()

from console import console_bp
app.register_blueprint(console_bp)

# =====================================================================
# 2. BILINGUAL BUTTON LABELS (structural — routing depends on these)
# =====================================================================

BTN = {
    "profile":  {"bn": "👤 আমার প্রোফাইল",   "en": "👤 Profile"},
    "vpn":      {"bn": "🛡️ ভিপিএন কিনুন",    "en": "🛡️ Buy VPN"},
    "proxy":    {"bn": "🌐 প্রক্সি কিনুন",    "en": "🌐 Buy Proxy"},
    "premium":  {"bn": "💎 প্রিমিয়াম কিনুন",  "en": "💎 Premium Buy"},
    "deposit":  {"bn": "💳 ডিপোজিট করুন",     "en": "💳 Deposit"},
    "support":  {"bn": "🎧 সাপোর্ট",          "en": "🎧 Support"},
    "refer":    {"bn": "🎁 রেফার করুন",       "en": "🎁 Refer"},
    "lang":     {"bn": "🌐 ভাষা",             "en": "🌐 Language"},
    "admin":    {"bn": "🛠️ ড্যাশবোর্ড / ব্রডকাস্ট", "en": "🛠️ Dashboard / Broadcast"},
    "cancel":   {"bn": "❌ বাতিল",            "en": "❌ Cancel"},
    "back":     {"bn": "🔙 পেছনে",            "en": "🔙 Back"},
    "confirm":  {"bn": "✅ কনফার্ম",          "en": "✅ Confirm"},
    "pay_now":  {"bn": "💳 এখনই পে করুন",     "en": "💳 Pay Now"},
    "manual_pay": {"bn": "📩 ম্যানুয়াল পেমেন্ট", "en": "📩 Manual Payment"},
    "main_menu": {"bn": "🏠 মূল মেনু",        "en": "🏠 Main Menu"},
}


def t(key: str, lang: str) -> str:
    e = BTN.get(key, {})
    return e.get(lang) or e.get("bn") or key


# =====================================================================
# 3. BOOTSTRAP
# =====================================================================

def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now(TZ).strftime(fmt)


def bootstrap():
    storage.bootstrap()


# =====================================================================
# 4. TELEGRAM HELPERS
# =====================================================================

def tg(method: str, params: dict = None):
    params = params or {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=20)
        return r.json()
    except requests.RequestException:
        return None


def send_text_file(chat_id, filename: str, content: str, caption: str = ""):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as fh:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document": (filename, fh, "text/plain")}, timeout=30,
            )
    except requests.RequestException:
        pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def h(s) -> str:
    return html.escape(str(s), quote=True)


def fmt(n) -> str:
    s = f"{float(n):.2f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def reply(chat_id, text: str, kb: dict = None, mid: int = None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb is not None:
        p["reply_markup"] = kb
    if mid:
        p["message_id"] = mid
        return tg("editMessageText", p)
    return tg("sendMessage", p)


def ikb(rows) -> dict:
    return {"inline_keyboard": rows}


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def cancel_row(lang):
    return [btn(t("cancel", lang), "main_menu")]


def main_keyboard(lang: str, is_adm: bool) -> dict:
    kb = [
        [t("profile", lang), t("vpn", lang)],
        [t("proxy", lang), t("premium", lang)],
        [t("deposit", lang), t("support", lang)],
        [t("refer", lang), t("lang", lang)],
    ]
    if is_adm:
        kb.append([t("admin", lang)])
    return {"keyboard": kb, "resize_keyboard": True, "is_persistent": True}


def is_admin(uid) -> bool:
    return str(uid) in ADMIN_IDS


def notify_admins(text: str, kb: dict = None):
    for a in ADMIN_IDS:
        reply(a, text, kb)


def get_lang(user: dict) -> str:
    return user.get("lang") or db_read(DB_SETTINGS).get("default_lang", "bn")


def is_channel_member(user_id, chat) -> bool:
    r = tg("getChatMember", {"chat_id": chat, "user_id": user_id})
    if not r or not r.get("ok"):
        return True  # fail-open if bot can't check (e.g. not admin there)
    return (r.get("result") or {}).get("status") in ("member", "administrator", "creator")


def missing_force_join(user_id):
    entries = db_read(DB_SETTINGS).get("force_join", [])
    return [e for e in entries if not is_channel_member(user_id, e.get("chat"))]


def bot_username() -> str:
    s = db_read(DB_SETTINGS)
    if s.get("bot_username"):
        return s["bot_username"]
    r = tg("getMe")
    u = ((r or {}).get("result") or {}).get("username", "")
    if u:
        db_update(DB_SETTINGS, lambda d: d.update({"bot_username": u}))
    return u


def mask_id(uid) -> str:
    uid = str(uid)
    return uid[:2] + "•" * (len(uid) - 4) + uid[-2:] if len(uid) > 4 else uid


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def mask_key(k: str) -> str:
    if not k:
        return "(not set)"
    return k[:4] + "…" + k[-4:] if len(k) > 10 else "••••"


# =====================================================================
# 5. USER HELPERS
# =====================================================================

def ensure_user(uid: str, frm: dict, start_payload: str, is_message: bool):
    def fn(users: dict):
        now = time.time()
        throttled = False
        if uid not in users:
            users[uid] = {
                "balance": 0.0, "username": frm.get("first_name", "User"),
                "tg_username": frm.get("username", ""), "joined": now_str(),
                "orders": [], "banned": False, "total_spent": 0.0, "total_deposited": 0.0,
                "lang": db_read(DB_SETTINGS).get("default_lang", "bn"),
            }
            m = re.match(r"^ref_(\d+)$", start_payload or "")
            if m and m.group(1) != uid and m.group(1) in users:
                users[uid]["referred_by"] = m.group(1)
                users[uid]["referred_at"] = now
        u = users[uid]
        u["username"] = frm.get("first_name") or u.get("username")
        u["tg_username"] = frm.get("username") or u.get("tg_username", "")
        u["last_seen"] = now_str()
        if not isinstance(u.get("orders"), list):
            u["orders"] = []
        if is_message:
            if now - float(u.get("last_msg_at", 0)) < 0.5:
                throttled = True
            u["last_msg_at"] = now
        return {"user": u, "throttled": throttled}
    return db_update(DB_USERS, fn)


def set_state(uid: str, step, data: dict = None):
    data = data or {}
    def fn(users: dict):
        if uid not in users:
            return
        if step is None:
            users[uid].pop("state", None)
        else:
            users[uid]["state"] = {"step": step, **data}
    db_update(DB_USERS, fn)


def set_lang(uid: str, lang: str):
    def fn(users: dict):
        if uid in users:
            users[uid]["lang"] = lang
    db_update(DB_USERS, fn)


def adjust_balance(uid: str, delta: float):
    def fn(users: dict):
        if uid not in users:
            return None
        users[uid]["balance"] = round(users[uid].get("balance", 0) + delta, 2)
        return users[uid]["balance"]
    return db_update(DB_USERS, fn)


# =====================================================================
# 6. UDDOKTAPAY GATEWAY
# =====================================================================

def gateway_config():
    s = db_read(DB_SETTINGS).get("uddoktapay", {})
    api_key = s.get("api_key") or UDDOKTAPAY_API_KEY_ENV
    base_url = (s.get("base_url") or UDDOKTAPAY_BASE_URL_ENV).rstrip("/") + "/"
    enabled = s.get("enabled", True) and bool(api_key) and bool(PUBLIC_BASE_URL)
    return enabled, api_key, base_url


def create_uddoktapay_charge(uid: str, amount: float, dep_id: str):
    enabled, api_key, base_url = gateway_config()
    if not enabled:
        return None
    users = db_read(DB_USERS)
    name = users.get(uid, {}).get("username", "User")
    payload = {
        "full_name": name, "email": f"user{uid}@telegram.bot", "amount": str(amount),
        "metadata": {"user_id": uid, "dep_id": dep_id},
        "redirect_url": f"{PUBLIC_BASE_URL}/pay/success",
        "cancel_url": f"{PUBLIC_BASE_URL}/pay/cancel",
        "return_type": "GET", "webhook_url": f"{PUBLIC_BASE_URL}/pay/webhook",
    }
    headers = {"RT-UDDOKTAPAY-API-KEY": api_key, "accept": "application/json", "content-type": "application/json"}
    try:
        r = requests.post(base_url + "api/checkout-v2", json=payload, headers=headers, timeout=20)
        data = r.json()
        if data.get("status"):
            return data.get("payment_url")
    except requests.RequestException:
        pass
    return None


def verify_uddoktapay_payment(invoice_id: str):
    enabled, api_key, base_url = gateway_config()
    if not enabled:
        return None
    headers = {"RT-UDDOKTAPAY-API-KEY": api_key, "accept": "application/json", "content-type": "application/json"}
    try:
        r = requests.post(base_url + "api/verify-payment", json={"invoice_id": invoice_id}, headers=headers, timeout=20)
        return r.json()
    except requests.RequestException:
        return None


# ---- Binance Pay (Merchant API v2) ----
BINANCE_PAY_BASE = "https://bpay.binanceapi.com"


def binance_config():
    s = db_read(DB_SETTINGS).get("binance", {})
    api_key = s.get("api_key", "")
    api_secret = s.get("api_secret", "")
    currency = s.get("currency", "USDT")
    enabled = s.get("enabled", False) and bool(api_key) and bool(api_secret) and bool(PUBLIC_BASE_URL)
    return enabled, api_key, api_secret, currency


def _binance_signature(secret: str, timestamp: str, nonce: str, body: str) -> str:
    payload = f"{timestamp}\n{nonce}\n{body}\n"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest().upper()


def _binance_nonce(n=32):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def create_binance_order(uid: str, amount: float, dep_id: str):
    enabled, api_key, api_secret, currency = binance_config()
    if not enabled:
        return None
    timestamp = str(int(time.time() * 1000))
    nonce = _binance_nonce()
    body = json.dumps({
        "env": {"terminalType": "WEB"},
        "merchantTradeNo": dep_id,
        "orderAmount": round(amount, 2),
        "currency": currency,
        "goods": {
            "goodsType": "02", "goodsCategory": "Z000",
            "referenceGoodsId": dep_id, "goodsName": "Wallet Top-up",
        },
        "webhookUrl": f"{PUBLIC_BASE_URL}/pay/binance/webhook",
    }, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": api_key,
        "BinancePay-Signature": _binance_signature(api_secret, timestamp, nonce, body),
    }
    try:
        r = requests.post(f"{BINANCE_PAY_BASE}/binancepay/openapi/v2/order", data=body, headers=headers, timeout=20)
        data = r.json()
        if data.get("status") == "SUCCESS":
            return data.get("data", {}).get("checkoutUrl")
    except requests.RequestException:
        pass
    return None


def verify_binance_webhook_signature(timestamp: str, nonce: str, raw_body: str, signature: str) -> bool:
    _, _, api_secret, _ = binance_config()
    if not api_secret:
        return False
    expected = _binance_signature(api_secret, timestamp, nonce, raw_body)
    return hmac.compare_digest(expected, signature or "")


# =====================================================================
# 7. BUSINESS LOGIC
# =====================================================================

def execute_proxy_purchase(uid: str, ckey: str, qty: int):
    categories = db_read(DB_CATEGORIES)
    if ckey not in categories:
        return {"err": "category"}
    cat = categories[ckey]
    cost = round(cat["price"] * qty, 2)

    def fn(users: dict):
        if uid not in users:
            return {"err": "user"}
        u = users[uid]
        if u.get("balance", 0) < cost:
            return {"err": "balance", "need": cost, "have": u.get("balance", 0)}
        proxies = take_proxies(ckey, qty)
        if proxies is None:
            return {"err": "stock"}
        u["balance"] = round(u["balance"] - cost, 2)
        u["total_spent"] = round(u.get("total_spent", 0) + cost, 2)
        u.setdefault("orders", []).insert(0, {
            "time": datetime.now(TZ).strftime("%d %b %Y, %I:%M:%S %p"), "type": cat["name"],
            "qty": qty, "cost": cost, "proxies": proxies,
        })
        u["orders"] = u["orders"][:KEEP_ORDERS]
        return {"ok": True, "proxies": proxies, "cost": cost, "balance": u["balance"], "name": cat["name"]}

    res = db_update(DB_USERS, fn)
    if res and res.get("ok"):
        log_event("PURCHASE_PROXY", f"user={uid} cat={ckey} qty={qty} cost={cost}")
        left = stock_count(ckey)
        if left <= LOW_STOCK_ALERT:
            notify_admins(f"⚠️ <b>লো স্টক!</b>\n{h(cat['name'])} — বাকি আছে: <b>{left}</b>")
    return res or {"err": "unknown"}


def execute_catalog_purchase(uid: str, group: str, cat_key: str, prod_key: str):
    catalog = get_catalog(group)
    cat = catalog.get(cat_key)
    prod = (cat or {}).get("products", {}).get(prod_key)
    if not cat or not prod:
        return {"err": "category"}
    cost = round(float(prod["price"]), 2)
    label = f"{cat['name']} — {prod['name']}"

    def fn(users: dict):
        if uid not in users:
            return {"err": "user"}
        u = users[uid]
        if u.get("balance", 0) < cost:
            return {"err": "balance", "need": cost, "have": u.get("balance", 0)}
        account = unit_take_one(group, cat_key, prod_key)
        if account is None:
            return {"err": "stock"}
        u["balance"] = round(u["balance"] - cost, 2)
        u["total_spent"] = round(u.get("total_spent", 0) + cost, 2)
        u.setdefault("orders", []).insert(0, {
            "time": datetime.now(TZ).strftime("%d %b %Y, %I:%M:%S %p"), "type": label,
            "qty": 1, "cost": cost, "proxies": [account],
        })
        u["orders"] = u["orders"][:KEEP_ORDERS]
        return {"ok": True, "account": account, "cost": cost, "balance": u["balance"], "name": label}

    res = db_update(DB_USERS, fn)
    if res and res.get("ok"):
        log_event("PURCHASE_CATALOG", f"user={uid} group={group} cat={cat_key} prod={prod_key} cost={cost}")
        left = unit_stock_count(group, cat_key, prod_key)
        if left <= LOW_STOCK_ALERT:
            notify_admins(f"⚠️ <b>লো স্টক!</b>\n{h(label)} — বাকি আছে: <b>{left}</b>")
    return res or {"err": "unknown"}


def deliver_proxies(chat_id, title: str, proxies: list):
    if len(proxies) <= INLINE_DELIVERY_LIMIT:
        reply(chat_id, f"✅ <b>{h(title)}</b>\n\n<code>{h(chr(10).join(proxies))}</code>")
    else:
        send_text_file(chat_id, f"order_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.txt",
                        "\n".join(proxies) + "\n", f"✅ <b>{h(title)}</b> — {len(proxies)} টি")


def deposit_caption(d: dict, dep_id: str) -> str:
    status_map = {"pending": "⏳ অপেক্ষমান", "approved": "✅ অনুমোদিত", "rejected": "❌ বাতিল"}
    return (
        f"🔔 <b>ডিপোজিট রিকোয়েস্ট</b> #{dep_id}\n\n"
        f"👤 ইউজার: <code>{d['user']}</code>\n"
        f"💰 পরিমাণ: <b>{fmt(d['amount'])} {h(d['currency'])}</b>\n"
        f"🏦 মেথড: {h((d.get('method') or '-').upper())}\n"
        f"📝 TrxID: <code>{h(d['trx'])}</code>\n"
        f"📌 স্ট্যাটাস: {status_map.get(d['status'], d['status'])}"
    )


# credit_deposit, process_manual_deposit and stats_text now live in logic.py
# (shared with the web console) — see the import at the top of this file.


# =====================================================================
# 8. VIEWS
# =====================================================================

def show_admin_panel(chat_id, mid: int = None):
    st = db_read(DB_SETTINGS).get("bot_status", "ON")
    pending = sum(1 for d in db_read(DB_DEPOSITS).values() if d["status"] == "pending")
    reply(chat_id, "🛠️ <b>অ্যাডমিন প্যানেল</b>\nবট স্ট্যাটাস: " + ("🟢 চালু" if st == "ON" else "🔴 বন্ধ"), ikb([
        [btn("📊 পরিসংখ্যান", "adm_stats"), btn("👥 মেম্বার", "admmem_0")],
        [btn(f"⏳ অপেক্ষমান ডিপোজিট ({pending})", "adm_pending")],
        [btn("📢 ব্রডকাস্ট", "admin_broadcast")],
        [btn("🗂 প্রক্সি ক্যাটাগরি", "adm_cats"), btn("📥 প্রক্সি স্টক", "admin_upload_select_cat")],
        [btn("🛡️ VPN ম্যানেজ", "admcat_vpn"), btn("💎 Premium ম্যানেজ", "admcat_premium")],
        [btn("⚙️ সেটিংস", "adms_home")],
        [btn("🔴 বট বন্ধ করুন" if st == "ON" else "🟢 বট চালু করুন", "adm_toggle")],
        [btn("❌ বন্ধ", "main_menu")],
    ]), mid)


def show_settings_home(chat_id, mid=None):
    reply(chat_id, "⚙️ <b>সেটিংস</b>", ikb([
        [btn("🎧 সাপোর্ট লিংক", "adms_support")],
        [btn("🔐 Force-Join চ্যানেল/গ্রুপ", "adms_fj")],
        [btn("🎁 রেফারেল সেটিংস", "adms_ref")],
        [btn("💳 পেমেন্ট গেটওয়ে (UddoktaPay)", "adms_gw")],
        [btn("💛 পেমেন্ট গেটওয়ে (Binance Pay)", "adms_bn")],
        [btn("💰 ডিপোজিট নম্বর (bKash/Nagad)", "adms_pay")],
        [btn("📝 টেক্সট এডিট", "adms_texts")],
        [btn("🎨 Premium Emoji", "adms_emoji")],
        [btn("🔙 ড্যাশবোর্ড", "adm_back")],
    ]), mid)


def show_settings_support(chat_id, mid=None):
    link = db_read(DB_SETTINGS).get("support_link") or "(সেট করা নেই)"
    reply(chat_id, f"🎧 <b>সাপোর্ট লিংক</b>\n\nবর্তমান: {h(link)}", ikb([
        [btn("✏️ পরিবর্তন করুন", "adms_support_edit")],
        [btn("🔙 সেটিংস", "adms_home")],
    ]), mid)


def show_settings_fj(chat_id, mid=None):
    entries = db_read(DB_SETTINGS).get("force_join", [])
    rows = [[btn(f"🗑 {e['chat']}", f"fjdel_{i}")] for i, e in enumerate(entries)]
    rows.append([btn("➕ নতুন চ্যানেল/গ্রুপ যোগ করুন", "fjadd_start")])
    rows.append([btn("🔙 সেটিংস", "adms_home")])
    txt = "🔐 <b>Force-Join তালিকা</b>\n\n" + ("\n".join(f"{i}. {h(e['chat'])}" for i, e in enumerate(entries)) if entries else "কোনো এন্ট্রি নেই — বাধ্যতামূলক জয়েন বন্ধ আছে।")
    txt += "\n\n⚠️ বটকে অবশ্যই ওই চ্যানেল/গ্রুপে অ্যাডমিন বানাতে হবে।"
    reply(chat_id, txt, ikb(rows), mid)


def show_settings_ref(chat_id, mid=None):
    s = db_read(DB_SETTINGS)
    reply(chat_id, f"🎁 <b>রেফারেল সেটিংস</b>\n\nকমিশন: <b>{fmt(s.get('referral_percent',5))}%</b>\nমেয়াদ: <b>{int(s.get('referral_valid_days',365))} দিন</b>",
          ikb([[btn("✏️ পরিবর্তন করুন", "adms_ref_edit")], [btn("🔙 সেটিংস", "adms_home")]]), mid)


def show_settings_gw(chat_id, mid=None):
    s = db_read(DB_SETTINGS).get("uddoktapay", {})
    enabled = s.get("enabled", True)
    api_key = s.get("api_key") or UDDOKTAPAY_API_KEY_ENV
    base_url = s.get("base_url") or UDDOKTAPAY_BASE_URL_ENV
    is_sandbox = "sandbox" in base_url
    reply(chat_id, f"💳 <b>পেমেন্ট গেটওয়ে (UddoktaPay)</b>\n\n"
                   f"স্ট্যাটাস: {'🟢 চালু' if enabled else '🔴 বন্ধ'}\n"
                   f"মোড: {'🧪 Sandbox (টেস্ট)' if is_sandbox else '🟢 Live'}\n"
                   f"API Key: <code>{mask_key(api_key)}</code>\nBase URL: {h(base_url)}\n\n"
                   f"{'⚠️ PUBLIC_BASE_URL এনভায়রনমেন্ট ভ্যারিয়েবল সেট নেই — গেটওয়ে কাজ করবে না।' if not PUBLIC_BASE_URL else ''}",
          ikb([
              [btn("🔴 বন্ধ করুন" if enabled else "🟢 চালু করুন", "adms_gw_toggle")],
              [btn("🔑 API Key পরিবর্তন", "adms_gw_key")],
              [btn("🌐 Base URL পরিবর্তন", "adms_gw_url")],
              [btn("🔙 সেটিংস", "adms_home")],
          ]), mid)


def show_settings_binance(chat_id, mid=None):
    s = db_read(DB_SETTINGS).get("binance", {})
    enabled = s.get("enabled", False)
    api_key = s.get("api_key", "")
    api_secret = s.get("api_secret", "")
    currency = s.get("currency", "USDT")
    reply(chat_id, f"💛 <b>Binance Pay গেটওয়ে</b>\n\n"
                   f"স্ট্যাটাস: {'🟢 চালু' if enabled else '🔴 বন্ধ'}\n"
                   f"API Key: <code>{mask_key(api_key)}</code>\nAPI Secret: <code>{mask_key(api_secret)}</code>\n"
                   f"কারেন্সি: {h(currency)}\n\n"
                   f"{'⚠️ PUBLIC_BASE_URL সেট নেই — গেটওয়ে কাজ করবে না।' if not PUBLIC_BASE_URL else ''}\n"
                   f"{'⚠️ API Key/Secret সেট করুন এবং Binance Merchant Admin Portal-এ webhook URL যোগ করুন:' if not (api_key and api_secret) else 'Webhook URL:'}\n"
                   f"<code>{h(PUBLIC_BASE_URL)}/pay/binance/webhook</code>",
          ikb([
              [btn("🔴 বন্ধ করুন" if enabled else "🟢 চালু করুন", "adms_bn_toggle")],
              [btn("🔑 API Key পরিবর্তন", "adms_bn_key")],
              [btn("🔒 API Secret পরিবর্তন", "adms_bn_secret")],
              [btn("💱 কারেন্সি পরিবর্তন (USDT/BUSD)", "adms_bn_currency")],
              [btn("🔙 সেটিংস", "adms_home")],
          ]), mid)


def show_settings_pay(chat_id, mid=None):
    accounts = db_read(DB_SETTINGS).get("deposit_accounts", {})
    rows = [[btn(f"✏️ {m.upper()}: {n}", f"adms_pay_edit_{m}")] for m, n in accounts.items()]
    rows.append([btn("➕ নতুন মেথড যোগ করুন", "adms_pay_new")])
    rows.append([btn("🔙 সেটিংস", "adms_home")])
    reply(chat_id, "💰 <b>ডিপোজিট নম্বর</b>\n\nম্যানুয়াল পেমেন্টের সময় এই নম্বরগুলো দেখানো হয়।", ikb(rows), mid)


def show_settings_texts(chat_id, mid=None):
    keys = list_text_keys()
    rows = [[btn(k, f"adms_txt_{k}")] for k in keys]
    rows.append([btn("🔙 সেটিংস", "adms_home")])
    reply(chat_id, "📝 <b>টেক্সট এডিট</b>\n\nযে টেক্সটটা এডিট করতে চান সেটায় চাপুন:", ikb(rows), mid)


def show_settings_text_detail(chat_id, key, mid=None):
    bn = get_text(key, "bn")
    en = get_text(key, "en")
    reply(chat_id, f"📝 <b>{h(key)}</b>\n\n🇧🇩 <b>বাংলা:</b>\n{h(bn)}\n\n🇬🇧 <b>English:</b>\n{h(en)}",
          ikb([[btn("✏️ বাংলা এডিট করুন", f"adms_txtedit_{key}_bn"), btn("✏️ Edit English", f"adms_txtedit_{key}_en")],
               [btn("🔙 টেক্সট তালিকা", "adms_texts")]]), mid)


def show_settings_emoji(chat_id, mid=None):
    emojis = list_emojis()
    rows = [[btn(f"🗑 {e.get('label', k)}", f"emojidel_{k}")] for k, e in emojis.items()]
    rows.append([btn("➕ নতুন Emoji যোগ করুন", "emojiadd_start")])
    rows.append([btn("🔙 সেটিংস", "adms_home")])
    txt = ("🎨 <b>Premium Emoji</b>\n\n" +
           ("\n".join(f"{i+1}. {h(e.get('label', k))} — <code>{k}</code>" for i, (k, e) in enumerate(emojis.items())) if emojis else "কোনো emoji যোগ করা নেই।") +
           "\n\nটেক্সট এডিট করার সময় <code>{emoji:key}</code> লিখলে সেটা এই emoji-তে বদলে যাবে (যাদের Telegram Premium আছে তারা emoji দেখবে, বাকিরা ফলব্যাক ইমোজি দেখবে)।")
    reply(chat_id, txt, ikb(rows), mid)


def show_proxy_categories(chat_id, lang, mid: int = None):
    cats = db_read(DB_CATEGORIES)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    rows = []
    for k, c in cats.items():
        stk = stock_count(k)
        label = f"🌐 {c['name']} — {fmt(c['price'])} {cur} | " + (f"Stock: {stk}" if stk > 0 else ("স্টক শেষ" if lang == "bn" else "Out of stock"))
        rows.append([btn(label, f"buycat_{k}")])
    if not rows:
        reply(chat_id, "এই মুহূর্তে কোনো ক্যাটাগরি নেই।" if lang == "bn" else "No categories yet.", None, mid)
        return
    rows.append(cancel_row(lang))
    reply(chat_id, "📦 <b>ক্যাটাগরি সিলেক্ট করুন:</b>" if lang == "bn" else "📦 <b>Select a category:</b>", ikb(rows), mid)


def show_proxy_confirm(chat_id, uid, lang, ckey, qty, mid=None):
    cats = db_read(DB_CATEGORIES)
    users = db_read(DB_USERS)
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    if ckey not in cats:
        reply(chat_id, "❌ ক্যাটাগরি পাওয়া যায়নি।", None, mid)
        return
    if qty < 1 or qty > MAX_ORDER_QTY:
        reply(chat_id, f"❌ পরিমাণ ১ থেকে {MAX_ORDER_QTY} এর মধ্যে হতে হবে।", ikb([cancel_row(lang)]), mid)
        return
    stock = stock_count(ckey)
    if stock < qty:
        reply(chat_id, f"❌ পর্যাপ্ত স্টক নেই! বর্তমান স্টক: <b>{stock}</b>", ikb([cancel_row(lang)]), mid)
        return
    cost = round(cats[ckey]["price"] * qty, 2)
    bal = float(users.get(uid, {}).get("balance", 0))
    if bal < cost:
        reply(chat_id, f"❌ পর্যাপ্ত ব্যালেন্স নেই!\nপ্রয়োজন: <b>{fmt(cost)} {cur}</b>\nআপনার ব্যালেন্স: <b>{fmt(bal)} {cur}</b>",
              ikb([[btn(t('deposit', lang), "dep_menu")], cancel_row(lang)]), mid)
        return
    reply(chat_id, f"🛒 <b>অর্ডার কনফার্মেশন</b>\n\n📦 {h(cats[ckey]['name'])}\n🔢 পরিমাণ: <b>{qty}</b>\n"
                   f"💵 মোট খরচ: <b>{fmt(cost)} {cur}</b>\n👛 ব্যালেন্স: {fmt(bal)} {cur}",
          ikb([[btn(t('confirm', lang), f"buyok_{ckey}_{qty}"), btn(t('cancel', lang), "main_menu")]]), mid)


def show_catalog_categories(chat_id, group, lang, mid=None):
    cats = get_catalog(group)
    title = ("🛡️ VPN Store" if group == "vpn" else "💎 Premium Subscription Store")
    prompt = "আপনার পছন্দের Category নির্বাচন করুন।" if lang == "bn" else "Select a category."
    rows = [[btn(f"🗂 {c['name']} ({len(c.get('products', {}))})", f"catcat_{group}_{ck}")] for ck, c in cats.items()]
    if not rows:
        reply(chat_id, f"<b>{title}</b>\n\n" + ("এই মুহূর্তে কোনো ক্যাটাগরি নেই।" if lang == "bn" else "No categories yet."), None, mid)
        return
    rows.append(cancel_row(lang))
    reply(chat_id, f"<b>{title}</b>\n{prompt}", ikb(rows), mid)


def show_catalog_products(chat_id, group, cat_key, lang, mid=None):
    cats = get_catalog(group)
    cat = cats.get(cat_key)
    if not cat:
        reply(chat_id, "❌", None, mid)
        return
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    prompt = "থেকে Product নির্বাচন করুন।" if lang == "bn" else "Select a product."
    rows = []
    for pk, p in cat.get("products", {}).items():
        stk = unit_stock_count(group, cat_key, pk)
        label = f"{p['name']} — {fmt(p['price'])} {cur}" + ("" if stk > 0 else (" (স্টক শেষ)" if lang == "bn" else " (Out of stock)"))
        rows.append([btn(label, f"catprod_{group}_{cat_key}_{pk}")])
    rows.append([btn(t("back", lang), f"catback_{group}"), btn(t("cancel", lang), "main_menu")])
    reply(chat_id, f"<b>{h(cat['name'])}</b>\n{prompt}", ikb(rows), mid)


def show_catalog_confirm(chat_id, uid, lang, group, cat_key, prod_key, mid=None):
    cats = get_catalog(group)
    cat = cats.get(cat_key)
    prod = (cat or {}).get("products", {}).get(prod_key)
    if not cat or not prod:
        reply(chat_id, "❌", None, mid)
        return
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    stk = unit_stock_count(group, cat_key, prod_key)
    if stk < 1:
        reply(chat_id, "❌ স্টক শেষ!" if lang == "bn" else "❌ Out of stock!", ikb([cancel_row(lang)]), mid)
        return
    price = round(float(prod["price"]), 2)
    bal = float(db_read(DB_USERS).get(uid, {}).get("balance", 0))
    if bal < price:
        reply(chat_id, ("❌ পর্যাপ্ত ব্যালেন্স নেই!" if lang == "bn" else "❌ Insufficient balance!") + f"\n{fmt(price)} {cur} / {fmt(bal)} {cur}",
              ikb([[btn(t('deposit', lang), "dep_menu")], cancel_row(lang)]), mid)
        return
    reply(chat_id,
          f"🛒 <b>{('অর্ডার কনফার্মেশন' if lang=='bn' else 'Order Confirmation')}</b>\n\n"
          f"Product: {h(prod['name'])}\nCategory: {h(cat['name'])}\nPrice: <b>{fmt(price)} {cur}</b>\n"
          f"{'আপনার ব্যালেন্স' if lang=='bn' else 'Your balance'}: {fmt(bal)} {cur}",
          ikb([[btn(t('confirm', lang), f"catbuy_{group}_{cat_key}_{prod_key}"), btn(t('cancel', lang), "main_menu")]]), mid)


def show_deposit_menu(chat_id, lang, mid: int = None):
    prompt = get_text("deposit_prompt", lang, min=fmt(MIN_DEPOSIT))
    rows = [
        [btn("10 BDT", "depamt_10"), btn("50 BDT", "depamt_50")],
        [btn("100 BDT", "depamt_100"), btn("500 BDT", "depamt_500")],
        [btn(("✍️ কাস্টম পরিমাণ" if lang == "bn" else "✍️ Custom amount"), "deposit_custom_prompt")],
        cancel_row(lang),
    ]
    reply(chat_id, prompt, ikb(rows), mid)


def start_gateway_deposit(chat_id, uid, lang, amount, mid=None):
    s = db_read(DB_SETTINGS)
    cur = s.get("currency", "BDT")
    dep_id = binascii.hexlify(os.urandom(4)).decode()[:6].upper()

    def fn(deps: dict):
        deps[dep_id] = {"user": uid, "amount": amount, "currency": cur, "trx": "-",
                         "method": "gateway", "status": "pending", "created": now_str()}
    db_update(DB_DEPOSITS, fn)

    payment_url = create_uddoktapay_charge(uid, amount, dep_id)
    binance_enabled, _, _, binance_currency = binance_config()
    binance_url = create_binance_order(uid, amount, dep_id) if binance_enabled else None

    rows = []
    if payment_url:
        rows.append([url_btn(t("pay_now", lang), payment_url)])
    if binance_url:
        rows.append([url_btn(("💛 Binance Pay দিয়ে পে করুন" if lang == "bn" else "💛 Pay with Binance Pay"), binance_url)])
    rows.append([btn(t("manual_pay", lang), f"depmanual_{amount}")])
    rows.append(cancel_row(lang))

    if payment_url or binance_url:
        note = ("নিচের বাটনে চেপে পেমেন্ট সম্পূর্ণ করুন। পেমেন্ট হলেই ব্যালেন্স অটোমেটিক যোগ হয়ে যাবে।" if lang == "bn" else
                "Tap a button below to complete payment. Your balance will be credited automatically once payment succeeds.")
        reply(chat_id, f"💰 {'পরিমাণ' if lang=='bn' else 'Amount'}: <b>{fmt(amount)} {cur}</b>\n\n{note}", ikb(rows), mid)
    else:
        start_manual_deposit(chat_id, uid, lang, amount, mid)


def start_manual_deposit(chat_id, uid, lang, amount, mid=None):
    s = db_read(DB_SETTINGS)
    cur = s.get("currency", "BDT")
    accounts = "".join(f"💳 {h(method.upper())}: <code>{h(num)}</code>\n" for method, num in s.get("deposit_accounts", {}).items())
    set_state(uid, "dep_trx", {"amount": amount})
    reply(chat_id, f"{accounts}\n💰 {'পরিমাণ' if lang=='bn' else 'Amount'}: <b>{fmt(amount)} {cur}</b>\n\n" +
          ("উপরের নম্বরে Send Money করে TrxID পাঠান (ক্রিপ্টো/Binance পেমেন্ট হলে TxHash পাঠান):"
           if lang == "bn" else
           "Send Money to the number above, then send the TrxID (for crypto/Binance, send the TxHash):"),
          ikb([cancel_row(lang)]), mid)


def show_history(chat_id, user, lang):
    orders = user.get("orders", [])
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    if not orders:
        reply(chat_id, "📦 এখনো কোনো অর্ডার নেই।" if lang == "bn" else "📦 No orders yet.", None)
        return
    txt = ("📜 <b>সাম্প্রতিক অর্ডার</b>\n\n" if lang == "bn" else "📜 <b>Recent Orders</b>\n\n")
    rows = []
    for i, o in enumerate(orders[:HISTORY_LIMIT]):
        n = i + 1
        txt += f"{n}. {h(o['type'])} — {o['qty']} | {fmt(o['cost'])} {cur}\n    🕒 {o['time']}\n"
        if i < 5:
            rows.append([btn(f"📥 #{n}", f"ord_{i}")])
    reply(chat_id, txt, ikb(rows) if rows else None)


def show_language_menu(chat_id, lang, mid=None):
    reply(chat_id, "🌐 ভাষা নির্বাচন করুন / Select your language:", ikb([
        [btn("🇧🇩 বাংলা", "lang_bn"), btn("🇬🇧 English", "lang_en")],
        cancel_row(lang),
    ]), mid)


# =====================================================================
# 9. ADMIN COMMANDS (catalog creation only — everything else is buttons)
# =====================================================================

def admin_command(text: str, chat_id) -> bool:
    p = re.split(r"\s+", text.strip())
    cmd = p[0].split("@")[0].lower()
    cur = db_read(DB_SETTINGS).get("currency", "BDT")

    if cmd == "/admin":
        show_admin_panel(chat_id)
        return True

    if cmd == "/stats":
        reply(chat_id, stats_text())
        return True

    if cmd in ("/addbalance", "/cutbalance"):
        if len(p) < 3 or not p[1].isdigit() or not _is_number(p[2]) or float(p[2]) <= 0:
            reply(chat_id, f"ব্যবহার: <code>{cmd} USER_ID AMOUNT</code>")
            return True
        delta = float(p[2]) * (1 if cmd == "/addbalance" else -1)
        new = adjust_balance(p[1], delta)
        if new is None:
            reply(chat_id, "❌ ইউজার পাওয়া যায়নি।")
            return True
        reply(chat_id, f"✅ সম্পন্ন। নতুন ব্যালেন্স: <b>{fmt(new)} {cur}</b>")
        if delta > 0:
            reply(p[1], f"💰 আপনার ব্যালেন্সে <b>{fmt(delta)} {cur}</b> যোগ করা হয়েছে।")
        log_event("ADMIN_BALANCE", f"target={p[1]} delta={delta}")
        return True

    if cmd in ("/ban", "/unban"):
        if len(p) < 2 or not p[1].isdigit():
            reply(chat_id, f"ব্যবহার: <code>{cmd} USER_ID</code>")
            return True
        ban = cmd == "/ban"
        def fn(u):
            if p[1] not in u:
                return False
            u[p[1]]["banned"] = ban
            return True
        ok = db_update(DB_USERS, fn)
        reply(chat_id, ("🚫 ইউজার ব্যান করা হয়েছে।" if ban else "✅ ইউজার আনব্যান করা হয়েছে।") if ok else "❌ ইউজার পাওয়া যায়নি।")
        return True

    if cmd == "/userinfo":
        if len(p) < 2:
            reply(chat_id, "ব্যবহার: <code>/userinfo USER_ID</code>")
            return True
        u = db_read(DB_USERS).get(p[1])
        if not u:
            reply(chat_id, "❌ ইউজার পাওয়া যায়নি।")
            return True
        tgu = f" (@{h(u['tg_username'])})" if u.get("tg_username") else ""
        reply(chat_id, f"👤 <b>{h(u['username'])}</b>{tgu}\n🆔 <code>{p[1]}</code>\n💰 ব্যালেন্স: {fmt(u.get('balance', 0))} {cur}\n"
                       f"🛒 মোট খরচ: {fmt(u.get('total_spent', 0))} {cur}\n🏦 মোট ডিপোজিট: {fmt(u.get('total_deposited', 0))} {cur}\n"
                       f"📦 অর্ডার: {len(u.get('orders', []))}\n📅 জয়েন: {u.get('joined', '-')}\n🌐 ভাষা: {u.get('lang', 'bn')}\n"
                       f"🚫 ব্যান: {'হ্যাঁ' if u.get('banned') else 'না'}")
        return True

    return False


# =====================================================================
# 10. CALLBACK HANDLER
# =====================================================================

def handle_callback(cb: dict, uid: str, user: dict, is_adm: bool):
    cb_id = cb["id"]
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    mid = int(cb["message"]["message_id"])
    lang = get_lang(user)
    acked = {"done": False}

    def ack(text: str = "", alert: bool = False):
        if acked["done"]:
            return
        acked["done"] = True
        p = {"callback_query_id": cb_id}
        if text != "":
            p["text"] = text
            p["show_alert"] = alert
        tg("answerCallbackQuery", p)

    cur = db_read(DB_SETTINGS).get("currency", "BDT")

    try:
        if data == "main_menu":
            set_state(uid, None)
            reply(chat_id, "🏠", None, mid)
            tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                                "text": get_text("welcome", lang, name=h(user.get("username", "Friend"))),
                                "reply_markup": main_keyboard(lang, is_adm)})
            return

        if data.startswith("checkjoin_"):
            still_missing = missing_force_join(uid)
            if not still_missing:
                ack("✅")
                tg("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                tg("sendMessage", {"chat_id": chat_id, "text": "🎉", "reply_markup": main_keyboard(lang, is_adm)})
            else:
                ack("❌ Not joined yet!" if lang == "en" else "❌ আপনি এখনো জয়েন করেননি!", True)
            return

        # ---------- Proxy ----------
        if data.startswith("buycat_"):
            ckey = data[7:]
            cats = db_read(DB_CATEGORIES)
            if ckey not in cats:
                ack("Not found", True)
                return
            c = cats[ckey]
            stock = stock_count(ckey)
            if stock < 1:
                ack("❌ Out of stock!" if lang == "en" else "❌ স্টক শেষ!", True)
                return
            reply(chat_id, f"📦 <b>{h(c['name'])}</b>\n💵 {fmt(c['price'])} {cur} /each\n📊 Stock: {stock}\n\n" +
                           ("পরিমাণ সিলেক্ট করুন:" if lang == "bn" else "Select quantity:"),
                  ikb([
                      [btn("1", f"buyqty_{ckey}_1"), btn("5", f"buyqty_{ckey}_5")],
                      [btn("10", f"buyqty_{ckey}_10"), btn("20", f"buyqty_{ckey}_20")],
                      [btn(("✍️ কাস্টম" if lang == "bn" else "✍️ Custom"), f"buycust_{ckey}")],
                      [btn(t("back", lang), "buy_menu"), btn(t("cancel", lang), "main_menu")],
                  ]), mid)
            return

        if data == "buy_menu":
            show_proxy_categories(chat_id, lang, mid)
            return

        if data.startswith("buycust_"):
            set_state(uid, "buy_qty", {"cat": data[8:]})
            reply(chat_id, f"✍️ {'সংখ্যায় লিখুন' if lang=='bn' else 'Enter a number'} (max {MAX_ORDER_QTY}):", ikb([cancel_row(lang)]), mid)
            return

        m = re.match(r"^buyqty_(.+)_(\d+)$", data)
        if m:
            show_proxy_confirm(chat_id, uid, lang, m.group(1), int(m.group(2)), mid)
            return

        m = re.match(r"^buyok_(.+)_(\d+)$", data)
        if m:
            res = execute_proxy_purchase(uid, m.group(1), int(m.group(2)))
            if res.get("ok"):
                ack()
                reply(chat_id, f"✅ {fmt(res['cost'])} {cur} | {'বাকি' if lang=='bn' else 'Balance'}: {fmt(res['balance'])} {cur}",
                      ikb([[btn(t("main_menu", lang), "main_menu")]]), mid)
                deliver_proxies(chat_id, res["name"], res["proxies"])
                if NOTIFICATION_CHANNEL:
                    reply(NOTIFICATION_CHANNEL, f"📦 <b>Sold!</b>\n👤 <code>{mask_id(uid)}</code>\n🏷️ {h(res['name'])} x{m.group(2)}")
            else:
                msg = {"balance": "❌ Insufficient balance!", "stock": "❌ Out of stock!", "category": "❌ Not found!"}.get(res.get("err"), "❌ Error, try again.")
                ack(msg, True)
            return

        if data.startswith("ord_"):
            idx = int(data[4:])
            orders = user.get("orders", [])
            o = orders[idx] if 0 <= idx < len(orders) else None
            if not o:
                ack("Not found", True)
                return
            ack()
            send_text_file(chat_id, f"order_{idx+1}.txt", "\n".join(o.get("proxies", [])) + "\n", f"📥 {h(o['type'])}")
            return

        # ---------- VPN / Premium catalog ----------
        m = re.match(r"^catcat_(vpn|premium)_(.+)$", data)
        if m:
            show_catalog_products(chat_id, m.group(1), m.group(2), lang, mid)
            return

        m = re.match(r"^catback_(vpn|premium)$", data)
        if m:
            show_catalog_categories(chat_id, m.group(1), lang, mid)
            return

        m = re.match(r"^catprod_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            show_catalog_confirm(chat_id, uid, lang, m.group(1), m.group(2), m.group(3), mid)
            return

        m = re.match(r"^catbuy_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            group, cat_key, prod_key = m.group(1), m.group(2), m.group(3)
            res = execute_catalog_purchase(uid, group, cat_key, prod_key)
            if res.get("ok"):
                ack()
                order_id = f"{group.upper()}{binascii.hexlify(os.urandom(4)).decode()[:8].upper()}"
                reply(chat_id, f"✅ {'অর্ডার সম্পন্ন' if lang=='bn' else 'Order completed'}\n\n"
                               f"{'প্রোডাক্ট' if lang=='bn' else 'Product'}: {h(res['name'])}\nOrder ID: <code>{order_id}</code>\n\n"
                               f"📋 <b>{'ডিটেইলস' if lang=='bn' else 'Details'}:</b>\n<code>{h(res['account'])}</code>",
                      ikb([[btn(t("main_menu", lang), "main_menu")]]), mid)
                if NOTIFICATION_CHANNEL:
                    reply(NOTIFICATION_CHANNEL, f"📦 <b>Sold!</b>\n👤 <code>{mask_id(uid)}</code>\n🏷️ {h(res['name'])}")
            else:
                msg = {"balance": "❌ Insufficient balance!", "stock": "❌ Out of stock!", "category": "❌ Not found!"}.get(res.get("err"), "❌ Error.")
                ack(msg, True)
            return

        # ---------- Deposit ----------
        if data == "dep_menu":
            show_deposit_menu(chat_id, lang, mid)
            return

        if data == "deposit_custom_prompt":
            set_state(uid, "dep_amount")
            reply(chat_id, get_text("deposit_prompt", lang, min=fmt(MIN_DEPOSIT)), ikb([cancel_row(lang)]), mid)
            return

        if data.startswith("depamt_"):
            start_gateway_deposit(chat_id, uid, lang, float(data[7:]), mid)
            return

        if data.startswith("depmanual_"):
            start_manual_deposit(chat_id, uid, lang, float(data[10:]), mid)
            return

        # ---------- Language ----------
        if data.startswith("lang_"):
            new_lang = data[5:]
            set_lang(uid, new_lang)
            ack("✅")
            tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                                "text": get_text("welcome", new_lang, name=h(user.get("username", "Friend"))),
                                "reply_markup": main_keyboard(new_lang, is_adm)})
            return

        # ---------- Admin: deposit approval ----------
        m = re.match(r"^dep_(ok|no)_([A-Z0-9]+)$", data)
        if is_adm and m:
            r = process_manual_deposit(m.group(2), m.group(1) == "ok", uid)
            if not r["dep"]:
                ack("Not found", True)
                return
            if not r["changed"]:
                ack("⚠️ Already processed!", True)
            caption = deposit_caption(r["dep"], m.group(2))
            if cb["message"].get("photo"):
                tg("editMessageCaption", {"chat_id": chat_id, "message_id": mid, "caption": caption,
                                           "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []}})
            else:
                reply(chat_id, caption, ikb([[btn("🔙 প্যানেল", "adm_back")]]), mid)
            return

        if not is_adm:
            return

        # ---------- Admin panel core ----------
        if data == "adm_back":
            show_admin_panel(chat_id, mid)
            return

        if data == "adm_stats":
            reply(chat_id, stats_text(), ikb([[btn("🔙 ড্যাশবোর্ড", "adm_back")]]), mid)
            return

        if data == "adm_toggle":
            def fn(s):
                s["bot_status"] = "OFF" if s.get("bot_status", "ON") == "ON" else "ON"
                return s["bot_status"]
            new = db_update(DB_SETTINGS, fn)
            ack("🟢 ON" if new == "ON" else "🔴 OFF")
            show_admin_panel(chat_id, mid)
            return

        if data.startswith("admmem_"):
            page = max(0, int(data[7:]))
            users = db_read(DB_USERS)
            ids = list(users.keys())
            total = len(ids)
            sl = ids[page * MEMBERS_PER_PAGE: page * MEMBERS_PER_PAGE + MEMBERS_PER_PAGE]
            txt = f"👥 <b>মোট মেম্বার: {total}</b> (পেজ {page+1})\n\n"
            for i, uid2 in enumerate(sl):
                u = users[uid2]
                txt += f"{page*MEMBERS_PER_PAGE+i+1}. <code>{uid2}</code> {h(u.get('username',''))} — {fmt(u.get('balance',0))} {cur}" + (" 🚫" if u.get("banned") else "") + "\n"
            nav = []
            if page > 0:
                nav.append(btn("⬅️", f"admmem_{page-1}"))
            if (page + 1) * MEMBERS_PER_PAGE < total:
                nav.append(btn("➡️", f"admmem_{page+1}"))
            rows = ([nav] if nav else []) + [[btn("🔙 ড্যাশবোর্ড", "adm_back")]]
            reply(chat_id, txt, ikb(rows), mid)
            return

        if data == "adm_pending":
            deps = db_read(DB_DEPOSITS)
            rows, n = [], 0
            txt = "⏳ <b>অপেক্ষমান ডিপোজিট</b>\n\n"
            for dep_id in reversed(list(deps.keys())):
                d = deps[dep_id]
                if d["status"] != "pending":
                    continue
                n += 1
                if n > 10:
                    break
                txt += f"#{dep_id} — <code>{d['user']}</code> — {fmt(d['amount'])} {cur} — {h((d.get('method') or '-').upper())} — TrxID: <code>{h(d['trx'])}</code>\n"
                rows.append([btn(f"✅ #{dep_id}", f"dep_ok_{dep_id}"), btn(f"❌ #{dep_id}", f"dep_no_{dep_id}")])
            if n == 0:
                txt += "কোনো অপেক্ষমান রিকোয়েস্ট নেই।"
            rows.append([btn("🔙 ড্যাশবোর্ড", "adm_back")])
            reply(chat_id, txt, ikb(rows), mid)
            return

        if data == "admin_broadcast":
            set_state(uid, "adm_broadcast")
            reply(chat_id, "📢 ব্রডকাস্ট করতে যেকোনো মেসেজ পাঠান:", ikb([cancel_row(lang)]), mid)
            return

        # ---- proxy category admin ----
        if data == "admin_add_category":
            set_state(uid, "adm_cat_name")
            reply(chat_id, "✍️ নতুন প্রক্সি ক্যাটাগরির নাম লিখুন:", ikb([cancel_row(lang)]), mid)
            return

        if data == "admin_upload_select_cat":
            rows = [[btn(f"📂 {c['name']} ({stock_count(k)})", f"admupl_{k}")] for k, c in db_read(DB_CATEGORIES).items()]
            rows.append(cancel_row(lang))
            reply(chat_id, "📥 প্রক্সি ক্যাটাগরি সিলেক্ট করুন:", ikb(rows), mid)
            return

        if data.startswith("admupl_"):
            ckey = data[7:]
            if ckey not in db_read(DB_CATEGORIES):
                return
            set_state(uid, "adm_upload", {"cat": ckey})
            reply(chat_id, "📥 প্রক্সিগুলো পেস্ট করুন অথবা .txt ফাইল পাঠান:", ikb([cancel_row(lang)]), mid)
            return

        if data == "adm_cats":
            rows = [[btn(f"🗂 {c['name']}", f"admpcat_{k}")] for k, c in db_read(DB_CATEGORIES).items()]
            rows.append([btn("➕ নতুন ক্যাটাগরি", "admin_add_category")])
            rows.append([btn("🔙 ড্যাশবোর্ড", "adm_back")])
            reply(chat_id, "🗂 <b>প্রক্সি ক্যাটাগরি ম্যানেজ করুন:</b>", ikb(rows), mid)
            return

        if data.startswith("admpcat_"):
            ckey = data[8:]
            c = db_read(DB_CATEGORIES).get(ckey)
            if not c:
                return
            reply(chat_id, f"🗂 <b>{h(c['name'])}</b>\nকী: <code>{ckey}</code>\nমূল্য: {fmt(c['price'])} {cur}\nস্টক: {stock_count(ckey)}",
                  ikb([[btn("💰 মূল্য পরিবর্তন", f"admprice_{ckey}")],
                       [btn("🗑 ডিলিট", f"admdel_{ckey}")],
                       [btn("🔙 পেছনে", "adm_cats")]]), mid)
            return

        if data.startswith("admprice_"):
            set_state(uid, "adm_price", {"cat": data[9:]})
            reply(chat_id, "💰 নতুন মূল্য লিখুন:", ikb([cancel_row(lang)]), mid)
            return

        if data.startswith("admdelyes_"):
            ckey = data[10:]
            db_update(DB_CATEGORIES, lambda c: c.pop(ckey, None))
            delete_category_stock(ckey)
            log_event("CAT_DELETE", ckey)
            ack("Deleted")
            reply(chat_id, "✅ ডিলিট করা হয়েছে।", ikb([[btn("🔙 ড্যাশবোর্ড", "adm_back")]]), mid)
            return

        if data.startswith("admdel_"):
            ckey = data[7:]
            reply(chat_id, f"⚠️ ক্যাটাগরি <code>{ckey}</code> ডিলিট করবেন?", ikb([[btn("🗑 হ্যাঁ", f"admdelyes_{ckey}"), btn("❌ না", "adm_cats")]]), mid)
            return

        # ---- VPN / Premium catalog admin ----
        m = re.match(r"^admcat_(vpn|premium)$", data)
        if m:
            group = m.group(1)
            cats = get_catalog(group)
            rows = [[btn(f"🗂 {c['name']} ({len(c.get('products', {}))})", f"admcgo_{group}_{ck}")] for ck, c in cats.items()]
            rows.append([btn("➕ নতুন ক্যাটাগরি", f"admnewcat_{group}")])
            rows.append([btn("🔙 ড্যাশবোর্ড", "adm_back")])
            reply(chat_id, f"🗂 <b>{group.upper()} ক্যাটাগরি</b>", ikb(rows), mid)
            return

        m = re.match(r"^admnewcat_(vpn|premium)$", data)
        if m:
            set_state(uid, "adm_newcat_name", {"group": m.group(1)})
            reply(chat_id, "✍️ নতুন ক্যাটাগরির নাম লিখুন (যেমন: 7 Days VPN):", ikb([cancel_row(lang)]), mid)
            return

        m = re.match(r"^admcgo_(vpn|premium)_(.+)$", data)
        if m:
            group, cat_key = m.group(1), m.group(2)
            cat = get_catalog(group).get(cat_key, {})
            rows = [[btn(f"{p['name']} ({unit_stock_count(group, cat_key, pk)})", f"admpgo_{group}_{cat_key}_{pk}")]
                    for pk, p in cat.get("products", {}).items()]
            rows.append([btn("➕ নতুন প্রোডাক্ট", f"admnewprod_{group}_{cat_key}")])
            rows.append([btn("🔙 পেছনে", f"admcat_{group}")])
            reply(chat_id, f"🗂 <b>{h(cat.get('name',''))}</b>", ikb(rows), mid)
            return

        m = re.match(r"^admnewprod_(vpn|premium)_(.+)$", data)
        if m:
            set_state(uid, "adm_newprod_name", {"group": m.group(1), "cat": m.group(2)})
            reply(chat_id, "✍️ নতুন প্রোডাক্টের নাম লিখুন (যেমন: Cyberghost):", ikb([cancel_row(lang)]), mid)
            return

        m = re.match(r"^admpgo_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            group, cat_key, prod_key = m.group(1), m.group(2), m.group(3)
            prod = get_catalog(group).get(cat_key, {}).get("products", {}).get(prod_key, {})
            reply(chat_id, f"📦 <b>{h(prod.get('name',''))}</b>\nমূল্য: {fmt(prod.get('price',0))} {cur}\nস্টক: {unit_stock_count(group, cat_key, prod_key)}",
                  ikb([[btn("📥 স্টক আপলোড", f"admpup_{group}_{cat_key}_{prod_key}")],
                       [btn("💰 মূল্য পরিবর্তন", f"admpprice_{group}_{cat_key}_{prod_key}")],
                       [btn("🗑 ডিলিট", f"admpdel_{group}_{cat_key}_{prod_key}")],
                       [btn("🔙 পেছনে", f"admcgo_{group}_{cat_key}")]]), mid)
            return

        m = re.match(r"^admpprice_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            set_state(uid, "adm_prodprice", {"group": m.group(1), "cat": m.group(2), "prod": m.group(3)})
            reply(chat_id, "💰 নতুন মূল্য লিখুন:", ikb([cancel_row(lang)]), mid)
            return

        m = re.match(r"^admpup_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            set_state(uid, "adm_catupload", {"group": m.group(1), "cat": m.group(2), "prod": m.group(3)})
            reply(chat_id, "📥 প্রতিটা লাইনে একটা করে সম্পূর্ণ অ্যাকাউন্ট/ক্রেডেনশিয়াল পেস্ট করুন (যেমন: <code>email:pass</code>), অথবা .txt ফাইল পাঠান:",
                  ikb([cancel_row(lang)]), mid)
            return

        m = re.match(r"^admpdel_(vpn|premium)_([^_]+)_(.+)$", data)
        if m:
            delete_catalog_product(m.group(1), m.group(2), m.group(3))
            ack("Deleted")
            reply(chat_id, "✅ ডিলিট করা হয়েছে।", ikb([[btn("🔙 ড্যাশবোর্ড", "adm_back")]]), mid)
            return

        # ---- Settings home ----
        if data == "adms_home":
            show_settings_home(chat_id, mid)
            return

        # ---- Support link ----
        if data == "adms_support":
            show_settings_support(chat_id, mid)
            return
        if data == "adms_support_edit":
            set_state(uid, "adms_support_val")
            reply(chat_id, "✍️ নতুন সাপোর্ট লিংক পাঠান (যেমন: https://t.me/yourusername):", ikb([cancel_row(lang)]), mid)
            return

        # ---- Force-join ----
        if data == "adms_fj":
            show_settings_fj(chat_id, mid)
            return
        if data == "fjadd_start":
            set_state(uid, "adms_fj_chat")
            reply(chat_id, "✍️ চ্যানেল/গ্রুপের @username অথবা আইডি (-100...) পাঠান:", ikb([cancel_row(lang)]), mid)
            return
        if data.startswith("fjdel_"):
            idx = int(data[6:])
            def fn(s):
                entries = s.get("force_join", [])
                if 0 <= idx < len(entries):
                    entries.pop(idx)
            db_update(DB_SETTINGS, fn)
            ack("Deleted")
            show_settings_fj(chat_id, mid)
            return

        # ---- Referral ----
        if data == "adms_ref":
            show_settings_ref(chat_id, mid)
            return
        if data == "adms_ref_edit":
            set_state(uid, "adms_ref_percent")
            reply(chat_id, "✍️ কমিশন শতাংশ লিখুন (যেমন: 5):", ikb([cancel_row(lang)]), mid)
            return

        # ---- Gateway ----
        if data == "adms_gw":
            show_settings_gw(chat_id, mid)
            return
        if data == "adms_gw_toggle":
            def fn(s):
                s.setdefault("uddoktapay", {})
                s["uddoktapay"]["enabled"] = not s["uddoktapay"].get("enabled", True)
            db_update(DB_SETTINGS, fn)
            ack()
            show_settings_gw(chat_id, mid)
            return
        if data == "adms_gw_key":
            set_state(uid, "adms_gw_key_val")
            reply(chat_id, "✍️ UddoktaPay API Key পাঠান:", ikb([cancel_row(lang)]), mid)
            return
        if data == "adms_gw_url":
            set_state(uid, "adms_gw_url_val")
            reply(chat_id, "✍️ UddoktaPay Base URL পাঠান (যেমন: https://your-panel.com/):", ikb([cancel_row(lang)]), mid)
            return

        # ---- Binance Pay ----
        if data == "adms_bn":
            show_settings_binance(chat_id, mid)
            return
        if data == "adms_bn_toggle":
            def fn(s):
                s.setdefault("binance", {})
                s["binance"]["enabled"] = not s["binance"].get("enabled", False)
            db_update(DB_SETTINGS, fn)
            ack()
            show_settings_binance(chat_id, mid)
            return
        if data == "adms_bn_key":
            set_state(uid, "adms_bn_key_val")
            reply(chat_id, "✍️ Binance Pay API Key পাঠান (Binance Merchant Admin Portal থেকে):", ikb([cancel_row(lang)]), mid)
            return
        if data == "adms_bn_secret":
            set_state(uid, "adms_bn_secret_val")
            reply(chat_id, "✍️ Binance Pay API Secret পাঠান:", ikb([cancel_row(lang)]), mid)
            return
        if data == "adms_bn_currency":
            set_state(uid, "adms_bn_currency_val")
            reply(chat_id, "✍️ কারেন্সি লিখুন (USDT অথবা BUSD):", ikb([cancel_row(lang)]), mid)
            return

        # ---- Deposit numbers ----
        if data == "adms_pay":
            show_settings_pay(chat_id, mid)
            return
        if data == "adms_pay_new":
            set_state(uid, "adms_pay_method")
            reply(chat_id, "✍️ মেথডের নাম লিখুন (যেমন: bkash, nagad, binance):", ikb([cancel_row(lang)]), mid)
            return
        m = re.match(r"^adms_pay_edit_(.+)$", data)
        if m:
            set_state(uid, "adms_pay_number", {"method": m.group(1)})
            reply(chat_id, f"✍️ {h(m.group(1).upper())} এর নতুন নম্বর পাঠান (মুছতে 'none' লিখুন):", ikb([cancel_row(lang)]), mid)
            return

        # ---- Texts ----
        if data == "adms_texts":
            show_settings_texts(chat_id, mid)
            return
        m = re.match(r"^adms_txt_(.+)$", data)
        if m:
            show_settings_text_detail(chat_id, m.group(1), mid)
            return
        m = re.match(r"^adms_txtedit_(.+)_(bn|en)$", data)
        if m:
            set_state(uid, "adms_text_val", {"key": m.group(1), "lang": m.group(2)})
            reply(chat_id, f"✍️ '{h(m.group(1))}' ({m.group(2)}) এর নতুন টেক্সট পাঠান।\n\n"
                           "প্লেসহোল্ডার যেমন {name} {min} {percent} {days} {link} {count} {earned} {currency} অক্ষত রাখুন:",
                  ikb([cancel_row(lang)]), mid)
            return

        # ---- Premium Emoji ----
        if data == "adms_emoji":
            show_settings_emoji(chat_id, mid)
            return
        if data == "emojiadd_start":
            set_state(uid, "adm_emoji_name")
            reply(chat_id, "✍️ এই emoji-র জন্য একটা নাম/লেবেল লিখুন (শুধু নিজের চেনার জন্য, যেমন: fire, check_gold):",
                  ikb([cancel_row(lang)]), mid)
            return
        m = re.match(r"^emojidel_(.+)$", data)
        if m:
            delete_emoji(m.group(1))
            ack("Deleted")
            show_settings_emoji(chat_id, mid)
            return

    finally:
        ack()


# =====================================================================
# 11. MESSAGE HANDLER
# =====================================================================

def handle_message(msg: dict, uid: str, user: dict, is_adm: bool):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    doc = msg.get("document")
    state = user.get("state") or {}
    step = state.get("step")
    cur = db_read(DB_SETTINGS).get("currency", "BDT")
    lang = get_lang(user)
    kb = main_keyboard(lang, is_adm)

    if text.startswith("/start"):
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": get_text("welcome", lang, name=h(user.get("username", "Friend")))})
        return
    if text == "/cancel":
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "❌", "reply_markup": kb})
        return

    if is_adm and text.startswith("/") and admin_command(text, chat_id):
        return

    def matches(key):
        return text in (BTN[key]["bn"], BTN[key]["en"])

    if any(matches(k) for k in ("profile", "vpn", "proxy", "premium", "deposit", "support", "refer", "lang", "admin")):
        if step is not None:
            set_state(uid, None)
        if matches("vpn"):
            show_catalog_categories(chat_id, "vpn", lang)
        elif matches("premium"):
            show_catalog_categories(chat_id, "premium", lang)
        elif matches("proxy"):
            show_proxy_categories(chat_id, lang)
        elif matches("deposit"):
            show_deposit_menu(chat_id, lang)
        elif matches("profile"):
            tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                                "text": (f"👤 <b>{'প্রোফাইল' if lang=='bn' else 'My Profile'}</b>\n\n"
                                         f"Name: {h(user.get('username',''))}\n"
                                         + (f"Username: @{h(user['tg_username'])}\n" if user.get('tg_username') else "") +
                                         f"User ID: <code>{uid}</code>\nJoined: {user.get('joined','-')}\n\n"
                                         f"💰 <b>Wallet</b>\nBalance: <b>{fmt(user.get('balance',0))} {cur}</b>\n"
                                         f"Total Deposit: {fmt(user.get('total_deposited',0))} {cur}\n"
                                         f"Total Spent: {fmt(user.get('total_spent',0))} {cur}\n\n"
                                         f"👥 <b>Referral</b>\nTotal: {user.get('ref_count',0)} | Earned: {fmt(user.get('ref_earned',0))} {cur}")})
            show_history(chat_id, user, lang)
        elif matches("support"):
            link = db_read(DB_SETTINGS).get("support_link", "")
            if link:
                reply(chat_id, get_text("support_text", lang), ikb([[url_btn(("Open Support" if lang=="en" else "সাপোর্ট খুলুন"), link)]]))
            else:
                reply(chat_id, get_text("support_text", lang))
        elif matches("refer"):
            bu = bot_username()
            link = f"https://t.me/{bu}?start=ref_{uid}" if bu else "-"
            s = db_read(DB_SETTINGS)
            reply(chat_id, get_text("referral_text", lang, percent=fmt(s.get("referral_percent", 5)),
                                     days=int(s.get("referral_valid_days", 365)), link=h(link),
                                     count=user.get("ref_count", 0), earned=fmt(user.get("ref_earned", 0)), currency=cur))
        elif matches("lang"):
            show_language_menu(chat_id, lang)
        elif matches("admin") and is_adm:
            show_admin_panel(chat_id)
        return

    # ---------- step-based input (buyer flows) ----------
    if step == "buy_qty":
        if not text.isdigit() or int(text) < 1:
            reply(chat_id, "❌", ikb([cancel_row(lang)]))
            return
        set_state(uid, None)
        show_proxy_confirm(chat_id, uid, lang, str(state["cat"]), int(text))
        return

    if step == "dep_amount":
        if not _is_number(text) or float(text) < MIN_DEPOSIT:
            reply(chat_id, f"❌ Min {fmt(MIN_DEPOSIT)}", ikb([cancel_row(lang)]))
            return
        start_gateway_deposit(chat_id, uid, lang, round(float(text), 2))
        return

    if step == "dep_trx":
        trx = re.sub(r"\s+", "", text).upper()
        if not re.match(r"^[A-Z0-9]{6,64}$", trx):
            reply(chat_id, "❌ Invalid TrxID/TxHash.", ikb([cancel_row(lang)]))
            return
        for d in db_read(DB_DEPOSITS).values():
            if d["trx"].upper() == trx and d["status"] != "rejected":
                reply(chat_id, "⚠️ Already used!", ikb([cancel_row(lang)]))
                return
        dep_id = binascii.hexlify(os.urandom(4)).decode()[:6].upper()
        amount = float(state["amount"])

        def fn(deps: dict):
            for d in deps.values():
                if d["trx"].upper() == trx and d["status"] != "rejected":
                    return False
            deps[dep_id] = {"user": uid, "amount": amount, "currency": cur, "trx": trx,
                             "method": "manual", "status": "pending", "created": now_str()}
            return deps[dep_id]

        created = db_update(DB_DEPOSITS, fn)
        set_state(uid, None)
        if not created:
            reply(chat_id, "⚠️ Already used!", ikb([cancel_row(lang)]))
            return
        caption = deposit_caption(created, dep_id)
        for a in ADMIN_IDS:
            reply(a, caption, ikb([[btn("✅", f"dep_ok_{dep_id}"), btn("❌", f"dep_no_{dep_id}")]]))
        if NOTIFICATION_CHANNEL:
            reply(NOTIFICATION_CHANNEL, f"📥 New deposit request\n👤 <code>{mask_id(uid)}</code>\n💰 {fmt(amount)} {cur}")
        log_event("DEPOSIT_REQ", f"id={dep_id} user={uid} amount={amount} trx={trx}")
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ {'আপনার রিকোয়েস্ট' if lang=='bn' else 'Your request'} (#{dep_id}) {'পাঠানো হয়েছে, অনুমোদনের অপেক্ষায়।' if lang=='bn' else 'has been sent for approval.'}"})
        return

    # ----- admin: catalog / proxy steps -----
    if step == "adm_broadcast":
        if not is_adm:
            return
        set_state(uid, None)
        users = db_read(DB_USERS)
        sent = failed = 0
        for user_id, u in users.items():
            if not str(user_id).isdigit() or u.get("banned"):
                continue
            r = tg("copyMessage", {"chat_id": user_id, "from_chat_id": chat_id, "message_id": msg["message_id"]})
            sent += 1 if r and r.get("ok") else 0
            failed += 0 if r and r.get("ok") else 1
            time.sleep(0.05)
        log_event("BROADCAST", f"sent={sent} failed={failed}")
        tg("sendMessage", {"chat_id": chat_id, "text": f"📢 Sent: {sent} | Failed: {failed}", "reply_markup": kb})
        return

    if step == "adm_cat_name":
        if not is_adm or text == "":
            return
        key = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()[:30] or "cat"
        cats = db_read(DB_CATEGORIES)
        base, i = key, 2
        while key in cats:
            key = f"{base[:27]}_{i}"
            i += 1
        set_state(uid, "adm_cat_price", {"key": key, "name": text})
        reply(chat_id, "💰 মূল্য লিখুন:", ikb([cancel_row(lang)]))
        return

    if step == "adm_cat_price":
        if not is_adm:
            return
        if not _is_number(text) or float(text) <= 0:
            reply(chat_id, "❌", ikb([cancel_row(lang)]))
            return
        key, name, price = str(state["key"]), str(state["name"]), round(float(text), 2)
        db_update(DB_CATEGORIES, lambda c: c.update({key: {"name": name, "price": price}}))
        ensure_category_stock(key)
        set_state(uid, None)
        log_event("CAT_ADD", f"{key} price={price}")
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ ক্যাটাগরি তৈরি সফল!", "reply_markup": kb})
        return

    if step == "adm_price":
        if not is_adm:
            return
        if not _is_number(text) or float(text) <= 0:
            reply(chat_id, "❌", ikb([cancel_row(lang)]))
            return
        key, price = str(state["cat"]), round(float(text), 2)
        def fn(c):
            if key in c:
                c[key]["price"] = price
        db_update(DB_CATEGORIES, fn)
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ {fmt(price)} {cur}", "reply_markup": kb})
        return

    if step == "adm_newcat_name":
        if not is_adm or text == "":
            return
        group = str(state["group"])
        key = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()[:30] or "cat"
        cats = get_catalog(group)
        base, i = key, 2
        while key in cats:
            key = f"{base[:27]}_{i}"
            i += 1
        add_catalog_category(group, key, text)
        set_state(uid, None)
        log_event("CATALOG_CAT_ADD", f"{group}/{key}")
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ ক্যাটাগরি তৈরি হয়েছে: {h(text)}", "reply_markup": kb})
        return

    if step == "adm_newprod_name":
        if not is_adm or text == "":
            return
        set_state(uid, "adm_newprod_price", {"group": state["group"], "cat": state["cat"], "name": text})
        reply(chat_id, "💰 মূল্য লিখুন:", ikb([cancel_row(lang)]))
        return

    if step == "adm_newprod_price":
        if not is_adm:
            return
        if not _is_number(text) or float(text) <= 0:
            reply(chat_id, "❌ সঠিক মূল্য লিখুন।", ikb([cancel_row(lang)]))
            return
        group, cat_key, name = str(state["group"]), str(state["cat"]), str(state["name"])
        price = round(float(text), 2)
        prod_key = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:30] or "prod"
        base, i = prod_key, 2
        while prod_key in get_catalog(group).get(cat_key, {}).get("products", {}):
            prod_key = f"{base[:27]}_{i}"
            i += 1
        add_catalog_product(group, cat_key, prod_key, name, price)
        set_state(uid, None)
        log_event("CATALOG_PROD_ADD", f"{group}/{cat_key}/{prod_key} price={price}")
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ প্রোডাক্ট তৈরি হয়েছে: <b>{h(name)}</b> ({fmt(price)} {cur})\n\nএখন VPN/Premium ম্যানেজ থেকে এই প্রোডাক্টে গিয়ে স্টক আপলোড করুন।"})
        return

    if step == "adm_prodprice":
        if not is_adm:
            return
        if not _is_number(text) or float(text) <= 0:
            reply(chat_id, "❌ সঠিক মূল্য লিখুন।", ikb([cancel_row(lang)]))
            return
        group, cat_key, prod_key = str(state["group"]), str(state["cat"]), str(state["prod"])
        set_catalog_product_price(group, cat_key, prod_key, round(float(text), 2))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ মূল্য আপডেট হয়েছে: {fmt(round(float(text),2))} {cur}", "reply_markup": kb})
        return

    if step == "adm_upload":
        if not is_adm:
            return
        key = str(state["cat"])
        content = _extract_upload_text(doc, text)
        if content.strip() == "":
            reply(chat_id, "❌ Empty.", ikb([cancel_row(lang)]))
            return
        added, dupes = add_proxies(key, content)
        set_state(uid, None)
        log_event("UPLOAD", f"cat={key} added={added} dupes={dupes}")
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ {added} added, {dupes} duplicates skipped. Stock: {stock_count(key)}"})
        return

    if step == "adm_catupload":
        if not is_adm:
            return
        group, cat_key, prod_key = str(state["group"]), str(state["cat"]), str(state["prod"])
        content = _extract_upload_text(doc, text)
        if content.strip() == "":
            reply(chat_id, "❌ Empty.", ikb([cancel_row(lang)]))
            return
        added, dupes = unit_add_stock(group, cat_key, prod_key, content)
        set_state(uid, None)
        log_event("UPLOAD_CATALOG", f"{group}/{cat_key}/{prod_key} added={added} dupes={dupes}")
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ {added} added, {dupes} duplicates skipped. Stock: {unit_stock_count(group, cat_key, prod_key)}"})
        return

    # ----- admin: settings steps (button-driven config) -----
    if step == "adms_support_val":
        if not is_adm:
            return
        db_update(DB_SETTINGS, lambda s: s.update({"support_link": text}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ সাপোর্ট লিংক আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_fj_chat":
        if not is_adm:
            return
        set_state(uid, "adms_fj_link", {"chat": text})
        reply(chat_id, "✍️ এবার এর ইনভাইট লিংকটা পাঠান (যেমন: https://t.me/yourchannel):", ikb([cancel_row(lang)]))
        return

    if step == "adms_fj_link":
        if not is_adm:
            return
        def fn(s):
            s.setdefault("force_join", [])
            s["force_join"].append({"chat": str(state["chat"]), "link": text})
        db_update(DB_SETTINGS, fn)
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ যোগ হয়েছে: {h(state['chat'])}\n\n⚠️ বটকে ওই চ্যানেল/গ্রুপে অ্যাডমিন বানাতে ভুলবেন না।"})
        return

    if step == "adms_ref_percent":
        if not is_adm:
            return
        if not _is_number(text):
            reply(chat_id, "❌ সংখ্যা লিখুন।", ikb([cancel_row(lang)]))
            return
        set_state(uid, "adms_ref_days", {"percent": text})
        reply(chat_id, "✍️ কমিশন কত দিন পর্যন্ত বৈধ থাকবে লিখুন (দিন সংখ্যা, যেমন: 365):", ikb([cancel_row(lang)]))
        return

    if step == "adms_ref_days":
        if not is_adm:
            return
        if not _is_number(text):
            reply(chat_id, "❌ সংখ্যা লিখুন।", ikb([cancel_row(lang)]))
            return
        db_update(DB_SETTINGS, lambda s: s.update({"referral_percent": float(state["percent"]), "referral_valid_days": float(text)}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ রেফারেল সেটিংস আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_gw_key_val":
        if not is_adm:
            return
        db_update(DB_SETTINGS, lambda s: s.setdefault("uddoktapay", {}).update({"api_key": text}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ API Key আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_gw_url_val":
        if not is_adm:
            return
        db_update(DB_SETTINGS, lambda s: s.setdefault("uddoktapay", {}).update({"base_url": text}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ Base URL আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_bn_key_val":
        if not is_adm:
            return
        db_update(DB_SETTINGS, lambda s: s.setdefault("binance", {}).update({"api_key": text}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ Binance API Key আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_bn_secret_val":
        if not is_adm:
            return
        db_update(DB_SETTINGS, lambda s: s.setdefault("binance", {}).update({"api_secret": text}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ Binance API Secret আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_bn_currency_val":
        if not is_adm:
            return
        cur_val = text.strip().upper()
        if cur_val not in ("USDT", "BUSD"):
            reply(chat_id, "❌ শুধু USDT অথবা BUSD লিখুন।", ikb([cancel_row(lang)]))
            return
        db_update(DB_SETTINGS, lambda s: s.setdefault("binance", {}).update({"currency": cur_val}))
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ কারেন্সি {cur_val} সেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_pay_method":
        if not is_adm:
            return
        method = re.sub(r"[^a-zA-Z0-9]", "", text).lower()
        if not method:
            reply(chat_id, "❌ সঠিক নাম লিখুন।", ikb([cancel_row(lang)]))
            return
        set_state(uid, "adms_pay_number", {"method": method})
        reply(chat_id, f"✍️ {h(method.upper())} এর নম্বর পাঠান:", ikb([cancel_row(lang)]))
        return

    if step == "adms_pay_number":
        if not is_adm:
            return
        method = str(state["method"])
        def fn(s):
            s.setdefault("deposit_accounts", {})
            if text.strip().lower() == "none":
                s["deposit_accounts"].pop(method, None)
            else:
                s["deposit_accounts"][method] = text.strip()
        db_update(DB_SETTINGS, fn)
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ পেমেন্ট নম্বর আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adms_text_val":
        if not is_adm:
            return
        key, tlang = str(state["key"]), str(state["lang"])
        set_text(key, tlang, text)
        set_state(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ '{key}' ({tlang}) টেক্সট আপডেট হয়েছে।", "reply_markup": kb})
        return

    if step == "adm_emoji_name":
        if not is_adm or text == "":
            return
        key = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()[:30] or "emoji"
        set_state(uid, "adm_emoji_capture", {"label": text, "key": key})
        reply(chat_id, "🎨 এবার সেই Premium Emoji-টা একটা মেসেজে পাঠান অথবা ফরওয়ার্ড করুন (শুধু ওই emoji-টা একা পাঠালেই সবচেয়ে ভালো হয়):",
              ikb([cancel_row(lang)]))
        return

    if step == "adm_emoji_capture":
        if not is_adm:
            return
        entities = msg.get("entities") or msg.get("caption_entities") or []
        ce = next((e for e in entities if e.get("type") == "custom_emoji"), None)
        if not ce:
            reply(chat_id, "❌ এই মেসেজে কোনো Premium/Custom Emoji পাওয়া যায়নি। আবার চেষ্টা করুন, অথবা /cancel চাপুন।",
                  ikb([cancel_row(lang)]))
            return
        body = msg.get("text") or msg.get("caption") or ""
        fallback = body[ce["offset"]: ce["offset"] + ce["length"]] or "🔥"
        key, label = str(state["key"]), str(state["label"])
        add_emoji(key, ce["custom_emoji_id"], fallback, label)
        set_state(uid, None)
        log_event("EMOJI_ADD", f"{key} id={ce['custom_emoji_id']}")
        tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": kb,
                            "text": f"✅ Emoji যোগ হয়েছে: <b>{h(label)}</b>\n\nটেক্সট এডিটে ব্যবহার করুন: <code>{{emoji:{key}}}</code>"})
        return

    tg("sendMessage", {"chat_id": chat_id, "text": "👇", "reply_markup": kb})


def _extract_upload_text(doc, text):
    if doc:
        if (doc.get("file_size") or 0) > 5 * 1024 * 1024:
            return ""
        fi = tg("getFile", {"file_id": doc["file_id"]})
        if fi and fi.get("ok"):
            try:
                r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fi['result']['file_path']}", timeout=20)
                return r.text
            except requests.RequestException:
                return ""
        return ""
    return text


# =====================================================================
# 12. FLASK ROUTES
# =====================================================================

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET != "":
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
            abort(403)

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return "", 200

    msg = update.get("message")
    cb = update.get("callback_query")
    if not msg and not cb:
        return "", 200

    frm = (msg or {}).get("from") or (cb or {}).get("from")
    if not frm or frm.get("is_bot"):
        return "", 200
    chat_id = ((msg or {}).get("chat") or {}).get("id") or (((cb or {}).get("message") or {}).get("chat") or {}).get("id")
    if not chat_id:
        return "", 200
    if msg and (msg.get("chat", {}).get("type", "private") != "private"):
        return "", 200

    uid = str(frm["id"])
    is_adm = is_admin(uid)

    payload = ""
    if msg:
        m = re.match(r"^/start\s+(\S+)", msg.get("text", "") or "")
        if m:
            payload = m.group(1)

    res = ensure_user(uid, frm, payload, bool(msg))
    user = res["user"]
    if res["throttled"]:
        return "", 200
    lang = get_lang(user)

    if user.get("banned") and not is_adm:
        if cb:
            tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "🚫 Banned", "show_alert": True})
        else:
            reply(chat_id, "🚫 You are banned from using this bot." if lang == "en" else "🚫 আপনাকে এই বট ব্যবহার থেকে ব্যান করা হয়েছে।")
        return "", 200

    settings = db_read(DB_SETTINGS)
    if settings.get("bot_status", "ON") == "OFF" and not is_adm:
        if cb:
            tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Bot is offline", "show_alert": True})
        else:
            reply(chat_id, "💤 বট এখন অফলাইন আছে। কিছুক্ষণ পর চেষ্টা করুন।" if lang == "bn" else "💤 The bot is offline right now. Please try again later.")
        return "", 200

    if not is_adm:
        missing = missing_force_join(uid)
        if missing:
            if cb and cb.get("data", "").startswith("checkjoin_"):
                pass
            elif cb:
                tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
                return "", 200
            else:
                rows = [[url_btn(e.get("label") or e["chat"], e["link"])] for e in missing]
                rows.append([btn("✅ I've joined / জয়েন করেছি", "checkjoin_0")])
                reply(chat_id, get_text("force_join_text", lang), ikb(rows))
                return "", 200

    try:
        if cb:
            handle_callback(cb, uid, user, is_adm)
        else:
            handle_message(msg, uid, user, is_adm)
    except Exception as e:
        log_event("ERROR", f"{e}")

    return "", 200


@app.route("/pay/webhook", methods=["POST"])
def uddoktapay_webhook():
    _, api_key, _ = gateway_config()
    header_api = request.headers.get("RT-UDDOKTAPAY-API-KEY")
    if not header_api or header_api != api_key:
        abort(401)
    data = request.get_json(silent=True)
    if not data:
        abort(400)

    invoice_id = data.get("invoice_id")
    status = data.get("status")
    meta = data.get("metadata") or {}
    uid = str(meta.get("user_id") or "")
    dep_id = str(meta.get("dep_id") or "")
    amount = float(data.get("amount") or 0)

    verified = verify_uddoktapay_payment(invoice_id) or {}
    if verified.get("status") != "COMPLETED" or status != "COMPLETED":
        log_event("GATEWAY_WEBHOOK_IGNORED", f"invoice={invoice_id} status={status}/{verified.get('status')}")
        return "ok", 200

    def fn(deps: dict):
        d = deps.get(dep_id)
        if not d or d["status"] != "pending":
            return None
        d["status"] = "approved"
        d["trx"] = data.get("transaction_id", "-")
        d["method"] = data.get("payment_method", "uddoktapay")
        d["handled_by"] = "gateway"
        d["handled_at"] = now_str()
        return d

    dep = db_update(DB_DEPOSITS, fn)
    if dep and uid:
        currency = db_read(DB_SETTINGS).get("currency", "BDT")
        credit_deposit(uid, amount, currency)
        u = db_read(DB_USERS).get(uid, {})
        lang = get_lang(u)
        reply(uid, f"🎉 {'আপনার ডিপোজিট' if lang=='bn' else 'Your deposit of'} <b>{fmt(amount)} {currency}</b> "
                   f"{'স্বয়ংক্রিয়ভাবে অনুমোদিত হয়েছে!' if lang=='bn' else 'has been auto-approved!'}")
        log_event("GATEWAY_DEPOSIT_OK", f"dep={dep_id} user={uid} amount={amount}")
    return "ok", 200


@app.route("/pay/binance/webhook", methods=["POST"])
def binance_webhook():
    timestamp = request.headers.get("BinancePay-Timestamp", "")
    nonce = request.headers.get("BinancePay-Nonce", "")
    signature = request.headers.get("BinancePay-Signature", "")
    raw_body = request.get_data(as_text=True)

    if not verify_binance_webhook_signature(timestamp, nonce, raw_body, signature):
        abort(401)

    try:
        payload = json.loads(raw_body)
        inner = json.loads(payload.get("data", "{}"))
    except (ValueError, TypeError):
        return Response(json.dumps({"returnCode": "FAIL", "returnMessage": "bad payload"}), status=200, mimetype="application/json")

    biz_status = payload.get("bizStatus")
    dep_id = str(inner.get("merchantTradeNo") or "")
    amount = float(inner.get("orderAmount") or inner.get("totalFee") or 0)

    if biz_status == "PAY_SUCCESS" and dep_id:
        def fn(deps: dict):
            d = deps.get(dep_id)
            if not d or d["status"] != "pending":
                return None
            d["status"] = "approved"
            d["trx"] = inner.get("transactionId", "-")
            d["method"] = "binance"
            d["handled_by"] = "gateway"
            d["handled_at"] = now_str()
            return d

        dep = db_update(DB_DEPOSITS, fn)
        if dep:
            uid = str(dep["user"])
            currency = db_read(DB_SETTINGS).get("currency", "BDT")
            credit_deposit(uid, dep["amount"], currency)
            u = db_read(DB_USERS).get(uid, {})
            lang = get_lang(u)
            reply(uid, f"🎉 {'আপনার ডিপোজিট' if lang=='bn' else 'Your deposit of'} <b>{fmt(dep['amount'])} {currency}</b> "
                       f"{'স্বয়ংক্রিয়ভাবে অনুমোদিত হয়েছে!' if lang=='bn' else 'has been auto-approved!'}")
            log_event("BINANCE_DEPOSIT_OK", f"dep={dep_id} user={uid} amount={dep['amount']}")

    # Binance requires exactly this acknowledgement, or it will keep retrying.
    return Response(json.dumps({"returnCode": "SUCCESS", "returnMessage": None}), status=200, mimetype="application/json")


@app.route("/pay/success", methods=["GET"])
def pay_success():
    return Response("<h2>✅ Payment received</h2><p>You can close this window and go back to Telegram.</p>", mimetype="text/html")


@app.route("/pay/cancel", methods=["GET"])
def pay_cancel():
    return Response("<h2>❌ Payment cancelled</h2><p>You can close this window and go back to Telegram.</p>", mimetype="text/html")


def register_bot_commands():
    """Make sure the Telegram menu button (☰) always shows /start,
    even if the admin never ran /setcommands via BotFather."""
    if not BOT_TOKEN:
        return
    try:
        tg("setMyCommands", {"commands": [
            {"command": "start", "description": "Start / restart the bot"},
            {"command": "cancel", "description": "Cancel current action"},
        ]})
        tg("setChatMenuButton", {"menu_button": {"type": "commands"}})
    except Exception:
        pass


bootstrap()
register_bot_commands()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
