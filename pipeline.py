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
import profile as profile_mod

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


def topic_blocked(item: dict[str, Any]) -> bool:
    """选题硬过滤：命中画像「不写的主题」就丢弃。

    判定文本 = 标题 + 摘要。只看标题会漏——汽车稿标题常写成
    「东风日产 NX7 预售 12.99 万元起」，本身没有"汽车/SUV"，
    但摘要里有"SUV""大五座布局"，必须连摘要一起查。

    排除词优先级高于关键词相关分：素材里出现"宁德时代电池"会命中弱相关词
    "电池"，但那是整车消费资讯，不是这个号的选题——命中排除词一律丢弃。
    """
    words = profile_mod.excluded_words()
    if not words:
        return False
    text = f"{item.get('title', '')} {item.get('desc', '') or ''}".lower()
    hit = next((w for w in words if w.lower() in text), "")
    if hit:
        print(f"  选题过滤：「{item.get('title', '')[:32]}」命中「{hit}」，跳过")
        return True
    return False


def fetch_topic_items(limit: int) -> list[dict[str, Any]]:
    """抓选题，返回带来源的信息。

    国内源优先、且"有真实素材优先"的分层策略：
      1. 全网多平台热榜（知乎/百度/微博/公众号/B站/抖音/快手/头条）里技术话题：约 50%
         （公众号搜索结果最多留 1 条——那是同行写过的稿，多了就是洗稿）
      2. 国内技术媒体 RSS（IT之家/开源中国/雷峰网/爱范儿/博客园/量子位）：轮转补齐
      3. DailyHot 聚合源（可选，默认关）：补带摘要的跨平台热榜，约 20%
      4. 掘金技术热榜：只有标题，仅在有素材的源不足时兜底 1 条
      5. 全网热榜里只有标题的条目（抖音/快手/B站/头条）：再兜底
      6. GitHub 热门：境外站不稳，最后兜底
      7. 备用领域词保底
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
            # 账号定位的硬过滤——所有来源都过这一关（RSS 以前是漏网之鱼）
            if topic_blocked(candidate):
                continue
            key = (candidate.get("url") or candidate.get("title") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(candidate)
            added += 1

    # 1. 全网热榜技术话题：约 50%，至少 2 条
    #    只取带真实摘要的条目（抖音/快手只有标题，写出来必然空泛，故不用）
    hot_quota = max(2, round(limit * 0.5))
    hot_candidates = hot_topics.collect(max(limit * 2, 6), require_desc=True)
    # 公众号搜索结果全是"同行已经写过的稿"，命中最稳但最容易变成洗稿，
    # 所以只留 1 条当角度参考，其余位置让给知乎/百度这类"事件本身"的源
    wechat_rows = [x for x in hot_candidates if str(x.get("source", "")).startswith("公众号")]
    other_rows = [
        x for x in hot_candidates if not str(x.get("source", "")).startswith("公众号")
    ]
    take(other_rows + wechat_rows[:1], hot_quota)

    # 2. 国内技术媒体 RSS（IT之家/开源中国/雷峰网/爱范儿/博客园/量子位）：轮转补齐
    take(_fetch_rss_items(limit), limit)

    # 3. DailyHot 聚合源（默认关闭，需在 .env 写 USE_DAILYHOT=1）：
    #    补一批带摘要的跨平台热榜，只取相关分 > 0 的，配额 20%
    if len(items) < limit and getattr(config, "USE_DAILYHOT", False):
        daily = [
            x for x in hot_topics.fetch_dailyhot(max(limit, 6))
            if (x.get("desc") or "").strip()
            and hot_topics.relevance_score(x["title"]) > 0
        ]
        daily.sort(key=lambda x: hot_topics.relevance_score(x["title"]), reverse=True)
        take(daily, max(1, round(limit * 0.2)))

    # 4. 掘金技术热榜：只有标题无摘要，仅在有素材的源不够时兜底 1 条
    if len(items) < limit and getattr(config, "USE_JUEJIN_TOPICS", True):
        juejin_items = hot_topics.fetch_juejin(max(3, limit))
        juejin_items.sort(key=lambda x: hot_topics.relevance_score(x["title"]), reverse=True)
        take(juejin_items, 1)

    # 5. 全网热榜里只有标题的条目（如头条）：再兜底
    if len(items) < limit:
        take(hot_topics.collect(max(limit * 2, 6), require_desc=False), 1)

    # 6. GitHub 热门：境外站不稳，最后兜底 1 条
    if len(items) < limit and getattr(config, "USE_GITHUB_TOPICS", True):
        take(hot_topics.fetch_github(max(2, limit)), 1)

    # 7. 备用领域词保底
    if len(items) < limit:
        take(
            [{"title": t, "source": "领域词库", "hot": 0} for t in config.FALLBACK_TOPICS],
            limit,
        )

    return items[:limit]


def _call_model(prompt: str, target: dict[str, str]) -> tuple[str | None, str]:
    """调单个模型，返回 (内容, 状态)。状态: ok / ratelimit / error。

    target 是 dict（provider/base_url/model/api_key），不是一个模型名字符串：
    跨平台兜底时智谱和 DeepSeek 的地址、key 都不一样，
    只传模型名会把 deepseek-flash 发到智谱的接口上去，必 400。
    """
    payload: dict[str, Any] = {
        "model": target["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
    }
    # 智谱 4.7 与 DeepSeek V4.1 都默认开深度思考（更慢、更费 token，
    # 思考内容还可能混进正文）。写稿用不上推理，默认显式关掉；
    # .env 里 AI_THINKING=1 可恢复默认行为。
    if not getattr(config, "AI_THINKING", False) and (
        (target["provider"] == "zhipu" and "4.7" in target["model"])
        or target["provider"] == "deepseek"
    ):
        payload["thinking"] = {"type": "disabled"}

    try:
        response = requests.post(
            f"{target['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {target['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=getattr(config, "AI_TIMEOUT", 240),
        )
        if response.status_code == 429:
            return None, "ratelimit"
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"], "ok"
    except (requests.RequestException, KeyError, IndexError) as exc:
        print(f"  AI 调用失败({target['model']}): {exc}")
        return None, "error"


def call_ai(prompt: str) -> str | None:
    """按 config.ai_targets() 的顺序调用：主模型 → 兜底模型，全失败返回 None。

    兜底顺序由 .env 决定：
      AI_FALLBACK_PROVIDER=deepseek → 4.7 挂了换 DeepSeek（付费但稳）
      不配 → 退回同平台的 glm-4-flash（免费但会写出设问标题）
    限流和报错都会切换——免费档的 429 和偶发 500 都算"这家现在不能用"。
    """
    targets = config.ai_targets()
    if not targets:
        return None

    for index, target in enumerate(targets):
        content, status = _call_model(prompt, target)
        if status == "ok":
            if index:
                print(f"  已切到兜底模型 {target['model']} 出稿")
            return content

        nxt = targets[index + 1] if index + 1 < len(targets) else None
        if not nxt:
            print(f"  {target['model']} {'限流' if status == 'ratelimit' else '调用失败'}，无可用兜底")
            continue
        reason = "限流" if status == "ratelimit" else "调用失败"
        print(f"  {target['model']} {reason} → 换 {nxt['provider']}/{nxt['model']}")
    return None


def material_grade(desc: str) -> str:
    """按素材厚度分档：rich / thin / title_only。

    决定能写多长、能写多深。以前只有"有摘要/没摘要"两档，
    结果 20 字摘要也被当成充足素材，写出来既短又空。
    """
    text = (desc or "").strip()
    if len(text) >= 150:
        return "rich"
    if len(text) >= 40:
        return "thin"
    return "title_only"


def word_count(body: str) -> int:
    """统计正文可见字数（去掉标签，不计标点后的空白）。"""
    return len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body)))


def build_prompt(item: dict[str, Any]) -> str:
    """基于真实素材拼提示词，并要求 AI 不得编造事实。

    关键区分：素材厚 / 薄 / 只有标题。
    只有标题时 AI 无从得知事件经过，若仍按"新闻解读"写就必然编造，
    因此改成只写"概念澄清 + 应对方法"，并明确禁止描述任何经过和场景。
    """
    topic = item["title"]
    desc = item.get("desc", "").strip()
    source = item.get("source", "实时热榜")
    url = item.get("url", "").strip()

    grade = material_grade(desc)
    low, high = config.ARTICLE_WORDS[grade]
    length_rule = (
        f"2. 正文 {low}-{high} 字。字数靠「合法展开」那五条把每节写满，"
        "不是靠形容词和排比句灌水"
    )

    if grade == "title_only":
        material = (
            f"该话题来自{source}，只有标题、没有任何正文素材。\n"
            "因此本文只写两件事：这个概念/现象是什么，普通人该怎么看待和应对。\n"
            "绝对不要描述事件经过——不要写谁做了什么、发生在哪里、涉及多少人或多钱，"
            "这些信息你都没有，写出来就是编造。"
        )
    else:
        material = f"真实新闻摘要（来自{source}）：\n{desc}"
        if grade == "thin":
            material += (
                "\n（素材偏短，事件本身的信息就这么多。"
                "把篇幅放在机制解释、判断标准和操作路径上，不要替素材补经过。）"
            )
    task = "请围绕下面的真实话题写一篇解读文章。"

    source_line = f"\n原文链接：{url}" if url else ""

    return (
        f"你是内容写手。{task}\n\n"
        f"话题：{topic}\n"
        f"{material}{source_line}\n\n"
        f"{profile_mod.prompt_block()}\n\n"
        "真实性硬要求（必须遵守，违反即作废）：\n"
        "1. 只能使用上面提供的真实素材，不得编造任何具体数字、人名、机构名、"
        "时间、地点或事件细节\n"
        "2. 严禁虚构具体场景来举例。像'比如有些学生会用 AI 写作业'"
        "'某公司曾发生……'这类看似具体、实则编造的表述一律不许出现\n"
        "3. 通用道理和方法可以讲，但必须写成建议或分析，不能写成事实陈述\n"
        "4. 素材没有说明原因、动机、背景时，绝对不要替它推测。"
        "禁止写'技术实力雄厚''团队经验丰富''市场需求旺盛'这类没有依据的归因，"
        "宁可直说'具体原因未披露'\n"
        "5. 不要在文末写「信息来源」「出处」「原文链接」或任何来源标注——"
        "这类信息会在排版时被统一去掉，写进去只会是废话\n"
        "6. 篇幅不够时按「怎么把文章写厚而不注水」那五条展开，"
        "绝不靠编造事实或堆形容词凑字数\n\n"
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
        f"{config.INFO_GAIN_RULE}\n"
        "自检三问（写完自己过一遍）：\n"
        "· 标题里有没有具体信息点，让人一眼知道跟自己有关？\n"
        "· 开头三行之内，读者能不能看出'这事和我有什么干系'？\n"
        "· 读完有没有一个可以马上做的动作？如果全是'应当重视''要加强'，回去改。"
    )


def build_optimize_prompt(
    article: dict[str, Any], issues: list[str] | None = None
) -> str:
    """基于稿件里保存的真实素材，构建"重写优化"提示词。

    与首次生成不同：明确告诉模型这是二次优化，要用同一真实素材写出
    质量更高的一版，但真实性红线不变（不得编造数字/人名/时间/事件）。

    issues：上一版的代码自检问题清单。原稿有什么毛病必须明说，否则模型
    只会在旧稿基础上换措辞，同样的病照犯（2026-11-20 实测：同一句话被当成
    三个小节的结论句，不提它就再犯一次）。
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
    low, high = config.ARTICLE_WORDS[material_grade(desc)]

    return (
        f"你是资深内容编辑。下面这篇文章已发布过，现在要基于同一真实素材"
        f"重写一个更优质的版本。不要照抄旧稿，要给出新的切入角度、更清晰的"
        f"结构和更实用的建议。\n\n"
        f"话题：{topic}\n"
        f"{material}{source_line}\n\n"
        f"{profile_mod.prompt_block()}\n\n"
        "真实性硬要求（必须遵守，违反即作废）：\n"
        "1. 只能使用上面提供的真实素材，不得编造任何具体数字、人名、机构名、"
        "时间、地点或事件细节\n"
        "2. 严禁虚构具体场景来举例。像'比如有些学生会用 AI 写作业'"
        "'某公司曾发生……'这类看似具体、实则编造的表述一律不许出现\n"
        "3. 通用道理和方法可以讲，但必须写成建议或分析，不能写成事实陈述\n"
        "4. 素材没有说明原因、动机、背景时，绝对不要替它推测。"
        "禁止写'技术实力雄厚''团队经验丰富''市场需求旺盛'这类没有依据的归因，"
        "宁可直说'具体原因未披露'\n"
        "5. 不要在文末写「信息来源」「出处」「原文链接」或任何来源标注——"
        "这类信息会在排版时被统一去掉，写进去只会是废话\n"
        "6. 素材撑不满时宁可写短，绝不用编造的内容凑字数\n\n"
        "优化要求：\n"
        "1. 标题比旧稿更抓人但不夸张，20 字以内\n"
        "2. 开头结论更干脆，中段分点更可执行，结尾行动清单更具体\n"
        f"3. 全文 {low}-{high} 字，不要空话套话和营销腔\n\n"
        "输出格式（严格遵守，否则无法发布）：\n"
        f"{config.TITLE_RULE}\n"
        "2. 小标题用两个井号加一个空格开头，格式严格为：## 小标题文字\n"
        "3. 需要强调的句子用**这样**加粗，每节最多加粗一句\n"
        "4. 命令、配置片段用三个反引号单独包成代码块，一行一条命令\n"
        "5. 不要写导语说明、不要解释你的思路，直接输出标题和正文\n\n"
        f"{config.ARTICLE_SKELETON}\n"
        f"{config.INFO_GAIN_RULE}\n"
        + (
            "旧稿存在的具体问题（这一版必须全部改掉）：\n"
            + "\n".join(f"· {x}" for x in issues[:6])
            + "\n特别是：每个小节的加粗首句必须是**这一节自己的结论**，"
            "不许把同一句话换个地方反复当结论用。\n\n"
            if issues
            else ""
        )
        + "自检三问（写完自己过一遍）：\n"
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

    def clean_heading(text: str) -> str:
        """小标题里不许有加粗，也不许残留 Markdown 标记。

        模型很爱写 `## **变革难点一：组织结构**`，在公众号里等于标题又粗又带星号，
        而且会把加粗次数顶到十几处。小标题本身就是视觉重点，不需要再加粗。

        还有一种更隐蔽的：把标记写了两遍 `## ## 这种能力背后的核心逻辑`。
        外层那个 `## ` 被当成分隔符吃掉了，里层的 `## ` 留在文字里，
        发到公众号就显示成「## 这种能力背后的核心逻辑」（2026-11-20 线上实际撞到）。
        所以行首的 # 要反复剥干净，行尾的闭合 #（`## 标题 ##` 写法）也一并去掉。
        """
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"</?strong>", "", text)
        text = re.sub(r"^(?:#{1,6}\s*)+", "", text.strip())
        text = re.sub(r"\s*#+$", "", text)
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
            html_lines.append(f"<{level}>{clean_heading(text)}</{level}>")
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

        # 只挑"安全切点"：句末、后面不紧跟结束标签、且切到这里标签栈恰好为空。
        # 模型常把两句一起加粗（<strong>甲。乙。</strong>），句号在标签内部，
        # 从那里切会把标签劈成两半——这种位置直接不选，改用段内其他句号切。
        cuts = [
            m.end()
            for m in re.finditer(r"[。！？]", inner)
            if not inner[m.end():m.end() + 2].startswith("</")
            and tags_balanced(inner[: m.end()])
        ]
        if not cuts:
            return match.group(0)

        units: list[str] = []
        start = 0
        for pos in cuts:
            units.append(inner[start:pos])
            start = pos
        if start < len(inner):
            units.append(inner[start:])

        chunks: list[str] = []
        buf = ""
        for unit in units:
            if buf and visible(buf + unit) > limit:
                chunks.append(buf)
                buf = unit
            else:
                buf += unit
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


def ensure_bold(body: str) -> str:
    """给每节首句补上加粗。

    骨架要求"每节第一句加粗"，但模型被限流降级到 glm-4-flash 时经常
    一个加粗都不写，读者扫下来全是平字。这里只改标记不改文字：
    把小标题后第一段的首句包进 <strong>，补到 3 处为止。
    """
    if body.count("<strong>") >= 3:
        return body

    def bold_first_sentence(match: "re.Match[str]") -> str:
        head, para = match.group(1), match.group(2)
        if "<strong>" in para:
            return match.group(0)
        parts = re.split(r"(?<=[。！？])", para, maxsplit=1)
        if not parts or not parts[0].strip():
            return match.group(0)
        rest = parts[1] if len(parts) > 1 else ""
        return f"{head}<p><strong>{parts[0]}</strong>{rest}</p>"

    return re.sub(
        r"(<h2>.*?</h2>\s*)<p>(.*?)</p>",
        bold_first_sentence,
        body,
        flags=re.S,
    )


def tidy_spaces(body: str) -> str:
    """删掉中文之间多余的空格。

    模型常在句号后多打一个半角空格（"处于高危状态。 虽然目前……"），
    在公众号里会显示成一跳一跳的空隙。只处理两侧都是中文（含中文标点）的空格，
    数字和英文两侧留空格是需要的，不动。
    """
    cjk = r"\u4e00-\u9fff\u3000-\u303f\uff01-\uff60"
    return re.sub(rf"(?<=[{cjk}])[ \u3000]+(?=[{cjk}])", "", body)


def unwrap_paragraph_bold(body: str, keep: int = 5) -> str:
    """把"整段只有一句加粗"的段落拆掉加粗。

    骨架要求加粗句和后面正文连成一段，模型却偏爱把结论单独甩一行加粗。
    在公众号里那等于凭空多出一排小标题，读者扫下来全是重点，等于没有重点
    （实测一篇稿子 9 处加粗，5 处是这种整段加粗）。
    只在总数超过 keep 时动手，从最前面开始拆，保证不少于 keep 处。
    """
    total = body.count("<strong>")
    if total <= keep:
        return body

    budget = [total - keep]

    def repl(match: "re.Match[str]") -> str:
        if budget[0] <= 0:
            return match.group(0)
        budget[0] -= 1
        return f"<p>{match.group(1)}</p>"

    return re.sub(
        r"<p>\s*<strong>(.*?)</strong>\s*</p>", repl, body, flags=re.S
    )


def shorten_title(title: str, limit: int = 22) -> str:
    """标题超长时按中文标点切，尽量保住 12-22 字的信息量。

    公众号分享卡片大约 22 字就截断，"腾讯姚顺雨主导 AI 架构精简，而全公司 11 万人
    面临大调整，这类扁平化变革落地难点在哪？"这种 46 字标题，读者根本看不到重点。

    踩过的坑：只取第一个标点前的内容，遇到"混元大模型开源后，中小团队自建推理……"
    就会切出 8 个字的"混元大模型开源后"——短到没有任何信息点。
    所以首句不足 12 字时，改成丢掉首句、用后面完整的子句（"中小团队自建推理的成本账要怎么算"）。
    """
    text = re.sub(r"<[^>]+>", "", title).strip()
    if len(text) <= limit:
        return text

    parts = [p.strip() for p in re.split(r"[：，、；？！?!]", text) if p.strip()]

    # 1) 从第一句开始往后拼，能塞进 limit 的最长组合（保住主语）
    for take in range(len(parts), 0, -1):
        candidate = "，".join(parts[:take])
        if 12 <= len(candidate) <= limit:
            return candidate

    # 2) 首句太短（如 8 字），丢掉它，用后面的子句
    for index in range(1, len(parts)):
        if 12 <= len(parts[index]) <= limit:
            return parts[index]
        candidate = "，".join(parts[index:])
        if 12 <= len(candidate) <= limit:
            return candidate

    # 3) 实在没有合适切点（比如整句没有标点），才按字数硬切
    return text[:limit]


def cap_bold(body: str, limit: int = 5) -> str:
    """加粗超过 limit 处时兜底收敛：每节只留第一句加粗，总数不超过 limit。

    实测模型会把半篇都加粗（一次 23 处），结构自检虽然报错，重写却未必改掉，
    最后发出去等于全文没有重点。代码收敛比反复求模型可靠。
    """
    if body.count("<strong>") <= limit:
        return body

    out: list[str] = []
    kept = 0
    for block in re.split(r"(?=<h2>)", body):
        used_here = 0
        for seg in re.split(r"(<strong>.*?</strong>)", block, flags=re.S):
            if seg.startswith("<strong>") and (used_here >= 1 or kept >= limit):
                out.append(seg[len("<strong>"):-len("</strong>")])
                continue
            if seg.startswith("<strong>"):
                used_here += 1
                kept += 1
            out.append(seg)
    return "".join(out)


def drop_title_heading(body: str, title: str) -> str:
    """删掉和标题重复的第一个小标题。

    模型常把标题又原样写成第一个 ## 小标题，粘到公众号上就是"标题、标题"连着出现两遍。
    """
    clean = re.sub(r"<[^>]+>", "", title).strip()
    first = re.match(r"\s*<h2>(.*?)</h2>", body, re.S)
    if first and re.sub(r"<[^>]+>", "", first.group(1)).strip() == clean:
        return body[first.end():].lstrip()
    return body


def split_sentences(text: str, min_len: int = 12) -> list[str]:
    """按中文句末标点切句，只保留够长的（短句重复不算毛病）。"""
    return [
        s.strip()
        for s in re.split(r"(?<=[。！？!?])", text)
        if len(s.strip()) >= min_len
    ]


def dedupe_repeated(body: str, min_times: int = 2) -> str:
    """同一句话被写了多遍时兜底收敛，不改写任何文字。

    实测（2026-11-20 线上撞到）：模型把"通用模型直接驱动机器人正在从实验走向落地。"
    这一句既当正文第二段的结论，又在三个小节各当一次「加粗首句」，全文出现 4 次。
    读者看到的是同一句被反复强调——等于三个小节都没有自己的结论。

    只做两件绝对安全的事：
      1. 整段只有这一句、且这句在别处也出现 → 删掉整段（信息量为零，删了不亏）；
      2. 同一句被加粗 3 次以上 → 第二次起去掉加粗，只强调一次。
    真正"每节结论各不相同"只能靠 structure_issues 报出来让模型重写，代码改不了语义。
    """
    para_re = re.compile(r"<p(?:\s[^>]*)?>.*?</p>", re.S)
    blocks = para_re.findall(body)
    if not blocks:
        return body

    texts = [re.sub(r"<[^>]+>", "", b).strip() for b in blocks]
    counts: dict[str, int] = {}
    for text in texts:
        for sent in split_sentences(text):
            counts[sent] = counts.get(sent, 0) + 1
    repeated = {s for s, n in counts.items() if n >= min_times}

    # 1) 整段就是一句重复话：删掉
    for block, text in zip(blocks, texts):
        if text and text in repeated:
            body = body.replace(block, "", 1)

    # 2) 加粗重复：只强调第一次
    seen_bold: set[str] = set()

    def fix_bold(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if counts.get(inner, 0) >= 3:
            key = re.sub(r"\s+", "", inner)
            if key in seen_bold:
                return inner
            seen_bold.add(key)
        return match.group(0)

    return re.sub(r"<strong>(.+?)</strong>", fix_bold, body)


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

    title = shorten_title(title)
    body = drop_title_heading(markdown_to_html("\n".join(body_lines)), title)
    return title, tidy_spaces(
        cap_bold(
            dedupe_repeated(
                strip_source_note(
                    strip_meta_notes(
                        unwrap_paragraph_bold(ensure_bold(split_long_paragraphs(body)))
                    )
                )
            )
        )
    )


def structure_issues(
    body: str, title: str = "", desc: str = "", min_words: int = 0
) -> list[str]:
    """检查成稿是否符合结构模板，只报会毁掉可读性的硬伤。

    实测模型会稳定犯这几类毛病：小标题写成"什么情况？""该怎么办？"、凑出五个小节、
    开头一律"近日，某某公司……"、结尾一律"综上所述"、把 300 字塞成一块、
    通篇加粗。这里做代码层校验，不合格就让模型带问题清单重写一次，
    比单纯在提示词里求它守规矩可靠。

    min_words：正文最低字数。用户反馈"文章有点短"，光在提示词里写字数是拦不住的，
    必须在代码层量一遍——模型说写了 1500 字，实际常常只有 700。
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
    # 小标题里残留 Markdown 标记：`## ## 标题` 会被转成「## 标题」显示在页面上
    if any(re.search(r"^\s*#|#+\s*$|\*\*", h) for h in heads):
        issues.append("小标题残留 Markdown 标记")

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

    # 篇幅：太短说明没讲透。这条是给"文章有点短"兜底的硬校验
    total_words = word_count(body)
    if min_words and total_words < min_words:
        issues.append(f"正文只有 {total_words} 字，低于 {min_words} 字，内容太薄")

    # 分节字数：三节各写一百多字，等于把一个观点拆成三份，读者拿不到增量
    if len(heads) >= 2:
        sections = re.split(r"<h2>.*?</h2>", body, flags=re.S)[1:]
        for index, section in enumerate(sections, 1):
            section_words = word_count(section)
            if section_words < config.SECTION_MIN_WORDS:
                issues.append(f"第 {index} 节只有 {section_words} 字，写得太敷衍")

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

    # 模板化排比："对普通用户而言……对从业者而言……对小团队而言……"
    # 这是模型凑字数最常用的手段：换三次主语把同一句话重说一遍，读者一个字都拿不到。
    # 扩写实测一次就冒出 9 处，字数上去了，稿子也废了，必须拦在代码层。
    template_hits = len(re.findall(r"对[^，。；：]{2,6}而言", plain))
    if template_hits >= 3:
        issues.append(f"出现 {template_hits} 处「对…而言」排比，是在套模板凑字")

    # 填充词密度：单个出现很正常，扎堆就是注水
    filler_words = (
        "有助于", "促进", "具有重要意义", "发挥重要作用", "提供有力支撑",
        "注入新动能", "迈上新台阶", "筑牢", "夯实",
    )
    filler_hits = sum(plain.count(word) for word in filler_words)
    if filler_hits >= 4:
        issues.append(f"填充词出现 {filler_hits} 次，内容在注水")

    # 9.2 分说成"满分"是硬伤，懂行的人一眼就看穿（两轮实测都出现过）
    if "满分" in plain and "满分" not in desc and "10.0" not in desc:
        issues.append("把评分说成「满分」，与素材不符")

    # 结尾：总结套话是读者划走的信号
    tail = plain[-120:]
    for word in ("综上所述", "希望对你有帮助", "希望对大家有帮助", "希望对大家有所启发"):
        if word in tail:
            issues.append(f"结尾用了总结套话「{word}」")
            break

    # 结尾要给人一个能做的动作或一句能回应的提问，温吞收尾最容易被划走
    action_words = (
        "检查", "查看", "确认", "升级", "自查", "试试", "看看", "执行",
        "留言", "评论", "转发", "收藏", "关注", "回复", "问一句",
    )
    if paragraphs and "？" not in paragraphs[-1] and not any(
        word in paragraphs[-1] for word in action_words
    ):
        issues.append("结尾没有给出动作或提问")

    # 同一句话反复出现：模型常把加粗句单独成段，后面又原样复述一遍；
    # 更常见的是拿同一句当多个小节的结论（2026-11-20 实测一篇里同一句出现 4 次）。
    # 报出次数和原句，模型才知道该改哪里——只说"有重复"它往往原样再写一遍。
    sentences = [s.strip() for s in re.split(r"[。！？!?]", plain) if len(s.strip()) >= 12]
    if sentences:
        worst_sent = max(set(sentences), key=sentences.count)
        times = sentences.count(worst_sent)
        if times > 1:
            issues.append(f"有句子被重复复述 {times} 次（{worst_sent[:18]}），同一个结论只讲一次")

    # 标题里的数字必须能在素材或正文里找到，防止"1000 亿"被写成"100 亿"
    if title and desc:
        title_nums = re.findall(r"\d+(?:\.\d+)?", title)
        material_nums = set(re.findall(r"\d+(?:\.\d+)?", desc + " " + plain))
        if any(n not in material_nums for n in title_nums):
            issues.append("标题里的数字与素材对不上")

    return issues


# ---- 去 AI 感质检：只诊断不改稿，供审核页显示"AI 味"评分 ----
# 借鉴 Easel 的 zh-ai-markers 思路：中文 AI 稿有固定的腔调特征——
# 公文腔套话、吊胃口开头、排比扎堆、结尾不落地、程度副词堆砌。
# 这里只报症状不自动改，因为改范文会破坏加入的真实细节，交给人在审核页定夺。
AI_TONE_MARKERS = (
    ("公文腔", (
        "值得注意的是", "不难发现", "众所周知", "由此可见", "总而言之",
        "综上所述", "在当今社会", "随着……的发展", "扮演着重要的角色",
        "具有重要意义", "发挥着重要作用", "起到了一定的作用",
    )),
    ("吊胃口", (
        "接下来我们就", "下面为大家", "你猜怎么着", "答案即将揭晓",
        "让我们拭目以待", "究竟是怎么回事", "背后真相令人",
    )),
    ("空泛升华", (
        "展望未来", "前景可期", "值得期待", "共同努力", "共创美好",
        "迈上新台阶", "书写新篇章", "交出满意答卷",
    )),
    ("程度堆砌", (
        "极大地", "显著地", "大幅度地", "深刻地", "全方位地",
        "有效地", "充分地", "实实在在地",
    )),
)


def ai_tone_check(body: str) -> dict[str, Any]:
    """检测正文的 AI 腔，返回评分与命中的症状（不改稿）。

    评分：满分 100，每命中一类症状按出现次数扣分，封底 0。
    只做字符串信号检测，误报率低；语义层面的"没营养"交给 content_advice。
    """
    plain = re.sub(r"<[^>]+>", "", body or "")
    hits: list[dict[str, str]] = []
    score = 100
    for label, words in AI_TONE_MARKERS:
        found = [w for w in words if w in plain]
        if not found:
            continue
        count = sum(plain.count(w) for w in found)
        hits.append({"label": label, "words": "、".join(found[:3]), "count": count})
        score -= min(25, 8 + count * 4)

    # 排比扎堆：同一句式"对……而言""不仅……而且"出现 3 次以上
    for pat, label in ((r"对[^，。]{1,12}而言", "排比句式"), (r"不仅[^，。]{1,20}而且", "递进排比")):
        n = len(re.findall(pat, plain))
        if n >= 3:
            hits.append({"label": label, "words": f"出现 {n} 次", "count": n})
            score -= min(20, 6 + n * 3)

    # 结尾是否落地：末段有具体动作/判断词算落地，全是升华算空
    tail = plain.strip()[-160:]
    action_words = ("检查", "确认", "试试", "先", "再", "应该", "可以", "建议", "步骤", "命令", "设置")
    if tail and not any(w in tail for w in action_words):
        hits.append({"label": "结尾不落地", "words": "末段没有可执行的动作或明确判断", "count": 1})
        score -= 10

    score = max(0, score)
    return {"score": score, "level": "轻" if score >= 80 else ("中" if score >= 60 else "重"), "hits": hits}


def body_to_text(body: str) -> str:
    """把成稿 HTML 还原成带 Markdown 标记的纯文本，供扩写提示词使用。"""
    text = re.sub(r"<h2>(.*?)</h2>", r"\n## \1\n", body, flags=re.S)
    text = re.sub(
        r"<pre><code>(.*?)</code></pre>",
        lambda m: "\n```\n" + m.group(1) + "\n```\n",
        text,
        flags=re.S,
    )
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.S)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.S)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def section_word_counts(body: str) -> list[int]:
    """按小标题切段，返回每节的可见字数（不含第一节之前的开头）。"""
    sections = re.split(r"<h2>.*?</h2>", body, flags=re.S)[1:]
    return [word_count(section) for section in sections]


def build_expand_prompt(
    title: str, body: str, item: dict[str, Any], min_words: int
) -> str:
    """扩写提示词：同一素材把稿子写厚，只许补解释，不许补事实。

    为什么单独写一个提示词而不是让模型"重写一遍"：
    重写会换角度、换观点，读者看到的是另一篇文章；扩写保留原稿的判断和结构，
    只在每节内部把"为什么""对谁有影响""怎么判断""怎么做"讲透。
    """
    desc = (item.get("desc") or "").strip()
    grade = material_grade(desc)
    low, high = config.ARTICLE_WORDS[grade]
    material = desc if desc else "（本题只有标题，没有任何正文素材）"
    counts = section_word_counts(body)
    detail = "、".join(f"第 {i} 节 {n} 字" for i, n in enumerate(counts, 1)) or "暂无小标题"
    current = word_count(body)

    return (
        "你是资深编辑。下面这篇稿子事实没问题，但写得太薄，读者会觉得没讲透。\n"
        f"当前全文 {current} 字（{detail}），请扩写到 {low}-{high} 字，不得少于 {min_words} 字。\n"
        "**先把每节补到 300 字以上再动全文**；某一节信息确实不够，就把两节并成一节，"
        "另起一节写判断标准或操作路径，小标题仍然是 3 个。\n\n"
        "扩写红线（违反即作废）：\n"
        "1. 原稿里的事实、数字、版本号、人名、机构、链接一律保留，一个字都不许改\n"
        "2. 不许新增任何具体事件经过、金额、时间、地点、数据——素材里没有的绝对不能写\n"
        "3. 不许为了凑字数编例子。想举例只能用'假设''如果'开头的假设句，不能写成发生过的事\n"
        "4. 保留原有小标题和整体结构，在每节内部补充 1-2 段；小标题最多 4 个\n"
        "5. 不许用排比、宏大形容词、'值得注意的是''不难发现'这类填充句\n"
        "6. **禁止套模板**：不要出现'对普通用户而言''对从业者而言''对小团队而言'"
        "这种把读者硬分成三类的排比，也不要每节都写成'影响 + 意义 + 建议'三连。\n"
        "7. 全文加粗总共不超过 5 处，扩写不许新增加粗\n\n"
        "补什么由你判断，从下面挑真正适合这一篇的（挑 2-3 类就够，不要逐条套）：\n"
        "· 影响分岔：这件事对不同处境的人结论不一样，说清哪类人该动、哪类人可以等；\n"
        "· 判断标准：给出可以自己对照的分界线，让人知道自己落在哪一边；\n"
        "· 机制解释：用通用原理说明它为什么成立、什么条件下会失效；\n"
        "· 常见误解：多数人的第一反应是什么，那个反应错在哪；\n"
        "· 操作路径：从哪一步开始，做完怎么验证。\n"
        "**每补一段都要有新信息。写不出新信息就保持原长度，宁可短，不要用'有助于''促进''具有重要意义'"
        "这类词把句子撑长——那种段落读者一眼就能看出是凑的。**\n\n"
        f"话题：{item.get('title') or title}\n"
        f"唯一素材来源（{item.get('source', '热榜')}）：{material}\n\n"
        "原稿：\n"
        f"{body_to_text(body)}\n\n"
        "输出要求：第一行是标题，然后直接是正文。不要写'以下是扩写版本'之类的说明，"
        "不要输出解释性文字。小标题用 ## 开头，需要强调的句子用**这样**加粗，每节最多一句。"
    )


def is_length_issue(issue: str) -> bool:
    """判断一条问题是不是"只是字数不够"，而不是结构硬伤。

    只有纯字数问题才值得扩写。稿子要是没小标题、标题 46 字、五节各一百字，
    扩写只会把毛病放大——那种必须回炉重写或直接交人工。
    """
    return issue.startswith("正文只有") or bool(re.match(r"第 \d+ 节只有", issue))


# 扩写前必须清零的硬伤。这类问题在扩写里改不好，只会被写得更长更乱。
# 注意「结尾没有动作」「加粗不足」不在此列——那两条扩写时顺手就修了，
# 拿它们卡扩写，会让"只差字数"的稿子白白停在 600 字。
HARD_ISSUE_MARKERS = (
    "没有小标题", "超过 4 个", "设问句", "素材未提供", "重复复述",
    "新闻体", "套模板", "注水", "标题", "评分说成",
)


def has_hard_issue(issues: list[str]) -> bool:
    """是否还有扩写解决不了的硬伤。"""
    return any(
        not is_length_issue(x)
        and not x.startswith(_ADVICE_TAG)
        and any(m in x for m in HARD_ISSUE_MARKERS)
        for x in issues
    )


# 建议项统一前缀，用于从「硬伤」里剔除——建议改不掉也必须允许写回
_ADVICE_TAG = "[内容建议] "


def content_advice(body: str, desc: str = "") -> list[str]:
    """内容质量「建议」，不是硬伤——只回灌给模型改进，绝不阻止写回。

    为什么要分开：structure_issues 里的每一条在重写时都会被当成必须修的硬伤，
    修不掉就导致整篇不写回。而"这节有没有增量"是语义判断，模型未必一次改好，
    代码也可能误判。把它做成建议，既给了模型改进方向，又不会因为改不干净
    就用不了稿子（2026-11-20 重写卡死的教训）。

    检测的都是能客观量化的信号：
      1. 每节有没有具体锚点：数字、版本号、英文专名、明确的动作词；
      2. 全篇空泛判断句是否扎堆（没有锚点的短句）；
      3. 素材里有数字但正文一个都没用上——大概率在复述空话。
    """
    advice: list[str] = []
    # desc 可能来自热榜某源，字段缺失时为 None；re.findall 收到 None 会抛
    # TypeError，直接让 /generate 变 500。这里统一兜底成空串。
    desc = desc or ""
    plain = re.sub(r"<[^>]+>", "", body)
    sections = re.split(r"<h2>.*?</h2>", body, flags=re.S)[1:]

    # 具体锚点：数字 / 版本号 / 英文专名 / 可执行的动作词
    anchor_words = (
        "检查", "查看", "确认", "升级", "自查", "修改", "运行", "执行",
        "打开", "关闭", "删除", "重启", "命令", "设置", "选项", "版本",
    )
    for index, section in enumerate(sections, 1):
        text = re.sub(r"<[^>]+>", "", section)
        has_num = bool(re.search(r"\d", text))
        has_en = bool(re.search(r"[A-Za-z]{3,}", text))
        has_action = any(w in text for w in anchor_words)
        if not (has_num or has_en or has_action):
            advice.append(_ADVICE_TAG +
                f"第 {index} 节没有任何具体信息（数字/专名/可执行动作），"
                "读起来像空话，请补一个能落地的细节")

    # 素材里有数字，正文却一个数字都没出现：基本可以断定在复述
    material_nums = set(re.findall(r"\d+(?:\.\d+)?", desc))
    if material_nums and not re.search(r"\d", plain):
        advice.append(_ADVICE_TAG + "素材里有具体数字（版本/金额/时间），正文却一个都没用上，请写进去")

    # 空泛判断扎堆
    vague = (
        "值得关注", "影响深远", "意义重大", "至关重要", "拭目以待",
        "大幅提高", "显著提升", "带来变革",
    )
    hits = sum(plain.count(w) for w in vague)
    if hits >= 3:
        advice.append(_ADVICE_TAG + f"空泛判断句出现 {hits} 次，请换成具体事实或明确动作")

    return advice


def strip_meta_notes(body: str) -> str:
    """删掉模型写进正文的自我说明。

    弱模型遇到被截断的素材会补一句「（此处原文中断，未给出完整内容）」，
    有时还会加一行「以下是扩写后的版本」。这些既不是内容也不是给读者的提示，
    粘到公众号上非常难看，代码层直接清掉。
    """
    body = re.sub(r"[（(]\s*此处原文中断[^）)]*[）)]", "", body)
    body = re.sub(
        r"<p>\s*(以下|下面)是[^<]{0,24}(扩写|改写|优化|版本|正文)[^<]*</p>", "", body
    )
    return body


def strip_source_note(body: str) -> str:
    """删掉正文末尾的「信息来源/出处/原文链接」标注段。

    用户 2026-11-23 反馈：成品里挂着别人的公众号名（如"信息来源：公众号·某某《...》"）
    会把读者引到对方那里，造成流失。默认关掉，config.SHOW_SOURCE_NOTE=1 时保留。

    只在正文末尾处理，避免误删正文中间正常引用的"来源"字样。命中的两种形态：
      1. 独立小标题 <h2>信息来源</h2> / <h2>出处</h2> 连同其后直到下一个标题的内容；
      2. 结尾的 <p>信息来源：.../出处：.../原文链接：...</p> 段落。
    """
    if config.SHOW_SOURCE_NOTE:
        return body

    # 来源标注一律在正文末尾，从命中处删到结尾即可。覆盖模型各种写法：
    # <h2>信息来源</h2>、<p>信息来源</p>、<p>信息来源：xxx</p>，以及后面拖着的链接段。
    source_head = (
        r"<\s*(?:h[1-6]|p|div|section)[^>]*>\s*"
        r"(?:信息来源与声明|信息来源|原文出处|参考资料|来源|出处)"
        r"\s*(?:[：:][^<]*)?\s*</\s*(?:h[1-6]|p|div|section)\s*>"
    )
    cut = re.search(source_head, body, flags=re.S | re.I)
    if cut:
        if cut.start() >= len(body) * 0.6:
            body = body[: cut.start()].rstrip()
        else:
            body = re.sub(
                source_head + r"\s*(?:<p[^>]*>.*?</p>\s*){0,3}",
                "",
                body,
                count=1,
                flags=re.S | re.I,
            )
    return body


def expand_article(
    title: str, body: str, item: dict[str, Any], min_words: int
) -> tuple[str, str] | None:
    """扩写一次，返回 (标题, 正文)；没变长、变注水或调用失败返回 None。

    只在"结构已经合格但字数不够"时调用——结构都不对的稿子，扩写只会把毛病放大。
    验收卡四道：结构只差字数、字数必须增加、不能超过上限、不能引入新毛病。
    踩过的坑：第一次只比字数，模型把 1062 字灌到 1327 字却塞进 9 处"对…而言"排比；
    第二次弱模型一口气冲到 2034 字，还写出"（此处原文中断，未给出完整内容）"这种 meta 话。
    """
    desc = (item.get("desc") or "").strip()
    grade = material_grade(desc)
    upper = int(config.ARTICLE_WORDS[grade][1] * 1.2)
    before = word_count(body)
    before_issues = structure_issues(body, title, desc, min_words)
    if has_hard_issue(before_issues):
        print("  仍有结构硬伤，跳过扩写（扩写只会放大毛病）")
        return None

    raw = call_ai(build_expand_prompt(title, body, item, min_words))
    if not raw:
        return None
    new_title, new_body = parse_response(raw, title)
    after = word_count(new_body)
    if after <= before:
        print(f"  扩写没有变长（{before} → {after} 字），保留原稿")
        return None
    if after > upper:
        print(f"  扩写冲过头（{after} 字 > 上限 {upper} 字），保留原稿")
        return None

    after_issues = structure_issues(new_body, new_title, desc, min_words)
    watered = [x for x in after_issues if "套模板" in x or "注水" in x]
    if watered:
        print(f"  扩写注入空话（{'；'.join(watered)}），保留原稿")
        return None
    if len(after_issues) > len(before_issues):
        print(f"  扩写引入 {len(after_issues) - len(before_issues)} 处新问题，保留原稿")
        return None

    print(f"  扩写：{before} → {after} 字，问题 {len(before_issues)} → {len(after_issues)}")
    return new_title, new_body


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
        # 按素材厚度定字数门槛：厚素材要求长，只有标题的反而不能长（长了必注水）
        min_words = config.ARTICLE_MIN_WORDS[material_grade(desc)]
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

            # 结构不合格 / 内容空泛：共用这一次重写机会（只重试一次，控成本）。
            # content_advice 是建议不是硬伤，单独判断，不影响"结构合格"的结论。
            issues = structure_issues(body, title, desc, min_words)
            advice = content_advice(body, desc)
            if issues or advice:
                reason = "结构自检未过" if issues else "内容增量不足"
                print(f"  {reason}：{'、'.join(issues or advice)} → 重写一次")
                fix_prompt = (
                    f"{prompt}\n\n"
                    f"你上一版存在这些问题：{'；'.join(issues + advice)}。\n"
                    "请严格按上面的结构模板重写全文：小标题必须是陈述句、"
                    "数量控制在 3 个、每节第一句加粗、每节写到 300 字以上、"
                    "去掉空话套话。每节都要有读者能拿到的具体信息，"
                    "不要只是把同一件事换成几种说法。"
                )
                raw2 = call_ai(fix_prompt)
                if raw2:
                    title2, body2 = parse_response(raw2, topic)
                    # 用「结构问题 + 内容建议」总数比，任一侧改善就采纳。
                    # 只比 issues 的话，纯内容建议（issues 为空）时新稿永远不会被采纳。
                    before_score = len(issues) + len(advice)
                    after_score = len(structure_issues(body2, title2, desc, min_words)) + len(
                        content_advice(body2, desc)
                    )
                    if after_score < before_score:
                        title, body = title2, body2

            # 重试也未必改得掉，最后统一把编造的配置行剥一次
            body = strip_unsafe_config(body, desc)

            # 重写一遍还是偏短：换扩写（同一判断、同一素材，只把每节讲透）
            # 先确认剩下的问题只有字数——结构还有硬伤时扩写只会放大毛病，直接交人工
            remaining = structure_issues(body, title, desc, min_words)
            if word_count(body) < min_words and not has_hard_issue(remaining):
                # 最多两次：第一次常因模型"顺手压缩"而变短，第二次带着每节字数再试
                for _ in range(2):
                    if word_count(body) >= min_words:
                        break
                    expanded = expand_article(title, body, topic_item, min_words)
                    if not expanded:
                        continue
                    title, body = expanded
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


def optimize_article(
    article_id: str, max_retry: int = 1
) -> dict[str, Any] | None:
    """基于原稿真实素材，用大模型重写一篇，覆盖当前稿件。

    - 保持 status=approved 与 approved_at 不变（不刷新 30 天保留期起算点）
    - 只在"重写结果确实更好"时才写回：AI 失败、或结构硬伤没修掉，一律返回 None，
      原稿毫发无损
    - 成功返回更新后的稿件字典，供接口回显新标题/正文

    为什么要卡结构再覆盖（2026-11-20 血的教训）：
    原来只要 AI 返回就写回。实测一次重写，模型输出通篇没有小标题（整段铺陈），
    覆盖后把一篇结构合格、已推草稿箱的稿子变成废物，只能从备份回滚。
    重写比初稿更该被校验——初稿至少还走了一次 structure_issues 重试。
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
    # 先给旧稿做一次自检，把毛病明说给模型——不提的话它只在旧稿上换措辞，同样的病再犯一遍
    desc = (article.get("desc") or "").strip()
    min_words = config.ARTICLE_MIN_WORDS[material_grade(desc)]
    issues = structure_issues(
        article.get("body", ""), article.get("title", ""), desc, min_words
    )
    # 内容建议一并告诉模型，但不进 hard 判断——改不掉也允许写回，
    # 否则一篇结构合格、只是略空泛的稿子会被判死（2026-11-20 教训）。
    advice = content_advice(article.get("body", ""), desc)
    issues = issues + advice

    new_title, new_body = article.get("title", ""), article.get("body", "")
    for attempt in range(max_retry + 1):
        raw = call_ai(build_optimize_prompt(article, issues))
        if raw is None:
            # AI 失败/限流：不写回，原稿保持不变
            print("  重写失败（模型不可用），保留原稿")
            return None

        cand_title, cand_body = parse_response(raw, article.get("title", "优化稿件"))
        left = structure_issues(cand_body, cand_title, desc, min_words)
        # 建议项不算硬伤：改不掉也允许写回，否则结构合格只是略空泛的稿子会被判死
        hard = [
            x for x in left
            if not is_length_issue(x) and not x.startswith(_ADVICE_TAG)
        ]
        if not hard:
            new_title, new_body = cand_title, cand_body
            break

        print(f"  重写第 {attempt + 1} 次仍有结构硬伤：{'、'.join(hard)}")
        if attempt == max_retry:
            print("  重写不合格，保留原稿（不拿坏稿覆盖好稿）")
            return None
        # 带着这次的问题清单再要一版
        issues = hard

    # 优化后仍然偏短：同素材扩写一次（和初稿走同一套规则，不多一个特例）
    if word_count(new_body) < min_words:
        expanded = expand_article(new_title, new_body, article, min_words)
        if expanded:
            new_title, new_body = expanded
    new_body = strip_unsafe_config(new_body, desc)

    article["title"] = new_title
    article["body"] = new_body
    article["mode"] = "AI优化重写"
    article["updated_at"] = datetime.now().isoformat(timespec="seconds")
    # status / approved_at 均不改动：已通过状态与保留期起算点保持原样
    queue[index] = article
    save_queue(queue)
    return article


def build_retitle_prompt(article: dict[str, Any]) -> str:
    """拼「换标题」的提示词：给正文 + 真实素材，要 3 个不同公式的候选。

    和 build_optimize_prompt 的区别：这里只让模型动标题，明确禁止改正文角度。
    素材仍要带上，因为标题里的数字必须能对得上原文，不能自己编。
    """
    topic = article.get("topic") or article.get("title", "")
    desc = (article.get("desc") or "").strip()
    source = article.get("source", "实时热榜")
    # 正文只给前 1600 字：够模型理解文章讲了什么，又不至于把 token 撑爆
    body = body_to_text(article.get("body", ""))[:1600]

    if desc:
        material = f"真实素材（来自{source}）：\n{desc}"
    else:
        material = (
            f"该话题来自{source}，只有原标题、没有正文素材。\n"
            "标题里的数字只能来自下面的正文，正文没有就别写数字。"
        )

    return (
        "你是公众号标题编辑。任务只有一个：给同一篇文章拟 3 个不同角度的标题。\n"
        "不要重写正文，不要改变文章结论，只输出标题。\n\n"
        f"原标题：{topic}\n"
        f"{material}\n\n"
        f"文章正文（节选）：\n{body}\n\n"
        f"{profile_mod.prompt_block(mode='title')}\n\n"
        f"{config.TITLE_VARIANTS_RULE}"
    )


def _parse_title_candidates(raw: str, desc: str, body: str) -> list[str]:
    """从模型输出里拆出最多 3 个候选标题，逐个过校验。

    模型常犯：加「1.」编号、加引号、写成「标题：xxx」、三个候选其实换汤不换药、
    自己编数字。这里逐条剥壳 + 去重 + 数字对账，宁可少给也别把坏标题摆上去让人选。
    """
    seen: set[str] = set()
    out: list[str] = []
    material_nums = set(re.findall(r"\d+(?:\.\d+)?", desc + " " + body))

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # 剥「1.」「①」「-」「*」等行首编号，剥引号和「标题：」前缀
        line = re.sub(r"^\s*(?:\d+[.、)]|[①②③④⑤]|[-*])\s*", "", line)
        line = strip_title_prefix(line)
        line = line.strip().strip("\"'「」《》").strip()
        if not line or "<" in line or ">" in line:
            continue

        # 诱导词：和结构校验同一份黑名单
        if any(w in line for w in ("揭秘", "震惊", "速看", "炸裂", "马上删", "惊呆", "血亏", "封神")):
            continue
        # 长度：太短没信息，太长在分享卡片被截断
        if not (10 <= len(line) <= 24):
            continue
        # 数字对账：素材/正文里找不到的数字不收
        nums = re.findall(r"\d+(?:\.\d+)?", line)
        if nums and not all(n in material_nums for n in nums):
            continue
        # 去重：模型爱把同一个标题换两个字再给一遍
        key = re.sub(r"\s|[，。！？、：；]", "", line)
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= 3:
            break
    return out


def regenerate_titles(article: dict[str, Any]) -> list[str]:
    """基于已通过稿件的正文与素材，生成 3 个备选标题（不写回队列）。

    返回空列表代表 AI 不可用或候选全部不合格，调用方据此提示用户，
    原稿标题始终保持不动。
    """
    raw = call_ai(build_retitle_prompt(article))
    if not raw:
        print("  换标题失败（模型不可用），保留原标题")
        return []
    desc = (article.get("desc") or "").strip()
    body = article.get("body", "")
    candidates = _parse_title_candidates(raw, desc, body)
    if not candidates:
        print(f"  换标题候选全部不合格，保留原标题：{raw[:80]}")
    return candidates


def apply_title(article_id: str, title: str) -> dict[str, Any] | None:
    """把用户选中的候选标题写回稿件，只动 title，不动正文/状态/头图。

    已通过稿件的 approved_at 不刷新——标题微调不该让稿件"重新过期"一轮。
    mode 留痕为「AI换标题」，方便回溯是谁改的。
    """
    clean = shorten_title(title)
    if not clean or not (8 <= len(clean) <= 24):
        return None
    queue = load_queue()
    index = -1
    for i, article in enumerate(queue):
        if article.get("id") == article_id:
            index = i
            break
    if index == -1:
        return None
    article = queue[index]
    article["title"] = clean
    article["mode"] = "AI换标题"
    article["updated_at"] = datetime.now().isoformat(timespec="seconds")
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


def expand_pending(limit: int = 0) -> int:
    """把队列里偏短的待审稿扩写一遍，返回实际改写的篇数。

    为什么要单独一个命令：用户反馈"提交审核的文章有点短"时，
    队列里已经攒了一批 600-800 字的旧稿。规则改了只影响新稿，
    旧稿得回填一次。只动 pending（待审）的，已通过/已发布的一律不碰——
    那些可能已经发出去了，改了也对不上。
    """
    queue = load_queue()
    targets = [
        article for article in queue
        if article.get("status") == "pending"
        and word_count(article.get("body", "")) < config.ARTICLE_MIN_WORDS[
            material_grade(article.get("desc", ""))
        ]
    ]
    targets.sort(key=lambda a: word_count(a.get("body", "")))
    if limit:
        targets = targets[:limit]
    if not targets:
        print("没有偏短的待审稿")
        return 0

    changed = 0
    for article in targets:
        before = word_count(article.get("body", ""))
        desc = article.get("desc", "")
        min_words = config.ARTICLE_MIN_WORDS[material_grade(desc)]
        print(f"· {article['title'][:26]}（{before} 字 → 目标 {min_words}+）")
        result = expand_article(article.get("title", ""), article["body"], article, min_words)
        if not result:
            continue
        title, body = result
        article["title"] = title
        article["body"] = strip_unsafe_config(body, desc)
        article["mode"] = "AI扩写"
        article["updated_at"] = datetime.now().isoformat(timespec="seconds")
        changed += 1

    if changed:
        save_queue(queue)
    print(f"完成：{changed}/{len(targets)} 篇已扩写")
    return changed


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
    parser.add_argument(
        "--expand",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help="把偏短的待审稿扩写一遍（可指定最多 N 篇，不填=全部）",
    )
    args = parser.parse_args()

    if args.cleanup:
        run_cleanup()
    elif args.expand is not None:
        expand_pending(args.expand)
    else:
        run(args.count, args.demo)


if __name__ == "__main__":
    main()
