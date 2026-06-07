"""把抓到的数据渲染成 NotebookLM 友好的 Markdown。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path


def _fmt_time(ts: int) -> str:
    if not ts:
        return "未知"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_num(n) -> str:
    if n is None or n == "":
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 10000:
        return f"{n / 10000:.1f}w"
    return str(int(n)) if n == int(n) else f"{n:.2f}"


def _fmt_duration(ms: int) -> str:
    if not ms:
        return "-"
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


# 精细指标渲染顺序：(key, 中文标签, 是否百分比)
METRIC_LABELS = [
    ("completion_rate", "完播率", True),
    ("completion_rate_5s", "5秒完播率", True),
    ("avg_view_second", "平均播放时长(秒)", False),
    ("avg_view_proportion", "平均播放进度", True),
    ("bounce_rate_2s", "2秒跳出率", True),
    ("cover_click_rate", "封面点击率", True),
    ("like_rate", "点赞率", True),
    ("comment_rate", "评论率", True),
    ("share_rate", "分享率", True),
    ("favorite_rate", "收藏率", True),
    ("fan_view_proportion", "粉丝播放占比", True),
    ("homepage_visit_count", "主页访问数", False),
    ("subscribe_count", "新增关注", False),
    ("unsubscribe_count", "取关数", False),
]


def render_report(video: dict, script: str, comments: list[dict]) -> str:
    lines: list[str] = []
    desc = video.get("desc") or "(无标题)"
    aweme_id = video["aweme_id"]

    lines.append(f"# {desc}")
    lines.append("")
    lines.append(f"- 视频 ID：`{aweme_id}`")
    lines.append(f"- 发布时间：{_fmt_time(video.get('create_time', 0))}")
    lines.append(f"- 时长：{_fmt_duration(video.get('duration_ms', 0))}")
    lines.append(f"- 链接：https://www.douyin.com/video/{aweme_id}")
    lines.append(f"- 抓取时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append("## 播放数据")
    lines.append("")
    lines.append(f"- 播放：{_fmt_num(video.get('play_count'))}")
    lines.append(f"- 点赞：{_fmt_num(video.get('digg_count'))}")
    lines.append(f"- 评论：{_fmt_num(video.get('comment_count'))}")
    lines.append(f"- 收藏：{_fmt_num(video.get('collect_count'))}")
    lines.append(f"- 分享：{_fmt_num(video.get('share_count'))}")
    lines.append("")

    metrics = video.get("metrics") or {}
    rendered = [(lbl, _pct(metrics[k]) if pct else _fmt_num(metrics[k]))
                for k, lbl, pct in METRIC_LABELS
                if k in metrics and metrics[k] not in (None, "", -1)]
    lines.append("## 精细指标（创作者中心）")
    lines.append("")
    if rendered:
        for lbl, val in rendered:
            lines.append(f"- {lbl}：{val}")
        lines.append("")
        lines.append("> 完播率 / 2秒跳出率 / 封面点击率是判断「钩子抓不抓人、内容留不留人」的核心信号——"
                     "比单纯播放量更能解释一条为什么爆 / 为什么扑。")
    else:
        lines.append("（未取到——该作品可能不在创作者中心统计范围，或为共创/特殊作品）")
    lines.append("")

    lines.append("## 原始稿子")
    lines.append("")
    lines.append(script.strip() if script.strip() else "（未提供）")
    lines.append("")

    lines.append(f"## 评论（按点赞降序，共 {len(comments)} 条）")
    lines.append("")
    if not comments:
        lines.append("（未抓到评论，可能评论区被关闭）")
    else:
        for c in comments:
            text = (c.get("text") or "").replace("\n", " ").strip()
            reply = f" 💬{c['reply_comment_total']}" if c.get("reply_comment_total") else ""
            loc = f" [{c['ip_label']}]" if c.get("ip_label") else ""
            name = c.get("user_name") or ""
            prefix = f"- [👍{c['digg_count']}{reply}]{loc}"
            lines.append(f"{prefix} {name}：{text}" if name else f"{prefix} {text}")
    lines.append("")

    return "\n".join(lines)


def slugify(text: str, max_len: int = 30) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if ch in bad else ch for ch in text).strip()
    return out[:max_len] or "untitled"


def output_dir_for(video: dict, root: Path) -> Path:
    date = _fmt_time(video.get("create_time", 0))[:10].replace("未知", "nodate")
    slug = slugify(video.get("desc") or video["aweme_id"])
    return root / f"{date}_{slug}"
