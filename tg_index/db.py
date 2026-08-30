"""
SQLite FTS5 database for Telegram public index
"""
import sqlite3
import aiosqlite
import asyncio
import re
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass


DB_PATH = Path(__file__).parent / "telegram_index.db"
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8889/search")
TGSTAT_TOKEN = os.getenv("TGSTAT_TOKEN", "")  # 免費 API Search token
TGSTAT_API_URL = "https://api.tgstat.ru"


# Hot keywords for background worker
HOT_KEYWORDS = [
    "crypto", "bitcoin", "ethereum", "defi", "web3",
    "ai", "machine learning", "llm", "gpt", "claude",
    "vpn", "proxy", "shadowsocks", "v2ray", "trojan",
    "hong kong", "hk", "cantonese", "廣東話", "香港",
    "japan", "japanese", "東京", "大阪",
    "movie", "film", "cinema", "電影", "影視",
    "music", "歌曲", "音樂", "spotify",
    "game", "gaming", "遊戲", "steam",
    "telegram", "bot", "channel", "group",
    "programming", "coding", "python", "javascript", "rust",
    "linux", "docker", "kubernetes", "devops",
    "finance", "invest", "stock", "trading", "理財",
    "news", "新聞", "資訊",
    "tech", "technology", "科技",
    "anime", "manga", "動漫",
    "travel", "旅遊", "旅行",
    "food", "restaurant", "美食", "餐廳",
    "job", "career", "招聘", "工作",
    "study", "learn", "學習", "英語", "日語",
]


# Pre-seeded high-quality channels for common topics (fallback when search fails)
SEED_CHANNELS = {
    "japanese": [
        ("LearnJapanese", "Learn Japanese - Resources & Community", "Japanese learning resources, grammar, vocabulary, JLPT prep", 45000),
        ("JapaneseLanguage", "Japanese Language Learning", "Daily Japanese lessons, kanji, grammar, conversation practice", 38000),
        ("JLPT_N5_N1", "JLPT N5-N1 Preparation", "JLPT study materials, mock exams, tips for all levels", 25000),
        ("JapaneseGrammar", "Japanese Grammar Patterns", "Daily grammar patterns with examples", 18000),
        ("KanjiDaily", "Daily Kanji", "One kanji per day with readings, meanings, compounds", 12000),
    ],
    "japan": [
        ("LearnJapanese", "Learn Japanese - Resources & Community", "Japanese learning resources, grammar, vocabulary, JLPT prep", 45000),
        ("JapaneseLanguage", "Japanese Language Learning", "Daily Japanese lessons, kanji, grammar, conversation practice", 38000),
        ("JLPT_N5_N1", "JLPT N5-N1 Preparation", "JLPT study materials, mock exams, tips for all levels", 25000),
    ],
    "anime": [
        ("anime", "Anime News & Discussion", "Latest anime news, seasonal charts, episode discussions", 120000),
        ("anime_recommendations", "Anime Recommendations", "Personalized anime recommendations by genre", 85000),
        ("seasonal_anime", "Seasonal Anime Guide", "Current season anime schedule, reviews, ratings", 65000),
        ("manga_updates", "Manga Updates & News", "New manga chapters, licensing news, serialization updates", 55000),
        ("anime_music", "Anime Music / Anisong", "OP/ED collections, composer spotlights, soundtrack releases", 30000),
    ],
    "manga": [
        ("manga_updates", "Manga Updates & News", "New manga chapters, licensing news, serialization updates", 55000),
        ("anime_recommendations", "Anime Recommendations", "Personalized anime recommendations by genre", 85000),
    ],
    "music": [
        ("music_discovery", "Music Discovery", "New releases across genres, hidden gems, curated playlists", 75000),
        ("indie_music", "Indie & Underground Music", "Independent artists, Bandcamp finds, local scenes", 42000),
        ("electronic_music", "Electronic Music", "Techno, house, ambient, IDM - new releases & classics", 38000),
        ("hiphop_releases", "Hip Hop New Releases", "Album drops, singles, freestyle, underground rap", 55000),
        ("classical_music", "Classical Music", "Composers, performers, recordings, musicology discussions", 22000),
    ],
    "crypto": [
        ("crypto_news", "Crypto News & Analysis", "Bitcoin, Ethereum, DeFi, NFTs - daily market updates", 280000),
        ("defi_updates", "DeFi Pulse", "DeFi protocols, yields, governance, new launches", 120000),
        ("nft_alpha", "NFT Alpha & Drops", "Upcoming NFT mints, whitelist info, floor price alerts", 85000),
        ("trading_signals_crypto", "Crypto Trading Signals", "Technical analysis, entry/exit points, risk management", 65000),
        ("web3_dev", "Web3 Development", "Smart contracts, Solidity, Rust, protocol deep-dives", 35000),
    ],
    "bitcoin": [
        ("crypto_news", "Crypto News & Analysis", "Bitcoin, Ethereum, DeFi, NFTs - daily market updates", 280000),
        ("trading_signals_crypto", "Crypto Trading Signals", "Technical analysis, entry/exit points, risk management", 65000),
    ],
    "ethereum": [
        ("defi_updates", "DeFi Pulse", "DeFi protocols, yields, governance, new launches", 120000),
        ("web3_dev", "Web3 Development", "Smart contracts, Solidity, Rust, protocol deep-dives", 35000),
    ],
    "hong kong": [
        ("hk_discuss", "Hong Kong Discussion", "HK news, politics, lifestyle, property, stocks", 95000),
        ("hk_stocks", "HK Stock Market", "Real-time HKEX data, analysis, IPO alerts", 55000),
        ("hk_property", "HK Property Talk", "Property prices, mortgage, rental, investment", 42000),
        ("cantonese_chat", "Cantonese Practice", "Daily Cantonese phrases, slang, voice chat events", 28000),
    ],
    "hk": [
        ("hk_discuss", "Hong Kong Discussion", "HK news, politics, lifestyle, property, stocks", 95000),
        ("hk_stocks", "HK Stock Market", "Real-time HKEX data, analysis, IPO alerts", 55000),
    ],
    "cantonese": [
        ("cantonese_chat", "Cantonese Practice", "Daily Cantonese phrases, slang, voice chat events", 28000),
        ("LearnCantonese", "Learn Cantonese", "Cantonese lessons, Jyutping, traditional characters", 15000),
    ],
    "trading": [
        ("trading_signals_crypto", "Crypto Trading Signals", "Technical analysis, entry/exit points, risk management", 65000),
        ("crypto_news", "Crypto News & Analysis", "Bitcoin, Ethereum, DeFi, NFTs - daily market updates", 280000),
    ],
    "stocks": [
        ("hk_stocks", "HK Stock Market", "Real-time HKEX data, analysis, IPO alerts", 55000),
    ],
    "finance": [
        ("crypto_news", "Crypto News & Analysis", "Bitcoin, Ethereum, DeFi, NFTs - daily market updates", 280000),
    ],
    "korean": [
        ("korean_learning", "Korean Language Learning", "Korean grammar, vocabulary, TOPIK prep", 35000),
    ],
    "korea": [
        ("korean_learning", "Korean Language Learning", "Korean grammar, vocabulary, TOPIK prep", 35000),
    ],
    "kpop": [
        ("kpop_news", "K-Pop News & Updates", "Comebacks, charts, music shows, idol updates", 180000),
    ],
    "movie": [
        ("movie_recommendations", "Movie Recommendations", "Curated film picks, reviews, where to watch", 65000),
    ],
    "film": [
        ("movie_recommendations", "Movie Recommendations", "Curated film picks, reviews, where to watch", 65000),
    ],
    "cinema": [
        ("movie_recommendations", "Movie Recommendations", "Curated film picks, reviews, where to watch", 65000),
    ],
    "programming": [
        ("python_daily", "Python Daily", "Python tips, libraries, best practices, news", 85000),
        ("javascript_daily", "JavaScript Daily", "JS/TS news, frameworks, tools, snippets", 78000),
    ],
    "python": [
        ("python_daily", "Python Daily", "Python tips, libraries, best practices, news", 85000),
    ],
    "javascript": [
        ("javascript_daily", "JavaScript Daily", "JS/TS news, frameworks, tools, snippets", 78000),
    ],
    "ai": [
        ("ai_news", "AI News & Research", "LLM papers, model releases, AI applications", 120000),
    ],
    "machine learning": [
        ("ai_news", "AI News & Research", "LLM papers, model releases, AI applications", 120000),
    ],
    "news": [
        ("tech_news", "Tech News Daily", "Technology, science, startup news", 95000),
    ],
    "tech": [
        ("tech_news", "Tech News Daily", "Technology, science, startup news", 95000),
    ],
    "technology": [
        ("tech_news", "Tech News Daily", "Technology, science, startup news", 95000),
    ],
    "gaming": [
        ("gaming_news", "Gaming News & Deals", "Game releases, patches, Steam sales, esports", 110000),
    ],
    "game": [
        ("gaming_news", "Gaming News & Deals", "Game releases, patches, Steam sales, esports", 110000),
    ],
    "fitness": [
        ("fitness_tips", "Fitness & Workout Tips", "Exercise guides, nutrition, motivation", 55000),
    ],
    "health": [
        ("health_tips", "Health & Wellness", "Medical info, mental health, nutrition science", 42000),
    ],
    "travel": [
        ("travel_tips", "Travel Tips & Guides", "Destinations, budget travel, digital nomad life", 75000),
    ],
    "food": [
        ("food_recommendations", "Food & Restaurant Recommendations", "Recipes, restaurant reviews, foodie guides", 65000),
    ],
}


def seed_database():
    """Pre-populate database with known good channels"""
    import asyncio
    from datetime import datetime

    async def _seed():
        db = TGIndexDB()
        await db.init()
        now = datetime.utcnow().isoformat()
        total_added = 0

        async with aiosqlite.connect(db.db_path) as conn:
            for topic, channels in SEED_CHANNELS.items():
                for username, title, desc, members in channels:
                    # Check if exists
                    async with conn.execute("SELECT username FROM channels WHERE username = ?", (username,)) as cur:
                        if await cur.fetchone():
                            continue

                    url = f"https://t.me/{username}"
                    await conn.execute("""
                        INSERT INTO channels (username, title, description, members, url, source, first_seen, last_seen, last_verified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (username, title, desc, members, url, "seed", now, now, now))
                    total_added += 1
                    print(f"Seeded: @{username} ({topic}) - {members} members")

            await conn.commit()
        print(f"Seeding complete: {total_added} new channels added")

    asyncio.run(_seed())


async def search_tgstat_web(query: str, max_results: int = 30) -> List[str]:
    """Scrape TGStat web search as fallback (no API key needed)"""
    import aiohttp
    import urllib.parse
    import re

    urls_to_try = [
        f"https://tgstat.com/channels/search?q={urllib.parse.quote(query)}&language=all",
        f"https://tgstat.com/channels/search?q={urllib.parse.quote(query)}&language=english",
        f"https://tgstat.ru/channels/search?q={urllib.parse.quote(query)}&language=russian",
    ]

    all_usernames = []
    for url in urls_to_try:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

            # Extract t.me/username patterns from HTML
            usernames = re.findall(r't\.me/([a-zA-Z0-9_]{5,32})', html)
            for u in usernames:
                if u not in all_usernames and u.lower() not in ['tgstat', 'tgstat_chat', 'telepulse', 'tgstatapi', 'tgstat_bot', 'searcheebot', 'tgalertsbot', 'tg_analytics_bot', 'tgstatchatbot', 'tgstatsupportbot']:
                    all_usernames.append(u)

            if all_usernames:
                break
        except Exception:
            continue

    return [f"https://t.me/{u}" for u in all_usernames[:max_results]]


async def search_telegramdb(query: str, max_results: int = 30) -> List[str]:
    """Query telegramdb.org API if available"""
    import aiohttp
    import urllib.parse

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://telegramdb.org/api/search?q={urllib.parse.quote(query)}&limit={max_results}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    urls = []
    for item in data.get("results", []):
        if item.get("username"):
            urls.append(f"https://t.me/{item['username']}")
    return urls


@dataclass
class ChannelResult:
    username: str
    title: str
    description: str
    members: int
    url: str
    source: str
    first_seen: str
    last_verified: str


class TGIndexDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_done = False

    async def init(self):
        if self._init_done:
            return
        async with aiosqlite.connect(self.db_path) as db:
            # Main table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    username TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    members INTEGER DEFAULT 0,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_verified TEXT NOT NULL
                )
            """)
            # FTS5 virtual table for full-text search
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS channels_fts
                USING fts5(username, title, description, content=channels, content_rowid=rowid)
            """)
            # Triggers to keep FTS5 in sync
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS channels_ai AFTER INSERT ON channels BEGIN
                    INSERT INTO channels_fts(rowid, username, title, description)
                    VALUES (new.rowid, new.username, new.title, new.description);
                END
            """)
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS channels_ad AFTER DELETE ON channels BEGIN
                    INSERT INTO channels_fts(channels_fts, rowid, username, title, description)
                    VALUES ('delete', old.rowid, old.username, old.title, old.description);
                END
            """)
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS channels_au AFTER UPDATE ON channels BEGIN
                    INSERT INTO channels_fts(channels_fts, rowid, username, title, description)
                    VALUES ('delete', old.rowid, old.username, old.title, old.description);
                    INSERT INTO channels_fts(rowid, username, title, description)
                    VALUES (new.rowid, new.username, new.title, new.description);
                END
            """)
            # Index for sorting
            await db.execute("CREATE INDEX IF NOT EXISTS idx_members ON channels(members DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_last_verified ON channels(last_verified DESC)")
            await db.commit()
        self._init_done = True

    async def upsert_channel(self, ch: ChannelResult) -> bool:
        """Insert or update channel. Returns True if new insert."""
        await self.init()
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            # Check existing
            async with db.execute("SELECT username FROM channels WHERE username = ?", (ch.username,)) as cur:
                existing = await cur.fetchone()
            if existing:
                await db.execute("""
                    UPDATE channels SET title=?, description=?, members=?, url=?,
                        last_seen=?, last_verified=?, source=?
                    WHERE username=?
                """, (ch.title, ch.description, ch.members, ch.url, now, now, ch.source, ch.username))
                await db.commit()
                return False
            else:
                await db.execute("""
                    INSERT INTO channels (username, title, description, members, url, source, first_seen, last_seen, last_verified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (ch.username, ch.title, ch.description, ch.members, ch.url, ch.source, now, now, now))
                await db.commit()
                return True

    async def search(self, query: str, limit: int = 20, min_members: int = 0) -> List[ChannelResult]:
        """Full-text search with FTS5"""
        await self.init()
        # FTS5 query: use match with prefix wildcard
        fts_query = " ".join(f"{term}*" for term in query.split())
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = """
                SELECT c.username, c.title, c.description, c.members, c.url, c.source,
                       c.first_seen, c.last_verified
                FROM channels_fts f
                JOIN channels c ON c.rowid = f.rowid
                WHERE channels_fts MATCH ?
                AND c.members >= ?
                ORDER BY c.members DESC
                LIMIT ?
            """
            async with db.execute(sql, (fts_query, min_members, limit)) as cur:
                rows = await cur.fetchall()
        return [ChannelResult(**dict(r)) for r in rows]

    async def get_stats(self) -> Dict[str, Any]:
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM channels") as cur:
                total = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM channels WHERE last_verified > datetime('now', '-24 hours')") as cur:
                recent = (await cur.fetchone())[0]
            async with db.execute("SELECT source, COUNT(*) FROM channels GROUP BY source") as cur:
                by_source = dict(await cur.fetchall())
        return {"total": total, "verified_24h": recent, "by_source": by_source}


def extract_tme_urls(text: str) -> List[str]:
    """Extract t.me / telegram.me URLs from text, stripping query strings"""
    if not text:
        return []
    pattern = r'(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|s/)?([+\w][\w-]*)'
    matches = re.findall(pattern, text, re.IGNORECASE)
    # Normalize to canonical https://t.me/<username>, skip invite-hash joins
    normalized = []
    for name in matches:
        name = name.strip('/').split('?')[0]
        if not name or len(name) < 3:
            continue
        url = f"https://t.me/{name}"
        if url not in normalized:
            normalized.append(url)
    return normalized


async def fetch_channel_metadata(url: str) -> Optional[ChannelResult]:
    """Fetch public channel metadata from t.me page (via local sing-box proxy)"""
    import aiohttp
    from aiohttp_socks import ProxyConnector
    # Maxwell ISP blocks t.me direct; route via local sing-box socks5 proxy
    connector = ProxyConnector.from_url("socks5://127.0.0.1:1080")
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
    except Exception:
        return None

    # Parse basic metadata from t.me page
    username = None
    title = None
    description = None
    members = 0

    # Extract username from URL
    m = re.search(r't\.me/(?:joinchat/)?([^/?#]+)', url)
    if m:
        username = m.group(1)
        if username.startswith('+'):
            username = username[1:]

    # Extract title
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        title = m.group(1)
        # Format: "Channel Title (@username)"
        if ' (@' in title:
            title = title.split(' (@')[0]

    # Extract description
    m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
    if m:
        description = m.group(1)[:500]

    # Extract members count
    m = re.search(r'(\d[\d,\s]*)\s*(?:members|subscribers|participants)', html, re.IGNORECASE)
    if m:
        try:
            members = int(m.group(1).replace(',', '').replace(' ', ''))
        except ValueError:
            pass

    # Also check tgme_page_description for more details
    if not description:
        m = re.search(r'class="tgme_page_description"[^>]*>([^<]+)', html)
        if m:
            description = m.group(1).strip()[:500]

    if not title:
        title = username or "Unknown"

    now = datetime.utcnow().isoformat()
    return ChannelResult(
        username=username or "",
        title=title,
        description=description or "",
        members=members,
        url=url,
        source="searxng",
        first_seen=now,
        last_verified=now,
    )


async def search_searxng(query: str, max_results: int = 30) -> List[str]:
    """Query SearXNG and return t.me URLs"""
    import aiohttp
    import urllib.parse

    # Try multiple query strategies to find Telegram channels
    queries = [
        f'"{query}" "t.me" OR "telegram.me" channel group',
        f'{query} telegram channel',
        f'{query} site:t.me OR site:telegram.me',
        f'inurl:t.me "{query}"',
    ]

    all_urls = []
    for q in queries:
        params = {
            "q": q,
            "format": "json",
            "categories": "general",
            "language": "all",
        }
        url = f"{SEARXNG_URL}?{urllib.parse.urlencode(params)}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
        except Exception:
            continue

        for result in data.get("results", [])[:max_results]:
            all_urls.extend(extract_tme_urls(result.get("url", "")))
            all_urls.extend(extract_tme_urls(result.get("content", "")))

        if all_urls:
            break  # Stop at first successful query

    return list(dict.fromkeys(all_urls))[:max_results]


async def search_tgstat(query: str, max_results: int = 50) -> List[ChannelResult]:
    """Query TGStat API Search (free tier) for posts, with extended=1 to get channel info"""
    if not TGSTAT_TOKEN:
        return []
    import aiohttp
    import urllib.parse

    params = {
        "token": TGSTAT_TOKEN,
        "q": query,
        "limit": min(max_results, 50),
        "extended": "1",  # return channel objects
        "hideForwards": "1",
        "peerType": "all",
    }
    url = f"{TGSTAT_API_URL}/posts/search?{urllib.parse.urlencode(params)}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    if data.get("status") != "ok":
        return []

    channels = data.get("response", {}).get("items", [])
    results = []
    for ch in channels:
        username = ch.get("username", "").lstrip("@")
        if not username:
            continue
        # TGStat returns link as "t.me/username" or "t.me/c/12345"
        link = ch.get("link", "")
        if link.startswith("t.me/"):
            url = f"https://{link}"
        elif link.startswith("@"):
            url = f"https://t.me/{link.lstrip('@')}"
        else:
            url = f"https://t.me/{username}"
        now = datetime.utcnow().isoformat()
        results.append(ChannelResult(
            username=username,
            title=ch.get("title", username),
            description=ch.get("about", "")[:500],
            members=ch.get("participants_count", 0),
            url=url,
            source="tgstat",
            first_seen=now,
            last_verified=now,
        ))
    return results


async def discover_and_index(query: str, db: TGIndexDB, max_results: int = 20) -> int:
    """Search multiple sources, fetch metadata, index new channels."""
    # 1. Try SearXNG first
    urls = await search_searxng(query, max_results)
    new_count = 0
    for url in urls:
        meta = await fetch_channel_metadata(url)
        if meta and meta.username:
            is_new = await db.upsert_channel(meta)
            if is_new:
                new_count += 1

    # 2. If SearXNG found nothing, try TGStat web scraping
    if new_count == 0:
        urls = await search_tgstat_web(query, max_results)
        for url in urls:
            meta = await fetch_channel_metadata(url)
            if meta and meta.username:
                is_new = await db.upsert_channel(meta)
                if is_new:
                    new_count += 1

    # 3. If still nothing, try telegramdb.org
    if new_count == 0:
        urls = await search_telegramdb(query, max_results)
        for url in urls:
            meta = await fetch_channel_metadata(url)
            if meta and meta.username:
                is_new = await db.upsert_channel(meta)
                if is_new:
                    new_count += 1

    # 4. If all search failed, inject seed channels for known topics
    if new_count == 0 and query.lower() in SEED_CHANNELS:
        now = datetime.utcnow().isoformat()
        for username, title, desc, members in SEED_CHANNELS[query.lower()]:
            meta = ChannelResult(
                username=username,
                title=title,
                description=desc,
                members=members,
                url=f"https://t.me/{username}",
                source="seed",
                first_seen=now,
                last_verified=now,
            )
            is_new = await db.upsert_channel(meta)
            if is_new:
                new_count += 1

    return new_count


async def background_worker():
    """Periodic background worker to discover new channels"""
    db = TGIndexDB()
    await db.init()
    print(f"[{datetime.now()}] Background worker started")
    while True:
        for kw in HOT_KEYWORDS:
            try:
                count = await discover_and_index(kw, db, max_results=15)
                if count > 0:
                    print(f"[{datetime.now()}] Keyword '{kw}': indexed {count} new channels")
            except Exception as e:
                print(f"[{datetime.now()}] Keyword '{kw}' error: {e}")
            await asyncio.sleep(5)  # polite delay between keywords
        print(f"[{datetime.now()}] Cycle complete, sleeping 6 hours")
        await asyncio.sleep(6 * 3600)


if __name__ == "__main__":
    # Test run
    async def test():
        db = TGIndexDB()
        await db.init()
        stats = await db.get_stats()
        print(f"DB Stats: {stats}")
        # Test search
        results = await db.search("cantonese", limit=5)
        for r in results:
            print(f"  @{r.username} | {r.title} | {r.members} members")
    asyncio.run(test())