#!/usr/bin/env python3
"""JAVBUS JSON Plugin v1 engine — Python port for Telegram bot.

Covers: baseUrl/announcement (single + multi-hop), GET search/detail,
html (regex) + json (dot-path), templates, fields/fileFields/defaults.
Ref: docs/json_plugin_v1.md + example.json
"""
import base64
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("javbus")

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

# ── utils ──
def _b64_decode(s: str, urlsafe: bool = False) -> str:
    try:
        pad = "=" * (-len(s) % 4)
        data = base64.urlsafe_b64decode(s + pad) if urlsafe else base64.b64decode(s + pad)
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return s

def _hex_decode(s: str) -> str:
    try:
        return bytes.fromhex(s.strip()).decode("utf-8", errors="ignore")
    except Exception:
        return s

def _decode_value(s: str, method: str) -> str:
    m = (method or "none").lower()
    if m == "base64":
        return _b64_decode(s, False)
    if m == "base64url":
        return _b64_decode(s, True)
    if m == "hex":
        return _hex_decode(s)
    return s

def _strip_html(text: str) -> str:
    if not text:
        return ""
    # 去标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 解码实体
    text = text.replace("&nbsp;", " ").replace("&#160;", " ").replace("&#xA0;", " ")
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    # 合并空白
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _get_by_path(obj: Any, path: str) -> Any:
    """点路径读取，支持数组下标 data.0.name"""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur

def _render_template(tmpl: str, vars_map: dict) -> str:
    """模板替换，支持已知变量，未知保留原样"""
    if not tmpl:
        return tmpl
    # 特殊：query 家族已在外层计算好
    for k, v in vars_map.items():
        tmpl = tmpl.replace("{" + k + "}", str(v) if v is not None else "")
    return tmpl

def _build_vars(query: str, page: int, extra: dict = None) -> dict:
    q_enc = urllib.parse.quote(query, safe="")
    q_raw = query
    # URL-safe base64 去掉末尾 =
    q_b64 = base64.urlsafe_b64encode(query.encode("utf-8")).decode().rstrip("=")
    q_b64_std = base64.b64encode(query.encode("utf-8")).decode().rstrip("=")
    vars_map = {
        "query": q_enc,
        "queryRaw": q_raw,
        "queryBase64": q_b64,
        "queryBase64Std": q_b64_std,
        "page": str(page),
        "page0": str(page - 1),
    }
    if extra:
        vars_map.update(extra)
    return vars_map

def _resolve_url(base: str, url: str) -> str:
    if not url:
        return base or ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not base:
        return url
    return urllib.parse.urljoin(base.rstrip("/") + "/", url.lstrip("/"))

# ── announcement ──
_announcement_cache: Dict[str, str] = {}  # plugin_id -> resolved baseUrl

async def resolve_base_url(plugin: dict, http: httpx.AsyncClient = None) -> str:
    """解析 announcement 返回最终 baseUrl，带内存缓存"""
    pid = plugin.get("id", "")
    if pid in _announcement_cache:
        return _announcement_cache[pid]

    ann = plugin.get("announcement") or {}
    if not ann.get("enabled"):
        return (plugin.get("baseUrl") or "").strip()

    # 有缓存且非首次搜索？此处仅内存缓存，重启后会重新解析
    base = (plugin.get("baseUrl") or "").strip()
    # 如果 baseUrl 已有且非空，先尝试直接用；失败时再走发布页（由调用方重试逻辑处理）
    # 这里只做发布页解析
    try:
        resolved = await _resolve_announcement(plugin, http)
        if resolved:
            _announcement_cache[pid] = resolved
            log.info("announcement %s -> %s", pid, resolved)
            return resolved
    except Exception as e:
        log.warning("announcement resolve failed %s: %s", pid, e)
    return base

async def _resolve_announcement(plugin: dict, http: httpx.AsyncClient = None) -> str:
    ann = plugin.get("announcement") or {}
    steps = ann.get("steps")
    headers = plugin.get("headers") or {}
    close = False
    if http is None:
        http = httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers)
        close = True

    try:
        # 多跳
        if isinstance(steps, list) and len(steps) > 0:
            prev_url = None
            value = ""
            for idx, step in enumerate(steps):
                url_tmpl = step.get("url") or step.get("URL") or ""
                # 第一步必须有 url，后续可省略 -> 用 {value}
                if idx == 0:
                    if not url_tmpl:
                        raise ValueError("announcement steps[0] missing url")
                    url = url_tmpl
                else:
                    if not url_tmpl:
                        url_tmpl = "{value}"
                    # 变量替换 {value}/{url}/{origin}/{host}
                    if prev_url:
                        parsed = urllib.parse.urlparse(prev_url if value.startswith("http") else value)
                        # value 可能是裸域名，补成 url
                        val_url = value if value.startswith("http") else "https://" + value
                        parsed_val = urllib.parse.urlparse(val_url)
                        origin = f"{parsed_val.scheme}://{parsed_val.netloc}"
                        host = parsed_val.hostname or ""
                    else:
                        origin = ""
                        host = ""
                    url = url_tmpl.replace("{value}", value).replace("{url}", value).replace("{origin}", origin).replace("{host}", host)
                    # 如果替换后不是完整 url 且 value 是域名，尝试补全
                    if not url.startswith("http") and value:
                        # 相对路径情况
                        url = _resolve_url(value if value.startswith("http") else "https://" + value, url)

                pattern = step.get("urlPattern") or step.get("extract") or ""
                decoding = step.get("urlDecoding") or step.get("decode") or "none"
                target = step.get("targetPattern") or ""

                r = await http.get(url)
                r.raise_for_status()
                html = r.text
                candidates = re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL)
                # re.findall 对无捕获组返回完整匹配，有捕获组返回捕获组内容
                # 统一取第1个捕获组
                if not candidates:
                    raise ValueError(f"announcement step {idx} no match url={url} pattern={pattern[:60]}")
                # 如果 pattern 有多捕获组，findall 返回 tuple
                flat = []
                for c in candidates:
                    if isinstance(c, tuple):
                        c = c[0] if c else ""
                    flat.append(c.strip())

                # targetPattern 优先：附近 500 字符内包含关键词
                if target:
                    pri, rest = [], []
                    for c in flat:
                        # 查找该候选在 html 中的位置附近是否含 target
                        pos = html.find(c)
                        nearby = html[max(0, pos - 500): pos + len(c) + 500] if pos >= 0 else ""
                        if target in nearby:
                            pri.append(c)
                        else:
                            rest.append(c)
                    ordered = pri + rest
                else:
                    ordered = flat

                # 去重保持顺序
                seen = set()
                uniq = []
                for c in ordered:
                    dc = _decode_value(c, decoding).strip()
                    if dc and dc not in seen:
                        seen.add(dc)
                        uniq.append(dc)
                if not uniq:
                    raise ValueError(f"announcement step {idx} empty after decode")
                value = uniq[0]
                # 标准化为完整 URL
                if not value.startswith("http"):
                    # 裸域名
                    if "." in value and "/" not in value:
                        value = "https://" + value
                prev_url = url
            # 最终 value 即 baseUrl
            if not value.endswith("/"):
                # 补斜杠便于 urljoin
                if "." in value and value.count("/") == 2:  # https://host
                    value += "/"
            return value

        # 单步
        url = ann.get("url", "")
        pattern = ann.get("urlPattern", "")
        decoding = ann.get("urlDecoding", "none")
        target = ann.get("targetPattern", "")
        if not url or not pattern:
            return (plugin.get("baseUrl") or "").strip()
        r = await http.get(url)
        r.raise_for_status()
        html = r.text
        candidates = re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if not candidates:
            raise ValueError(f"announcement no match url={url}")
        flat = [c[0] if isinstance(c, tuple) else c for c in candidates]
        flat = [c.strip() for c in flat if c and c.strip()]
        if target:
            pri, rest = [], []
            for c in flat:
                pos = html.find(c)
                nearby = html[max(0, pos - 500): pos + len(c) + 500] if pos >= 0 else ""
                if target in nearby:
                    pri.append(c)
                else:
                    rest.append(c)
            flat = pri + rest
        seen = set()
        uniq = []
        for c in flat:
            dc = _decode_value(c, decoding).strip()
            if dc and dc not in seen:
                seen.add(dc)
                uniq.append(dc)
        if not uniq:
            raise ValueError("announcement empty after decode")
        value = uniq[0]
        if not value.startswith("http"):
            if "." in value and "/" not in value:
                value = "https://" + value
        if not value.endswith("/") and value.count("/") == 2:
            value += "/"
        return value
    finally:
        if close:
            await http.aclose()

def clear_announcement_cache(plugin_id: str = None):
    if plugin_id:
        _announcement_cache.pop(plugin_id, None)
    else:
        _announcement_cache.clear()

# ── plugin loading ──
def load_plugins(plugins_dir: str = None) -> List[dict]:
    d = plugins_dir or PLUGINS_DIR
    if not os.path.isdir(d):
        return []
    plugins = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(d, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                p = json.load(f)
            # 跳过 disabled
            if p.get("enabled") is False:
                continue
            # 基本校验
            if not p.get("id") or not p.get("search"):
                log.warning("plugin %s missing id/search skip", fname)
                continue
            p["_file"] = fname
            plugins.append(p)
        except Exception as e:
            log.warning("load plugin %s failed: %s", fname, e)
    return plugins

# ── search parsing ──
def _apply_defaults(item: dict, plugin: dict) -> dict:
    defaults = plugin.get("defaults") or {}
    for k, tmpl in defaults.items():
        if item.get(k):
            continue
        # 构造变量：infoHash 家族 + sourceItemId
        info_hash = item.get("infoHash") or ""
        vars_map = {
            "infoHash": info_hash,
            "infoHashLower": info_hash.lower(),
            "infoHashUpper": info_hash.upper(),
            "infoHashEncoded": urllib.parse.quote(info_hash, safe=""),
            "infoHashLowerEncoded": urllib.parse.quote(info_hash.lower(), safe=""),
            "infoHashUpperEncoded": urllib.parse.quote(info_hash.upper(), safe=""),
            "sourceItemId": item.get("sourceItemId") or "",
            "sourceItemIdEncoded": urllib.parse.quote(str(item.get("sourceItemId") or ""), safe=""),
        }
        item[k] = _render_template(str(tmpl), vars_map)
    return item

def _map_fields(raw: dict, plugin: dict) -> Optional[dict]:
    """处理 fields 映射 + defaults + 必要字段检查"""
    fields = plugin.get("fields") or {}
    item = {}
    for dst, src in fields.items():
        # dst 是内部字段，src 是捕获组名或 json path
        # raw 来自 regex 命名捕获或 json 路径，已是展开的 dict
        item[dst] = raw.get(src, "")

    # 清理：html 捕获值去标签
    for k, v in list(item.items()):
        if isinstance(v, str):
            # 已经在提取时 _strip_html，这里不再重复，但保留
            item[k] = v.strip()
        elif v is None:
            item[k] = ""
        else:
            item[k] = str(v)

    # defaults 补全 magnet/webUrl 等
    item = _apply_defaults(item, plugin)

    # 必要字段：sourceItemId 必须有，否则丢弃（用 infoHash 兜底）
    if not item.get("sourceItemId"):
        if item.get("infoHash"):
            item["sourceItemId"] = item["infoHash"]
        else:
            return None

    # webUrl 相对补全
    if item.get("webUrl"):
        base = (plugin.get("baseUrl") or "").strip()
        # 若 announcement 已解析，用缓存
        pid = plugin.get("id", "")
        if pid in _announcement_cache:
            base = _announcement_cache[pid]
        item["webUrl"] = _resolve_url(base, item["webUrl"])

    # magnet 标准化
    if item.get("magnet"):
        item["magnet"] = item["magnet"].strip()

    # 插件来源标记
    item["_plugin_id"] = plugin.get("id", "")
    item["_plugin_name"] = plugin.get("name", plugin.get("id", ""))

    return item

def _parse_html_search(html: str, plugin: dict) -> List[dict]:
    search = plugin.get("search") or {}
    root_pat = search.get("rootPattern") or ""
    item_pat = search.get("itemPattern") or ""
    if not item_pat:
        return []

    # JAVBUS 插件用 JS/Dart 風格命名捕獲 (?<name>...)，Python 需轉為 (?P<name>...)
    def _js_to_py(pat: str) -> str:
        return re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', pat)

    # 先截取 root 区域
    scope = html
    if root_pat:
        try:
            py_root = _js_to_py(root_pat)
            m = re.search(py_root, html, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
            if m:
                scope = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        except re.error as e:
            log.warning("rootPattern regex error %s: %s", plugin.get("id"), e)

    results = []
    fields = plugin.get("fields") or {}
    field_order = list(fields.keys())  # 顺序捕获时按此顺序

    try:
        py_item = _js_to_py(item_pat)
        regex = re.compile(py_item, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
    except re.error as e:
        log.warning("itemPattern regex error %s: %s", plugin.get("id"), e)
        return []

    for m in regex.finditer(scope):
        raw = {}
        gd = m.groupdict()
        if gd and any(v is not None for v in gd.values()):
            # 命名捕获组
            for k, v in gd.items():
                if v is not None:
                    raw[k] = _strip_html(v)
        else:
            # 顺序捕获组
            groups = m.groups()
            for idx, field_key in enumerate(field_order):
                if idx < len(groups) and groups[idx] is not None:
                    src = fields[field_key]  # 此时 src 只是占位，实际按顺序
                    # 顺序模式：捕获组按 fields 出现顺序对应
                    raw[src] = _strip_html(groups[idx])
                    # 同时也按 dst 存一份，方便 _map_fields 直接取
                    raw[field_key] = _strip_html(groups[idx])

        item = _map_fields(raw, plugin)
        if item:
            results.append(item)

    return results

def _parse_json_search(data: Any, plugin: dict) -> List[dict]:
    search = plugin.get("search") or {}
    items_path = search.get("itemsPath") or ""
    fields = plugin.get("fields") or {}

    items_raw = _get_by_path(data, items_path) if items_path else data
    if not isinstance(items_raw, list):
        log.warning("json search itemsPath %s not list plugin=%s", items_path, plugin.get("id"))
        return []

    results = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        raw = {}
        for dst, src_path in fields.items():
            val = _get_by_path(entry, src_path)
            if val is not None:
                raw[src_path] = str(val) if not isinstance(val, str) else val
                raw[dst] = raw[src_path]
        # 也保留原始 entry 供 defaults 使用（infoHash 等）
        for k, v in entry.items():
            if k not in raw:
                raw[k] = str(v) if not isinstance(v, str) else v

        item = _map_fields(raw, plugin)
        if item:
            results.append(item)
    return results

# ── main search ──
async def search_one_plugin(plugin: dict, query: str, page: int = 1, http: httpx.AsyncClient = None) -> Tuple[List[dict], dict]:
    """搜索单个插件，返回 (items, meta). meta 含 total/lastPage/currentPage"""
    search = plugin.get("search") or {}
    response_type = (search.get("responseType") or "json").lower()
    url_tmpl = search.get("url") or ""
    page_size = int(search.get("pageSize") or 20)
    headers = {**(plugin.get("headers") or {}), **(search.get("headers") or {})}

    base_url = await resolve_base_url(plugin, http)
    vars_map = _build_vars(query, page)
    url = _render_template(url_tmpl, vars_map)
    url = _resolve_url(base_url, url)

    # 请求
    close = False
    if http is None:
        http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers)
        close = True
    else:
        # 合并 headers 到请求
        pass

    try:
        # 如果外来 http 没有对应 headers，单独传
        r = await http.get(url, headers=headers)
        r.raise_for_status()

        if response_type == "html":
            html = r.text
            items = _parse_html_search(html, plugin)
            # total / lastPage 从 html 正则提取
            total = None
            last_page = None
            total_pat = search.get("totalPattern") or ""
            if total_pat:
                try:
                    py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', total_pat)
                    m = re.search(py_pat, html, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    if m:
                        total = int(re.sub(r"\D", "", m.group(1)) or 0)
                except Exception:
                    pass
            last_pat = search.get("lastPagePattern") or ""
            if last_pat:
                try:
                    py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', last_pat)
                    m = re.search(py_pat, html, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    if m:
                        last_page = int(re.sub(r"\D", "", m.group(1)) or 0)
                except Exception:
                    pass
            if last_page is None and total is not None and page_size > 0:
                last_page = max(1, (total + page_size - 1) // page_size)
            meta = {"total": total, "lastPage": last_page, "currentPage": page, "pageSize": page_size}
            return items, meta
        else:
            # json
            try:
                data = r.json()
            except Exception:
                # 可能是 text/json
                data = json.loads(r.text)
            items = _parse_json_search(data, plugin)
            total = None
            last_page = None
            cur_page = page
            try:
                if search.get("totalPath"):
                    v = _get_by_path(data, search["totalPath"])
                    if v is not None:
                        total = int(str(v).strip())
                if search.get("lastPagePath"):
                    v = _get_by_path(data, search["lastPagePath"])
                    if v is not None:
                        last_page = int(str(v).strip())
                if search.get("currentPagePath"):
                    v = _get_by_path(data, search["currentPagePath"])
                    if v is not None:
                        cur_page = int(str(v).strip())
            except Exception:
                pass
            if last_page is None and total is not None and page_size > 0:
                last_page = max(1, (total + page_size - 1) // page_size)
            meta = {"total": total, "lastPage": last_page, "currentPage": cur_page, "pageSize": page_size}
            return items, meta
    except httpx.HTTPStatusError as e:
        # 搜索失败时尝试发布页刷新重试一次
        ann = plugin.get("announcement") or {}
        if ann.get("enabled") and e.response.status_code in (404, 403, 500, 502, 503):
            try:
                clear_announcement_cache(plugin.get("id"))
                new_base = await resolve_base_url(plugin, http)
                new_url = _render_template(url_tmpl, vars_map)
                new_url = _resolve_url(new_base, new_url)
                if new_url != url:
                    r2 = await http.get(new_url, headers=headers)
                    r2.raise_for_status()
                    if response_type == "html":
                        items = _parse_html_search(r2.text, plugin)
                    else:
                        data = r2.json()
                        items = _parse_json_search(data, plugin)
                    return items, {"total": None, "lastPage": None, "currentPage": page, "pageSize": page_size}
            except Exception as e2:
                log.warning("retry after announcement failed %s: %s", plugin.get("id"), e2)
        raise
    finally:
        if close:
            await http.aclose()

async def search_all_plugins(query: str, page: int = 1, plugins: List[dict] = None) -> Dict[str, Any]:
    """并发搜索所有插件，合并结果"""
    import asyncio
    pls = plugins if plugins is not None else load_plugins()
    if not pls:
        return {"items": [], "meta": {}, "errors": ["no plugins"]}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        async def one(p):
            try:
                items, meta = await search_one_plugin(p, query, page, http)
                return p.get("id"), items, meta, None
            except Exception as e:
                log.warning("plugin %s search error: %s", p.get("id"), e)
                return p.get("id"), [], {}, str(e)

        results = await asyncio.gather(*[one(p) for p in pls])

    all_items = []
    errors = []
    for pid, items, meta, err in results:
        if err:
            errors.append(f"{pid}: {err}")
        all_items.extend(items)

    # 去重：按 magnet 或 infoHash
    seen = set()
    uniq = []
    for it in all_items:
        key = (it.get("magnet") or it.get("infoHash") or it.get("sourceItemId") or "").strip()
        if not key:
            uniq.append(it)
            continue
        if key not in seen:
            seen.add(key)
            uniq.append(it)

    # 按 seeders / score 降序（如果有）
    def sort_key(x):
        try:
            s = int(str(x.get("seeders") or 0).replace(",", "").strip() or 0)
        except:
            s = 0
        return -s

    uniq.sort(key=sort_key)

    return {"items": uniq, "meta": {"page": page, "count": len(uniq)}, "errors": errors}

# ── detail ──
async def fetch_detail(plugin: dict, item: dict, http: httpx.AsyncClient = None) -> dict:
    """抓取详情页，补全 magnet/infoHash/files"""
    detail = plugin.get("detail")
    if not detail:
        return item

    response_type = (detail.get("responseType") or "html").lower()
    url_tmpl = detail.get("url") or ""
    headers = {**(plugin.get("headers") or {}), **(detail.get("headers") or {})}
    base_url = await resolve_base_url(plugin, http)

    # 详情变量
    info_hash = item.get("infoHash") or ""
    vars_map = {
        "sourceItemId": item.get("sourceItemId") or "",
        "sourceItemIdEncoded": urllib.parse.quote(str(item.get("sourceItemId") or ""), safe=""),
        "infoHash": info_hash,
        "infoHashLower": info_hash.lower(),
        "infoHashUpper": info_hash.upper(),
        "infoHashEncoded": urllib.parse.quote(info_hash, safe=""),
        "infoHashLowerEncoded": urllib.parse.quote(info_hash.lower(), safe=""),
        "infoHashUpperEncoded": urllib.parse.quote(info_hash.upper(), safe=""),
    }
    url = _render_template(url_tmpl, vars_map)
    url = _resolve_url(base_url, url)

    close = False
    if http is None:
        http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers)
        close = True
    try:
        r = await http.get(url, headers=headers)
        r.raise_for_status()

        if response_type == "html":
            html = r.text
            # file 列表
            files = []
            file_root_pat = detail.get("fileRootPattern") or ""
            file_pat = detail.get("filePattern") or ""
            scope = html
            if file_root_pat:
                try:
                    py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', file_root_pat)
                    m = re.search(py_pat, html, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    if m:
                        scope = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                except re.error:
                    pass
            if file_pat:
                try:
                    py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', file_pat)
                    fre = re.compile(py_pat, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    file_fields = plugin.get("fileFields") or {}
                    for m in fre.finditer(scope):
                        entry = {}
                        gd = m.groupdict()
                        if gd and any(v is not None for v in gd.values()):
                            for k, v in gd.items():
                                if v is not None:
                                    entry[k] = _strip_html(v)
                        else:
                            # 顺序
                            groups = m.groups()
                            order = list(file_fields.keys())
                            for idx, fk in enumerate(order):
                                if idx < len(groups) and groups[idx] is not None:
                                    entry[file_fields[fk]] = _strip_html(groups[idx])
                                    entry[fk] = _strip_html(groups[idx])
                        # 映射
                        mapped = {}
                        for dst, src in (file_fields or {}).items():
                            mapped[dst] = entry.get(src, entry.get(dst, ""))
                        if mapped.get("path"):
                            files.append(mapped)
                except re.error as e:
                    log.warning("filePattern error %s: %s", plugin.get("id"), e)

            # 主 itemPattern 补全字段
            item_pat = detail.get("itemPattern") or ""
            if item_pat:
                try:
                    # 先 rootPattern
                    root_pat = detail.get("rootPattern") or ""
                    d_scope = html
                    if root_pat:
                        py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', root_pat)
                        m2 = re.search(py_pat, html, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                        if m2:
                            d_scope = m2.group(1) if m2.lastindex and m2.lastindex >= 1 else m2.group(0)
                    py_pat = re.sub(r'\(\?<([A-Za-z_][A-Za-z0-9_]*)>', r'(?P<\1>', item_pat)
                    mre = re.compile(py_pat, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    m = mre.search(d_scope)
                    if m:
                        raw = {}
                        gd = m.groupdict()
                        if gd and any(v is not None for v in gd.values()):
                            for k, v in gd.items():
                                if v is not None:
                                    raw[k] = _strip_html(v)
                        else:
                            groups = m.groups()
                            fields = plugin.get("fields") or {}
                            order = list(fields.keys())
                            for idx, fk in enumerate(order):
                                if idx < len(groups) and groups[idx] is not None:
                                    raw[fields[fk]] = _strip_html(groups[idx])
                                    raw[fk] = _strip_html(groups[idx])
                        # 合并到 item（缺失才补）
                        for k, v in raw.items():
                            # fields 映射的值也要映射到 dst
                            # raw 已含命名捕获，这里直接按 fields 映射
                            pass
                        # 用 _map_fields 风格：把 raw 按 fields 映射后合并
                        mapped = {}
                        for dst, src in (plugin.get("fields") or {}).items():
                            if src in raw and raw[src]:
                                mapped[dst] = raw[src]
                            elif dst in raw and raw[dst]:
                                mapped[dst] = raw[dst]
                        for k, v in mapped.items():
                            if not item.get(k) and v:
                                item[k] = v
                except re.error as e:
                    log.warning("detail itemPattern error %s: %s", plugin.get("id"), e)

            if files:
                item["files"] = files

        else:
            # json detail
            data = r.json()
            root_path = detail.get("rootPath") or ""
            files_path = detail.get("filesPath") or ""
            obj = _get_by_path(data, root_path) if root_path else data
            if isinstance(obj, dict):
                fields = plugin.get("fields") or {}
                for dst, src in fields.items():
                    val = _get_by_path(obj, src)
                    if val is not None and not item.get(dst):
                        item[dst] = str(val)
                # files
                if files_path:
                    arr = _get_by_path(obj if root_path else data, files_path) or _get_by_path(data, files_path)
                    if isinstance(arr, list):
                        ffields = plugin.get("fileFields") or {}
                        files = []
                        for entry in arr:
                            if not isinstance(entry, dict):
                                continue
                            f = {}
                            for dst, src in ffields.items():
                                v = _get_by_path(entry, src)
                                if v is not None:
                                    f[dst] = str(v)
                            if f.get("path"):
                                files.append(f)
                        if files:
                            item["files"] = files
        # 最后再跑一次 defaults
        item = _apply_defaults(item, plugin)
        return item
    finally:
        if close:
            await http.aclose()
