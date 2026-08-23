# ai_tg_img

Telegram 生圖 Bot — 用 SenseNova (sensenova-u1-fast) OpenAI 兼容端點生成圖片。

## 功能

- `/draw <描述>` — 文生圖，回覆圖片
- `/draw <比例> <描述>` — 指定比例（如 `/draw 16:9 a cyberpunk cat`）
- 支持 11 種比例：1:1 2:3 3:2 3:4 4:3 4:5 5:4 16:9 9:16 21:9 9:21
- 圖片即時下載後發送（SenseNova 返回嘅臨時 URL 只有 1 小時有效）

## 部署

```bash
python3 -m venv venv
./venv/bin/pip install python-telegram-bot openai httpx

cat > .env << EOF
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
SENSENOVA_API_KEY=sk-xxxx
EOF
chmod 600 .env

python bot.py
```

### systemd

```ini
[Unit]
Description=Telegram Draw Bot (SenseNova)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/drawbot
EnvironmentFile=/opt/drawbot/.env
ExecStart=/opt/drawbot/venv/bin/python /opt/drawbot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 群組使用

喺 @BotFather → Bot Settings → Group Privacy → Turn off，
bot 先可以喺群組接收所有成員嘅 `/draw` 指令。

## 注意

- 模型固定 `sensenova-u1-fast`，最小尺寸 2048x2048
- prompt 上限 4096 tokens；安全審查嚴格，敏感內容會返回 code 18
