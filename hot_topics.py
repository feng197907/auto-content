"""热榜选题抓取：抓全网实时热榜 + 国内技术社区热榜，带真实摘要和原文链接。

设计思路（重要）：
    全网热榜多是大众话题，与"AI/数码科技"垂直领域未必相关。
    所以不是直接拿热榜写稿，而是：
      1. 抓头条/百度实时热榜（带热度值、真实摘要、原文链接）
      2. 抓掘金技术热榜（国内开发者社区实时榜，纯技术内容）
      3. 用领域关键词打分，筛出相关的
      4. 相关的优先用并标注热度，不够时用备用词保底

    每条条目都带 desc（真实新闻摘要）和 url（原文链接），
    供下游"基于真实素材写作"使用，避免 AI 凭空编造。
    没有摘要的源（头条/CSDN）desc 留空，下游不会据此编造细节。

    领域关键词在 config.py 的 KEYWORDS_STRONG / KEYWORDS_WEAK 里配置，
    改方向只需改那两组词，不用动本文件。

数据源无需 key，失败自动降级，不影响主流程。
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any

import requests

import config

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TOUTIAO_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
BAIDU_URL = "https://top.baidu.com/board?tab=realtime"
GITHUB_API = "https://api.github.com/search/repositories"
JUEJIN_HOT_API = "https://api.juejin.cn/content_api/v1/content/article_rank"


def fetch_toutiao(timeout: int = 8) -> list[dict[str, Any]]:
    """抓今日头条热榜，返回带原文链接的条目。

    头条接口只给标题和热度，不提供正文摘要，因此 desc 留空，
    绝不用推测或编造的内容填充。
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            TOUTIAO_URL, headers=HEADERS, timeout=timeout
        )
        response.raise_for_status()
        for item in response.json().get("data", []):
            title = item.get("Title", "").strip()
            if not title:
                continue
            result.append({
                "title": title,
                "desc": "",
                "url": item.get("Url", ""),
                "hot": int(item.get("HotValue") or 0),
                "source": "头条热榜",
            })
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"  头条热榜抓取失败: {exc}")
    return result


def fetch_baidu(timeout: int = 8) -> list[dict[str, Any]]:
    """抓百度实时热搜，返回带真实摘要和原文链接的条目。

    百度把数据嵌在 HTML 里，每个条目是一个 JSON 对象，字段顺序为
    appUrl/desc/hotScore/.../url/word —— desc、hotScore、url 都在 word 之前。

    因此按 word 切块：每个 word 的"当前块"= 上一个 word 结束到本 word 结束，
    在该块内取 desc/url/hotScore，保证三者一定属于同一条目。
    （旧实现在 word 之后的窗口里找 url，当前条目缺失时会误抓下一条的链接。）
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            BAIDU_URL, headers=HEADERS, timeout=timeout
        )
        response.raise_for_status()
        text = response.text
    except requests.RequestException as exc:
        print(f"  百度热搜抓取失败: {exc}")
        return result

    prev_end = 0
    for word_match in re.finditer(r'"word":"([^"]+)"', text):
        block = text[prev_end:word_match.end()]
        prev_end = word_match.end()

        title = word_match.group(1).strip()
        if not title:
            continue

        desc_match = re.search(r'"desc":"([^"]*)"', block)
        url_match = re.search(r'"url":"([^"]*)"', block)
        hot_match = re.search(r'"hotScore":"?(\d+)"?', block)

        result.append({
            "title": title,
            "desc": re.sub(r"\s+", " ", desc_match.group(1)).strip() if desc_match else "",
            "url": (
                url_match.group(1).replace("\\/", "/")
                if url_match else ""
            ),
            "hot": int(hot_match.group(1)) if hot_match else 0,
            "source": "百度热搜",
        })
    return result


def fetch_github(limit: int = 5, days: int = 7) -> list[dict[str, Any]]:
    """抓 GitHub 近 N 天活跃的 AI 相关热门仓库，作为技术选题。

    用官方 Search API（无需 token，限流 10 次/分钟，每天跑一次足够）。
    返回真实项目名、简介、仓库链接和 star 数，供 AI 基于事实写作。
    失败静默返回空列表，不影响主流程。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        response = requests.get(
            GITHUB_API,
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
            params={
                # 近 N 天有更新、100+ star、名称或简介含 AI 的项目
                "q": f"AI in:name,description stars:>100 pushed:>{since}",
                "sort": "stars",
                "order": "desc",
                "per_page": min(max(limit * 2, 5), 20),
            },
            timeout=10,
        )
        response.raise_for_status()
        repos = response.json().get("items", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  GitHub 热门抓取失败: {exc}")
        return []

    result: list[dict[str, Any]] = []
    for repo in repos[:limit]:
        name = (repo.get("full_name") or "").strip()
        if not name:
            continue
        desc = (repo.get("description") or "").strip()
        # 标题写成"仓库名：一句话简介"，让下游 AI 有明确话题可写
        title = f"{name}：{desc[:60]}" if desc else name
        result.append({
            "title": title,
            "desc": desc,
            "url": repo.get("html_url", ""),
            "hot": int(repo.get("stargazers_count") or 0),
            "source": "GitHub热门",
        })
    return result


def fetch_juejin(limit: int = 5, timeout: int = 10) -> list[dict[str, Any]]:
    """抓掘金技术热榜（国内开发者社区实时热榜，按热度排序）。

    接口只给标题、文章 id、热度/阅读量，不提供正文摘要，
    因此 desc 留空，绝不用推测内容填充。
    失败静默返回空列表，不影响主流程。

    注：CSDN 热榜接口（hot-rank）实测有反爬，短时间多次请求即返回 521，
    不可作为稳定源，故改用掘金。
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            JUEJIN_HOT_API,
            headers={
                **HEADERS,
                "Referer": "https://juejin.cn/",
                "Accept": "application/json, text/plain, */*",
            },
            params={"category_id": 1, "type": "hot"},
            timeout=timeout,
        )
        response.raise_for_status()
        rows = (response.json().get("data") or [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  掘金热榜抓取失败: {exc}")
        return []

    for row in rows:
        content = row.get("content") or {}
        title = (content.get("title") or "").strip()
        article_id = content.get("content_id")
        if not title or not article_id:
            continue
        counter = row.get("content_counter") or {}
        result.append({
            "title": title,
            "desc": "",  # 接口无摘要，留空避免编造
            "url": f"https://juejin.cn/post/{article_id}",
            "hot": int(counter.get("hot_rank") or counter.get("view") or 0),
            "source": "掘金热榜",
        })
        if len(result) >= limit:
            break
    return result


def relevance_score(title: str) -> int:
    """给标题打领域相关分：强相关 +3，弱相关 +1。"""
    score = 0
    for word in config.KEYWORDS_STRONG:
        if word in title:
            score += 3
    for word in config.KEYWORDS_WEAK:
        if word in title:
            score += 1
    return score


def collect(limit: int, require_desc: bool = True) -> list[dict[str, Any]]:
    """抓所有热榜并按相关分排序，只返回相关的前 limit 条。

    每条带 score（相关分）、hot（热度）、desc（真实摘要）、url（原文链接）。

    require_desc=True（默认）时只返回带真实摘要的条目。
    头条热榜接口只给标题、不给摘要，这类条目会被滤掉——
    因为"只有标题"写出来的稿子必然是空泛套话，宁可不要。
    需要用标题级热点兜底时传 False。
    """
    if not config.USE_HOT_TOPICS:
        return []

    raw: list[dict[str, Any]] = []
    raw.extend(fetch_toutiao())
    raw.extend(fetch_baidu())

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in raw:
        key = item["title"]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    scored: list[dict[str, Any]] = []
    for item in unique:
        if require_desc and not (item.get("desc") or "").strip():
            continue
        score = relevance_score(item["title"])
        if score > 0:
            item["score"] = score
            scored.append(item)

    scored.sort(key=lambda x: (x["score"], x["hot"]), reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------------
# DailyHotApi 聚合源（可选）
#
# 用一个第三方实例统一拿多平台热榜，省掉逐站适配。但它是自部署项目，公共实例
# 常年不稳定，所以这里做成：默认关闭 + 多实例回退 + 字段宽容解析。
# 想启用：先跑 `python hot_topics.py --check dailyhot` 确认能通，
# 再在 .env 里写 USE_DAILYHOT=1（或改 config.USE_DAILYHOT）。
# ---------------------------------------------------------------------------

def _pick(raw: dict[str, Any], *keys: str) -> str:
    """从条目里按候选键名取第一个非空值——不同实例字段名有出入。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value:
            return str(value)
    return ""


def fetch_dailyhot(limit: int = 10, timeout: int = 8) -> list[dict[str, Any]]:
    """从 DailyHotApi 拉多平台热榜，失败返回空列表（不抛异常）。"""
    bases = [b.rstrip("/") for b in getattr(config, "DAILYHOT_BASES", []) if b]
    if not bases:
        return []

    platforms = getattr(config, "DAILYHOT_PLATFORMS", [])
    result: list[dict[str, Any]] = []
    used_base = ""

    for base in bases:
        try:
            probe = requests.get(f"{base}/{platforms[0]}", headers=HEADERS, timeout=timeout)
            if probe.status_code == 200 and probe.json().get("data") is not None:
                used_base = base
                break
        except Exception:
            continue
    if not used_base:
        return []

    for platform in platforms:
        try:
            resp = requests.get(f"{used_base}/{platform}", headers=HEADERS, timeout=timeout)
            if resp.status_code != 200:
                continue
            payload = resp.json()
        except Exception:
            continue

        for raw in (payload.get("data") or [])[:limit]:
            title = _pick(raw, "title", "name", "word", "query")
            if not title:
                continue
            url = _pick(raw, "url", "mobileUrl", "link", "href")
            hot_raw = _pick(raw, "hot", "hotValue", "hot_score", "score")
            hot = int(re.sub(r"\D", "", hot_raw) or 0) if hot_raw else 0
            result.append({
                "title": title,
                "desc": _pick(raw, "desc", "description", "summary", "content"),
                "url": url,
                "hot": hot,
                "source": f"DailyHot·{platform}",
            })

    return result


# ---------------------------------------------------------------------------
# 源健康检查：哪个源挂了，一条命令就能看见
#
# 以前是「到点没生成出稿子」才发现源失效，得逐个手测。
# 现在把探测独立出来，跑一次就知道每个源的状态、条数、耗时，
# 也能放进每日任务里，源失效时直接告警。
# ---------------------------------------------------------------------------

def source_health(timeout: int = 10) -> list[dict[str, Any]]:
    """逐个探测选题源，返回每个源的状态。"""
    checks: list[tuple[str, Any]] = [
        ("头条热榜", lambda: fetch_toutiao(timeout)),
        ("百度热搜", lambda: fetch_baidu(timeout)),
        ("掘金热榜", lambda: fetch_juejin(5, timeout)),
        ("GitHub 热门", lambda: fetch_github(5)),
    ]
    if getattr(config, "USE_DAILYHOT", False):
        checks.append(("DailyHot 聚合", lambda: fetch_dailyhot(5, timeout)))

    rows: list[dict[str, Any]] = []
    for name, fn in checks:
        started = time.time()
        try:
            items = fn()
            rows.append({
                "name": name,
                "ok": bool(items),
                "count": len(items),
                "ms": int((time.time() - started) * 1000),
                "error": "" if items else "返回空列表",
            })
        except Exception as exc:  # noqa: BLE001 - 健康检查不能因单源异常中断
            rows.append({
                "name": name,
                "ok": False,
                "count": 0,
                "ms": int((time.time() - started) * 1000),
                "error": str(exc)[:120],
            })

    # RSS 源：只探状态码和条目数，不解析内容
    # 注意：有的源给的是 Atom（<feed><entry>），不是 RSS 2.0（<item>），
    # 只数 <item> 会把博客园这类源误判成挂了，两种都要认。
    for src in getattr(config, "TOPIC_SOURCES", []):
        started = time.time()
        try:
            resp = requests.get(src["url"], headers=HEADERS, timeout=timeout)
            count = 0
            if resp.status_code == 200:
                count = len(re.findall(r"<item[\s>]|<entry[\s>]", resp.text))
            rows.append({
                "name": f"RSS·{src['name']}",
                "ok": resp.status_code == 200 and count > 0,
                "count": count,
                "ms": int((time.time() - started) * 1000),
                "error": (
                    f"HTTP {resp.status_code}" if resp.status_code != 200
                    else ("返回里没有 item/entry，可能被反爬拦截" if count == 0 else "")
                ),
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "name": f"RSS·{src['name']}",
                "ok": False,
                "count": 0,
                "ms": int((time.time() - started) * 1000),
                "error": str(exc)[:120],
            })

    return rows


def print_health(rows: list[dict[str, Any]]) -> None:
    """把健康检查结果打成对齐的表格。"""
    if not rows:
        print("没有启用任何选题源")
        return
    ok_count = sum(1 for r in rows if r["ok"])
    name_width = max(len(r["name"]) for r in rows) + 2
    print(f"{'源'.ljust(name_width)} {'状态':<6} {'条数':>4} {'耗时':>7}  备注")
    print("-" * (name_width + 32))
    for r in rows:
        status = "OK" if r["ok"] else "FAIL"
        print(
            f"{r['name'].ljust(name_width)} {status:<6} {r['count']:>4} "
            f"{str(r['ms']) + 'ms':>7}  {r['error']}"
        )
    print("-" * (name_width + 32))
    print(f"可用 {ok_count}/{len(rows)} 个源")


if __name__ == "__main__":
    import sys

    if "--health" in sys.argv:
        print_health(source_health())
    elif "--check" in sys.argv and "dailyhot" in sys.argv:
        rows = fetch_dailyhot(3)
        if rows:
            print(f"DailyHot 可用，取到 {len(rows)} 条：")
            for row in rows:
                print(f"  [{row['source']}] {row['title'][:40]} | 热度 {row['hot']}")
        else:
            print("DailyHot 不可用：所有实例都连不上，或返回结构不符。")
    else:
        print("用法：python hot_topics.py --health | --check dailyhot")
