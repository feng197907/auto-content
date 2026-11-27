"""话题投稿：用户给一个自己找到的新闻话题，联网搜真实素材后写成热文稿件。

和自动热榜的区别：
    自动流水线是"先抓热榜 → 机器挑话题 → 写稿"；
    本模块是"人给话题 → 围绕这个话题联网检索 → 写稿"。
    人把控选题方向，机器负责找素材和成稿，避免机器挑的话题不对味。

素材来源（免 key，多源降级）：
    1. 360 搜索（www.so.com）——主源。实测中文相关性最好，res-desc 带摘要和
       发布日期，标题/摘要直接能当素材；
    2. 必应 RSS（cn.bing.com/search?format=rss）——备用源。注意必应中文分词
       会把"黄仁勋"拆成单字"黄"，返回一堆百科噪声，所以必须过相关性过滤器；
    3. 全都没搜到 → 返回空素材，让上层明确告知用户"没搜到，别硬写"，绝不编造。

相关性过滤（关键，不然搜了也白搜）：
    从话题提取中文 2-4 字词 + 英文单词作关键词，剔除"最新/是什么/怎么样"等
    停用词；每条素材的标题+摘要必须命中至少 1 个关键词，且优先保留命中多个的。
    这样"黄仁勋 英伟达"就绝不会被"黄（汉语文字）_百度百科"混进来。

真实性底线（和 pipeline 一脉相承）：
    搜索结果只做清洗、去重、截断，不改写措辞；
    每条素材带 source 和 url，汇总成"真实新闻摘要"喂给现有 build_prompt，
    由同一套 AI 写稿 + 结构校验 + 编造配置剥离链路成稿，不另起炉灶。
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import quote

import requests

import config

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SO_URL = "https://www.so.com/s"
BING_SEARCH_URL = "https://cn.bing.com/search"

# 话题里的泛词，提取关键词时剔除，避免"最新/怎么办"这种词到处命中
_STOP_WORDS = {
    "最新", "最近", "今天", "昨天", "现在", "目前", "如何", "怎么", "怎样",
    "什么", "为什么", "怎么办", "是否", "真的", "到底", "一览", "汇总",
    "新闻", "消息", "进展", "消息", "相关", "关于", "一下", "一个",
    "the", "and", "for", "with", "what", "why", "how", "latest", "new",
}


def _clean(text: str, limit: int = 300) -> str:
    """去 HTML 标签、反转义、压空白、截长度。只清洗不改写措辞。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def topic_keywords(topic: str) -> list[str]:
    """从话题提取检索关键词：英文整词 + 中文 2-4 字词组。

    中文没有空格分词，用滑动窗口取 2/3/4 字词；命中任一即算相关。
    这样"黄仁勋 英伟达"能提出"黄仁勋""仁勋""英伟达"等，
    而"黄（汉语文字）"只命中单字，不会过关。
    """
    words: set[str] = set()
    for chunk in re.split(r"[\s,，。、:：/|（）()【】\[\]]+", topic):
        chunk = chunk.strip()
        if not chunk:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9.+-]{1,}", chunk):
            low = chunk.lower()
            if low not in _STOP_WORDS:
                words.add(low)
            continue
        # 中文串：取 2-4 字连续片段
        han = re.findall(r"[\u4e00-\u9fff]+", chunk)
        for run in han:
            for size in (4, 3, 2):
                for i in range(len(run) - size + 1):
                    frag = run[i:i + size]
                    if frag not in _STOP_WORDS:
                        words.add(frag)
    # 长词优先（"黄仁勋"排在"仁勋"前），过滤掉被更长词完全包含的短词
    ordered = sorted(words, key=len, reverse=True)
    kept: list[str] = []
    for w in ordered:
        if any(w != k and w in k for k in kept):
            continue
        kept.append(w)
    return kept[:24]


def relevance(title: str, desc: str, keywords: list[str]) -> int:
    """素材与话题的相关度：命中的不同关键词个数。0 表示不相关，应丢弃。"""
    if not keywords:
        return 1
    blob = (title + " " + desc).lower()
    return sum(1 for kw in keywords if kw in blob)


def _looks_useful(title: str, desc: str) -> bool:
    """过滤明显没用的搜索结果：广告、纯导航、摘要过短。"""
    if not title or len(title) < 6:
        return False
    min_desc = max(0, int(getattr(config, "TOPIC_MIN_DESC", 12)))
    if len(desc) < min_desc:
        return False
    blob = title + desc
    spam = ("点击领取", "扫码", "加微信", "免费领取", "立即下载", "优惠券", "包邮")
    if any(word in blob for word in spam):
        return False
    return True


def _dedup_key(item: dict[str, Any]) -> str:
    """按 URL 主路径或标题去重。"""
    url = str(item.get("url", "")).split("?")[0].rstrip("/")
    return url or str(item.get("title", ""))[:24]


def search_so(topic: str, limit: int, timeout: int, keywords: list[str]) -> list[dict[str, Any]]:
    """360 搜索主源：HTML 解析 res-list 结果块，带真实摘要与日期。"""
    results: list[dict[str, Any]] = []
    try:
        response = requests.get(
            SO_URL,
            params={"q": topic, "pn": "10"},
            headers=BROWSER_HEADERS,
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"[topic_research] 360搜索返回 {response.status_code}")
            return results
    except requests.RequestException as exc:
        print(f"[topic_research] 360搜索失败: {exc}")
        return results

    for block in re.findall(r'<li[^>]*class="res-list"[^>]*>(.*?)</li>', response.text, re.S):
        h_m = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        d_m = re.search(r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>', block, re.S)
        a_m = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"', block, re.S)
        title = _clean(h_m.group(1), 160) if h_m else ""
        desc = _clean(d_m.group(1), 400) if d_m else ""
        url = unescape(a_m.group(1)) if a_m else ""
        if url and url.startswith("/"):
            url = "https://www.so.com" + url
        if not url.startswith("http") or not _looks_useful(title, desc):
            continue
        score = relevance(title, desc, keywords)
        if score < 1:
            continue
        results.append({
            "title": title, "desc": desc, "url": url,
            "source": "360搜索", "score": score,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def search_bing(topic: str, limit: int, timeout: int, keywords: list[str]) -> list[dict[str, Any]]:
    """必应 RSS 备用源。中文分词差，必须过相关性过滤器。"""
    results: list[dict[str, Any]] = []
    try:
        response = requests.get(
            BING_SEARCH_URL,
            params={"q": topic, "format": "rss", "setlang": "zh-CN", "count": limit + 6},
            headers=BROWSER_HEADERS,
            timeout=timeout,
        )
        if response.status_code != 200:
            return results
    except requests.RequestException as exc:
        print(f"[topic_research] 必应搜索失败: {exc}")
        return results

    for block in re.findall(r"<item>(.*?)</item>", response.text, re.S):
        link_m = re.search(r"<link>(.*?)</link>", block, re.S)
        title_m = re.search(r"<title>(.*?)</title>", block, re.S)
        desc_m = re.search(r"<description>(.*?)</description>", block, re.S)
        url = _clean(link_m.group(1)) if link_m else ""
        title = _clean(title_m.group(1), 160) if title_m else ""
        desc = _clean(desc_m.group(1), 400) if desc_m else ""
        if not url.startswith("http") or not _looks_useful(title, desc):
            continue
        score = relevance(title, desc, keywords)
        if score < 1:
            continue
        title = re.sub(r"\s*[-–—]\s*[^-–—]{2,20}$", "", title).strip() or title
        results.append({
            "title": title, "desc": desc, "url": url,
            "source": "必应搜索", "score": score,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def research(topic: str) -> dict[str, Any]:
    """围绕话题联网检索，返回汇总后的素材与明细。

    返回结构：
        {
            "ok": bool, "topic": str,
            "materials": [...],   # 清洗、去重、相关性过滤后的素材
            "desc": str,          # 汇总成一段"真实新闻摘要"，喂给 build_prompt
            "url": str,           # 第一条原文链接
            "source": str,        # 素材来源标识
            "reason": str,        # ok=False 时的原因
        }
    """
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "topic": topic, "materials": [], "desc": "",
                "url": "", "source": "", "reason": "话题为空"}

    limit = max(1, min(10, int(getattr(config, "TOPIC_SEARCH_LIMIT", 6))))
    timeout = max(5, min(30, int(getattr(config, "TOPIC_SEARCH_TIMEOUT", 12))))
    keywords = topic_keywords(topic)

    materials: list[dict[str, Any]] = []
    materials.extend(search_so(topic, limit, timeout, keywords))
    # 360 不够再补必应（注意必应噪声大，已过相关性过滤）
    if len(materials) < limit:
        need = limit - len(materials)
        materials.extend(search_bing(topic, need, timeout, keywords))

    # 去重（URL 主路径 / 标题）
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in materials:
        key = _dedup_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    unique = unique[:limit]

    if not unique:
        return {
            "ok": False, "topic": topic, "materials": [], "desc": "",
            "url": "", "source": "",
            "reason": "联网没搜到和这个话题相关的素材，换个说法或更具体的话题再试",
        }

    # 汇总成一段"真实新闻摘要"：每条带来源与链接，措辞原样保留。
    parts: list[str] = []
    for idx, item in enumerate(unique, start=1):
        title = item["title"]
        desc = item["desc"]
        snippet = f"{title}：{desc}" if desc and desc not in title else title
        parts.append(f"【素材{idx}｜{item['source']}】{snippet}")
    desc_all = "\n".join(parts)
    max_desc = max(600, int(getattr(config, "TOPIC_MAX_MATERIAL_CHARS", 2200)))
    desc_all = desc_all[:max_desc]

    sources = sorted({item["source"] for item in unique})
    return {
        "ok": True,
        "topic": topic,
        "materials": unique,
        "desc": desc_all,
        "url": unique[0]["url"],
        "source": "联网检索（" + "、".join(sources) + "）",
        "reason": "",
    }


def build_topic_item(researched: dict[str, Any], column: str) -> dict[str, Any]:
    """把检索结果组装成 pipeline.make_article 能吃的 topic_item。"""
    return {
        "title": researched["topic"],
        "desc": researched["desc"],
        "url": researched["url"],
        "source": researched["source"],
        "hot": 0,
        "column": column,
        "origin": "manual_topic",  # 标记人工投稿，供前端/复盘识别
    }


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "人工智能最新进展"
    result = research(q)
    print(f"话题：{result['topic']}")
    print(f"关键词：{', '.join(topic_keywords(q)[:12])}")
    print(f"结果：{'成功' if result['ok'] else '失败'} {result['reason']}")
    print(f"素材条数：{len(result['materials'])}，来源：{result['source']}")
    print("--- 汇总素材 ---")
    print(result["desc"][:900])
