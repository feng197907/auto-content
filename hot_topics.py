"""热榜选题抓取：抓全网多平台实时热榜 + 国内技术社区热榜 + 海外一手源。

数据源（2026-11-24 报道方向调整后，开关都在 config.py）：
    国内热榜：知乎 / 百度 / 微博 / 公众号搜索（搜狗）/ B站 / 抖音 / 快手 / 头条
    国内技术：掘金、六个技术媒体 RSS（pipeline 里）、GitHub Trending
    海外一手：Hacker News 首页 / Product Hunt 当日榜 / Anthropic 官方新闻页
    厂商官方：OpenAI / Google AI / DeepMind / Meta（走 pipeline._fetch_official_items）
    暂不可用：V2EX（服务器直连不通，代码保留、默认关闭）

设计思路（重要）：
    全网热榜多是大众话题，与"AI 行业信息"垂直领域未必相关。
    所以不是直接拿热榜写稿，而是：
      1. 抓各平台实时热榜（带热度值、真实摘要、原文链接）
      2. 抓海内外一手源（官方源、HN、Product Hunt、GitHub 当日涨星）
      3. 用领域关键词打分（英文源用单词边界匹配，避免 "ai" 命中 "said"）
      4. 相关的按来源轮转取样，保证五个报道方向都有覆盖

    摘要有无必须分清，它决定能写出多长的稿：
      有摘要 —— 知乎（excerpt_area 整段问题描述，质量最高）、百度、微博（note）、
                公众号搜索（搜狗返回的摘要）、Hacker News（原文页 og:description）
      只有标题 —— 抖音、快手、B站、头条、Anthropic 新闻列表、Product Hunt（一句话 tagline）

    每条条目都带 desc（真实新闻摘要）和 url（原文链接），
    供下游"基于真实素材写作"使用，避免 AI 凭空编造。
    没有摘要的源 desc 一律留空，下游不会据此编造细节。

    领域关键词在 config.py 的 KEYWORDS_STRONG / KEYWORDS_WEAK 里配置，
    改方向只需改那两组词，不用动本文件。

数据源无需 key，失败自动降级，不影响主流程。
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from html import unescape
from typing import Any
from urllib.parse import quote

import requests

import config

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TOUTIAO_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
BAIDU_URL = "https://top.baidu.com/board?tab=realtime"
GITHUB_API = "https://api.github.com/search/repositories"
GITHUB_TRENDING_URL = "https://github.com/trending"
JUEJIN_HOT_API = "https://api.juejin.cn/content_api/v1/content/article_rank"
WEIBO_URL = "https://weibo.com/ajax/side/hotSearch"
DOUYIN_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
DOUYIN_BACKUP_URL = "https://aweme.snssdk.com/aweme/v1/hot/search/list/"
ZHIHU_URL = "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web"
KUAISHOU_URL = "https://www.kuaishou.com/graphql"
BILIBILI_URL = "https://api.bilibili.com/x/web-interface/search/square"
SOGOU_WEIXIN_URL = "https://weixin.sogou.com/weixin"
# 2026-11-24 新增三源：HN 走 Algolia（比官方 Firebase 快 8 倍，且直接带 points），
# Product Hunt 走官方 Atom feed，Anthropic 无 RSS 只能解析新闻页。
HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
PRODUCTHUNT_FEED = "https://www.producthunt.com/feed"
ANTHROPIC_NEWS_URL = "https://www.anthropic.com/news"
V2EX_HOT_URL = "https://www.v2ex.com/api/topics/hot.json"

# 浏览器 UA：微博/抖音/知乎/快手都做了反爬，默认的 python-requests UA 会被挡
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}



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


def _to_hot(value: Any) -> int:
    """把 "1196.5万" / "1163 万热度" / 12345 统一成整数热度，用于排序。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "")
    matched = re.search(r"(\d+(?:\.\d+)?)", text)
    if not matched:
        return 0
    hot = float(matched.group(1))
    if "亿" in text:
        hot *= 100_000_000
    elif "万" in text:
        hot *= 10_000
    return int(hot)


def _clean_text(text: str, limit: int = 400) -> str:
    """去标签、反转义、压缩空白，截到 limit 字。"""
    text = re.sub(r"<[^>]+>", "", unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_weibo(limit: int = 20, timeout: int = 8) -> list[dict[str, Any]]:
    """抓微博实时热搜。

    微博把榜单放在侧栏接口里，需要浏览器 UA + Referer，否则 403。
    note 字段多数时候和 word 一模一样（就是标题本身），这种情况视为没有摘要——
    拿标题当摘要喂给模型，它会把标题扩写成"新闻"，全是编的。
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            WEIBO_URL,
            headers={**BROWSER_HEADERS, "Referer": "https://weibo.com/"},
            timeout=timeout,
        )
        response.raise_for_status()
        rows = (response.json().get("data") or {}).get("realtime") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"  微博热搜抓取失败: {exc}")
        return result

    for row in rows[:limit]:
        word = (row.get("word") or "").strip()
        if not word:
            continue
        note = (row.get("note") or "").strip()
        result.append({
            "title": word,
            "desc": "" if note == word else _clean_text(note),
            "url": f"https://s.weibo.com/weibo?q={quote('#' + word + '#')}",
            "hot": _to_hot(row.get("num") or row.get("raw_hot")),
            "source": "微博热搜",
        })
    return result


def fetch_douyin(limit: int = 20, timeout: int = 8) -> list[dict[str, Any]]:
    """抓抖音热榜（只有标题和热度，没有摘要）。"""
    result: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for url in (DOUYIN_URL, DOUYIN_BACKUP_URL):
        try:
            response = requests.get(
                url,
                headers={**BROWSER_HEADERS, "Referer": "https://www.douyin.com/"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  抖音热榜抓取失败({url.split('/')[2]}): {exc}")
            continue
        # 两个接口结构不同：前者 word_list 在顶层，后者在 data 里
        rows = payload.get("word_list") or (payload.get("data") or {}).get("word_list") or []
        if rows:
            break

    for row in rows[:limit]:
        word = (row.get("word") or "").strip()
        if not word:
            continue
        result.append({
            "title": word,
            "desc": "",
            "url": f"https://www.douyin.com/search/{quote(word)}",
            "hot": _to_hot(row.get("hot_value")),
            "source": "抖音热榜",
        })
    return result


def fetch_zhihu(limit: int = 20, timeout: int = 8) -> list[dict[str, Any]]:
    """抓知乎热榜。

    这是所有社会热榜里摘要质量最好的一个：excerpt_area 给的是问题描述原文，
    足够写出有信息量的解读，所以它排在热榜类源的第一优先。
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            ZHIHU_URL,
            # 不带参数默认只回 3 条（实测），必须显式要 20 条
            params={"limit": 20, "desktop": "true"},
            headers={**BROWSER_HEADERS, "Referer": "https://www.zhihu.com/hot"},
            timeout=timeout,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"  知乎热榜抓取失败: {exc}")
        return result

    for row in rows[:limit]:
        target = row.get("target") or {}
        title = _clean_text(
            (target.get("title_area") or {}).get("text") or target.get("title") or "", 200
        )
        if not title:
            continue
        excerpt = _clean_text(
            (target.get("excerpt_area") or {}).get("text") or target.get("excerpt") or ""
        )
        # 知乎摘要尾巴常带"更多资讯请下载××客户端"，是推广语，不是素材
        excerpt = re.sub(r"更多资讯请下载.*$", "", excerpt).strip()
        result.append({
            "title": title,
            "desc": excerpt,
            "url": (target.get("link") or {}).get("url") or target.get("url") or "",
            "hot": _to_hot((target.get("metrics_area") or {}).get("text")),
            "source": "知乎热榜",
        })
    return result


def fetch_kuaishou(limit: int = 20, timeout: int = 8) -> list[dict[str, Any]]:
    """抓快手热榜（GraphQL，只有标题和热度，没有摘要）。

    注意返回结构：items 是和 result 同级的兄弟字段，不是 result 的子字段
    （result 是数字 1，踩过一次）。
    """
    result: list[dict[str, Any]] = []
    body = {
        "operationName": "visionHotRank",
        "variables": {"page": "home"},
        # 只能传 page；多传 period 会报 GRAPHQL_VALIDATION_FAILED
        "query": (
            "query visionHotRank($page: String) {"
            " visionHotRank(page: $page) { items { id name hotValue } } }"
        ),
    }
    try:
        response = requests.post(
            KUAISHOU_URL,
            json=body,
            headers={
                **BROWSER_HEADERS,
                "Referer": "https://www.kuaishou.com/",
                "Origin": "https://www.kuaishou.com",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        node = (response.json().get("data") or {}).get("visionHotRank") or {}
        rows = node.get("items") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"  快手热榜抓取失败: {exc}")
        return result

    for row in rows[:limit]:
        name = (row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        result.append({
            "title": name,
            "desc": "",
            "url": f"https://www.kuaishou.com/search/video?searchKey={quote(name)}",
            "hot": _to_hot(row.get("hotValue")),
            "source": "快手热榜",
        })
    return result


def fetch_bilibili(limit: int = 15, timeout: int = 8) -> list[dict[str, Any]]:
    """抓 B 站热搜（只有关键词，没有摘要）。

    这个源和"AI/数码"最对得上——B 站热搜里科技数码占比远高于微博抖音，
    所以在标题类源里它的相关命中率最高。
    """
    result: list[dict[str, Any]] = []
    try:
        response = requests.get(
            BILIBILI_URL,
            params={"limit": limit},
            headers={**BROWSER_HEADERS, "Referer": "https://www.bilibili.com/"},
            timeout=timeout,
        )
        response.raise_for_status()
        rows = ((response.json().get("data") or {}).get("trending") or {}).get("list") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"  B站热搜抓取失败: {exc}")
        return result

    for row in rows[:limit]:
        keyword = (row.get("keyword") or row.get("show_name") or "").strip()
        if not keyword:
            continue
        result.append({
            "title": keyword,
            "desc": "",
            "url": f"https://search.bilibili.com/all?keyword={quote(keyword)}",
            "hot": _to_hot(row.get("heat_score")),
            "source": "B站热搜",
        })
    return result


def fetch_wechat_search(limit: int = 12, timeout: int = 10) -> list[dict[str, Any]]:
    """抓搜狗微信的公众号文章搜索结果。

    为什么不是"微信热搜"：微信搜一搜的榜单只在微信客户端里，没有公开接口
    （搜狗微信的老热文榜 pcindex 已下线，实测 404）。能拿到的是公众号文章搜索：
    同行在写什么、什么标题有人看，对选题其实比榜单更有用。

    频控很严（连续多次请求会跳验证码），所以关键词限制在 3 个以内，
    并且在 health 检查里单独观察。链接是搜狗的跳转地址，带 token，会过期。
    """
    result: list[dict[str, Any]] = []
    keywords = getattr(config, "WECHAT_SEARCH_KEYWORDS", [])[:3]
    if not keywords:
        return result

    for keyword in keywords:
        try:
            response = requests.get(
                SOGOU_WEIXIN_URL,
                params={"type": 2, "query": keyword, "ie": "utf8"},
                headers={**BROWSER_HEADERS, "Referer": "https://weixin.sogou.com/"},
                timeout=timeout,
            )
            response.raise_for_status()
            html = response.text
        except requests.RequestException as exc:
            print(f"  微信搜索抓取失败({keyword}): {exc}")
            continue

        if "请输入验证码" in html or "antispider" in html:
            print(f"  微信搜索触发验证码，跳过剩余关键词（{keyword}）")
            break

        for block in re.findall(r'<div class="txt-box">(.*?)</li>', html, re.S)[:limit]:
            title_match = re.search(r"<h3>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
            if not title_match:
                continue
            title = _clean_text(title_match.group(2), 120)
            if not title:
                continue
            desc_match = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S)
            account_match = re.search(r'<span class="all-time-y2">(.*?)</span>', block, re.S)
            link = unescape(title_match.group(1)).replace("&amp;", "&")
            if link.startswith("/"):
                link = "https://weixin.sogou.com" + link
            account = _clean_text(account_match.group(1), 30) if account_match else ""
            result.append({
                "title": title,
                "desc": _clean_text(desc_match.group(1)) if desc_match else "",
                "url": link,
                # 搜狗不给阅读量，用不到热度，排 0 避免虚高
                "hot": 0,
                "source": f"公众号·{account}" if account else "公众号搜索",
            })
        if len(result) >= limit:
            break

    return result[:limit]


def _github_readme_usage(repo: str, timeout: int = 8) -> str:
    """抓仓库 README 里跟"安装/快速开始/用法"相关的片段，给专栏当真实素材。

    GitHub 的 raw+json 媒体类型直接返回 README 纯文本，省掉 base64 解码。
    失败就返回空串——专栏提示词会据此改说"以仓库 README 为准"，不硬编命令。
    """
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/readme",
            headers={**HEADERS, "Accept": "application/vnd.github.raw+json"},
            timeout=timeout,
        )
        if resp.status_code != 200 or not resp.text.strip():
            return ""
        lines = resp.text.splitlines()
        keys = ("install", "quick start", "quickstart", "getting started",
                "usage", "安装", "快速开始", "上手", "使用")
        hit = -1
        for i, line in enumerate(lines[:400]):
            low = line.lower()
            if low.strip().startswith("#") and any(k in low for k in keys):
                hit = i
                break
        snippet = "\n".join(lines[hit:hit + 45] if hit >= 0 else lines[:45])
        return _clean_text(snippet, 700)
    except (requests.RequestException, ValueError):
        return ""


def fetch_github_trending(
    limit: int = 10,
    since: str = "weekly",
    timeout: int = 25,
) -> list[dict[str, Any]]:
    """抓 GitHub 官方 Trending 榜——按"近期新增星标"排序的真正热度榜。

    为什么不用 Search API 的 sort=stars：那是历史累计总星，挑出来的永远是
    OpenClaw(40万) 这类攒了几年的老项目，不是"最近涨得快"的。Trending 页
    自带"N stars this week"字段，正是用户要的近期热度。

    页面是服务端渲染的 HTML，用 requests + re 解析，不引新依赖。
    境外站偶发慢/不通（实测 20s 左右），失败静默返回空，由调用方兜底。
    """
    try:
        response = requests.get(
            GITHUB_TRENDING_URL,
            params={"since": since},
            headers=BROWSER_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        html = response.text
    except (requests.RequestException, ValueError) as exc:
        print(f"  GitHub Trending 抓取失败: {exc}")
        return []

    # 本周新增星标的字段名随 since 变化（today / this week / this month）
    unit = {"daily": "today", "weekly": "this week", "monthly": "this month"}.get(since, "this week")
    gain_re = re.compile(rf"([\d,]+)\s+stars?\s+{re.escape(unit)}", re.I)

    result: list[dict[str, Any]] = []
    for block in re.findall(r'<article class="Box-row">(.*?)</article>', html, re.S):
        head = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if not head:
            continue
        repo_match = re.search(r'href="/([^"]+)"', head.group(1))
        if not repo_match:
            continue
        repo = repo_match.group(1).strip()
        if "/" not in repo:
            continue

        gain_match = gain_re.search(block)
        # Trending 页是千分位写法（"2,815"），_to_hot 只认纯数字，先去掉逗号
        stars_week = _to_hot(gain_match.group(1).replace(",", "")) if gain_match else 0
        total_match = re.search(
            rf'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', block, re.S
        )
        total_stars = (
            _to_hot(re.sub(r"<[^>]+>", "", total_match.group(1)).strip().replace(",", ""))
            if total_match else 0
        )
        desc_match = re.search(r"<p[^>]*>(.*?)</p>", block.split("</h2>", 1)[-1], re.S)
        desc = ""
        if desc_match:
            desc = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", desc_match.group(1)))).strip()
        lang_match = re.search(r'itemprop="programmingLanguage">([^<]+)', block)
        language = lang_match.group(1).strip() if lang_match else ""

        title = f"{repo}：{desc[:60]}" if desc else repo
        result.append({
            "title": title,
            "desc": desc,
            "url": f"https://github.com/{repo}",
            "hot": stars_week or total_stars,
            "source": "GitHub趋势榜",
            "repo": repo,
            "stars": total_stars,
            "stars_week": stars_week,
            "language": language,
        })
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# 2026-11-24 新增：Hacker News / Product Hunt / Anthropic 官方 / V2EX
#
# 报道方向调整后要覆盖"海外动态"与"厂商官方更新"。这几个源的共同问题是
# 没有中文摘要、甚至完全没有摘要，所以每个都要过一道 AI 相关性过滤 + 摘要补全：
#   · HN        —— 只给 title/points，用原文页 og:description 补一句
#   · PH        —— feed 自带一句话 tagline，够用但薄（走 thin 档写作）
#   · Anthropic —— 没 RSS，只能从新闻页抓 /news/ 链接
#   · V2EX      —— 服务器直连不通，代码保留、默认关闭
# ---------------------------------------------------------------------------

def _english_ai_hit(text: str) -> bool:
    """英文标题/简介是否属于 AI 领域。

    不复用 relevance_score 的"子串包含"匹配：英文标题转小写后，
    "ai" 会命中 "said""detail""main""captain"…… 误判率极高。
    这里按单词边界匹配（前后不能是字母数字），"ai-driven" 仍能命中。
    """
    low = (text or "").lower()
    if not low:
        return False
    for keyword in getattr(config, "AI_KEYWORDS_EN", []):
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        if re.search(pattern, low):
            return True
    return False


def _github_ai_hit(text: str) -> bool:
    """仓库名+简介是否属于 AI 项目，规则同上（单词边界匹配）。"""
    low = (text or "").lower()
    if not low:
        return False
    for keyword in getattr(config, "GITHUB_TRENDING_AI_KEYWORDS", []):
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        if re.search(pattern, low):
            return True
    return False


def _meta_description(url: str, timeout: int = 5) -> str:
    """取文章页的 og:description / meta description，给没有摘要的源补素材。

    只在页面确实给了 description 时才有结果；取不到就返回空，
    下游按"只有标题"处理，不会据此编造。
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        response = requests.get(
            url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True
        )
        if response.status_code != 200:
            return ""
        html = response.text[:400000]
    except requests.RequestException:
        return ""

    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property=["\']og:description|name=["\']description)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.S | re.I)
        if not match:
            continue
        text = unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 20:
            return _clean_text(text, 400)
    return ""


def fetch_hackernews(limit: int = 10, timeout: int = 12) -> list[dict[str, Any]]:
    """抓 Hacker News 首页，只留 AI 相关条目。

    用 Algolia 的 front_page 接口：实测 0.8s 返回 30 条，且直接给
    points / num_comments / story_text；官方 Firebase 要 6.8s 且只有 id 列表。
    多数条目没有正文，靠 _meta_description 取原文页 description 补一句；
    补不到就只带"首页分数 + 讨论数"这条真实元信息，不编内容。
    """
    try:
        response = requests.get(
            HN_ALGOLIA_URL,
            params={"tags": "front_page", "hitsPerPage": 60},
            headers=BROWSER_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        hits = response.json().get("hits") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"  Hacker News 抓取失败: {exc}")
        return []

    min_points = getattr(config, "HN_MIN_POINTS", 80)
    picked: list[tuple[int, dict[str, Any], str]] = []
    for hit in hits:
        title = _clean_text(hit.get("title") or "", 200)
        if not title:
            continue
        story_text = hit.get("story_text") or ""
        if not _english_ai_hit(f"{title} {story_text}"):
            continue
        points = _to_hot(hit.get("points"))
        if points < min_points:
            continue
        picked.append((points, hit, title))
    picked.sort(key=lambda row: row[0], reverse=True)

    # 补摘要要发一次外部请求，只给前 6 条补，避免选题阶段拖太久
    enrich_budget = 6
    result: list[dict[str, Any]] = []
    for points, hit, title in picked[: max(limit, 1)]:
        url = (hit.get("url") or "").strip()
        object_id = str(hit.get("objectID") or "")
        if not url and object_id:
            url = f"https://news.ycombinator.com/item?id={object_id}"
        comments = _to_hot(hit.get("num_comments"))

        desc = _clean_text(hit.get("story_text") or "")
        if not desc and enrich_budget > 0:
            desc = _meta_description(url)
            enrich_budget -= 1
        # 无论有没有正文，都把"首页多少分、多少条讨论"作为真实热度带上：
        # 这是编辑判断的依据，也是这里唯一能确定的事实。
        heat = f"Hacker News 首页 {points} 分、{comments} 条讨论。"
        desc = f"{desc}（{heat}）" if desc else heat

        result.append({
            "title": title,
            "desc": desc,
            "url": url,
            "hot": points,
            "source": "Hacker News",
            # prescore=4：一手源优先于二手热榜（热榜命中关键词是 3 分），
            # 否则同分时会被"万"级热度反超，HN 永远排在百度热搜后面
            "prescore": getattr(config, "FIRSTHAND_PRESCORE", 4),
            "points": points,
            "comments": comments,
        })
    return result


def fetch_producthunt(limit: int = 10, timeout: int = 10) -> list[dict[str, Any]]:
    """抓 Product Hunt 当日榜（官方 Atom feed），只留 AI 相关条目。

    实测 1.0s 返回 50 条，顺序即当日热度序（feed 里没有票数字段）。
    每条只有一句 tagline（约 60 字），属于薄素材——
    下游按 thin 档写作，靠"这是什么产品、解决什么问题"展开，不堆细节。

    hot 一律给 0、榜位单独放 rank：PH 是"新品上架"，没有票数和讨论量，
    以前把榜位换算成 hot=60/52 参与排序，等于把新品冒充热点，
    会顶掉知乎/百度真正的热榜条目。现在它只用 prescore=1 垫在最后兜底。
    """
    try:
        import feedparser
    except ImportError:
        print("  Product Hunt 跳过：没装 feedparser")
        return []

    try:
        response = requests.get(PRODUCTHUNT_FEED, headers=BROWSER_HEADERS, timeout=timeout)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except (requests.RequestException, ValueError) as exc:
        print(f"  Product Hunt 抓取失败: {exc}")
        return []

    result: list[dict[str, Any]] = []
    for index, entry in enumerate(feed.entries):
        if len(result) >= limit:
            break
        title = _clean_text(entry.get("title") or "", 120)
        if not title:
            continue
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "")
        summary = re.sub(r"\s+", " ", unescape(summary)).strip()
        # feed 的摘要尾巴固定挂着 "Discussion | Link"，是导航文字不是内容
        summary = re.sub(r"\s*Discussion\s*\|\s*Link\s*$", "", summary).strip()
        if not _english_ai_hit(f"{title} {summary}"):
            continue
        result.append({
            "title": title,
            "desc": summary,
            "url": entry.get("link") or "",
            "hot": 0,  # PH 不给票数，不冒充热度（榜位见 rank）
            "rank": index + 1,
            "source": "Product Hunt",
            "prescore": getattr(config, "PRODUCTHUNT_PRESCORE", 1),
        })
    return result


_ANTHROPIC_CACHE: dict[str, Any] = {"at": 0.0, "items": []}
_ANTHROPIC_TTL = 1800  # 30 分钟：新闻页 340KB，一次选题流程会多次调用 collect()


def fetch_anthropic(limit: int = 8, timeout: int = 12) -> list[dict[str, Any]]:
    """抓 Anthropic 新闻页（官方没有 RSS，三种常见地址实测全是 404）。

    实测能稳定解析出 20+ 条 /news/ 链接。标题优先取链接文字，
    取不到就把 slug 转成可读标题——宁可标题朴素，也不编一个不存在的标题。
    失败一律返回空，不影响其它源。
    """
    now = time.time()
    if now - _ANTHROPIC_CACHE["at"] < _ANTHROPIC_TTL and _ANTHROPIC_CACHE["items"]:
        return list(_ANTHROPIC_CACHE["items"])[:limit]

    try:
        response = requests.get(ANTHROPIC_NEWS_URL, headers=BROWSER_HEADERS, timeout=timeout)
        response.raise_for_status()
        html = response.text
    except requests.RequestException as exc:
        print(f"  Anthropic 抓取失败: {exc}")
        return []

    # 分类页不是文章，混进来会写成"Anthropic 有个公告栏目"这种废话
    skip_slugs = {
        "/news/announcements", "/news/features", "/news/research",
        "/news/policy", "/news/product", "/news/societal-impacts",
        "/news/interpretability", "/news/alignment",
        # 导航/分类入口：实测"Partnerships"这类会被当成一条新闻抓进来
        "/news/partnerships", "/news/events", "/news/teams", "/news/careers",
    }
    # 公司事务类公告不算"大模型产品更新"，写出来是财经新闻不是技术资讯。
    # 实测 2026-11-24 当天首页被 IPO 系列占满，不过滤的话整栏都是"上市路演"。
    corporate_terms = (
        "initial public offering", "ipo", "roadshow", "public benefit fund",
        "investor", "valuation", "lawsuit", "settlement", "privacy policy",
        "terms of service", "hiring", "careers", "bug bounty", "security",
        "prices", "closing of", "shares",
    )

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="(/news/[a-z0-9][a-z0-9\-]*)"[^>]*>(.*?)</a>', html, re.S):
        slug, inner = match.group(1), match.group(2)
        if slug in skip_slugs or slug in seen:
            continue
        text = unescape(re.sub(r"<[^>]+>", " ", inner))
        text = re.sub(r"\s+", " ", text).strip()
        # 取不到链接文字就丢弃，不用 slug 拼一个假标题：
        # 标题是下游写作的唯一事实来源，拼出来的标题会让 AI 顺着瞎猜内容。
        if len(text) < 8 or len(text) > 200:
            continue
        if any(term in text.lower() for term in corporate_terms):
            continue
        seen.add(slug)
        result.append({
            "title": _clean_text(text, 160),
            "desc": "",  # 列表页没有摘要，下游按"只有标题"处理
            "url": f"https://www.anthropic.com{slug}",
            "hot": max(0, 50 - len(result)),
            "source": "Anthropic",
            "prescore": getattr(config, "FIRSTHAND_PRESCORE", 4),
        })
        if len(result) >= max(limit, 1):
            break

    _ANTHROPIC_CACHE.update({"at": now, "items": result})
    return result


def fetch_v2ex(limit: int = 10, timeout: int = 10) -> list[dict[str, Any]]:
    """抓 V2EX 热帖。服务器直连不通（见 config.USE_V2EX_TOPICS 注释），默认不启用。

    保留实现的原因：换网络环境或加代理就能直接用，不用重写。
    在当前服务器上它必然抛连接异常并返回空，属预期行为，不是 bug。
    """
    try:
        response = requests.get(V2EX_HOT_URL, headers=BROWSER_HEADERS, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  V2EX 抓取失败（服务器直连不通，属已知情况）: {exc}")
        return []

    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _clean_text(row.get("title") or "", 200)
        if not title:
            continue
        result.append({
            "title": title,
            "desc": _clean_text(row.get("content") or "", 400),
            "url": row.get("url") or "",
            "hot": 0,  # 该接口不给热度值
            "source": "V2EX",
        })
    return result


def fetch_github_trending_daily_top(
    limit: int = 3, timeout: int = 25
) -> list[dict[str, Any]]:
    """GitHub Trending「当日涨星」里最靠前的 AI 项目（默认 TOP3）。

    与 fetch_github(column=True) 的区别：
      · 那个是"每 3 天 1 篇的深度专栏"，门槛 1000 星、按周涨星选，吃透一个项目；
      · 这个是"每日热点资讯"，按当日涨星选，进早报占 1 条，只讲"今天这个项目涨疯了"。
    since=daily 时 Trending 页给的是 "N stars today"，正是"当日涨星"。
    """
    rows = fetch_github_trending(limit=40, since="daily", timeout=timeout)
    if not rows:
        return []

    picked: list[dict[str, Any]] = []
    for row in rows:
        repo = str(row.get("repo") or "")
        text = f"{repo} {row.get('desc') or ''}"
        if not _github_ai_hit(text):
            continue
        item = dict(row)
        item["source"] = "GitHub当日涨星"
        item["prescore"] = getattr(config, "FIRSTHAND_PRESCORE", 4)
        picked.append(item)
        if len(picked) >= max(limit, 1):
            break
    return picked


def _github_repo_detail(repo: str, timeout: int = 8) -> dict[str, Any]:
    """取单个仓库的富素材（许可/topics/forks/更新时间），失败返回空字典。"""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        license_info = data.get("license") or {}
        return {
            "forks": int(data.get("forks_count") or 0),
            "language": (data.get("language") or "").strip(),
            "license": (license_info.get("spdx_id") or "").strip(),
            "pushed_at": (data.get("pushed_at") or "").strip(),
            "topics": list(data.get("topics") or [])[:8],
            "stars": int(data.get("stargazers_count") or 0),
            "created_at": (data.get("created_at") or "").strip(),
        }
    except (requests.RequestException, ValueError):
        return {}


def _fetch_github_column_growth(
    limit: int, min_stars: int, exclude_urls: set[str]
) -> list[dict[str, Any]]:
    """专栏专用：按"近期涨得最快"挑项目，双源互为兜底。

    主源 Trending 周榜（本周新增星标是硬数据）；不够时用 Search API
    找"近 30 天新建 + 已过 star 门槛"的项目（新建即涨得快，用累计星当增速代理）。
    两个源都按增长速度排序，而不是历史总星。
    """
    floor = min_stars if min_stars > 0 else 1000
    trend_days = getattr(config, "GITHUB_COLUMN_TREND_DAYS", 30)
    candidates: list[dict[str, Any]] = []

    # 1. 主源：Trending 周榜（按本周新增星标排序）
    trending = fetch_github_trending(limit=max(limit * 3, 15))
    for item in trending:
        url = item["url"].rstrip("/")
        if url in exclude_urls:
            continue
        # trending 的总星是页面抓的，够门槛才要（避免太冷门）
        if item.get("stars", 0) < floor:
            continue
        candidates.append(item)

    # 2. 兜底源：近 N 天新建且已过星标门槛（新建就涨到这个数 = 涨得快）
    if len(candidates) < limit:
        since = (date.today() - timedelta(days=trend_days)).isoformat()
        try:
            resp = requests.get(
                GITHUB_API,
                headers={**HEADERS, "Accept": "application/vnd.github+json"},
                params={
                    "q": f"AI in:name,description stars:>{floor} created:>{since}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": min(max(limit * 3, 15), 30),
                },
                timeout=10,
            )
            resp.raise_for_status()
            repos = resp.json().get("items", [])
        except (requests.RequestException, ValueError) as exc:
            print(f"  GitHub 新建热门抓取失败: {exc}")
            repos = []
        existing_urls = {c["url"].rstrip("/") for c in candidates}
        for repo in repos:
            name = (repo.get("full_name") or "").strip()
            url = (repo.get("html_url") or "").strip()
            if not name or not url or url.rstrip("/") in exclude_urls or url.rstrip("/") in existing_urls:
                continue
            stars = int(repo.get("stargazers_count") or 0)
            desc = (repo.get("description") or "").strip()
            candidates.append({
                "title": f"{name}：{desc[:60]}" if desc else name,
                "desc": desc,
                "url": url,
                "hot": stars,
                "source": "GitHub新锐",
                "repo": name,
                "stars": stars,
                "stars_week": stars,  # 新建项目的累计星即近期增量（近似）
                "language": (repo.get("language") or "").strip(),
                "license": ((repo.get("license") or {}).get("spdx_id") or "").strip(),
                "forks": int(repo.get("forks_count") or 0),
                "pushed_at": (repo.get("pushed_at") or "").strip(),
                "topics": list(repo.get("topics") or [])[:8],
                "created_at": (repo.get("created_at") or "").strip(),
            })
            existing_urls.add(url.rstrip("/"))

    # 3. 统一按近期增速降序（Trending 页自带顺序并非严格按新增星标）
    candidates.sort(key=lambda c: c.get("stars_week", 0), reverse=True)

    # 4. 给入选项目补 README 用法片段（"怎么用"这一节的真实依据）
    result: list[dict[str, Any]] = []
    for item in candidates[:limit]:
        row = dict(item)
        row["column"] = "github"
        row.setdefault("source", "GitHub趋势榜")
        # trending 候选只有基础字段（无 license/forks/topics），补一次详情接口；
        # 兜底源来自 Search API 已带全字段，跳过这次请求。
        if row.get("source") == "GitHub趋势榜":
            detail = _github_repo_detail(row["repo"])
            for key in ("license", "forks", "pushed_at", "topics", "created_at"):
                if detail.get(key) and not row.get(key):
                    row[key] = detail[key]
            if detail.get("stars"):
                row["stars"] = detail["stars"]
        usage = _github_readme_usage(row["repo"])
        if usage:
            row["readme"] = usage
        result.append(row)
    return result


def fetch_github(
    limit: int = 5,
    days: int = 7,
    min_stars: int = 0,
    column: bool = False,
    exclude_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """抓 GitHub 热门仓库。

    两种用途：
    · 默认（column=False）：当热点线索，返回简版，给主资讯流兜底。
    · 专栏（column=True）：挑"近期涨得最快"的项目——主源用官方 Trending 周榜
      （自带本周新增星标），不足时用"近 30 天新建且已过 star 门槛"的 Search API
      兜底（新建即涨得快）。绝不按历史累计总星排，否则永远是攒了几年的老项目。
      富素材带 README/许可/更新时间，供深度栏目基于事实写作。

    失败静默返回空列表，不影响主流程。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    exclude_urls = exclude_urls or set()
    if column:
        return _fetch_github_column_growth(limit, min_stars, exclude_urls)
    try:
        # star 门槛：专栏默认用 config 的门槛（≥1000），普通兜底保持 100
        stars_floor = min_stars if min_stars > 0 else (100 if not column else 1000)
        response = requests.get(
            GITHUB_API,
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
            params={
                # 近 N 天有更新、star 过门槛、名称或简介含 AI 的项目
                "q": f"AI in:name,description stars:>{stars_floor} pushed:>{since}",
                "sort": "stars",
                "order": "desc",
                # 专栏要多抓一些候选，好在排除已推荐过的项目后仍够选
                "per_page": min(max(limit * 3, 10), 30),
            },
            timeout=10,
        )
        response.raise_for_status()
        repos = response.json().get("items", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  GitHub 热门抓取失败: {exc}")
        return []

    result: list[dict[str, Any]] = []
    for repo in repos:
        name = (repo.get("full_name") or "").strip()
        if not name:
            continue
        url = (repo.get("html_url") or "").strip()
        # 专栏去重：这个仓库以前推荐过就跳过
        if column and url and url.rstrip("/") in exclude_urls:
            continue
        stars = int(repo.get("stargazers_count") or 0)
        desc = (repo.get("description") or "").strip()
        title = f"{name}：{desc[:60]}" if desc else name

        item: dict[str, Any] = {
            "title": title,
            "desc": desc,
            "url": url,
            "hot": stars,
            "source": "GitHub精选" if column else "GitHub热门",
        }
        if column:
            # 富素材：把专栏墙所需的客观信息都带上，AI 才有事实可写，
            # 不用去编 star / 许可 / 更新时间。
            license_info = repo.get("license") or {}
            item.update({
                "column": "github",
                "repo": name,
                "stars": stars,
                "forks": int(repo.get("forks_count") or 0),
                "language": (repo.get("language") or "").strip(),
                "license": (license_info.get("spdx_id") or "").strip(),
                "pushed_at": (repo.get("pushed_at") or "").strip(),
                "topics": list(repo.get("topics") or [])[:8],
            })
            # README 用法片段是"怎么用"这一节的真实依据，多一次请求但值得
            usage = _github_readme_usage(name)
            if usage:
                item["readme"] = usage
        result.append(item)
        if len(result) >= limit:
            break
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


def item_score(item: dict[str, Any]) -> int:
    """条目的领域分。

    海外源（HN / Product Hunt / Anthropic / GitHub Trending）的标题是英文，
    过不了中文关键词表，会在 collect() 里被判 0 分直接丢掉。
    这些抓取器自己已经做过一轮严格的单词边界过滤，并打了 prescore，
    这里优先信任它；没有 prescore 的条目仍按中文关键词表打分。
    """
    preset = item.get("prescore")
    if isinstance(preset, int) and preset > 0:
        return preset
    return relevance_score(str(item.get("title", "")))


def enabled_hot_sources() -> list[tuple[str, Any]]:
    """返回当前启用的热榜源，顺序即抓取与优先级顺序。

    排序依据是"能不能写出深度稿"：有真实摘要的（知乎/百度/微博/微信）在前，
    只有标题的（B站/抖音/快手/头条）在后——下游按这个顺序取，
    带摘要的先用，标题类的只在素材不够时兜底。
    """
    sources: list[tuple[str, Any]] = [("知乎热榜", fetch_zhihu), ("百度热搜", fetch_baidu)]
    # 2026-11-24 新增：海外与官方源插在"国内有摘要的源"之后、"只有标题的源"之前。
    # 顺序在这里不是决定性的（NEWS_SOURCE_DIVERSIFY 打开后会按来源轮转），
    # 保留顺序是为了让不轮转的老路径也有合理优先级。
    if getattr(config, "USE_HN_TOPICS", False):
        sources.append(("Hacker News", fetch_hackernews))
    if getattr(config, "USE_PRODUCTHUNT_TOPICS", False):
        sources.append(("Product Hunt", fetch_producthunt))
    if getattr(config, "USE_ANTHROPIC_HTML", False):
        sources.append(("Anthropic", fetch_anthropic))
    if getattr(config, "USE_V2EX_TOPICS", False):
        sources.append(("V2EX", fetch_v2ex))
    if getattr(config, "USE_WEIBO_TOPICS", True):
        sources.append(("微博热搜", fetch_weibo))
    if getattr(config, "USE_WECHAT_SEARCH", True):
        sources.append(("公众号搜索", fetch_wechat_search))
    if getattr(config, "USE_BILIBILI_TOPICS", True):
        sources.append(("B站热搜", fetch_bilibili))
    if getattr(config, "USE_DOUYIN_TOPICS", True):
        sources.append(("抖音热榜", fetch_douyin))
    if getattr(config, "USE_KUAISHOU_TOPICS", True):
        sources.append(("快手热榜", fetch_kuaishou))
    sources.append(("头条热榜", fetch_toutiao))
    return sources


def _diversify_by_source(scored: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按来源轮转取样：每个来源先出自己最高分的一条，再出第二条……

    为什么需要它：知乎热榜的 hot 是"万"级、HN 的 points 是"千"级、RSS 是 0。
    按 (相关分, 热度) 排，海外源和官方源永远排在热榜后面，
    五个报道方向里"HN / Product Hunt / GitHub 当日涨星"等于白接。
    轮转后早报 5 条会自然覆盖到多个来源，而不是被单一热榜占满。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in scored:  # scored 已按 (分, 热度) 降序，桶内天然有序
        # 公众号搜索的 source 是"公众号·某某号"，一个号一个名字。
        # 不归一化的话，10 条里能有 9 条是不同公众号，把 HN/Product Hunt 全挤掉。
        key = str(item.get("source", ""))
        if key.startswith("公众号"):
            key = "公众号搜索"
        buckets.setdefault(key, []).append(item)

    order = sorted(
        buckets,
        key=lambda name: (
            buckets[name][0].get("score", 0),
            buckets[name][0].get("hot", 0),
        ),
        reverse=True,
    )

    result: list[dict[str, Any]] = []
    round_index = 0
    while len(result) < limit:
        added = False
        for name in order:
            bucket = buckets[name]
            if round_index < len(bucket):
                result.append(bucket[round_index])
                added = True
                if len(result) >= limit:
                    break
        if not added:
            break
        round_index += 1
    return result


def collect(limit: int, require_desc: bool = True) -> list[dict[str, Any]]:
    """抓所有已启用的热榜并按相关分排序，只返回相关的前 limit 条。

    每条带 score（相关分）、hot（热度）、desc（真实摘要）、url（原文链接）。

    require_desc=True（默认）时只返回带真实摘要的条目。
    抖音/快手/B站/头条这几个接口只给标题、不给摘要，这类条目会被滤掉——
    因为"只有标题"写出来的稿子必然是空泛套话，宁可不要。
    需要用标题级热点兜底时传 False。
    """
    if not config.USE_HOT_TOPICS:
        return []

    raw: list[dict[str, Any]] = []
    for _name, fetcher in enabled_hot_sources():
        try:
            raw.extend(fetcher())
        except Exception as exc:  # noqa: BLE001 - 单源异常不能拖垮整个选题流程
            print(f"  选题源 {_name} 异常: {exc}")

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
        score = item_score(item)
        if score > 0:
            item["score"] = score
            scored.append(item)

    scored.sort(key=lambda x: (x["score"], x["hot"]), reverse=True)
    # 默认按分排就够；但报道方向扩到五个之后，纯按分排会让热榜吃掉所有位置，
    # 海外源/官方源的 hot 天生打不过"万"级热度，所以按来源轮转保证覆盖。
    if getattr(config, "NEWS_SOURCE_DIVERSIFY", False) and len(scored) > limit:
        return _diversify_by_source(scored, limit)
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
        (name, (lambda f=fetcher: f(timeout=timeout)))
        for name, fetcher in enabled_hot_sources()
    ]
    checks.extend([
        ("掘金热榜", lambda: fetch_juejin(5, timeout)),
        ("GitHub 热门", lambda: fetch_github(5)),
    ])
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
