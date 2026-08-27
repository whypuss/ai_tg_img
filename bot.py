#!/usr/bin/env python3
"""Telegram /draw bot — SenseNova sensenova-u1-fast 生圖"""
import asyncio
import base64
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse, quote_plus

import httpx
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SENSENOVA_API_KEY = os.environ["SENSENOVA_API_KEY"]
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL = "sensenova-u1-fast"
VISION_MODEL = "sensenova-6.7-flash-lite"
SUMMARY_MODEL = "deepseek-v4-flash"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "x")
OPENCODE_API_KEY = os.environ.get("OPENCODE_API_KEY", "x")
OPENCODE_API_KEY_2 = os.environ.get("OPENCODE_API_KEY_2", "x")
OPENCODE_BASE_URL = "https://opencode.ai/zen/v1"
CHAT_MODEL_POOL = [
    ("oc-nemotron", lambda: AsyncOpenAI(api_key=OPENCODE_API_KEY, base_url=OPENCODE_BASE_URL), "nemotron-3-ultra-free"),
    ("oc-hy3",      lambda: AsyncOpenAI(api_key=OPENCODE_API_KEY, base_url=OPENCODE_BASE_URL), "hy3-free"),
    ("oc2-nemotron", lambda: AsyncOpenAI(api_key=OPENCODE_API_KEY_2, base_url=OPENCODE_BASE_URL), "nemotron-3-ultra-free"),
    ("oc2-laguna",  lambda: AsyncOpenAI(api_key=OPENCODE_API_KEY_2, base_url=OPENCODE_BASE_URL), "laguna-s-2.1-free"),
    ("or-free",     lambda: AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1"), "openrouter/free"),
    ("sn-deepseek", lambda: client, "deepseek-v4-flash"),
    ("sn-glm",      lambda: client, "glm-5.2"),
    ("sn-6.7",      lambda: client, "sensenova-6.7-flash-lite"),
]

def _next_pool_idx(context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.bot_data.get("pool_idx", 0)
    context.bot_data["pool_idx"] = (idx + 1) % len(CHAT_MODEL_POOL)
    return idx

async def _chat_complete(context: ContextTypes.DEFAULT_TYPE,
                         system_prompt: str, user_msg: str,
                         max_tokens: int = 1024) -> str:
    start = _next_pool_idx(context)
    errors = []
    for i in range(len(CHAT_MODEL_POOL)):
        idx = (start + i) % len(CHAT_MODEL_POOL)
        label, client_fn, model = CHAT_MODEL_POOL[idx]
        try:
            c = client_fn()
            # 每個模型最多等 25 秒，避免 429 retry 疊加令回應延遲幾分鐘
            resp = await asyncio.wait_for(
                c.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_msg}],
                    max_tokens=max_tokens),
                timeout=25)
            raw = resp.choices[0].message.content
            if raw and raw.strip():
                return raw.strip()
            errors.append(f"{label}: empty response")
        except Exception as e:
            log.warning("pool skip %s: %s", label, e)
            errors.append(f"{label}: {e}")
    raise RuntimeError("所有模型輪詢完都失敗：" + " | ".join(errors[-5:]))

SIZES = {
    "1:1": "2048x2048",
    "2:3": "1664x2496", "3:2": "2496x1664",
    "3:4": "1760x2368", "4:3": "2368x1760",
    "4:5": "1824x2272", "5:4": "2272x1824",
    "16:9": "2752x1536", "9:16": "1536x2752",
    "21:9": "3072x1376", "9:21": "1344x3136",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("drawbot")

client = AsyncOpenAI(api_key=SENSENOVA_API_KEY, base_url=SENSENOVA_BASE_URL)

# ================= 管理面板：白名單 & 群組註冊表 =================
# 起步先用：Bot 擁有者 user ID（@userinfobot 查自己），冇設就所有人唔受限制
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WHITELIST_FILE = os.path.join(BASE_DIR, "whitelist.json")
GROUPS_REGISTRY_FILE = os.path.join(BASE_DIR, "groups_registry.json")
PAGE_SIZE = 6  # 面板每頁顯示群組數


def _load_json(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_whitelist() -> dict:
    data = _load_json(WHITELIST_FILE)
    data.setdefault("enabled", False)          # 總開關：False = 全開放（向後兼容）
    data.setdefault("chats", [])               # 已授權群組 list[int]
    data.setdefault("users", [])               # 已授權用戶 list[int]
    data.setdefault("chats_info", {})          # str(chat_id) -> {note, added_at}
    data.setdefault("users_info", {})
    return data


def save_whitelist(data: dict):
    _save_json(WHITELIST_FILE, data)


def load_groups_registry() -> dict:
    return _load_json(GROUPS_REGISTRY_FILE)


def save_groups_registry(data: dict):
    _save_json(GROUPS_REGISTRY_FILE, data)


def is_owner(update: Update) -> bool:
    """面板/管理指令只允許 OWNER_ID。未設定 OWNER_ID 時面板完全禁用（安全預設）。"""
    user = update.effective_user
    if not user or not OWNER_ID:
        return False
    return user.id == OWNER_ID


def is_authorized(update: Update) -> bool:
    """總開關關閉 = 全開放。開啟後：chat 或 user 任一在白名單即放行。"""
    wl = load_whitelist()
    if not wl.get("enabled"):
        return True
    if is_owner(update):
        return True
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    return chat_id in wl["chats"] or user_id in wl["users"]


async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """記錄任何群組/頻道活動到註冊表（含標題、類型、成員數、活躍時間）"""
    chat = update.effective_chat
    if not chat or chat.id > 0:
        return
    cid = str(chat.id)
    data = load_groups_registry()
    wl = load_whitelist()
    try:
        member_count = await context.bot.get_chat_member_count(chat.id)
    except Exception:
        member_count = data.get(cid, {}).get("member_count", 0)
    prev = data.get(cid, {})
    data[cid] = {
        "title": chat.title or prev.get("title", "Unknown"),
        "type": chat.type,
        "member_count": member_count,
        "username": chat.username or prev.get("username"),
        "first_seen": prev.get("first_seen", int(datetime.now().timestamp())),
        "last_active": int(datetime.now().timestamp()),
    }
    save_groups_registry(data)


def require_auth(handler):
    """裝飾器：唔授權就攔截（群組視訊會話保留 start 可用）"""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            try:
                await update.message.reply_text("🔒 此群組/用戶未授權使用本 Bot")
            except Exception:
                pass
            log.warning("Unauthorized access: chat=%s user=%s",
                        update.effective_chat.id if update.effective_chat else "?",
                        update.effective_user.id if update.effective_user else "?")
            return
        return await handler(update, context)
    return wrapper


# ================= 管理面板 UI =================
def _wl_btn(chat_id) -> str:
    """白名單按鈕文字：當前狀態 + 切換動作"""
    wl = load_whitelist()
    if chat_id in wl["chats"]:
        return "⬇️ 移出白名單"
    return "⬆️ 加入白名單"


async def _panel_edit(q, context, text, kb):
    """編輯面板訊息；訊息被刪就重發"""
    try:
        await q.edit_message_text(text, reply_markup=kb,
                                  parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await q.message.reply_text(text, reply_markup=kb,
                                   parse_mode="Markdown", disable_web_page_preview=True)


async def panel_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """主面板：狀態總覽 + 導航"""
    q = update.callback_query
    await q.answer()
    if not is_owner(update):
        await q.answer("❌ 無權限", show_alert=True)
        return
    wl = load_whitelist()
    reg = load_groups_registry()

    status = "🟢 開放（無限制）" if not wl["enabled"] else "🔴 白名單模式"
    kb = [[InlineKeyboardButton("📋 已授權群組", callback_data="admin:chats:0")],
          [InlineKeyboardButton("🌐 群組活動記錄", callback_data="admin:reg:0")],
          [InlineKeyboardButton("👤 已授權用戶", callback_data="admin:users:0")],
          [InlineKeyboardButton(("⛔ 開啟白名單" if not wl["enabled"]
                                 else "✅ 關閉白名單"), callback_data="admin:toggle")]]
    text = (f"🎛️ Bot 管理面板\n\n"
            f"狀態：{status}\n"
            f"授權群組：{len(wl['chats'])} 個\n"
            f"授權用戶：{len(wl['users'])} 個\n"
            f"活動群組：{len(reg)} 個\n\n"
            f"OWNER_ID：{'已設定' if OWNER_ID else '未設定（私聊可管）'}")
    await _panel_edit(q, context, text, InlineKeyboardMarkup(kb))


async def panel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """切換白名單總開關"""
    q = update.callback_query
    if not is_owner(update):
        await q.answer("❌ 無權限", show_alert=True)
        return
    wl = load_whitelist()
    wl["enabled"] = not wl["enabled"]
    save_whitelist(wl)
    await q.answer(
        "✅ 白名單已開啟 — 之後只有授權才能用"
        if wl["enabled"] else
        "⭕ 白名單已關閉 — 所有人可用",
        show_alert=True)
    await panel_home(update, context)


async def panel_chats(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """授權群組列表（分頁）"""
    q = update.callback_query
    await q.answer()
    wl = load_whitelist()
    wl_chats = list(wl["chats"])  # [] 表示空
    if page < 0:
        page = 0
    total = len(wl_chats)
    start = page * PAGE_SIZE
    chunk = wl_chats[start:start + PAGE_SIZE]

    lines = [f"📋 已授權群組（{total} 個）\n"]
    if not chunk:
        lines.append("_（未授權任何群組）_")
    for cid in chunk:
        info = wl["chats_info"].get(str(cid), {})
        note = f" — {info['note'][:20]}" if info.get("note") else ""
        lines.append(f"`{cid}`{note}")

    kb = [[InlineKeyboardButton(f"{c} ⬇️ 移除", callback_data=f"admin:unchat:{c}")
           for c in chunk[:3]],
          [InlineKeyboardButton(f"{c} ⬇️ 移除", callback_data=f"admin:unchat:{c}")
           for c in chunk[3:]] if len(chunk) > 3 else []]
    # 過濾空行
    kb = [row for row in kb if row]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin:chats:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin:chats:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 主面板", callback_data="admin:home"),
               InlineKeyboardButton("➕ 添加群組（用戶自加）", callback_data="admin:addchat:prompt")])
    await _panel_edit(q, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def panel_registry(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """群組活動記錄（分頁，含白名單狀態標記）"""
    q = update.callback_query
    await q.answer()
    wl = load_whitelist()
    reg = load_groups_registry()
    items = sorted(reg.items(), key=lambda kv: -kv[1].get("last_active", 0))
    if page < 0:
        page = 0
    total = len(items)
    start = page * PAGE_SIZE
    chunk = items[start:start + PAGE_SIZE]

    lines = [f"🌐 群組活動記錄（{total} 個）\n"]
    if not chunk:
        lines.append("_（尚未有任何群組活動）_")
    for cid, info in chunk:
        cid_int = int(cid)
        auth = "✅" if cid_int in wl["chats"] else "⬜"
        title = (info.get("title") or "Unknown")[:25]
        members = info.get("member_count", "?")
        last = datetime.fromtimestamp(info.get("last_active", 0)).strftime("%m-%d %H:%M")
        lines.append(f"{auth} `{cid_int}` {title} | 👥{members} | {last}")

    kb = [[InlineKeyboardButton(
        f"{'⬇️' if int(cid) in wl['chats'] else '⬆️'} {info.get('title', '')[:12]}",
        callback_data=f"admin:regact:{cid}") for cid, info in chunk]]
    # 一列最多 2 個，過多的換行
    kb = [kb[0][i:i + 2] for i in range(0, len(kb[0]), 2)] if kb and kb[0] else []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin:reg:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin:reg:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 主面板", callback_data="admin:home")])
    await _panel_edit(q, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def panel_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """授權用戶列表"""
    q = update.callback_query
    await q.answer()
    wl = load_whitelist()
    wl_users = list(wl["users"])
    if page < 0:
        page = 0
    total = len(wl_users)
    start = page * PAGE_SIZE
    chunk = wl_users[start:start + PAGE_SIZE]

    lines = [f"👤 已授權用戶（{total} 個）\n"]
    if not chunk:
        lines.append("_（未授權任何用戶）_")
    for uid in chunk:
        info = wl["users_info"].get(str(uid), {})
        note = f" — {info['note'][:20]}" if info.get("note") else ""
        lines.append(f"`{uid}`{note}")

    kb = [[InlineKeyboardButton(f"{u} ⬇️ 移除", callback_data=f"admin:unuser:{u}")
           for u in chunk[:3]],
          [InlineKeyboardButton(f"{u} ⬇️ 移除", callback_data=f"admin:unuser:{u}")
           for u in chunk[3:]] if len(chunk) > 3 else []]
    kb = [row for row in kb if row]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin:users:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin:users:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 主面板", callback_data="admin:home")])
    await _panel_edit(q, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def panel_reg_act(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    """單個群組操作：查看詳情 + 白名單切換"""
    q = update.callback_query
    await q.answer()
    wl = load_whitelist()
    reg = load_groups_registry()
    info = reg.get(cid, {})
    cid_int = int(cid)
    in_wl = cid_int in wl["chats"]
    wl_info = wl["chats_info"].get(cid, {})

    lines = [f"🌐 群組詳情：{info.get('title', 'Unknown')}\n",
             f"Chat ID：`{cid_int}`",
             f"類型：{info.get('type', '?')}",
             f"成員：{info.get('member_count', '?')}",
             f"User/name：@{info.get('username', '—')}",
             f"首次發現：{datetime.fromtimestamp(info.get('first_seen', 0)).strftime('%Y-%m-%d %H:%M')}",
             f"最後活躍：{datetime.fromtimestamp(info.get('last_active', 0)).strftime('%Y-%m-%d %H:%M')}",
             f"白名單：{'✅ 已授權' if in_wl else '❌ 未授權'}",
             f"備註：{wl_info.get('note', '—')}"]
    kb = [[InlineKeyboardButton("⬇️ 移出白名單" if in_wl else "⬆️ 加入白名單",
                                callback_data=f"admin:regtoggle:{cid}")],
          [InlineKeyboardButton("🔙 活動記錄", callback_data="admin:reg:0"),
           InlineKeyboardButton("🔙 主面板", callback_data="admin:home")]]
    await _panel_edit(q, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def panel_reg_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    """從群組詳情切換白名單狀態"""
    q = update.callback_query
    wl = load_whitelist()
    cid_int = int(cid)
    if cid_int in wl["chats"]:
        wl["chats"].remove(cid_int)
        note = wl["chats_info"].pop(cid, None)
        if note:
            log.info("removed whitelist chat %s note=%s", cid, note.get("note"))
    else:
        wl["chats"].append(cid_int)
        reg = load_groups_registry()
        info = reg.get(cid, {})
        wl["chats_info"][cid] = {
            "note": f"from panel {info.get('title', '')[:20]}",
            "added_at": int(datetime.now().timestamp()),
        }
    save_whitelist(wl)
    await q.answer("已更新 ✅", show_alert=False)
    await panel_reg_act(update, context, cid)


async def panel_unchat(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    """從白名單移除群組"""
    q = update.callback_query
    wl = load_whitelist()
    cid_int = int(cid)
    if cid_int in wl["chats"]:
        wl["chats"].remove(cid_int)
        wl["chats_info"].pop(cid, None)
        save_whitelist(wl)
    await q.answer("已移除 ❌", show_alert=False)
    await panel_chats(update, context, 0)


async def panel_unuser(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: str):
    """從白名單移除用戶"""
    q = update.callback_query
    wl = load_whitelist()
    uid_int = int(uid)
    if uid_int in wl["users"]:
        wl["users"].remove(uid_int)
        wl["users_info"].pop(uid, None)
        save_whitelist(wl)
    await q.answer("已移除 ❌", show_alert=False)
    await panel_users(update, context, 0)


async def panel_addchat_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """提示用戶輸入 chat_id（或轉發一個群組訊息）"""
    q = update.callback_query
    await q.answer()
    # 儲存等待狀態，讓下一個文字訊息被捕獲
    context.user_data["admin_waiting"] = "addchat"
    kb = [[InlineKeyboardButton("🔙 主面板", callback_data="admin:home")]]
    await _panel_edit(
        q, context,
        "✏️ 請輸入要授權的群組 **Chat ID**（負數，例如 `-1001234567890`）\n"
        "💡 或者：把群組裡的一條訊息**轉發**到這個私聊，我會自動讀出該群組 ID，\n"
        "又或者：直接傳一條群組訊息連結（t.me/groupname/123）",
        InlineKeyboardMarkup(kb))


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """當面板等待輸入時，捕獲用戶發送的文字/轉發，解析 chat_id 並加白名單"""
    if not context.user_data.get("admin_waiting"):
        return
    if not is_owner(update):
        return
    mode = context.user_data.pop("admin_waiting", None)
    msg = update.message

    cid = None
    if msg.forward_from_chat:  # 轉發群組訊息
        cid = msg.forward_from_chat.id
        title = msg.forward_from_chat.title or ""
    elif msg.text:
        txt = msg.text.strip()
        m = re.search(r"-?\d{5,}", txt)   # 任意負/正長數字
        if m:
            cid = int(m.group(0))
        else:
            m2 = re.search(r"t\.me/([A-Za-z0-9_]{5,})/(\d+)", txt)
            if m2:
                chat_username, msg_id = m2.group(1), int(m2.group(2))
                try:
                    chat = await context.bot.get_chat("@" + chat_username)
                    cid = chat.id
                    title = chat.title or chat_username
                except Exception:
                    await msg.reply_text("❌ 無法解析該群組連結")
                    return

    if cid is None:
        await msg.reply_text("❌ 無法解析群組 ID，請用 /panel 再試，或直接發送數字的 chat_id")
        return

    if cid >= 0:
        await msg.reply_text(
            "❌ Chat ID 必須係負數（群組），例如 `-1001234567890`。\n"
            "用戶 ID 授權請用 /panel → 👤 已授權用戶",
            parse_mode="Markdown")
        return

    wl = load_whitelist()
    if cid not in wl["chats"]:
        wl["chats"].append(cid)
        wl["chats_info"][str(cid)] = {
            "note": title if locals().get("title") else f"manual {datetime.now().strftime('%m-%d')}",
            "added_at": int(datetime.now().timestamp()),
        }
        save_whitelist(wl)
    await msg.reply_text(f"✅ 已授權：`{cid}`", parse_mode="Markdown")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """面板回撥路由：admin:<action>:<value>"""
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("admin:"):
        return
    if not is_owner(update):
        await q.answer("❌ 無權限", show_alert=True)
        return
    try:
        parts = q.data.split(":")
        action = parts[1] if len(parts) > 1 else "home"
        value = parts[2] if len(parts) > 2 else ""
        if action == "home":
            await panel_home(update, context)
        elif action == "toggle":
            await panel_toggle(update, context)
        elif action == "chats":
            await panel_chats(update, context, int(value or 0))
        elif action == "reg":
            await panel_registry(update, context, int(value or 0))
        elif action == "users":
            await panel_users(update, context, int(value or 0))
        elif action == "regact":
            await panel_reg_act(update, context, value)
        elif action == "regtoggle":
            await panel_reg_toggle(update, context, value)
        elif action == "unchat":
            await panel_unchat(update, context, value)
        elif action == "unuser":
            await panel_unuser(update, context, value)
        elif action == "addchat":
            await panel_addchat_prompt(update, context)
        else:
            await panel_home(update, context)
    except Exception as e:
        log.exception("admin callback error: %s", q.data)
        try:
            await q.answer(f"❌ 出錯：{str(e)[:60]}", show_alert=True)
        except Exception:
            pass


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/panel — 開啟管理面板"""
    if not is_owner(update):
        await update.message.reply_text("🔒 無權限使用管理面板")
        return
    wl = load_whitelist()
    reg = load_groups_registry()
    status = "🟢 開放" if not wl["enabled"] else "🔴 白名單模式"
    kb = [[InlineKeyboardButton("📋 已授權群組", callback_data="admin:chats:0")],
          [InlineKeyboardButton("🌐 群組活動記錄", callback_data="admin:reg:0")],
          [InlineKeyboardButton("👤 已授權用戶", callback_data="admin:users:0")],
          [InlineKeyboardButton(("⛔ 開啟白名單" if not wl["enabled"]
                                 else "✅ 關閉白名單"), callback_data="admin:toggle")]]
    text = (f"🎛️ Bot 管理面板\n\n"
            f"狀態：{status}\n"
            f"授權群組：{len(wl['chats'])} 個\n"
            f"授權用戶：{len(wl['users'])} 個\n"
            f"活動群組：{len(reg)} 個")
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def download_image(item):
    if getattr(item, "url", None):
        async with httpx.AsyncClient(timeout=120) as hc:
            r = await hc.get(item.url)
            r.raise_for_status()
            buf = io.BytesIO(r.content)
    else:
        buf = io.BytesIO(base64.b64decode(item.b64_json))
    buf.name = "image.png"
    buf.seek(0)
    return buf


def parse_args(args):
    size = "2048x2048"
    items = list(args)
    if items and items[0] in SIZES:
        size = SIZES[items.pop(0)]
    elif items and items[-1] in SIZES:
        size = SIZES[items.pop()]
    return " ".join(items).strip(), size


async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt, size = parse_args(list(context.args))
    if not prompt:
        await update.message.reply_text(
            "用法：/draw <描述> [比例]\n例如：/draw 16:9 一隻賽博朋克風格嘅貓\n"
            f"支持比例：{' '.join(SIZES)}")
        return

    status = await update.message.reply_text("🎨 生成中，請稍候…")
    try:
        item = (await client.images.generate(
            model=IMAGE_MODEL, prompt=prompt, n=1, size=size)).data[0]
        caption = f"🎨 {prompt}\n({size})"
        photo = await download_image(item)
        await update.message.reply_photo(photo=photo, caption=caption)
        await status.delete()
    except Exception as e:
        log.exception("generate failed")
        msg = str(e)
        if "sensitive" in msg.lower() or "code 18" in msg:
            msg = "圖片被安全審查拒絕（敏感內容），換個描述試下"
        await status.edit_text(f"❌ 生成失敗：{msg[:400]}")


async def get_source_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    reply = msg.reply_to_message
    if not reply:
        return None
    photo = reply.photo
    doc = reply.document
    buf = io.BytesIO()
    if photo:
        data = await (photo[-1]).get_file()
        await data.download_to_memory(buf)
    elif doc and (doc.mime_type or "").startswith("image/"):
        data = await doc.get_file()
        await data.download_to_memory(buf)
    elif reply.sticker:
        data = await reply.sticker.get_file()
        await data.download_to_memory(buf)
    else:
        return None
    buf.seek(0)
    return buf.read()


async def redraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    img = await get_source_image(update, context)
    if img is None:
        await update.message.reply_text(
            "用法：回覆一張圖片，然後發 /redraw <想點改>\n例如：/redraw 加上雪景同黃昏光線")
        return
    if not prompt:
        await update.message.reply_text("請輸入想點改張圖，例如：/redraw 變成水彩畫風格")
        return

    status = await update.message.reply_text("🖌️ 圖生圖處理中…")
    try:
        b64 = base64.b64encode(img).decode()
        vision = await client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text":
                     "請用英文詳細描述這張圖片的畫面內容（主體、構圖、風格、色調、光線、背景），"
                     "輸出純描述，不要開場白。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                max_tokens=1024)
        base_desc = vision.choices[0].message.content.strip()
        full_prompt = f"{base_desc}. Modification: {prompt}"
        resp = await client.images.generate(
            model=IMAGE_MODEL, prompt=full_prompt[:4000], n=1, size="2048x2048")
        item = resp.data[0]
        photo = await download_image(item)
        photo.name = "image.png"
        await update.message.reply_photo(photo=photo, caption=f"🖌️ {prompt}")
        await status.delete()
    except Exception as e:
        log.exception("redraw failed")
        msg = str(e)
        if "sensitive" in msg.lower() or "code 18" in msg:
            msg = "圖片被安全審查拒絕（敏感內容），換個描述試下"
        await status.edit_text(f"❌ 失敗：{msg[:400]}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("Hi！用 /draw <描述> 生圖 🎨\n"
            "圖生圖：回覆一張圖 + /redraw <想點改>\n"
            "總結聊天記錄：/sum [條數，預設200]\n"
            "問答：/ans <問題>\n"
            "搜歌（播放+下載）：/sing <歌名或歌手>\n"
            "Google 搜索：/G <關鍵詞>\n"
            "搜圖片：/P <關鍵詞>\n"
            "搜影片（播放）：/V <關鍵詞>\n"
            "搜 JAV：/M <番號/關鍵詞> 或 /J（Jable 直播源）\n"
            "搜 Telegram 群組：/find <關鍵字>\n"
            "朗讀：/say（普通話）/sayc（粵語）<文字>")
    # 擁有者私聊：底部加面板按鈕
    if is_owner(update):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎛️ 管理面板", callback_data="admin:home")]])
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text)


CHAT_HISTORY_MAX = 500
CHAT_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chat_history.json")


def _load_hist() -> dict:
    try:
        with open(CHAT_HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_hist(hist: dict):
    with open(CHAT_HISTORY_FILE, "w") as f:
        json.dump(hist, f)


def chat_hist(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list:
    if "chat_history" not in context.bot_data:
        context.bot_data["chat_history"] = _load_hist()
    hist = context.bot_data["chat_history"]
    hist.setdefault(str(chat_id), [])
    return hist[str(chat_id)]


async def record_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not msg.text or msg.text.startswith("/"):
        return
    name = msg.from_user.full_name if msg.from_user else "?"
    h = chat_hist(context, msg.chat_id)
    h.append({"name": name, "text": msg.text[:1000], "ts": int(msg.date.timestamp())})
    if len(h) > CHAT_HISTORY_MAX:
        del h[: len(h) - CHAT_HISTORY_MAX]
    if len(h) % 10 == 0:
        _save_hist(context.bot_data["chat_history"])


async def sum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 200
    if context.args and context.args[0].isdigit():
        n = max(5, min(int(context.args[0]), CHAT_HISTORY_MAX))
    h = chat_hist(context, update.effective_chat.id)[-n:]
    if not h:
        await update.message.reply_text(
            "暫時冇聊天記錄可以總結。\n"
            "（Bot 要喺 @BotFather 關閉 Group Privacy 先能記錄群組訊息）")
        return

    status = await update.message.reply_text(f"📝 總結最近 {len(h)} 句…")
    try:
        lines = [f"[{m['name']}] {m['text']}" for m in h]
        transcript = "\n".join(lines)
        system_prompt = (
            "你是一個群組聊天總結助手。用繁體中文書面語輸出結構化總結，"
            "要求如下：\n"
            "1. 每個 bullet 至少兩句話，具體說明「誰」說了「什麼」，不要只寫一句摘要\n"
            "2. 包含具體數據、決定、人名，不要模糊處理\n"
            "3. 按話題分組，有邏輯層級\n"
            "4. 總長控制在500字左右（唔好太少，要資訊豐富）\n"
            "5. 不要口語或粵語用詞\n"
            "格式：\n📌 主要話題\n• 具體內容（包含人物、事件、數據）\n"
            "📌 討論要點\n• 詳述各人觀點與論據\n"
            "📌 結論 / 決定\n• 有共識的事項\n"
            "📌 未解決問題\n• 仍有分歧或未定之處")
        raw = await _chat_complete(context, system_prompt,
                                   f"請總結以下聊天記錄（注意保留關鍵資訊，不要過度壓縮）：\n\n{transcript}",
                                   max_tokens=2000)
        summary = (raw or "").strip() or "（模型冇返回內容，試多次或者減少條數）"
        await status.edit_text(f"📝 最近 {len(h)} 句總結\n\n{summary[:4000]}",
                               disable_web_page_preview=True)
    except Exception as e:
        log.exception("sum failed")
        await status.edit_text(f"❌ 總結失敗：{str(e)[:400]}")


VOICE_CANTONESE = "zh-HK-HiuMaanNeural"
VOICE_MANDARIN = "zh-CN-XiaoxiaoNeural"


async def _tts_stream(communicate):
    import edge_tts
    try:
        async for chunk in communicate.stream():
            yield chunk
    except (asyncio.TimeoutError, ConnectionError) as e:
        log.warning("tts stream retry after: %s", e)
        async for chunk in communicate.stream():
            yield chunk


async def tts_command(update: Update, context: ContextTypes.DEFAULT_TYPE, voice: str):
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = (update.message.reply_to_message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "用法：/say <文字>（普通話）或 /sayc <文字>（粵語），"
            "或者回覆一句訊息打 /say 自動朗讀，上限500字")
        return

    buf = io.BytesIO()
    status = await update.message.reply_text("🔊 合成中…")
    try:
        text = text[:500]
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        async for attempt in _tts_stream(communicate):
            if attempt["type"] == "audio":
                buf.write(attempt["data"])
        buf.seek(0)
        buf.name = "speech.mp3"
        await update.message.reply_voice(voice=buf)
        await status.delete()
    except Exception as e:
        log.exception("tts failed")
        if buf.tell() > 1000:
            buf.seek(0)
            buf.name = "speech.mp3"
            await update.message.reply_voice(voice=buf)
            await status.delete()
            return
        await status.edit_text(f"❌ 語音合成失敗：{str(e)[:300]}")


async def say_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tts_command(update, context, VOICE_MANDARIN)


async def sayc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tts_command(update, context, VOICE_CANTONESE)


async def ans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = " ".join(context.args).strip()
    reply = update.message.reply_to_message
    if not question and not reply:
        await update.message.reply_text(
            "用法：\n"
            "  /ans <問題>  直接問，如 /ans doff是什麼\n"
            "  回覆訊息 + /ans  對別人嘅話追問")
        return

    status = await update.message.reply_text("思考中…")
    try:
        system_prompt = ("你是一個聊天群組助手。用繁體中文書面語回答，"
                         "嚴格限制在30字以內，直接給答案，不要客套、不要開場白、不要展開。")
        if reply and (reply.text or "").strip():
            target = (reply.text or "").strip()
            user_msg = f"有人喺群組講咗：「{target[:500]}」"
            if question:
                user_msg += f"\n用戶想知：{question[:200]}"
        else:
            user_msg = f"用戶問題：{question[:500]}"
        user_msg += "\n請用最多30字回應。"

        raw = await _chat_complete(context, system_prompt, user_msg, max_tokens=100)
        answer = (raw.strip() or "（冇答案，試多次）")[:60]
        await status.edit_text(answer)
    except Exception as e:
        log.exception("ans failed")
        await status.edit_text(f"❌ 失敗：{str(e)[:200]}")


# ================= 音樂搜索 /song =================
# 音源 = GD Music API（同 whymusicall 插件：joox/netease/kuwo/bilibili 四子源）
GD_API = "https://music-api.gdstudio.xyz/api.php"
GD_SOURCES = ["joox", "netease", "kuwo", "bilibili"]
GD_BITRATES = [320, 128]  # 音質降級階梯
AUDIO_SUFFIXES = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus"}
# 上傳 Telegram 嘅信號量：最多 2 個並行，避免慢上行同時塞爆再超時
UPLOAD_SEM = asyncio.Semaphore(2)


async def _gd_get(params: dict):
    """打 GD 上游。失敗拋異常。"""
    async with httpx.AsyncClient(timeout=40) as hc:
        r = await hc.get(GD_API, params=params)
    data = r.json()
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("detail")):
        raise ValueError(str((data or {}).get("detail") or r.status_code))
    return data


def _gd_song(raw) -> dict:
    """GD 原始歌曲 → 統一 dict（冇 id 或冇歌名就丟）"""
    sid = str(raw.get("url_id") or raw.get("id") or "")
    name = str(raw.get("name") or "").strip()
    if not sid or not name:
        return None
    artist = raw.get("artist")
    if isinstance(artist, list):
        artist = " / ".join(str(a) for a in artist if a)
    else:
        artist = str(artist or "").strip()
    return {"id": sid, "source": str(raw.get("source") or ""),
            "name": name, "artist": artist,
            "album": str(raw.get("album") or "")}


def _norm(s: str) -> str:
    """歸一化比對用：去小寫、去非中英數字元"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "").lower())


async def _gd_search(keyword: str) -> list:
    """四子源並發搜索，同名同歌手去重（保留先出現嗰個）"""
    async def _one(source):
        try:
            data = await _gd_get({"types": "search", "source": source,
                                  "name": keyword, "count": 8, "pages": 1})
            return source, data
        except Exception as e:
            log.warning("song search %s failed: %s", source, e)
            return source, []

    results = await asyncio.gather(*[_one(s) for s in GD_SOURCES])
    songs, seen = [], set()
    for source, data in results:
        if not isinstance(data, list):
            continue
        for raw in data:
            song = _gd_song(raw)
            if not song:
                continue
            key = (_norm(song["name"]), _norm(song["artist"]))
            if key in seen:
                continue
            seen.add(key)
            songs.append(song)
    return songs


async def _gd_url(source: str, sid: str) -> str:
    """解音源 URL：320 → 128 逐級試"""
    for br in GD_BITRATES:
        try:
            data = await _gd_get({"types": "url", "source": source,
                                  "id": sid, "br": br})
        except Exception as e:
            log.warning("url fail %s/%s br=%s: %s", source, sid, br, e)
            continue
        url = (data or {}).get("url") or ""
        if url:
            return url
    return ""


async def _resolve_song(song: dict):
    """原源解唔到就去其餘子源搵同一首歌再解（whymusicall resolveUrl 思路）"""
    url = await _gd_url(song["source"], song["id"])
    if url:
        return url, song["source"]
    keyword = f"{song['name']} {song['artist']}".strip()
    target = _norm(song["name"])
    for source in GD_SOURCES:
        if source == song["source"]:
            continue
        try:
            data = await _gd_get({"types": "search", "source": source,
                                  "name": keyword, "count": 5, "pages": 1})
        except Exception as e:
            log.warning("fallback search %s failed: %s", source, e)
            continue
        if not isinstance(data, list):
            continue
        for raw in data:
            cand = _gd_song(raw)
            if not cand:
                continue
            cname = _norm(cand["name"])
            if not (cname == target or (len(target) >= 2 and
                                        (cname.startswith(target) or target.startswith(cname)))):
                continue
            cartist, artist = _norm(cand["artist"]), _norm(song["artist"])
            if artist and cartist and artist not in cartist and cartist not in artist:
                continue
            url = await _gd_url(source, cand["id"])
            if url:
                return url, source
    return "", ""


def _audio_suffix(url: str, ctype: str) -> str:
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    if suffix in AUDIO_SUFFIXES:
        return suffix
    if "m4a" in ctype:
        return ".m4a"
    if "flac" in ctype:
        return ".flac"
    if "wav" in ctype:
        return ".wav"
    if "ogg" in ctype:
        return ".ogg"
    return ".mp3"


async def _download_audio(url: str):
    """下載音頻到 BytesIO。>45MB 或失敗回傳 (None, "")"""
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as hc:
            async with hc.stream("GET", url) as r:
                r.raise_for_status()
                buf = io.BytesIO()
                async for chunk in r.aiter_bytes(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > 45 * 1024 * 1024:
                        return None, ""
        buf.seek(0)
        return buf, r.headers.get("content-type", "")
    except Exception as e:
        log.warning("audio download failed: %s", e)
        return None, ""


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """搜索 Telegram 公開群組/頻道：本地 FTS5 優先，無結果再 SearXNG 發現"""
    from tg_index.db import TGIndexDB, discover_and_index

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "用法：/find <關鍵字>\n"
            "例：/find cantonese\n"
            "搜索全球公開嘅 Telegram 群組/頻道")
        return

    status = await update.message.reply_text(f"🔍 搵緊「{query}」…")

    try:
        db = TGIndexDB()
        # 1. 本地 FTS5 先
        results = await db.search(query, limit=10)

        # 2. 無結果 → SearXNG 即時發現並入庫
        if not results:
            await discover_and_index(query, db, max_results=15)
            results = await db.search(query, limit=10)
    except Exception as e:
        log.exception("find search error")
        await status.edit_text(f"❌ 搜索失敗：{str(e)[:200]}")
        return

    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關嘅群組/頻道")
        return

    lines = [f"🔎 「{query}」搵到 {len(results)} 個：\n"]
    for r in results:
        desc = (r.description or "")[:80]
        lines.append(
            f"📢 [{r.title}](https://t.me/{r.username})\n"
            f"   👥 {r.members:,} 成員\n"
            f"   {desc}")
    lines.append("\n💡 資料庫會持續自動累積新群組")
    await status.edit_text("\n".join(lines), parse_mode="Markdown",
                           disable_web_page_preview=True)


# ================= Google 搜索 /G =================
SEARXNG_URL = "http://localhost:8889/search"

async def google_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Google 搜索：/G <關鍵詞>（透過 SearXNG，自動用可用引擎）"""
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text(
            "用法：/G <關鍵詞>\n例：/G Python 非同步教學\n或回覆訊息 + /G 自動搜索該內容")
        return

    status = await update.message.reply_text(f"🔍 搜尋「{query}」…")
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            # 不指定 engines，讓 SearXNG 自動用可用的（避免 google/bing 被 CAPTCHA 擋）
            params = {"q": query, "format": "json", "categories": "general", "safesearch": 0}
            r = await hc.get(SEARXNG_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("google search failed")
        await status.edit_text(f"❌ 搜索失敗：{str(e)[:200]}")
        return

    results = data.get("results", [])
    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關結果")
        return

    lines = [f"🔎 「{query}」前 {min(len(results), 5)} 個結果：\n"]
    for i, item in enumerate(results[:5], 1):
        title = item.get("title", "無標題")
        url = item.get("url", "")
        snippet = (item.get("content") or "")[:120]
        engines = item.get("engines", [])
        lines.append(f"{i}. [{title}]({url})\n   {snippet}…\n   🔧 {', '.join(engines) if engines else '未知'}")

    text = "\n".join(lines) + "\n\n💡 結果來自 SearXNG 可用引擎"
    await status.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# ================= 圖片搜索 /P =================
async def _download_img(url: str, timeout: int = 15) -> bytes | None:
    """下載圖片，失敗返回 None"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Referer": url}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers, verify=False) as hc:
            r = await hc.get(url)
            r.raise_for_status()
            return r.content
    except Exception:
        return None


async def image_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """圖片搜索：/P <關鍵詞> → SearXNG 搜圖 → 下載 4 張 → Telegram 顯示"""
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text(
            "用法：/P <關鍵詞>\n例：/P 貓咪\n或回覆訊息 + /P 自動搜索該內容")
        return

    status = await update.message.reply_text(f"🖼️ 搜尋圖片「{query}」…")
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            params = {"q": query, "format": "json", "categories": "images", "safesearch": 0}
            r = await hc.get(SEARXNG_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("image search failed")
        await status.edit_text(f"❌ 搜索失敗：{str(e)[:200]}")
        return

    results = data.get("results", [])
    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關圖片")
        return

    # 取前 8 個有 img_src 的結果，並行下載，取最先成功的 4 張
    candidates = [r for r in results if r.get("img_src")][:8]
    if not candidates:
        await status.edit_text(f"😿 搵唔到可顯示嘅圖片")
        return

    await status.edit_text(f"⬇️ 下載緊 {min(len(candidates), 4)} 張圖片…")

    download_tasks = [_download_img(c["img_src"]) for c in candidates]
    downloaded = await asyncio.gather(*download_tasks)

    photos = []
    for raw, item in zip(downloaded, candidates):
        if raw and len(raw) > 1000:
            buf = io.BytesIO(raw)
            buf.seek(0)
            buf.name = "image.jpg"
            photos.append((buf, item.get("title", "")[:60]))
        if len(photos) >= 4:
            break

    if not photos:
        await status.edit_text("❌ 圖片下載失敗，可能被來源網站擋住")
        return

    # send_media_group 最多 10 張，用 InputMediaPhoto
    from telegram import InputMediaPhoto
    media = []
    for buf, title in photos:
        buf.seek(0)
        media.append(InputMediaPhoto(media=buf, caption=title))

    try:
        await context.bot.send_media_group(chat_id=update.message.chat_id, media=media)
        await status.delete()
    except Exception as e:
        log.exception("send_media_group failed")
        # fallback: 逐張發
        for buf, title in photos:
            buf.seek(0)
            try:
                await update.message.reply_photo(photo=buf, caption=title)
            except Exception:
                pass
        await status.delete()


# ================= 視頻搜索 /V =================
VIDEO_MAX_SIZE = 45 * 1024 * 1024  # Telegram bot 上傳上限 50MB，留餘量
VIDEO_DOWNLOAD_SEM = asyncio.Semaphore(2)

async def _extract_video(url: str) -> tuple:
    """用 yt-dlp 提取視頻直接 URL + 標題。回傳 (url, title) 或 (None, None)"""
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "format": "best[ext=mp4][filesize<50M]/best[filesize<50M]/best",
        "noplaylist": True,
    }
    try:
        loop = asyncio.get_event_loop()
        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await loop.run_in_executor(None, _extract)
        if not info:
            return None, None
        title = info.get("title", "")
        # 嘗試拿直接 URL
        if info.get("url"):
            return info["url"], title
        formats = info.get("formats", [])
        if formats:
            return formats[-1].get("url", ""), title
        return None, title
    except Exception as e:
        log.warning("yt-dlp extract failed for %s: %s", url, e)
        return None, None


async def video_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """視頻搜索：/V <關鍵詞> → 搜片 → 下載 → Telegram 播放"""
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text(
            "用法：/V <關鍵詞>\n例：/V 貓咪搞笑\n或回覆訊息 + /V 自動搜索該內容")
        return

    status = await update.message.reply_text(f"🎬 搜尋影片「{query}」…")
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            params = {"q": query, "format": "json", "categories": "videos", "safesearch": 0}
            r = await hc.get(SEARXNG_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("video search failed")
        await status.edit_text(f"❌ 搜索失敗：{str(e)[:200]}")
        return

    results = data.get("results", [])
    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關影片")
        return

    # 只保留有可下載 URL 的結果（youtube/bilibili 等）
    candidates = [r for r in results if r.get("url")]
    if not candidates:
        await status.edit_text(f"😿 搵唔到可播放嘅影片")
        return

    # 顯示搜索結果列表 + Inline 按鈕
    top = candidates[:5]
    context.user_data["video_results"] = top
    lines = []
    for i, item in enumerate(top, 1):
        title = item.get("title", "無標題")[:60]
        url = item.get("url", "")
        # 標記來源
        if "youtube" in url:
            src = "▶️ YouTube"
        elif "bilibili" in url or "b23.tv" in url:
            src = "📺 B站"
        else:
            src = "🔗 其他"
        lines.append(f"{i}. {title}\n   {src}")

    kb = [[InlineKeyboardButton(f"{i} ▶️ 播放", callback_data=f"vid:p:{i-1}")]
          for i in range(1, len(top) + 1)]
    text = f"🎬 「{query}」搵到 {len(top)} 條影片：\n\n" + "\n".join(lines) + "\n\n揀一個嚟播👇"
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        kind, idx_s = q.data.split(":")[1:]
        idx = int(idx_s)
    except (ValueError, IndexError):
        await q.answer("❌ 連結失效，重新 /V", show_alert=True)
        return
    results = context.user_data.get("video_results") or []
    if not (0 <= idx < len(results)):
        await q.answer("❌ 結果已過期，重新 /V", show_alert=True)
        return

    video = results[idx]
    url = video.get("url", "")
    title = video.get("title", "影片")
    chat_id = q.message.chat_id

    status = await context.bot.send_message(chat_id, f"⏳ 正在提取「{title[:40]}」…")
    try:
        # 1. yt-dlp 提取直接 URL
        direct_url, vtitle = await asyncio.wait_for(_extract_video(url), timeout=30)
        if not direct_url:
            await status.edit_text(f"❌ 提取失敗，可能不支援此影片來源\n🔗 {url}")
            return

        # 2. 下載影片
        await status.edit_text(f"⬇️ 下載中「{vtitle[:40]}」…")
        buf = io.BytesIO()
        async with VIDEO_DOWNLOAD_SEM:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as hc:
                async with hc.stream("GET", direct_url) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(64 * 1024):
                        buf.write(chunk)
                        if buf.tell() > VIDEO_MAX_SIZE:
                            await status.edit_text(
                                f"❌ 檔案太大（>{VIDEO_MAX_SIZE // 1024 // 1024}MB），Telegram 無法發送")
                            return

        if buf.tell() < 1000:
            await status.edit_text("❌ 下載失敗，檔案為空")
            return

        # 3. 發送到 Telegram
        await status.edit_text("📤 上傳中…")
        buf.seek(0)
        buf.name = "video.mp4"
        fname = re.sub(r'[\\/:*?"<>|\r\n]+', "_", vtitle or title)[:60] or "video"
        await context.bot.send_video(
            chat_id=chat_id, video=buf,
            caption=f"🎬 {fname}",
            supports_streaming=True)
        await status.delete()
    except asyncio.TimeoutError:
        await status.edit_text("❌ 提取超時，影片可能太大或不支援")
    except Exception as e:
        log.exception("video callback failed")
        try:
            await status.edit_text(f"❌ 出錯：{str(e)[:200]}")
        except Exception:
            pass


async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(context.args).strip()
    reply = update.message.reply_to_message
    if not keyword and reply and (reply.text or "").strip():
        keyword = " ".join((reply.text or "").split())[:100]
    if not keyword:
        await update.message.reply_text(
            "用法：/sing <關鍵字>（最多 3 個結果）\n"
            "例：/sing 浮誇\n"
            "或者回覆一句歌詞／歌名再打 /sing")
        return

    status = await update.message.reply_text(f"🔍 搵緊「{keyword}」…")
    try:
        songs = await _gd_search(keyword)
    except Exception as e:
        log.exception("song search error")
        await status.edit_text(f"❌ 搜索失敗：{str(e)[:200]}")
        return
    if not songs:
        await status.edit_text(f"😿 搵唔到「{keyword}」相關嘅歌")
        return

    results = songs[:3]
    context.user_data["song_results"] = results
    lines = []
    for i, s in enumerate(results, 1):
        singer = f" — {s['artist']}" if s["artist"] else ""
        lines.append(f"{i}. {s['name']}{singer}（{s['source']}）")
    kb = [[InlineKeyboardButton(f"{i} ▶️ 播放", callback_data=f"song:p:{i-1}"),
           InlineKeyboardButton(f"{i} ⬇️ 下載", callback_data=f"song:d:{i-1}")]
          for i in range(1, len(results) + 1)]
    text = f"🎵 「{keyword}」結果（{len(results)} 個）：\n\n" + \
           "\n".join(lines) + "\n\n揀一個嚟播／下載👇"
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def song_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        kind, idx_s = q.data.split(":")[1:]
        idx = int(idx_s)
    except (ValueError, IndexError):
        await q.answer("❌ 連結失效，重新 /song", show_alert=True)
        return
    results = context.user_data.get("song_results") or []
    if not (0 <= idx < len(results)):
        await q.answer("❌ 結果已過期，重新 /song", show_alert=True)
        return
    song = results[idx]
    chat_id = q.message.chat_id

    status = await context.bot.send_message(chat_id, f"⏳ {song['name']}：搵緊音源…")
    try:
        url, source = await asyncio.wait_for(_resolve_song(song), timeout=90)
        if not url:
            await status.edit_text(f"❌ 「{song['name']}」所有子源都冇可播放音源")
            return
        buf, ctype = await _download_audio(url)
        if buf is None:
            await status.edit_text("❌ 下載音頻失敗（檔案太大或網絡問題）")
            return

        base = f"{song['name']} - {song['artist']}".strip(" -")
        fname = re.sub(r'[\\/:*?"<>|\r\n]+', "_", base)[:80] or "song"
        buf.name = fname + _audio_suffix(url, ctype)

        async with UPLOAD_SEM:
            if kind == "p":
                await context.bot.send_audio(
                    chat_id=chat_id, audio=buf,
                    title=song["name"], performer=song["artist"] or None,
                    caption=f"🎵 {song['name']}" +
                            (f" — {song['artist']}" if song["artist"] else ""))
            else:
                await context.bot.send_document(
                    chat_id=chat_id, document=buf,
                    filename=buf.name,
                    caption=f"⬇️ {song['name']}" +
                            (f" — {song['artist']}" if song["artist"] else "") +
                            f"\n源：{source}")
        await status.delete()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("song callback failed")
        try:
            await status.edit_text(f"❌ 出錯：{str(e)[:200]}")
        except Exception:
            pass


# ================= JAV 搜索 /M（JAVDB + 磁力） =================
JAV_SEARCH_URL = "https://javdb.com/search"
JAV_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
          "Accept-Language": "zh-TW,zh;q=0.9"}


async def _fetch_javdb(query: str) -> list:
    """搜索 JAVDB，返回前 8 個結果"""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=JAV_UA) as hc:
            params = {"q": query}
            r = await hc.get(JAV_SEARCH_URL, params=params)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        log.warning("javdb search failed: %s", e)
        return []

    # 解析 HTML，提取影片資訊
    results = []
    import re
    # 匹配影片卡片：<a href="/v/xxx" class="box" title="...">
    # 注意：class 和 title 可能順序不同，用簡單模式
    pattern = r'<a[^>]+href="/v/([^"]+)"[^>]*class="[^"]*box[^"]*"[^>]*title="([^"]*)"[^>]*>.*?<img[^>]+src="([^"]+)".*?<div class="video-title"><strong>([^<]+)</strong>([^<]*)</div>'
    matches = re.findall(pattern, html, re.DOTALL)
    for vid, title, img, code, subtitle in matches[:8]:
        results.append({
            "id": vid,
            "title": title.strip(),
            "code": code.strip(),
            "url": f"https://javdb.com/v/{vid}",
            "cover": img,
        })
    return results


async def _fetch_javdb_magnet(url: str) -> str:
    """抓取單個 JAVDB 詳情頁，返回第一個磁力連結（無則空字串）"""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=JAV_UA) as hc:
            r = await hc.get(url)
            r.raise_for_status()
            html = r.text
        m = re.search(r"magnet:\?xt=[^\"'<>]+", html)
        return m.group(0).replace("&", "&") if m else ""
    except Exception as e:
        log.warning("javdb detail failed %s: %s", url, e)
        return ""



# ================= 正妹AV 搜索 /J (goodav17.com) =================
GOODAV_SEARCH_URL = "https://goodav17.com/search"
GOODAV_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
             "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
             "Accept-Language": "zh-TW,zh;q=0.9"}


async def _fetch_goodav(query: str) -> list:
    """搜索 goodav17.com (正妹AV)，返回前 8 個結果"""
    # URL 格式: https://goodav17.com/search/QUERY
    search_url = f"{GOODAV_SEARCH_URL}/{query}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=GOODAV_UA) as hc:
            r = await hc.get(search_url)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        log.warning("goodav search failed: %s", e)
        return []

    # 解析 HTML，提取影片資訊
    results = []
    import re
    # 影片卡片結構：
    # <div class='movie'> ... <a href="URL"><img src='COVER' ... alt='...'> ... <a href="URL">TITLE</a>
    pattern = r"<div class='movie'>.*?<a href=\"([^\"]+)\">\s*<img[^>]*src='([^']+)'[^>]*alt='([^']+)'[^>]*>.*?<a href=\"[^\"]+\">([^<]+)</a>"
    matches = re.findall(pattern, html, re.DOTALL)
    for url, cover, alt, title in matches[:8]:
        # 從 alt 或 title 提取番號
        code = ""
        for text in [alt, title]:
            m = re.search(r'([A-Z]{2,5}-\d{3,5})', text, re.IGNORECASE)
            if m:
                code = m.group(1).upper()
                break
        if not code:
            m = re.search(r'/html/(\d+)', url)
            code = m.group(1) if m else ""
        results.append({
            "id": code,
            "title": title.strip(),
            "code": code,
            "url": url,
            "cover": cover,
        })
    return results


async def jav_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """JAV 搜索：/M <番號/關鍵詞> → JAVDB 搜索 → 顯示封面圖 + 鏈接"""
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text(
            "用法：/M <番號或關鍵詞>\n例：/M SSIS-001\n例：/M 葵つかさ")
        return

    status = await update.message.reply_text(f"🔍 JAVDB 搜尋「{query}」…")
    results = await _fetch_javdb(query)
    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關影片")
        return

    context.user_data["jav_results"] = results

    # 下載封面圖並行
    await status.edit_text(f"⬇️ 下載緊 {len(results)} 張封面…")
    download_tasks = [_download_img(r["cover"]) for r in results]
    downloaded = await asyncio.gather(*download_tasks)

    # 準備 media group
    from telegram import InputMediaPhoto
    media = []
    for i, (raw, item) in enumerate(zip(downloaded, results)):
        if raw and len(raw) > 1000:
            buf = io.BytesIO(raw)
            buf.seek(0)
            buf.name = "cover.jpg"
            # media group caption 唔支援 markdown link，只顯示文字
            caption = f"{item['code']} - {item['title'][:80]}"
            media.append(InputMediaPhoto(media=buf, caption=caption))
        if len(media) >= 8:
            break

    if not media:
        await status.edit_text("❌ 封面圖下載失敗")
        return

    # 發送相簿
    try:
        await context.bot.send_media_group(chat_id=update.message.chat_id, media=media)
        await status.delete()
    except Exception as e:
        log.exception("jav send_media_group failed")
        # fallback: 逐張發 + 文字
        for buf, item in zip([m.media for m in media], results):
            buf.seek(0)
            try:
                await update.message.reply_photo(photo=buf, caption=f"{item['code']} - {item['title'][:80]}\n🔗 {item['url']}")
            except Exception:
                pass
        await status.delete()
        return

    # 相簿發送成功後，額外發一條可點擊嘅連結列表
    link_lines = [f"🔗 JAVDB 連結（{query}）："]
    for item in results[:len(media)]:
        link_lines.append(f"• [{item['code']} - {item['title'][:50]}]({item['url']})")
    await update.message.reply_text("\n".join(link_lines), parse_mode="Markdown",
                                     disable_web_page_preview=True)


async def jable_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """正妹AV 搜索：/J <番號/關鍵詞> → goodav17.com 搜索 → 顯示封面 + 播放頁面連結"""
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = (update.message.reply_to_message.text or "").strip()
    if not query:
        await update.message.reply_text("用法：/J <番號或關鍵詞>\n例：/J SSIS-001\n例：/J 巨乳")
        return

    status = await update.message.reply_text(f"🔍 正妹AV 搜尋「{query}」…")
    results = await _fetch_goodav(query)
    if not results:
        await status.edit_text(f"😿 搵唔到「{query}」相關影片")
        return

    context.user_data["jav_results"] = results  # 共用 key，callback 同用

    # 下載封面圖並行
    await status.edit_text(f"⬇️ 下載緊 {len(results)} 張封面…")
    download_tasks = [_download_img(r["cover"]) for r in results]
    downloaded = await asyncio.gather(*download_tasks)

    # 準備 media group
    from telegram import InputMediaPhoto
    media = []
    for i, (raw, item) in enumerate(zip(downloaded, results)):
        if raw and len(raw) > 1000:
            buf = io.BytesIO(raw)
            buf.seek(0)
            buf.name = "cover.jpg"
            caption = f"{item['code']} - {item['title'][:80]}"
            media.append(InputMediaPhoto(media=buf, caption=caption))
        if len(media) >= 8:
            break

    if not media:
        await status.edit_text("❌ 封面圖下載失敗")
        return

    try:
        await context.bot.send_media_group(chat_id=update.message.chat_id, media=media)
        await status.delete()
    except Exception as e:
        log.exception("goodav send_media_group failed")
        for buf, item in zip([m.media for m in media], results):
            buf.seek(0)
            try:
                await update.message.reply_photo(photo=buf, caption=f"{item['code']} - {item['title'][:80]}\n🔗 {item['url']}")
            except Exception:
                pass
        await status.delete()
        return

    # 相簿後發可點擊連結
    link_lines = [f"🔗 正妹AV 連結（{query}）："]
    for item in results[:len(media)]:
        link_lines.append(f"• [{item['code']} - {item['title'][:50]}]({item['url']})")
    await update.message.reply_text("\n".join(link_lines), parse_mode="Markdown",
                                     disable_web_page_preview=True)


# ================= 自動問答 & 深夜胡說 =================
# 關鍵字觸發：中文/英文問號 + 常見粵語/普通話疑問詞/語氣詞
QUESTION_PATTERN = re.compile(
    r'[？?]'                    # 問號
    r'|點解|點咁|點做|點整|點樣|點知|點得|點會|點先'  # 粵語點X
    r'|係咩|咩係|咩嘢|咩事|邊個|邊度|邊條|邊隻|邊陣'   # 粵語疑問代詞
    r'|唔知|唔明|唔識|為咩|點解|做咩|點會|會唔會|會唔會'  # 粵語疑問句
    r'|什麼|為什麼|為何|如何|怎麼|怎樣|哪裡|哪兒|誰|甚麼'   # 普通話疑問詞
    r'|嗎|呢|吧|嘛|啊$'         # 語氣助詞結尾
)

# 貼圖池（模組級單一來源，_auto_answer 同 chatter_loop 共用，保證一致）
STICKER_FILE_IDS = [
    "CAACAgUAAxkBAAFSkSdqi-L-JRDzKVGBBifAaH9kauNCOgACMBIAAnWzWFfMKrxDZ-htzj0E",  # 魚魚魚
    "CAACAgEAAxkBAAFSkTFqi-NQs3Z6dF4lDS4KM7MO_nMvMgACxwYAAl5W0EaNDg941gy6FD0E",  # Impala
    "CAACAgEAAxkBAAFSkURqi-RKW1E4p9BaCRfB8daHKVP1XQACwQQAAgz70UbE4sUkroXwgT0E",  # 新增1
    "CAACAgUAAxkBAAFSkfdqi_HQkwsoyRMHJQRVaMx97B_4iAACQxEAAvp44FcioNqUUIk5az0E",  # ncsbymhy
    "CAACAgUAAxkBAAFSkf9qi_JpWp4Hwt-y6CI4sYhvSy3QtwACwhsAAqir6FYtmqaU2lczbz0E",  # superkekey
]


# 話題池：每次回覆隨機抽一個，令內容類型分散
CHAT_TOPICS = [
    "聊经济话题：美股、比特币、特朗普政策、美联储、通胀、加密货币",
    "讲一个冷笑话或冷知识",
    "说一句有哲学感的人生观察",
    "聊聊艺术、电影或音乐",
    "讲一个都市传说或灵异小故事",
    "发一下人生感慨，聊时间价值、机会成本",
    "纯粹说句无厘头废话",
    "用歇后语或歌词碎片回应",
    "反问群里的人一个问题，引起讨论",
    "聊赚钱路子、副业、投资心态",
    "吐槽一下日常生活的小事",
    "聊科技、AI、数码产品的观察",
]


async def _auto_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       target_text: str, sticker_prob: float = 0.2):
    """群组自动回复。sticker_prob=貼圖機率，其餘發 LLM 文字（本函數一定有回應）"""
    try:
        if STICKER_FILE_IDS and random.random() < sticker_prob:
            sticker_id = random.choice(STICKER_FILE_IDS)
            await context.bot.send_sticker(chat_id=update.message.chat_id,
                                            sticker=sticker_id)
            return

        topic = random.choice(CHAT_TOPICS)
        system_prompt = (
            "你是一个在群里吹水的人，语气自然、随意。"
            "你用简体中文普通话（混合书面语）回应，别像 AI、别客套、别开场白。"
            f"这次你的话题方向：{topic}。\n"
            "• 每句 20-50 字，可以有 emoji，别太长\n"
            "严禁：说自己是 AI、机器人、语言模型；礼貌客套话；标准客服语气；输出英文单词"
        )
        user_msg = f"群里有人说：「{target_text[:500]}」\n你作为一个在场吹水的人，自然回应一句（20-50字）。"
        raw = await _chat_complete(context, system_prompt, user_msg, max_tokens=150)
        answer = (raw.strip() or "（没答案）")[:80]
        await context.bot.send_message(chat_id=update.message.chat_id,
                                        text=answer)
    except Exception as e:
        log.exception("auto_answer failed")
        # LLM 全掛 fallback：發貼圖代替，保證有反應
        try:
            if STICKER_FILE_IDS:
                await context.bot.send_sticker(
                    chat_id=update.message.chat_id,
                    sticker=random.choice(STICKER_FILE_IDS))
        except Exception:
            log.exception("sticker fallback also failed")


async def auto_msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """非 command 文字：記錄 + 群組自動檢測 tag bot / 問題關鍵字"""
    msg = update.message
    if not msg or not msg.text:
        return

    # 面板等待輸入模式：捕獲 chat_id / 轉發 / 連結
    if context.user_data.get("admin_waiting"):
        await handle_admin_input(update, context)
        return

    # 記錄群組到註冊表（靠 msg 作最大努力）
    await register_group(update, context)

    # 記錄聊天
    name = msg.from_user.full_name if msg.from_user else "?"
    h = chat_hist(context, msg.chat_id)
    h.append({"name": name, "text": msg.text[:1000], "ts": int(msg.date.timestamp())})
    if len(h) > CHAT_HISTORY_MAX:
        del h[: len(h) - CHAT_HISTORY_MAX]
    if len(h) % 10 == 0:
        _save_hist(context.bot_data["chat_history"])

    # 只在群組自動回覆
    if msg.chat_id > 0:
        return

    # 白名單模式：非授權群組唔自動回覆
    if not is_authorized(update):
        log.info("skip auto-answer in unauthorized chat=%s", msg.chat_id)
        return

    text = msg.text.strip()
    bot_username = (context.bot.username or "").lstrip("@")

    # ① Tag bot 回覆（100% 回應：20% 貼圖 / 80% 文字）
    if bot_username and re.search(r'@' + re.escape(bot_username) + r'\b', text):
        user_part = re.sub(r'@' + re.escape(bot_username) + r'\s*', '', text).strip()
        await _auto_answer(update, context, user_part or "hi", sticker_prob=0.2)
        return

    # ② 問題關鍵字觸發（10% 回應：20% 貼圖 / 80% 文字）
    if QUESTION_PATTERN.search(text) and random.random() < 0.1:
        await _auto_answer(update, context, text, sticker_prob=0.2)
        return

    # ③ 路人隨機回覆（10% 機率會回；回時 70% 貼圖 / 30% 文字）
    if random.random() < 0.1:
        await _auto_answer(update, context, text, sticker_prob=0.7)


async def sticker_msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """有人發貼圖 → 15% 回貼圖 / 20% 回文字 / 65% 唔理"""
    msg = update.message
    if not msg or not msg.sticker or msg.chat_id > 0:
        return
    log.info("sticker_msg_handler triggered: chat=%s from=%s",
             msg.chat_id, msg.from_user.full_name if msg.from_user else "?")
    # 記錄到聊天歷史
    name = msg.from_user.full_name if msg.from_user else "?"
    h = chat_hist(context, msg.chat_id)
    h.append({"name": name, "text": "[贴图]", "ts": int(msg.date.timestamp())})
    if len(h) > CHAT_HISTORY_MAX:
        del h[: len(h) - CHAT_HISTORY_MAX]
    # 15% 回貼圖（唔用自己個 file_id，用對方發嘅，似真人互動）
    r = random.random()
    if r < 0.15:
        try:
            await context.bot.send_sticker(chat_id=msg.chat_id,
                                            sticker=msg.sticker.file_id)
        except Exception:
            log.exception("sticker reply failed")
    elif r < 0.35:
        # 20% 回文字（走 _auto_answer，sticker_prob=0 保證唔再發貼圖）
        await _auto_answer(update, context, "發貼圖", sticker_prob=0.0)
    # else 65% 唔理


async def chatter_loop(context: ContextTypes.DEFAULT_TYPE):
    """背景：每日 11:00–02:00（跨日）每 60 分鐘喺群組講一句廢話（由 45min 改為 60min）"""
    INTERVAL = 60 * 60

    def in_window() -> bool:
        # 用戶要求香港時間 11:00AM–02:00AM next day（UTC+8）
        hk = datetime.now() + timedelta(hours=8)
        h = hk.hour
        return h >= 11 or h <= 1  # HK 11–23 or 0–1

    while True:
        chat_hist_data = context.bot_data.get("chat_history", {})
        group_ids = [int(cid) for cid in chat_hist_data
                     if chat_hist_data[cid] and int(cid) < 0]
        if not group_ids:
            await asyncio.sleep(60)
            continue

        chat_id = random.choice(group_ids)
        if in_window():
            try:
                # 機率分布：20% 貼圖 / 80% LLM 文字（由 40% 貼圖改為 20%） 
                if STICKER_FILE_IDS and random.random() < 0.2:
                    sticker_id = random.choice(STICKER_FILE_IDS)
                    await context.bot.send_sticker(chat_id=chat_id,
                                                    sticker=sticker_id)
                    log.info("chatter sticker → %s: %s", chat_id, sticker_id)
                    await asyncio.sleep(INTERVAL)
                    continue

                topic = random.choice(CHAT_TOPICS)
                system_prompt = (
                    "你是一个在群里吹水的人，每日 HK 11AM-2AM 会出现。"
                    "语气自然、随意。"
                    "用简体中文普通话（混合书面语），每句 20-50 字，可以有 emoji。"
                    f"这次你的话题方向：{topic}。\n"
                    "严禁：说自己是 AI、机器人、语言模型；礼貌客套话；标准客服语气；输出英文单词"
                )
                user_msg = "现在是 HK 深夜/凌晨，群里好安静。主动说一句引起大家兴趣的话。"
                raw = await _chat_complete(context, system_prompt, user_msg, max_tokens=200)
                msg = (raw.strip() or "夜了，聊两句 🌙")[:300]
                await context.bot.send_message(chat_id=chat_id, text=msg)
                log.info("chatter → %s: %s", chat_id, msg[:40])
            except Exception as e:
                log.exception("chatter failed")
            await asyncio.sleep(INTERVAL)
        else:
            # Off window: HK 2–10 (UTC 18:00–02:00)
            hk = datetime.now() + timedelta(hours=8)
            # 跳到 HK 11:00 = UTC 03:00
            now_utc = datetime.now()
            target = now_utc.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now_utc:
                target = (now_utc + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
            wait = max(60, int((target - now_utc).total_seconds()))
            log.info("chatter idle, next window in %ds", wait)
            await asyncio.sleep(wait)


async def chatter_bootstrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第一次啟動時建立 chatter 背景任務（一次性觸發）"""
    if context.bot_data.get("chatter_started"):
        return
    context.bot_data["chatter_started"] = True
    asyncio.create_task(chatter_loop(context))


async def _set_bot_commands(app):
    """啟動時向 Telegram 註冊指令選單（底部 / 按鈕彈出）"""
    from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

    # 所有人可見的指令（名稱必須全小寫、字母數字下劃線）
    public_cmds = [
        BotCommand("start", "使用說明"),
        BotCommand("draw", "AI 生圖"),
        BotCommand("redraw", "圖生圖（回覆圖片）"),
        BotCommand("sum", "總結聊天記錄"),
        BotCommand("ans", "問問題"),
        BotCommand("sing", "搜歌"),
        BotCommand("find", "搜 TG 群組"),
        BotCommand("g", "Google 搜索"),
        BotCommand("p", "搜圖片"),
        BotCommand("v", "搜影片"),
        BotCommand("m", "搜 JAV (JAVDB)"),
        BotCommand("j", "搜 JAV (Jable 直播源)"),
        BotCommand("say", "朗讀（普通話）"),
        BotCommand("sayc", "朗讀（粵語）"),
    ]
    # 擁有者私聊專屬指令
    owner_cmds = public_cmds + [
        BotCommand("panel", "管理面板"),
    ]
    try:
        await app.bot.set_my_commands(public_cmds, scope=BotCommandScopeDefault())
        if OWNER_ID:
            await app.bot.set_my_commands(
                owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        log.info("Bot commands registered (%d public, %d owner)",
                 len(public_cmds), len(owner_cmds))
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def main():
    # concurrent_updates: 預設 PTB 係順序處理更新（一個 handler 跑完先到下一個），
    # AI 唸嘢時（/draw /sum /ans…）會令 /sing /say /貼圖回復全部排隊延遲。
    # 開並行後每個 update 獨立 task，非 AI 功能即時回應。
    # Timeout：PTB 預設 media write timeout=20s，上傳 10MB+ 音頻會 TimedOut，
    # 放寬媒體上傳到 300s；pool 都有 30s 等位，並行時唔會池排隊超時。
    app = (ApplicationBuilder().token(TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .read_timeout(60).write_timeout(120)
           .media_write_timeout(300)
           .connect_timeout(15).pool_timeout(30)
           .post_init(_set_bot_commands)
           .build())
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("draw", require_auth(draw_command)))
    app.add_handler(CommandHandler("redraw", require_auth(redraw_command)))
    app.add_handler(CommandHandler("sum", require_auth(sum_command)))
    app.add_handler(CommandHandler("say", require_auth(say_command)))
    app.add_handler(CommandHandler("sayc", require_auth(sayc_command)))
    app.add_handler(CommandHandler("ans", require_auth(ans_command)))
    app.add_handler(CommandHandler("sing", require_auth(song_command)))
    app.add_handler(CommandHandler("find", require_auth(find_command)))
    app.add_handler(CommandHandler("g", require_auth(google_search_command)))
    app.add_handler(CommandHandler("G", require_auth(google_search_command)))
    app.add_handler(CommandHandler("p", require_auth(image_search_command)))
    app.add_handler(CommandHandler("P", require_auth(image_search_command)))
    app.add_handler(CommandHandler("v", require_auth(video_search_command)))
    app.add_handler(CommandHandler("V", require_auth(video_search_command)))
    app.add_handler(CommandHandler("m", require_auth(jav_search_command)))
    app.add_handler(CommandHandler("M", require_auth(jav_search_command)))
    app.add_handler(CommandHandler("j", require_auth(jable_search_command)))
    app.add_handler(CommandHandler("J", require_auth(jable_search_command)))
    app.add_handler(CallbackQueryHandler(song_callback, pattern="^song:"))
    app.add_handler(CallbackQueryHandler(video_callback, pattern="^vid:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_msg_handler))
    app.add_handler(MessageHandler(filters.Sticker.ALL & ~filters.FORWARDED, sticker_msg_handler))
    app.add_handler(MessageHandler(filters.ALL, chatter_bootstrap))
    log.info("Draw bot started")
    app.run_polling()


if __name__ == "__main__":
    main()