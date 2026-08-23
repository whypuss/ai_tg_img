#!/usr/bin/env python3
"""Telegram /draw bot — SenseNova sensenova-u1-fast 生圖"""
import asyncio
import base64
import io
import logging
import os

import httpx
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SENSENOVA_API_KEY = os.environ["SENSENOVA_API_KEY"]
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL = "sensenova-u1-fast"
VISION_MODEL = "sensenova-6.7-flash-lite"

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

client = OpenAI(api_key=SENSENOVA_API_KEY, base_url=SENSENOVA_BASE_URL)


async def loop_run(fn):
    """同步 openai SDK 調用放 executor，避免阻塞 event loop"""
    return await asyncio.get_running_loop().run_in_executor(None, fn)


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
    """返回 (prompt, size)。支持開頭帶比例，如 /draw 16:9 一隻貓"""
    size = "2048x2048"
    if args and args[0] in SIZES:
        size = SIZES[args.pop(0)]
    return " ".join(args).strip(), size


async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt, size = parse_args(list(context.args))
    if not prompt:
        await update.message.reply_text(
            "用法：/draw <描述> [比例]\n例如：/draw 16:9 一隻賽博朋克風格嘅貓\n"
            f"支持比例：{' '.join(SIZES)}")
        return

    status = await update.message.reply_text("🎨 生成中，請稍候…")
    try:
        item = (await loop_run(lambda: client.images.generate(
            model=IMAGE_MODEL, prompt=prompt, n=1, size=size))).data[0]

        caption = f"🎨 {prompt}"
        photo = await download_image(item)
        await update.message.reply_photo(photo=photo, caption=caption)
        await status.delete()

    except Exception as e:
        log.exception("generate failed")
        msg = str(e)
        if "sensitive" in msg.lower() or "code 18" in msg:
            msg = "圖片被安全審查拒絕（敏感內容），換個描述試下"
        await status.edit_text(f"❌ 生成失敗：{msg[:400]}")


def get_source_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """由回覆嘅訊息攞原圖 bytes，唔係回覆就返回 None"""
    msg = update.message
    reply = msg.reply_to_message
    if not reply:
        return None
    photo = reply.photo or (reply.sticker if getattr(reply, "sticker", None) and not reply.sticker.is_animated else None)
    doc = reply.document
    data = None
    if photo:
        data = (photo[-1]).get_file()
    elif doc and (doc.mime_type or "").startswith("image/"):
        data = doc.get_file()
    elif reply.sticker:
        data = reply.sticker.get_file()
    else:
        return None
    buf = io.BytesIO()
    data.download(out=buf)
    buf.seek(0)
    return buf.read()


async def redraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """圖生圖：/redraw <改動描述>，回覆一張圖使用"""
    prompt = " ".join(context.args).strip()
    img = await asyncio.to_thread(get_source_image, update, context)
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
        vision = await loop_run(
            lambda: client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text":
                     "請用英文詳細描述這張圖片的畫面內容（主體、構圖、風格、色調、光線、背景），"
                     "輸出純描述，不要開場白。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                max_tokens=1024))
        base_desc = vision.choices[0].message.content.strip()

        # 2) 原圖描述 + 用戶改動要求 → 生圖
        full_prompt = f"{base_desc}. Modification: {prompt}"
        resp = await loop_run(lambda: client.images.generate(
            model=IMAGE_MODEL, prompt=full_prompt[:4000], n=1, size="2048x2048"))
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
                                    "圖生圖：回覆一張圖 + /redraw <想點改>")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("draw", draw_command))
    app.add_handler(CommandHandler("redraw", redraw_command))
    log.info("Draw bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
