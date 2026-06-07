"""抖音创作者中心数据 + 评论抓取（增强版）。

相比原版的两点增强：
1. **解析精细指标**：原版的 fetch_video_detail 把创作者中心数据接口的响应整坨 dump、不解析。
   这里改用 `item/list?fields=metrics` 同源 fetch，结构化解析出完播率/2s跳出率/封面点击率/
   各类转化率等字段（item/list 不需要 a_bogus 签名）。
2. **评论换公开接口 + a_bogus**：原版走前台滚动 / 创作者后台。创作者后台评论接口对**老作品
   会返回空**（实测），前台滚动又慢。这里改用 www.douyin.com 的公开评论接口
   `/aweme/v1/web/comment/list/` + 纯 Python a_bogus 签名（见 abogus.py），稳、快、老视频也能拿。

登录一次后 Cookie 持久化在 .auth/，之后复用。
"""
from __future__ import annotations

import asyncio
import json
import time
from random import choices
from string import ascii_letters, digits
from urllib.parse import quote, urlencode

import httpx
from playwright.async_api import BrowserContext, Page, Response, async_playwright

from abogus import ABogus, USERAGENT
from paths import auth_dir, debug_dir

CREATOR_HOME = "https://creator.douyin.com/creator-micro/home"
CREATOR_CONTENT = "https://creator.douyin.com/creator-micro/content/manage"
DATA_CENTER = "https://creator.douyin.com/creator-micro/data-center/content"

# item/list 带 metrics（完播率等）。宽时间窗 + max_cursor 翻页；该接口不需要 a_bogus。
ITEM_LIST_BASE = (
    "https://creator.douyin.com/web/api/creator/item/list"
    "?count=50&fields=visibility,metrics,review"
    "&status_list[]=102&status_list[]=143&need_long_article=true"
    "&start_time=1577808000000&end_time=1893456000000"  # 2020-01 ~ 2030-01
)

# 公开评论接口（www 域）+ a_bogus 签名
COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"
BASE_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "update_version_code": "170400", "pc_client_type": "1", "pc_libra_divert": "Windows",
    "support_h265": "1", "support_dash": "1", "version_code": "290100", "version_name": "29.1.0",
    "cookie_enabled": "true", "screen_width": "1536", "screen_height": "864",
    "browser_language": "zh-CN", "browser_platform": "Win32", "browser_name": "Chrome",
    "browser_version": "139.0.0.0", "browser_online": "true", "engine_name": "Blink",
    "engine_version": "139.0.0.0", "os_name": "Windows", "os_version": "10",
    "cpu_core_num": "16", "device_memory": "8", "platform": "PC",
    "downlink": "10", "effective_type": "4g", "round_trip_time": "200",
}


def _num(v):
    """字符串数字转 int/float；非数字原样返回。"""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def _fake_mstoken(n: int = 156) -> str:
    return "".join(choices(ascii_letters + digits + "-_", k=n))


class Session:
    """单浏览器会话，按顺序跑多步抓取。"""

    def __init__(self, ctx: BrowserContext, pw) -> None:
        self.ctx = ctx
        self.pw = pw

    @classmethod
    async def open(cls, headless: bool = False) -> "Session":
        pw = await async_playwright().start()
        auth_path = auth_dir()
        auth_path.mkdir(exist_ok=True)
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(auth_path),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        return cls(ctx, pw)

    async def close(self) -> None:
        try:
            await self.ctx.close()
        finally:
            await self.pw.stop()


async def ensure_login(timeout_s: int = 300) -> bool:
    """扫码登录；检测到 sessionid 后自动关闭。"""
    sess = await Session.open()
    try:
        page = await sess.ctx.new_page()
        await page.goto(CREATOR_HOME)
        print(f"[登录] 在弹出的 Chromium 窗口里扫码。最多等 {timeout_s} 秒……")
        for i in range(timeout_s):
            try:
                cookies = await sess.ctx.cookies("https://creator.douyin.com")
                has_session = any(c["name"] in ("sessionid", "sessionid_ss") for c in cookies)
                if has_session and "login" not in page.url:
                    print(f"[登录] ✓ 检测到登录态（用时 {i}s）")
                    await asyncio.sleep(1)
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
        print("[登录] 超时未检测到登录态。")
        return False
    finally:
        await sess.close()


async def fetch_recent_videos(sess: Session, limit: int = 50) -> list[dict]:
    """从创作者中心拉最近视频列表（基础 stat + 元信息）。"""
    captured: list[dict] = []
    all_urls: list[str] = []
    page = await sess.ctx.new_page()

    async def on_response(resp: Response) -> None:
        all_urls.append(resp.url)
        if any(k in resp.url for k in (
            "/janus/douyin/creator/pc/work_list",
            "/aweme/v1/creator/item/list",
        )):
            try:
                captured.append({"url": resp.url, "data": await resp.json()})
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(CREATOR_CONTENT, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(8)
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 1200)")
            await asyncio.sleep(1.5)
        videos = _parse_video_list(captured, limit)
        if not videos:
            dbg = debug_dir()
            dbg.mkdir(parents=True, exist_ok=True)
            (dbg / "creator_urls.txt").write_text("\n".join(all_urls), encoding="utf-8")
            print(f"[诊断] 视频列表为空，{len(all_urls)} 个请求已 dump。")
        return videos
    finally:
        await page.close()


def _parse_video_list(captured: list[dict], limit: int) -> list[dict]:
    videos: list[dict] = []
    for item in captured:
        data = item["data"]
        candidates: list = []
        if isinstance(data, dict):
            for key in ("aweme_list", "item_list", "items", "list"):
                if key in data and isinstance(data[key], list):
                    candidates = data[key]
                    break
            if not candidates and isinstance(data.get("data"), dict):
                for key in ("aweme_list", "item_list", "items", "list"):
                    if key in data["data"] and isinstance(data["data"][key], list):
                        candidates = data["data"][key]
                        break
        for v in candidates:
            videos.append(_normalize_video(v))
    seen = set()
    dedup = []
    for v in videos:
        if v["aweme_id"] in seen:
            continue
        seen.add(v["aweme_id"])
        dedup.append(v)
    return dedup[:limit]


def _normalize_video(v: dict) -> dict:
    aweme_id = v.get("aweme_id") or v.get("item_id") or v.get("id") or ""
    stats = v.get("statistics") or v.get("stats") or {}
    video_info = v.get("video") or {}
    return {
        "aweme_id": str(aweme_id),
        "desc": v.get("desc") or v.get("title") or "",
        "create_time": v.get("create_time") or v.get("createTime") or 0,
        "duration_ms": video_info.get("duration") or v.get("duration") or 0,
        "play_count": stats.get("play_count") or v.get("play_count") or 0,
        "digg_count": stats.get("digg_count") or v.get("digg_count") or 0,
        "comment_count": stats.get("comment_count") or v.get("comment_count") or 0,
        "share_count": stats.get("share_count") or v.get("share_count") or 0,
        "collect_count": stats.get("collect_count") or v.get("collect_count") or 0,
        "metrics": {},  # 由 fetch_item_metrics 填充
        "raw": v,
    }


async def fetch_item_metrics(sess: Session, target_aweme_id: str) -> dict:
    """同源 fetch item/list（带 metrics）翻页，返回目标作品的精细指标 dict。

    返回原始文本在 Python 端 json.loads——避免 JS 的 JSON.parse 把 19 位作品 id
    当 Number 丢精度（会导致 id 匹配不上）。
    """
    page = await sess.ctx.new_page()
    js = """async (url) => {
        const r = await fetch(url, {credentials:'include', headers:{accept:'application/json'}});
        return await r.text();
    }"""
    try:
        await page.goto(DATA_CENTER, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        cursor = None
        seen_cursors: set = set()
        for _ in range(60):  # 翻页上限保护
            url = ITEM_LIST_BASE if cursor is None else f"{ITEM_LIST_BASE}&max_cursor={cursor}"
            try:
                data = json.loads(await page.evaluate(js, url))
            except Exception:
                break
            if data.get("status_code") not in (0, None):
                break
            for it in data.get("items", []):
                if str(it.get("id")) == str(target_aweme_id):
                    return {k: _num(v) for k, v in (it.get("metrics") or {}).items()}
            if not data.get("has_more"):
                break
            cursor = data.get("max_cursor")
            if cursor in (None, 0) or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            await asyncio.sleep(0.4)
        return {}
    finally:
        await page.close()


def _normalize_comment_web(c: dict) -> dict:
    user = c.get("user") or {}
    return {
        "cid": str(c.get("cid") or ""),
        "text": c.get("text") or "",
        "digg_count": c.get("digg_count") or 0,
        "reply_comment_total": c.get("reply_comment_total") or 0,
        "create_time": c.get("create_time") or 0,
        "user_name": user.get("nickname") or "",
        "ip_label": c.get("ip_label") or "",
    }


def fetch_comments_abogus(aweme_id: str, cookie_str: str, max_count: int = 50) -> list[dict]:
    """www.douyin.com 公开评论接口 + a_bogus 签名。比创作者后台稳、老视频也能拿。"""
    ab = ABogus()
    http = httpx.Client(timeout=20, headers={"User-Agent": USERAGENT, "Referer": "https://www.douyin.com/"})
    out: list[dict] = []
    seen: set = set()
    cursor = 0
    try:
        for _ in range(100):  # 翻页上限保护
            params = BASE_PARAMS | {
                "aweme_id": aweme_id, "cursor": cursor, "count": 20, "item_type": "0",
                "insert_ids": "", "whale_cut_token": "", "cut_version": "1", "rcFT": "",
                "version_code": "170400", "version_name": "17.4.0", "msToken": _fake_mstoken(),
            }
            query = urlencode(params, quote_via=quote)
            query += "&a_bogus=" + ab.get_value(query, "GET")
            try:
                j = http.get(f"{COMMENT_API}?{query}", headers={"Cookie": cookie_str}).json()
            except Exception as exc:
                print(f"       评论请求异常（停止）: {exc}")
                break
            if j.get("status_code") != 0:
                break
            for c in j.get("comments") or []:
                nc = _normalize_comment_web(c)
                if nc["cid"] and nc["cid"] not in seen:
                    seen.add(nc["cid"])
                    out.append(nc)
            if not j.get("has_more") or len(out) >= max_count:
                break
            cursor = j.get("cursor")
            time.sleep(0.4)
    finally:
        http.close()
    out.sort(key=lambda x: x["digg_count"], reverse=True)
    return out[:max_count]


async def fetch_all(aweme_id: str) -> dict:
    """一个会话跑完：视频基础数据 + 精细指标；再用公开接口抓评论。"""
    sess = await Session.open()
    cookie_str = ""
    video: dict = {}
    try:
        print("  → 创作者中心：视频列表")
        videos = await fetch_recent_videos(sess, limit=50)
        video = next((v for v in videos if v["aweme_id"] == aweme_id), None)
        if not video:
            print(f"       未在最近 {len(videos)} 条里找到 {aweme_id}，用最小元数据继续。")
            video = _normalize_video({"aweme_id": aweme_id})
        else:
            print(f"       ✓ {video.get('desc', '')[:40]}")

        print("  → 创作者中心：解析精细指标（完播率等）")
        video["metrics"] = await fetch_item_metrics(sess, aweme_id)
        if video["metrics"]:
            cr = video["metrics"].get("completion_rate")
            print(f"       ✓ {len(video['metrics'])} 项指标（完播率 {cr}）")
        else:
            print("       （未取到精细指标，可能该作品不在统计范围）")

        cookies = await sess.ctx.cookies("https://www.douyin.com")
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    finally:
        await sess.close()

    print("  → 公开接口抓评论（a_bogus）")
    comments = fetch_comments_abogus(aweme_id, cookie_str, max_count=50)
    print(f"       ✓ {len(comments)} 条评论")
    return {"video": video, "comments": comments}


if __name__ == "__main__":
    asyncio.run(ensure_login())
