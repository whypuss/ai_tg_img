"""
SQLite FTS5 database for Telegram public index
"""
import sqlite3
import aiosqlite
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass


DB_PATH = Path(__file__).parent / "telegram_index.db"
SEARXNG_URL = "http://192.168.31.55:8889/search"


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
]


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

    params = {
        "q": f"{query} site:t.me OR site:telegram.me",
        "format": "json",
        "categories": "general",
        "language": "all",
    }
    url = f"{SEARXNG_URL}?{urllib.parse.urlencode(params)}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    urls = []
    for result in data.get("results", [])[:max_results]:
        urls.extend(extract_tme_urls(result.get("url", "")))
        urls.extend(extract_tme_urls(result.get("content", "")))

    return list(dict.fromkeys(urls))[:max_results]


async def discover_and_index(query: str, db: TGIndexDB, max_results: int = 20) -> int:
    """Search SearXNG, fetch metadata, index new channels. Returns count of new channels."""
    urls = await search_searxng(query, max_results)
    new_count = 0
    for url in urls:
        meta = await fetch_channel_metadata(url)
        if meta and meta.username:
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