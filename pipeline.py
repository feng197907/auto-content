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


def _smart_summary(raw: str, limit: int = 400) -> str:
    """压空白并按句挑选摘要，优先保住"含数字/版本号"的句子。

    背景：原来直接 [:300] 硬截断，把"修复版本 1.30.7"这类关键句截掉了，
    AI 只能写"更新到最新版本"这种废话。含数字的句子几乎都是干货
    （版本号、CVE 编号、时间、金额、比例），必须留下。
    """
    text = re.sub(r"\s+", " ", raw).strip()
    if len(text) <= limit:
        return text

    sentences = [s for s in re.split(r"(?<=[。！？!?；;])\s*", text) if s]
    picked: list[str] = []
    total = 0
    for index, sentence in enumerate(sentences):
        has_number = bool(re.search(r"\d", sentence))
        if index >= 2 and not has_number:
            continue
        if total + len(sentence) > limit and picked:
            continue
        picked.append(sentence)
        total += len(sentence)

    out = "".join(picked).strip()
    return out or text[:limit]


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
                summary = _smart_summary(summary)
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
        f"{config.TITLE_RULE}\n"
        f"{length_rule}\n"
        "3. 小标题必须用两个井号加一个空格开头，格式严格为：## 小标题文字\n"
        "   （不要写成 H2、不要写成'一、'、不要带尖括号或任何 HTML 标签）\n"
        "4. 需要强调的句子用**这样**加粗，每节最多加粗一句，不要通篇加粗\n"
        "5. 命令、配置片段用三个反引号单独包成代码块，一行一条命令；"
        "普通叙述不要放代码块\n"
        "6. 不要写导语说明、不要解释你的思路，直接输出标题和正文\n\n"
        f"{config.ARTICLE_SKELETON}\n"
        "自检三问（写完自己过一遍）：\n"
        "· 标题里有没有具体信息点，让人一眼知道跟自己有关？\n"
        "· 开头三行之内，读者能不能看出'这事和我有什么干系'？\n"
        "· 读完有没有一个可以马上做的动作？如果全是'应当重视''要加强'，回去改。"
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
        f"{config.TITLE_RULE}\n"
        "2. 小标题用两个井号加一个空格开头，格式严格为：## 小标题文字\n"
        "3. 需要强调的句子用**这样**加粗，每节最多加粗一句\n"
        "4. 命令、配置片段用三个反引号单独包成代码块，一行一条命令\n"
        "5. 不要写导语说明、不要解释你的思路，直接输出标题和正文\n\n"
        f"{config.ARTICLE_SKELETON}\n"
        "自检三问（写完自己过一遍）：\n"
        "· 标题里有没有具体信息点，让人一眼知道跟自己有关？\n"
        "· 开头三行之内，读者能不能看出'这事和我有什么干系'？\n"
        "· 读完有没有一个可以马上做的动作？如果全是'应当重视''要加强'，回去改。"
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
    """把 AI 常见的 Markdown 标记转成审核页需要的 HTML 标签。

    支持的标记：## 小标题、- 列表、**加粗**、`行内代码`、``` 命令块。
    加粗和代码必须保留——之前一律剥掉，正文粘到公众号就没有重点句了。
    """
    import re
    from html import escape

    lines = text.splitlines()
    html_lines: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            html_lines.append("</ul>")
            in_list = False

    def clean(text: str) -> str:
        """行内标记转标签，并去掉可能残留的 h 标签。"""
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        # AI 偶尔输出 <H2>xxx</H2> 这类字面标签，剥掉标签只留文字
        text = re.sub(r"</?[hH][1-6]>", "", text)
        return text.strip()

    def flush_code() -> None:
        nonlocal in_code, code_lines
        if in_code:
            body = escape("\n".join(code_lines).strip("\n"))
            html_lines.append(f"<pre><code>{body}</code></pre>")
            in_code = False
            code_lines = []

    for line in lines:
        stripped = line.strip()

        # 代码围栏：``` 开 / ``` 关（保留内部缩进和换行）
        if stripped.startswith("```"):
            if in_code:
                flush_code()
            else:
                close_list()
                in_code = True
                code_lines = []
            continue
        if in_code:
            code_lines.append(line.rstrip())
            continue

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
    flush_code()
    return "\n".join(html_lines)


def split_long_paragraphs(body: str, limit: int = 120) -> str:
    """把超长的纯文本段落按句子拆开，一段只留一件事。

    手机上 17px 字号一行约 21 字，120 字就是五六行，读者看到一大坨字会直接划走。
    模型经常把三点论证塞成一段，与其反复求它，不如在代码层拆干净。

    只处理标签能在句内配平的段落：含 <strong>/<code> 的段落一旦在标签中间切开，
    HTML 就坏了，那种交给人审更安全。
    """

    def visible(text: str) -> int:
        return len(re.sub(r"<[^>]+>", "", text))

    void_tags = {"br", "img", "hr", "input", "meta", "link", "source"}

    def tags_balanced(text: str) -> bool:
        """句内标签是否成对。

        只数尖括号个数不够——`<strong>前半句。` 这种句子尖括号也是 1 对 1 相等，
        实际标签却跨到了下一句，切开就是坏 HTML。所以按标签栈严格校验。
        """
        stack: list[str] = []
        for match in re.finditer(r"</?([a-zA-Z0-9]+)[^>]*>", text):
            tag = match.group(1).lower()
            if tag in void_tags:
                continue
            if match.group(0).startswith("</"):
                if not stack or stack.pop() != tag:
                    return False
            else:
                stack.append(tag)
        return not stack

    def split_one(match: "re.Match[str]") -> str:
        inner = match.group(1)
        if not inner or visible(inner) <= limit:
            return match.group(0)
        # 切点不能落在结束标签前面：`<strong>结论。</strong>接正文` 这种，
        # 从"结论。"后面切会把 </strong> 甩到下一段开头，HTML 就坏了
        sentences = [s for s in re.split(r"(?<=[。！？])(?!</)", inner) if s]
        # 标签必须整句闭合，否则拼回去就是坏 HTML（标签跨句时放弃拆分，交给人审）
        if not all(tags_balanced(s) for s in sentences):
            return match.group(0)
        chunks: list[str] = []
        buf = ""
        for sentence in sentences:
            if buf and visible(buf + sentence) > limit:
                chunks.append(buf)
                buf = sentence
            else:
                buf += sentence
        if buf:
            chunks.append(buf)
        if len(chunks) <= 1:
            return match.group(0)
        return "".join(f"<p>{chunk}</p>" for chunk in chunks)

    return re.sub(r"<p>(.*?)</p>", split_one, body, flags=re.S)


def strip_unsafe_config(body: str, desc: str) -> str:
    """删掉代码块里素材没给过的配置文件行。

    模型很爱在「怎么缓解」那一节自己贴一行 listen 配置，实测写出过
    `listen 80 http2;`（HTTP 端口配 HTTP/2，语法本身就是错的）。
    这种示例比不写更危险——读者照抄可能把服务改挂。正文里的描述性说明
    （"移除 listen 指令里的 http2 参数"）还在，不影响读者理解。

    命令块（nginx -v、systemctl reload nginx）不带分号或花括号，不受影响。
    """

    def clean_block(match: "re.Match[str]") -> str:
        lines = match.group(1).splitlines()
        kept = [
            line
            for line in lines
            if not (
                line.strip()
                and not line.strip().startswith("#")
                and (";" in line or "{" in line)
                and line.strip() not in desc
            )
        ]
        # 剥完只剩注释（或原本就全是注释）：没有可执行内容，深色块摆在那儿反而突兀
        if not any(line.strip() and not line.strip().startswith("#") for line in kept):
            return ""
        if len(kept) == len(lines):
            return match.group(0)
        return "<pre><code>" + "\n".join(kept) + "</code></pre>"

    return re.sub(r"<pre><code>(.*?)</code></pre>", clean_block, body, flags=re.S)


def tidy_spaces(body: str) -> str:
    """删掉中文之间多余的空格。

    模型常在句号后多打一个半角空格（"处于高危状态。 虽然目前……"），
    在公众号里会显示成一跳一跳的空隙。只处理两侧都是中文（含中文标点）的空格，
    数字和英文两侧留空格是需要的，不动。
    """
    cjk = r"\u4e00-\u9fff\u3000-\u303f\uff01-\uff60"
    return re.sub(rf"(?<=[{cjk}])[ \u3000]+(?=[{cjk}])", "", body)


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
    return title, tidy_spaces(split_long_paragraphs(body))


def structure_issues(body: str, title: str = "", desc: str = "") -> list[str]:
    """检查成稿是否符合结构模板，只报会毁掉可读性的硬伤。

    实测模型会稳定犯这几类毛病：小标题写成"什么情况？""该怎么办？"、凑出五个小节、
    开头一律"近日，某某公司……"、结尾一律"综上所述"、把 300 字塞成一块、
    通篇加粗。这里做代码层校验，不合格就让模型带问题清单重写一次，
    比单纯在提示词里求它守规矩可靠。
    """
    issues: list[str] = []

    def strip_tags(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    # 段落和纯文本各取一份：段落长度、开头结尾都要按段落判断，不能整篇揉成一串
    paragraphs = [strip_tags(p).strip() for p in re.findall(r"<p>(.*?)</p>", body, re.S)]
    paragraphs = [p for p in paragraphs if p]
    plain = strip_tags(body)

    # 标题：过长会在分享卡片被截断，诱导词既降权又掉信任
    if title:
        clean_title = strip_tags(title).strip()
        if len(clean_title) > 22:
            issues.append(f"标题 {len(clean_title)} 字，超过 22 字会被截断")
        if any(w in clean_title for w in ("揭秘", "震惊", "速看", "炸裂", "马上删", "惊呆")):
            issues.append("标题带了诱导词")

    # 小标题：3 个左右，必须是陈述句
    heads = re.findall(r"<h2>(.*?)</h2>", body)
    if not heads:
        issues.append("没有小标题")
    elif len(heads) > 4:
        issues.append(f"小标题 {len(heads)} 个，超过 4 个")
    if any(re.search(r"[？?]\s*$", h) for h in heads):
        issues.append("小标题用了设问句")

    # 开头钩子：新闻联播体一出现，读者三秒内就划走了
    if paragraphs:
        opening = paragraphs[0][:40]
        for word in ("近日", "据悉", "随着", "在当今", "众所周知"):
            if word in opening:
                issues.append(f"开头用了新闻体套话「{word}」")
                break

    # 段落长度：手机上超过 5 行就是一片黑压压的字
    long_paragraphs = [p for p in paragraphs if len(p) > 120]
    if long_paragraphs:
        issues.append(f"有 {len(long_paragraphs)} 段超过 120 字，手机上会连成一片")

    # 加粗：太少扫不到重点，太多等于没有重点
    bold_count = body.count("<strong>")
    need_bold = min(3, len(heads)) if heads else 1
    if bold_count < need_bold:
        issues.append("加粗的重点句不足")
    elif bold_count > 8:
        issues.append(f"加粗 {bold_count} 处太多，重点被稀释")

    # 代码块里冒出配置文件语法（带分号或大括号），但素材从没给过——这是在教人改配置，
    # 写错一行就能把服务搞挂，比不写更糟。命令块一般不带分号，所以不会误伤
    for block in re.findall(r"<pre><code>(.*?)</code></pre>", body, re.S):
        suspicious = [
            line.strip()
            for line in strip_tags(block).splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and (";" in line or "{" in line)
            and line.strip() not in desc
        ]
        if suspicious:
            issues.append("代码块里有素材未提供的配置行")
            break

    # 空话套话
    empty_phrases = ["及时更新", "引起重视", "加强安全意识", "值得关注", "至关重要"]
    if sum(1 for phrase in empty_phrases if phrase in body) >= 2:
        issues.append("空话套话过多")

    # 9.2 分说成"满分"是硬伤，懂行的人一眼就看穿（两轮实测都出现过）
    if "满分" in plain and "满分" not in desc and "10.0" not in desc:
        issues.append("把评分说成「满分」，与素材不符")

    # 结尾：总结套话是读者划走的信号
    tail = plain[-120:]
    for word in ("综上所述", "希望对你有帮助", "希望对大家有帮助", "希望对大家有所启发"):
        if word in tail:
            issues.append(f"结尾用了总结套话「{word}」")
            break

    # 同一句话反复出现：模型常把加粗句单独成段，后面又原样复述一遍
    sentences = [s.strip() for s in re.split(r"[。！？!?]", plain) if len(s.strip()) >= 12]
    if any(sentences.count(s) > 1 for s in sentences):
        issues.append("有句子被重复复述")

    # 标题里的数字必须能在素材或正文里找到，防止"1000 亿"被写成"100 亿"
    if title and desc:
        title_nums = re.findall(r"\d+(?:\.\d+)?", title)
        material_nums = set(re.findall(r"\d+(?:\.\d+)?", desc + " " + plain))
        if any(n not in material_nums for n in title_nums):
            issues.append("标题里的数字与素材对不上")

    return issues


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
        prompt = build_prompt(topic_item)
        raw = call_ai(prompt)
        if raw is None:
            # AI 调用失败时，降级到事实卡，绝不退回假套话
            title = topic
            body = build_factual_card(topic_item)
            mode = "真实素材卡（AI 失败降级）"
        else:
            title, body = parse_response(raw, topic)
            mode = "AI 改写"

            # 结构不合格：把问题清单回灌给模型，重写一次（只重试一次，控成本）
            issues = structure_issues(body, title, desc)
            if issues:
                print(f"  结构自检未过：{'、'.join(issues)} → 重写一次")
                fix_prompt = (
                    f"{prompt}\n\n"
                    f"你上一版存在这些问题：{'；'.join(issues)}。\n"
                    "请严格按上面的结构模板重写全文：小标题必须是陈述句、"
                    "数量控制在 3 个、每节第一句加粗、去掉空话套话。"
                )
                raw2 = call_ai(fix_prompt)
                if raw2:
                    title2, body2 = parse_response(raw2, topic)
                    if len(structure_issues(body2, title2, desc)) < len(issues):
                        title, body = title2, body2

            # 重试也未必改得掉，最后统一把编造的配置行剥一次
            body = strip_unsafe_config(body, desc)

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
