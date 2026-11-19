"""核心流水线：抓选题 -> AI 写初稿 -> 排版 -> 入队。

用法：
    py -3 pipeline.py          # 真实模式（需要 config.py 里填 API key）
    py -3 pipeline.py --demo   # 演示模式（不需要 key，用内置样稿验证流程）
    py -3 pipeline.py --count 3  # 指定本次生成数量
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import feedparser
import requests

import config
import hot_topics

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / config.QUEUE_FILE

def build_factual_card(item: dict[str, Any]) -> str:
    """无 AI key 时，用真实抓取的数据拼一张"事实卡"，绝不编造正文。

    与旧版假套话的区别：这里每个字都来自真实热榜数据——
    标题、摘要、来源、原文链接全是真的，没有一段凭空写的"建议"。
    """
    parts = ["<h2>事件摘要</h2>"]
    desc = item.get("desc", "").strip()
    if desc:
        parts.append(f"<p>{desc}</p>")
        parts.append(
            "<p>以上摘要引自公开热榜报道，仅作事实陈述，"
            "不代表本站观点。</p>"
        )
    else:
        parts.append(
            "<p>该话题来自实时热榜，当前数据接口未提供正文摘要，"
            "请点击下方原文链接查看完整报道。本站不对其内容做任何编造或推测。</p>"
        )

    parts.append("<h2>信息来源</h2>")
    source = item.get("source", "实时热榜")
    hot = item.get("hot", 0)
    meta = f"来源：{source}"
    if hot:
        meta += f" · 实时热度：{hot:,}"
    parts.append(f"<p>{meta}</p>")

    url = item.get("url", "").strip()
    if url:
        parts.append(f'<p>原文链接：<a href="{url}">{url}</a></p>')

    parts.append("<h2>说明</h2>")
    parts.append(
        "<p>这是一条未经 AI 改写的原始资讯卡。"
        "配置 AI key 后，系统会基于以上真实素材撰写带分析的原创文章。"
        "当前模式不含任何虚构的细节、数字或案例。</p>"
    )
    return "\n".join(parts)


def load_queue() -> list[dict[str, Any]]:
    """读取稿件队列，文件不存在或损坏时返回空列表。"""
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(
            QUEUE_PATH.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(queue: list[dict[str, Any]]) -> None:
    """写入稿件队列。"""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_rss_items(limit: int) -> list[dict[str, Any]]:
    """轮转抓配置里的国内 RSS 技术源，带真实摘要和原文链接。

    先把各源前若干条都取下来，再按"每轮每源取 1 条"轮转，
    避免条目多的源（如 IT之家 60 条）把配额占满，保证来源多样。
    requests 取内容再交给 feedparser（可控超时），单源失败不影响其他源。
    """
    pools: list[list[dict[str, Any]]] = []
    for source in config.TOPIC_SOURCES:
        try:
            response = requests.get(
                source["url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=10,
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            pool: list[dict[str, Any]] = []
            for entry in feed.entries[:8]:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                summary = entry.get("summary") or entry.get("description") or ""
                # 摘要可能是 HTML，剥标签压空白，截断避免过长
                summary = re.sub(r"<[^>]+>", " ", summary)
                summary = re.sub(r"\s+", " ", summary).strip()[:300]
                pool.append({
                    "title": title,
                    "desc": summary,
                    "url": entry.get("link", ""),
                    "source": source.get("name", "RSS"),
                    "hot": 0,
                })
            if pool:
                pools.append(pool)
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响整体
            print(f"  选题源 {source.get('name')} 抓取失败: {exc}")

    # 轮转取：第 1 轮各源取 1 条，第 2 轮再各取 1 条……
    items: list[dict[str, Any]] = []
    round_index = 0
    while len(items) < limit:
        added = False
        for pool in pools:
            if round_index < len(pool):
                items.append(pool[round_index])
                added = True
                if len(items) >= limit:
                    break
        if not added:
            break
        round_index += 1
    return items


def fetch_topic_items(limit: int) -> list[dict[str, Any]]:
    """抓选题，返回带来源的信息。

    国内源优先、且"有真实素材优先"的分层策略：
      1. 全网热榜（百度，带真实摘要）技术话题：约 40%
      2. 国内技术媒体 RSS（IT之家/开源中国/雷峰网/爱范儿/博客园/量子位）：轮转补齐
      3. 掘金技术热榜：只有标题，仅在有素材的源不足时兜底 1 条
      4. 全网热榜里只有标题的条目（头条）：再兜底
      5. GitHub 热门：境外站不稳，最后兜底
      6. 备用领域词保底
    每条含 {title, desc, url, source, hot}，供审核页展示与 AI 基于事实写作。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(candidates: list[dict[str, Any]], quota: int) -> None:
        """按配额取候选，用 url（退回 title）去重，不超过总上限 limit。"""
        added = 0
        for candidate in candidates:
            if added >= quota or len(items) >= limit:
                break
            key = (candidate.get("url") or candidate.get("title") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(candidate)
            added += 1

    # 1. 全网热榜技术话题：约 40%，至少 1 条
    #    只取带真实摘要的条目（头条只有标题，写出来必然空泛，故不用）
    hot_quota = max(1, round(limit * 0.4))
    take(hot_topics.collect(max(limit * 2, 6), require_desc=True), hot_quota)

    # 2. 国内技术媒体 RSS（IT之家/开源中国/雷峰网/爱范儿/博客园/量子位）：轮转补齐
    take(_fetch_rss_items(limit), limit)

    # 3. 掘金技术热榜：只有标题无摘要，仅在有素材的源不够时兜底 1 条
    if len(items) < limit and getattr(config, "USE_JUEJIN_TOPICS", True):
        juejin_items = hot_topics.fetch_juejin(max(3, limit))
        juejin_items.sort(key=lambda x: hot_topics.relevance_score(x["title"]), reverse=True)
        take(juejin_items, 1)

    # 4. 全网热榜里只有标题的条目（如头条）：再兜底
    if len(items) < limit:
        take(hot_topics.collect(max(limit * 2, 6), require_desc=False), 1)

    # 5. GitHub 热门：境外站不稳，最后兜底 1 条
    if len(items) < limit and getattr(config, "USE_GITHUB_TOPICS", True):
        take(hot_topics.fetch_github(max(2, limit)), 1)

    # 6. 备用领域词保底
    if len(items) < limit:
        take(
            [{"title": t, "source": "领域词库", "hot": 0} for t in config.FALLBACK_TOPICS],
            limit,
        )

    return items[:limit]


def _call_model(prompt: str, model: str) -> tuple[str | None, str]:
    """调单个模型，返回 (内容, 状态)。状态: ok / ratelimit / error。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
    }
    # 智谱 4.7 默认开深度思考（慢且混入思考过程），显式关掉
    if config.AI_PROVIDER == "zhipu" and "4.7" in model:
        payload["thinking"] = {"type": "disabled"}

    try:
        response = requests.post(
            f"{config.AI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        if response.status_code == 429:
            return None, "ratelimit"
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"], "ok"
    except (requests.RequestException, KeyError, IndexError) as exc:
        print(f"  AI 调用失败({model}): {exc}")
        return None, "error"


def call_ai(prompt: str) -> str | None:
    """调 AI 生成初稿：主模型限流时自动降级到备用模型，失败返回 None。

    免费模型访问量大，4.7 常被限流；降级到同样免费的老版保证出稿稳定。
    两个都失败才返回 None，由上层降级到真实素材卡。
    """
    if not config.AI_API_KEY:
        return None

    models = [config.AI_MODEL]
    if config.AI_FALLBACK_MODEL and config.AI_FALLBACK_MODEL not in models:
        models.append(config.AI_FALLBACK_MODEL)

    for model in models:
        content, status = _call_model(prompt, model)
        if status == "ok":
            return content
        if status == "ratelimit":
            print(f"  {model} 限流" + ("，切换备用模型" if model != models[-1] else ""))
            continue
    return None


def build_prompt(item: dict[str, Any]) -> str:
    """基于真实素材拼提示词，并要求 AI 不得编造事实。

    关键区分：有摘要 vs 只有标题。
    只有标题时 AI 无从得知事件经过，若仍按"新闻解读"写就必然编造，
    因此改成只写"概念澄清 + 应对方法"，并明确禁止描述任何经过和场景。
    """
    topic = item["title"]
    desc = item.get("desc", "").strip()
    source = item.get("source", "实时热榜")
    url = item.get("url", "").strip()

    if desc:
        material = f"真实新闻摘要（来自{source}）：\n{desc}"
        task = "请围绕下面的真实话题写一篇解读文章。"
        length_rule = "2. 正文 600-900 字，素材少就写短，不要注水"
    else:
        material = (
            f"该话题来自{source}，只有标题、没有任何正文素材。\n"
            "因此本文只写两件事：这个概念/现象是什么，普通人该怎么看待和应对。\n"
            "绝对不要描述事件经过——不要写谁做了什么、发生在哪里、涉及多少人或多钱，"
            "这些信息你都没有，写出来就是编造。"
        )
        task = "请围绕下面的真实话题写一篇分析文章。"
        length_rule = "2. 正文 400-700 字（素材有限，不要为凑字数编内容）"

    source_line = f"\n原文链接：{url}" if url else ""

    return (
        f"你是内容写手。{task}\n\n"
        f"话题：{topic}\n"
        f"{material}{source_line}\n\n"
        f"写作领域：{config.WRITING_NICHE}\n"
        f"风格要求：{config.WRITING_STYLE}\n\n"
        "真实性硬要求（必须遵守，违反即作废）：\n"
        "1. 只能使用上面提供的真实素材，不得编造任何具体数字、人名、机构名、"
        "时间、地点或事件细节\n"
        "2. 严禁虚构具体场景来举例。像'比如有些学生会用 AI 写作业'"
        "'某公司曾发生……'这类看似具体、实则编造的表述一律不许出现\n"
        "3. 通用道理和方法可以讲，但必须写成建议或分析，不能写成事实陈述\n"
        "4. 素材没有说明原因、动机、背景时，绝对不要替它推测。"
        "禁止写'技术实力雄厚''团队经验丰富''市场需求旺盛'这类没有依据的归因，"
        "宁可直说'具体原因未披露'\n"
        "5. 如果有原文链接，在文末用「信息来源」标注出处\n"
        "6. 素材撑不满时宁可写短，绝不用编造的内容凑字数\n\n"
        "输出格式（严格遵守，否则无法发布）：\n"
        "1. 第一行只写标题，20 字以内，不要加'标题：'字样，不要加任何标记\n"
        f"{length_rule}\n"
        "3. 每个小标题必须用两个井号开头，格式严格为：## 小标题文字\n"
        "   （例如：## 这事儿有多严重），不要写成 H2、不要写成'一、'、"
        "不要带尖括号或任何 HTML 标签\n"
        "4. 分点用'－'开头，一句话一段，段落之间空一行\n"
        "5. 结尾给一条明确可用的建议即可，不要强行凑成清单\n"
        "6. 不要写代码块，不要解释你的思路，直接输出标题和正文"
    )


def build_optimize_prompt(article: dict[str, Any]) -> str:
    """基于稿件里保存的真实素材，构建"重写优化"提示词。

    与首次生成不同：明确告诉模型这是二次优化，要用同一真实素材写出
    质量更高的一版，但真实性红线不变（不得编造数字/人名/时间/事件）。
    """
    topic = article.get("topic") or article.get("title", "")
    desc = (article.get("desc") or "").strip()
    source = article.get("source", "实时热榜")
    url = (article.get("url") or "").strip()

    if desc:
        material = f"真实新闻摘要（来自{source}）：\n{desc}"
    else:
        material = (
            f"该话题来自{source}，只有标题、没有任何正文素材。\n"
            "因此本文只写两件事：这个概念/现象是什么，普通人该怎么看待和应对。\n"
            "绝对不要描述事件经过——不要写谁做了什么、发生在哪里、涉及多少人或多钱，"
            "这些信息你都没有，写出来就是编造。"
        )
    source_line = f"\n原文链接：{url}" if url else ""

    return (
        f"你是资深内容编辑。下面这篇文章已发布过，现在要基于同一真实素材"
        f"重写一个更优质的版本。不要照抄旧稿，要给出新的切入角度、更清晰的"
        f"结构和更实用的建议。\n\n"
        f"话题：{topic}\n"
        f"{material}{source_line}\n\n"
        f"写作领域：{config.WRITING_NICHE}\n"
        f"风格要求：{config.WRITING_STYLE}\n\n"
        "真实性硬要求（必须遵守，违反即作废）：\n"
        "1. 只能使用上面提供的真实素材，不得编造任何具体数字、人名、机构名、"
        "时间、地点或事件细节\n"
        "2. 严禁虚构具体场景来举例。像'比如有些学生会用 AI 写作业'"
        "'某公司曾发生……'这类看似具体、实则编造的表述一律不许出现\n"
        "3. 通用道理和方法可以讲，但必须写成建议或分析，不能写成事实陈述\n"
        "4. 素材没有说明原因、动机、背景时，绝对不要替它推测。"
        "禁止写'技术实力雄厚''团队经验丰富''市场需求旺盛'这类没有依据的归因，"
        "宁可直说'具体原因未披露'\n"
        "5. 如果有原文链接，在文末用「信息来源」标注出处\n"
        "6. 素材撑不满时宁可写短，绝不用编造的内容凑字数\n\n"
        "优化要求：\n"
        "1. 标题比旧稿更抓人但不夸张，20 字以内\n"
        "2. 开头结论更干脆，中段分点更可执行，结尾行动清单更具体\n"
        "3. 全文 800-1200 字，不要空话套话和营销腔\n\n"
        "输出格式（严格遵守，否则无法发布）：\n"
        "1. 第一行只写标题，20 字以内，不要加'标题：'字样，不要加任何标记\n"
        "2. 每个小标题用两个井号开头，格式严格为：## 小标题文字\n"
        "3. 分点用'－'开头，一句话一段，段落之间空一行\n"
        "4. 不要写代码块，不要解释你的思路，直接输出标题和正文"
    )


def strip_title_prefix(text: str) -> str:
    """剥掉 AI 输出里的'标题：'/'标题:'/'#'等前缀。"""
    text = text.strip()
    for prefix in ("标题：", "标题:", "Title:", "title:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.lstrip("#").strip()


def markdown_to_html(text: str) -> str:
    """把 AI 常见的 Markdown 标记转成审核页需要的 HTML 标签。"""
    import re

    lines = text.splitlines()
    html_lines: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_lines.append("</ul>")
            in_list = False

    def clean(text: str) -> str:
        """去掉加粗/斜体标记和可能残留的尖括号标签。"""
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)
        # AI 偶尔输出 <H2>xxx</H2> 这类字面标签，剥掉标签只留文字
        text = re.sub(r"</?[hH][1-6]>", "", text)
        return text.strip()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 0) 整行被尖括号标签包住：<H2>xxx</H2> / <h3>xxx</h3>
        wrapped = re.match(r"^<[hH]([1-6])>(.+)</[hH]\1>$", stripped)
        # 1) Markdown 标题 ## / ### -> h2，#### -> h3
        heading = re.match(r"^(#{2,4})\s+(.+)$", stripped)
        # 2) 字面 H2/H3 标记（AI 常输出 "H2 这事儿..."）
        literal_h = re.match(r"^[hH]([23])[、.\s]+(.+)$", stripped)
        # 3) 中文序号标题："一、xxx" "二、xxx"，保留序号在标题里
        cn_h = re.match(r"^([一二三四五六七八九十]+、\s*.+)$", stripped)

        matched_title = None
        if wrapped:
            level_num = int(wrapped.group(1))
            matched_title = (f"h{min(level_num, 3)}", wrapped.group(2))
        elif heading:
            matched_title = (
                "h3" if len(heading.group(1)) >= 4 else "h2",
                heading.group(2),
            )
        elif literal_h:
            matched_title = (f"h{literal_h.group(1)}", literal_h.group(2))
        elif cn_h and len(cn_h.group(1)) <= 30:
            matched_title = ("h2", cn_h.group(1))

        if matched_title:
            close_list()
            level, text = matched_title
            html_lines.append(f"<{level}>{clean(text)}</{level}>")
            continue

        # 列表项：- / － / * / 1. / 1、开头
        item = re.match(r"^(?:[-－*]|\d+[.、])\s+(.+)$", stripped)
        if item:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{clean(item.group(1))}</li>")
            continue

        close_list()
        html_lines.append(f"<p>{clean(stripped)}</p>")

    close_list()
    return "\n".join(html_lines)


def parse_response(raw: str, fallback_title: str) -> tuple[str, str]:
    """从 AI 输出里拆出标题和正文，并把 Markdown 转成 HTML。"""
    lines = raw.strip().splitlines()
    title = fallback_title
    body_lines = lines

    if lines:
        first = strip_title_prefix(lines[0])
        if first and len(first) <= 40:
            title = first
            body_lines = lines[1:]

    body = markdown_to_html("\n".join(body_lines))
    return title, body


def make_article(topic_item: dict[str, Any], demo: bool) -> dict[str, Any]:
    """生成一个稿件条目，记录真实来源，绝不编造正文。"""
    topic = topic_item["title"]
    source = topic_item.get("source", "领域词库")
    url = topic_item.get("url", "")
    desc = topic_item.get("desc", "")

    if demo:
        title = topic
        body = build_factual_card(topic_item)
        mode = "真实素材卡"
    else:
        raw = call_ai(build_prompt(topic_item))
        if raw is None:
            # AI 调用失败时，降级到事实卡，绝不退回假套话
            title = topic
            body = build_factual_card(topic_item)
            mode = "真实素材卡（AI 失败降级）"
        else:
            title, body = parse_response(raw, topic)
            mode = "AI 改写"

    return {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "topic": topic,
        "source": source,
        "hot": topic_item.get("hot", 0),
        "url": url,
        "desc": desc,
        "mode": mode,
        "body": body,
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def optimize_article(article_id: str) -> dict[str, Any] | None:
    """基于原稿真实素材，用大模型重写一篇，覆盖当前稿件。

    - 保持 status=approved 与 approved_at 不变（不刷新 30 天保留期起算点）
    - 只在 AI 成功返回后才写回，失败返回 None，原稿毫发无损
    - 成功返回更新后的稿件字典，供接口回显新标题/正文
    """
    queue = load_queue()
    index = -1
    for i, article in enumerate(queue):
        if article.get("id") == article_id:
            index = i
            break
    if index == -1:
        return None

    article = queue[index]
    raw = call_ai(build_optimize_prompt(article))
    if raw is None:
        # AI 失败/限流：不写回，原稿保持不变
        return None

    new_title, new_body = parse_response(raw, article.get("title", "优化稿件"))
    article["title"] = new_title
    article["body"] = new_body
    article["mode"] = "AI优化重写"
    article["updated_at"] = datetime.now().isoformat(timespec="seconds")
    # status / approved_at 均不改动：已通过状态与保留期起算点保持原样
    queue[index] = article
    save_queue(queue)
    return article


def _parse_time(value: Any) -> datetime | None:
    """把稿件里的时间字段解析成 datetime，异常/缺失返回 None（保守保留）。"""
    if not value or not isinstance(value, str):
        return None
    try:
        # fromisoformat 能解析 "2026-11-19T16:33:43" 这种本系统写入的格式
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _article_age_days(
    article: dict[str, Any], now: datetime
) -> tuple[float, int] | None:
    """按稿件状态选时间基准，算出 (存活天数, 保留天数)。无法判断返回 None。

    - 已通过：以 approved_at 为准（真正"通过/发布"的那一刻），
      老数据没有该字段则退回 updated_at，再退回 created_at。
    - 其他（待审核/已退回）：以 created_at 为准。
    """
    if article.get("status") == "approved":
        base = (
            _parse_time(article.get("approved_at"))
            or _parse_time(article.get("updated_at"))
            or _parse_time(article.get("created_at"))
        )
        limit = config.KEEP_APPROVED_DAYS
    else:
        base = _parse_time(article.get("created_at"))
        limit = config.KEEP_PENDING_DAYS
    if base is None:
        return None
    return (now - base).total_seconds() / 86400, limit


def cleanup_queue(queue: list[dict[str, Any]], now: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
    """清理过期稿件，返回 (保留的队列, 删除数量)。

    规则：
    - 待审核/已退回 存活 > KEEP_PENDING_DAYS 天 → 删除
    - 已通过         存活 > KEEP_APPROVED_DAYS 天 → 删除
    - 时间字段缺失/格式损坏 → 无法判断，一律保留（绝不误删）
    """
    now = now or datetime.now()
    kept: list[dict[str, Any]] = []
    removed = 0
    for article in queue:
        result = _article_age_days(article, now)
        if result is None:
            # 时间不可解析：保守起见保留，避免误删
            kept.append(article)
            continue
        age_days, limit = result
        if age_days > limit:
            removed += 1
            print(
                f"  清理过期稿件：{article.get('title', '')[:24]} "
                f"（状态 {article.get('status')}，已 {age_days:.1f} 天 > {limit} 天）"
            )
        else:
            kept.append(article)
    return kept, removed


def run_cleanup() -> int:
    """独立执行一次清理（供命令行/每日任务调用），返回删除数量。"""
    queue = load_queue()
    before = len(queue)
    kept, removed = cleanup_queue(queue)
    if removed:
        save_queue(kept)
    print(f"清理完成：删除 {removed} 篇，队列 {before} → {len(kept)} 篇。")
    return removed


def run(count: int, demo: bool) -> list[dict[str, Any]]:
    """执行一次流水线，返回本次新增的稿件列表。

    app.py 的网页端一键生成也调用这个函数，因此需要返回结果。
    """
    mode = "演示模式" if demo else "真实模式"
    print(f"启动流水线（{mode}），本次生成 {count} 篇：")

    topic_items = fetch_topic_items(count)
    if not topic_items:
        print("没有可用选题，退出。")
        return []

    queue = load_queue()
    created: list[dict[str, Any]] = []
    for item in topic_items[:count]:
        hot_mark = f"（热度 {item['hot']:,}）" if item["hot"] else ""
        print(f"  - 选题: {item['title']} [{item['source']}]{hot_mark}")
        article = make_article(item, demo)
        created.append(article)
        queue.insert(0, article)

    # 生成完顺带清理过期稿件（新稿件时间为 now，不会被误删）
    queue, removed = cleanup_queue(queue)
    save_queue(queue)
    print(
        f"完成：新增 {len(created)} 篇，清理 {removed} 篇，"
        f"队列共 {len(queue)} 篇。"
    )
    return created


def main() -> None:
    """解析命令行参数并运行。"""
    parser = argparse.ArgumentParser(description="自动化内容流水线")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="演示模式，不调用 AI，用内置样稿验证流程",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=config.BATCH_SIZE,
        help="本次生成篇数",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="只清理过期稿件，不生成新稿",
    )
    args = parser.parse_args()

    if args.cleanup:
        run_cleanup()
    else:
        run(args.count, args.demo)


if __name__ == "__main__":
    main()
