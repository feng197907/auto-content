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
