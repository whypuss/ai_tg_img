# ai_tg_img

Telegram 生圖 / 多功能 Bot — SenseNova (sensenova-u1-fast) + 多模型輪詢 + 本地 SearXNG 搜索。

## 功能一覽

| 指令 | 說明 |
|------|------|
| `/draw <描述> [比例]` | 🎨 文生圖，支持 11 種比例（1:1, 16:9, 9:16, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 21:9, 9:21） |
| `/redraw <想點改>` | 🖌️ 圖生圖：回覆一張圖片 + 指令，用 SenseNova Vision 理解圖片再重繪 |
| `/sum [條數]` | 📝 總結聊天記錄（預設 200 條，最多 500） |
| `/ans <問題>` | 💬 問答：直接問或回覆訊息追問，嚴格 ≤30 字回答 |
| `/sing <歌名/歌手>` | 🎵 搜歌（joox/netease/kuwo/bilibili 四子源），支援播放 + 下載 |
| `/g <關鍵詞>` | 🔍 網頁搜索：透過本地 SearXNG（自動選可用引擎，避開 CAPTCHA），返回前 5 條結果 |
| `/find <關鍵字>` | 🔎 搜 Telegram 公開群組/頻道（本地 FTS5 + SearXNG 發現） |
| `/say <文字>` | 🔊 普通話 TTS（Edge TTS zh-CN-XiaoxiaoNeural） |
| `/sayc <文字>` | 🔊 粵語 TTS（Edge TTS zh-HK-HiuMaanNeural） |
| 回覆訊息 + `/say` | 自動朗讀被回覆的文字 |

### 自動回覆（群組）

- **@Tag bot** (`@whyimg_bot`) → 100% 回覆（20% 貼圖 / 80% 文字）
- **問題關鍵字** (`?`、`點解`、`什麼` 等) → 10% 機率回覆
- **路人隨機** → 10% 機率回覆（70% 貼圖 / 30% 文字）
- **別人發貼圖** → 15% 回貼圖 / 20% 回文字 / 65% 唔理
- **深夜主動發言** (HK 11:00–02:00) → 每 60 分鐘一次（20% 貼圖 / 80% 文字）

> 模型池：opencode (nemotron/hy3/laguna) + openrouter/free + sensenova (deepseek/glm/6.7-flash)

## 本地開發

```bash
# 1. 建立虛擬環境
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. 環境變數
cat > .env << EOF
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
SENSENOVA_API_KEY=sk-xxxx
OPENROUTER_API_KEY=sk-or-xxxx   # 可選
OPENCODE_API_KEY=sk-xxxx        # 可選
OPENCODE_API_KEY_2=sk-xxxx      # 可選
TGSTAT_TOKEN=xxxxx              # 可選，/find 用
EOF
chmod 600 .env

# 3. 執行
./venv/bin/python bot.py
```

### 依賴
```
python-telegram-bot>=21.0
openai>=1.0
httpx>=0.27
edge-tts
```

## 生產部署 (systemd, Maxwell)

```ini
# /etc/systemd/system/drawbot.service
[Unit]
Description=Telegram Draw Bot (SenseNova)
After=network-online.target

[Service]
Type=simple
User=maxwell
WorkingDirectory=/home/maxwell/drawbot
EnvironmentFile=/home/maxwell/drawbot/.env
ExecStart=/home/maxwell/drawbot/venv/bin/python /home/maxwell/drawbot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now drawbot
journalctl -u drawbot -f
```

## SearXNG (搜索後端)

Maxwell 已跑 Docker：
```bash
docker run -d --name searxng -p 8889:8080 \
  -e BASE_URL=http://localhost:8889 \
  -e INSTANCE_NAME=local \
  searxng/searxng:latest
```

Bot 內呼叫 `http://localhost:8889/search?q=...&format=json&categories=general`，不指定 engines 讓 SearXNG 自動用可用的（避開 google/bing CAPTCHA）。

## 群組權限

@BotFather → Bot Settings → Group Privacy → **Turn off**，
bot 先能喺群組讀取非指令訊息（記錄聊天、自動回覆、/sum、/find 生效）。

## 注意事項

- SenseNova 生圖模型固定 `sensenova-u1-fast`，最小尺寸 2048×2048，URL 1 小時過期
- Prompt 上限 4096 tokens；安全審查嚴格（code 18 = 敏感內容）
- 聊天記錄存 `.chat_history.json`，每 10 條自動落盤
- 並行更新已開啟 (`concurrent_updates=True`)：AI 生圖/總結唔會阻塞其他指令