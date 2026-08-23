#!/usr/bin/env python3
"""Telegram /draw bot — SenseNova sensenova-u1-fast 生圖"""
import base64
import io
import logging
import os

import httpx
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SENSENOVA_API_KEY = os.environ["SENSENOVA_API_KEY"]
SENSENOVA_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL = "sensenova-u1-fast"

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
        # openai SDK 的 images.generate 內部用同步 http；放 executor 避免阻塞 event loop
        import asyncio
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: client.images.generate(
            model=IMAGE_MODEL, prompt=prompt, n=1, size=size))
        item = resp.data[0]

        caption = f"🎨 {prompt}"
        if getattr(item, "url", None):
            # URL 只有 1 小時有效 → 即刻下載再發送，避免 Telegram 伺服器拉取失敗
            async with httpx.AsyncClient(timeout=120) as hc:
                r = await hc.get(item.url)
                r.raise_for_status()
                photo = io.BytesIO(r.content)
                photo.name = "image.png"
            await update.message.reply_photo(photo=photo, caption=caption)
        elif getattr(item, "b64_json", None):
            photo = io.BytesIO(base64.b64decode(item.b64_json))
            photo.name = "image.png"
            await update.message.reply_photo(photo=photo, caption=caption)
        else:
            await status.edit_text("❌ 返回格式不認得")
            return
        await status.delete()

    except Exception as e:
        log.exception("generate failed")
        msg = str(e)
        if "sensitive" in msg.lower() or "code 18" in msg:
            msg = "圖片被安全審查拒絕（敏感內容），換個描述試下"
        await status.edit_text(f"❌ 生成失敗：{msg[:400]}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi！用 /draw <描述> 生圖 🎨")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("draw", draw_command))
    log.info("Draw bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
