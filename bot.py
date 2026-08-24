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

import httpx
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SENSENOVA_API_KEY = os.environ["SENSENOVA_API_KEY"]
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL = "sensenova-u1-fast"
VISION_MODEL = "sensenova-6.7-flash-lite"
SUMMARY_MODEL = "deepseek-v4-flash"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "x")
CHAT_MODEL_POOL = [
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
            resp = await c.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_msg}],
                max_tokens=max_tokens)
            raw = resp.choices[0].message.content
            if raw:
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
    await update.message.reply_text("Hi！用 /draw <描述> 生圖 🎨\n"
                                    "圖生圖：回覆一張圖 + /redraw <想點改>\n"
                                    "總結聊天記錄：/sum [條數，預設200]\n"
                                    "問答：/ans <問題>\n"
                                    "朗讀：/say（普通話）/sayc（粵語）<文字>")


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
            "4. 總長控制在200字左右（唔好太少，要資訊豐富）\n"
            "5. 不要口語或粵語用詞\n"
            "格式：\n📌 主要話題\n• 具體內容（包含人物、事件、數據）\n"
            "📌 討論要點\n• 詳述各人觀點與論據\n"
            "📌 結論 / 決定\n• 有共識的事項\n"
            "📌 未解決問題\n• 仍有分歧或未定之處")
        raw = await _chat_complete(context, system_prompt,
                                   f"請總結以下聊天記錄（注意保留關鍵資訊，不要過度壓縮）：\n\n{transcript}",
                                   max_tokens=500)
        summary = (raw or "").strip() or "（模型冇返回內容，試多次或者減少條數）"
        summary = summary[:300]
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

    status = await update.message.reply_text("🤖 思考中…")
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
        await status.edit_text(f"🤖 {answer}")
    except Exception as e:
        log.exception("ans failed")
        await status.edit_text(f"❌ 失敗：{str(e)[:200]}")


# ================= 自動問答 & 深夜胡說 =================
QUESTION_PATTERN = re.compile(r'[？?]|點解|點咁|點做|點整|係咩|咩係|邊個|邊度|唔知|為咩|點會|會唔會')


async def _auto_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       target_text: str):
    """群組自動回覆（Tag bot / 關鍵字觸發共用）"""
    try:
        system_prompt = ("你是一個聊天群組助手。用繁體中文書面語回答，"
                         "嚴格限制在30字以內，直接給答案，不要客套、不要開場白、不要展開。")
        user_msg = f"有人喺群組講咗：「{target_text[:500]}」\n請用最多30字回應。"
        raw = await _chat_complete(context, system_prompt, user_msg, max_tokens=100)
        answer = (raw.strip() or "（冇答案）")[:60]
        # 用 bot.send_message 直接發，唔會成為 reply
        await context.bot.send_message(chat_id=update.message.chat_id,
                                        text=f"🤖 {answer}")
    except Exception as e:
        log.exception("auto_answer failed")


async def auto_msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """非 command 文字：記錄 + 群組自動檢測 tag bot / 問題關鍵字"""
    msg = update.message
    if not msg or not msg.text:
        return

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

    text = msg.text.strip()
    bot_username = (context.bot.username or "").lstrip("@")

    # ① Tag bot 回覆
    if bot_username and re.search(r'@' + re.escape(bot_username) + r'\b', text):
        user_part = re.sub(r'@' + re.escape(bot_username) + r'\s*', '', text).strip()
        await _auto_answer(update, context, user_part or "hi")
        return

    # ② 問題關鍵字觸發
    if QUESTION_PATTERN.search(text):
        await _auto_answer(update, context, text)


async def chatter_loop(context: ContextTypes.DEFAULT_TYPE):
    """背景：每日 11:00–02:00（跨日）每 45 分鐘喺群組講一句廢話"""
    INTERVAL = 45 * 60

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
                system_prompt = ("你是一個群組聊天嚟嘅 AI，每晚 11 點到凌晨 2 點會自動出現。"
                                 "你語氣輕鬆、幽默、稍微離譜/哲學，講嘅嘢要似人講嘅，唔好好似 AI。"
                                 "用繁體中文粵語口語（混合書面語），每句 20–50 字，可以有 emoji。"
                                 "偶爾發問句勾起群友回覆，偶爾講冷知識、都市傳說、人生感悟、無厘頭比喻。"
                                 "不要客套、不要開場白、不要解釋自己係 AI。"
                                 "每一次講嘢都要唔同，唔好重複。")
                user_msg = ("而家係深夜，群組裏好靜。講一句可以引起大家興趣嘅嘢，"
                            "話題隨機（天文、歷史、都市傳說、人生、電影、飲食、奇怪冷知識都可以）。")
                raw = await _chat_complete(context, system_prompt, user_msg, max_tokens=200)
                msg = (raw.strip() or "夜咗，傾吓計唄 🌙")[:300]
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


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("draw", draw_command))
    app.add_handler(CommandHandler("redraw", redraw_command))
    app.add_handler(CommandHandler("sum", sum_command))
    app.add_handler(CommandHandler("say", say_command))
    app.add_handler(CommandHandler("sayc", sayc_command))
    app.add_handler(CommandHandler("ans", ans_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_msg_handler))
    app.add_handler(MessageHandler(filters.ALL, chatter_bootstrap))
    log.info("Draw bot started")
    app.run_polling()


if __name__ == "__main__":
    main()