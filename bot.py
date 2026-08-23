#!/usr/bin/env python3
"""Telegram /draw bot — SenseNova sensenova-u1-fast 生圖"""
import asyncio
import base64
import io
import json
import logging
import os

import httpx
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SENSENOVA_API_KEY = os.environ["SENSENOVA_API_KEY"]
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL = "sensenova-u1-fast"
VISION_MODEL = "sensenova-6.7-flash-lite"
SUMMARY_MODEL = "deepseek-v4-flash"  # 非 reasoning 模型，/sum 用（flash-lite 燒晒 tokens 喺 thinking）
# 模型輪詢池：多個 provider 輪流用，分散 quota
FALLBACK_API_KEY = os.environ.get("OPENCODE_API_KEY", "x")
CHAT_MODEL_POOL = [
    # (provider_label, client, model)
    ("sn-deepseek", lambda: client, "deepseek-v4-flash"),
    ("sn-glm",      lambda: client, "glm-5.2"),
    ("sn-6.7",      lambda: client, "sensenova-6.7-flash-lite"),
    ("oc-nemotron", lambda: AsyncOpenAI(api_key=FALLBACK_API_KEY, base_url="https://opencode.ai/zen/v1"), "nemotron-3-ultra-free"),
    ("oc-preview",  lambda: AsyncOpenAI(api_key=FALLBACK_API_KEY, base_url="https://opencode.ai/zen/v1"), "x-preview-f-free"),
    ("oc-laguna",   lambda: AsyncOpenAI(api_key=FALLBACK_API_KEY, base_url="https://opencode.ai/zen/v1"), "laguna-s-2.1-free"),
]

# 全局輪詢計數器（bot_data 持久化，每個 request 輪轉）
def _next_pool_idx(context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.bot_data.get("pool_idx", 0)
    context.bot_data["pool_idx"] = (idx + 1) % len(CHAT_MODEL_POOL)
    return idx

async def _chat_complete(context: ContextTypes.DEFAULT_TYPE,
                         system_prompt: str, user_msg: str,
                         max_tokens: int = 1024) -> str:
    """輪詢池調用：順序試每個模型，第一個成功返回，429/403 自動跳下一個"""
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
            err = f"{label}: {e}"
            log.warning("pool skip %s", err)
            errors.append(err)
            continue
    raise RuntimeError("所有模型輪詢完都失敗：" + " | ".join(errors[-5:]))

# 比例快捷映射 → SenseNova 支持的尺寸
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
    """SenseNova 返回 URL（1小時有效）→ 即刻下載；或 b64 解碼"""
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
    """返回 (prompt, size)。比例可放開頭或結尾，如 /draw 9:16 一隻貓 或 /draw 一隻貓 9:16"""
    size = "2048x2048"
    items = list(args)
    # 嘗試頭部
    if items and items[0] in SIZES:
        size = SIZES[items.pop(0)]
    # 嘗試結尾
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
    """由回覆嘅訊息攞原圖 bytes，唔係回覆就返回 None"""
    msg = update.message
    reply = msg.reply_to_message
    if not reply:
        return None
    photo = reply.photo or (reply.sticker if getattr(reply, "sticker", None) and not reply.sticker.is_animated else None)
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
    """圖生圖：/redraw <改動描述>，回覆一張圖使用"""
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

        # 1) 用視覺模型分析原圖 → 詳細描述 prompt
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

        # 2) 原圖描述 + 用戶改動要求 → 生圖
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
                                    "總結聊天記錄：/sum [條數，預設100]")


# ---------------- 聊天記錄總結（持久化到檔案，重啟不丟）----------------
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
    """被動記錄每個 chat 嘅文字訊息（需要 Group Privacy off 先收齊）"""
    msg = update.message or update.edited_message
    if not msg or not msg.text or msg.text.startswith("/"):
        return
    name = msg.from_user.full_name if msg.from_user else "?"
    h = chat_hist(context, msg.chat_id)
    h.append({"name": name, "text": msg.text[:1000], "ts": int(msg.date.timestamp())})
    if len(h) > CHAT_HISTORY_MAX:
        del h[: len(h) - CHAT_HISTORY_MAX]
    # 每 10 條保存一次，避免寫入太頻
    if len(h) % 10 == 0:
        _save_hist(context.bot_data["chat_history"])


async def sum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """總結最近 N 句聊天記錄：/sum [N]"""
    n = 100
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
            "你是一個群組聊天總結助手。用繁體中文書面語輸出簡潔總結（不要口語或粵語用詞），"
            "總字數嚴格限制在120字以內：主要話題、討論要點、決定、未解決問題。用 bullet list。")
        raw = await _chat_complete(context, system_prompt,
                                   f"請總結以下聊天記錄：\n\n{transcript}",
                                   max_tokens=300)
        summary = (raw or "").strip() or "（模型冇返回內容，試多次或者減少條數）"
        summary = summary[:120]
        await status.edit_text(f"📝 最近 {len(h)} 句總結\n\n{summary[:4000]}",
                               disable_web_page_preview=True)
    except Exception as e:
        log.exception("sum failed")
        await status.edit_text(f"❌ 總結失敗：{str(e)[:400]}")


# ---------------- TTS（Edge-TTS，免費）----------------
VOICE_CANTONESE = "zh-HK-HiuMaanNeural"  # 粵語女聲
VOICE_MANDARIN = "zh-CN-XiaoxiaoNeural"  # 普通話女聲


async def _tts_stream(communicate):
    """edge-tts 帶重試：TimeoutError / 網絡抖動時重連一次（音頻其實已收大部分）"""
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
        # 回覆某句訊息 + /say → 自動朗讀該訊息文字
        text = (update.message.reply_to_message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "用法：/say <文字>（普通話）或 /sayc <文字>（粵語），"
            "或者回覆一句訊息打 /say 自動朗讀，上限500字")
        return

    status = await update.message.reply_text("🔊 合成中…")
    try:
        text = text[:500]
        import edge_tts
        # 回覆訊息可能好長 → 分段合成；超時重試一次
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for attempt in _tts_stream(communicate):
            if attempt["type"] == "audio":
                buf.write(attempt["data"])
        buf.seek(0)
        buf.name = "speech.mp3"
        await update.message.reply_voice(voice=buf)
        await status.delete()
    except Exception as e:
        log.exception("tts failed")
        # 音頻其實收到咗（只是串流尾超時）→ 照發
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
    """/ans 問答：回覆訊息 + /ans（加追問），或直接 /ans 問題"""
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


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("draw", draw_command))
    app.add_handler(CommandHandler("redraw", redraw_command))
    app.add_handler(CommandHandler("sum", sum_command))
    app.add_handler(CommandHandler("say", say_command))
    app.add_handler(CommandHandler("sayc", sayc_command))
    app.add_handler(CommandHandler("ans", ans_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, record_message))
    log.info("Draw bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
