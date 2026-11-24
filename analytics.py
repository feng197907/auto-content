"""发布后数据回收与复盘（Easel「归因」层的轻量落地）。

为什么需要：项目原来发完就结束，不知道哪类标题、哪个选题源的文章表现好，
每天从零开始。这里让用户把公众号后台的几个数字录进来，自动按「标题公式 /
选题源 / 字数档」聚合，积累出可指导生成的经验。

设计取舍：
  - 不接公众号 API（阅读数据没有开放接口），靠手动录入，每周 5 分钟；
  - 存储用 data/analytics.json，和 data/queue.json 同风格，无数据库依赖；
  - 一条记录对应「一篇内容 × 一个渠道」，按 (article_id, channel) 去重——
    同一篇隔几天再看数据就更新那条；
  - 标题公式靠特征自动打标（和 config.TITLE_VARIANTS_RULE 的三类对齐），
    也允许录入时手动指定，避免自动判断不准。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "analytics.json"

# 标题公式标签，和 config.TITLE_VARIANTS_RULE 的三类一一对应
FORMULA_SPECIFIC = "具体信息点"
FORMULA_CONTRAST = "反差反常识"
FORMULA_BENEFIT = "利益直给"
FORMULA_OTHER = "其他"
FORMULAS = (FORMULA_SPECIFIC, FORMULA_CONTRAST, FORMULA_BENEFIT, FORMULA_OTHER)

# 渠道：同一篇内容在公众号里有两个分发位置，阅读数各算各的。
# 文章推给粉丝，贴图会被推荐给非粉丝——混成一列就把差异抹平了：
# 2026-11-24 实录「超级智能体」文章 4 阅读、贴图 17 阅读，
# 合并成 21 就看不出贴图其实是它的主要曝光来源。
CHANNEL_ARTICLE = "文章"
CHANNEL_PHOTO = "贴图"
CHANNELS = (CHANNEL_ARTICLE, CHANNEL_PHOTO)

# 品类：队列里的 column，加上 2026-04 那批程序员随笔（另一类内容）。
# 历史随笔单独成组，不混进 AI 稿的公式均值——那批阅读 150/190，
# 混进去会让「反差」组凭空变好，其实是受众基数和内容类型不同。
GENRE_HISTORY = "历史随笔"
COLUMN_LABELS = {
    "ai_news": "AI资讯",
    "ai_sidebiz": "副业选题",
    "github": "GitHub专栏",
}

# 覆盖率门槛：互动太少时「互动效率」不可比（一篇 5 次转发就能把 30 阅读的稿
# 拉到 167/千次，和 150 阅读 0 互动的稿放一起排序毫无意义）。
# 只有互动总数够多、整体互动率也够高时，才把主指标切成效率。
MIN_INTERACTIONS = 20
MIN_EFFICIENCY_PER_MILLE = 5.0

# 反差 / 利益的特征模式，用于自动打标。按命中数量打分取最高，
# 不是「命中一个就归这一类」——标题通常同时踩两类特征。
_CONTRAST_PATTERNS = (
    r"不是.{0,14}(?:才|而是)",   # 做公众号不是AI副业起点，能力才是
    r"看似.{0,14}(?:但|却)",     # 门槛看似低，但赚钱没想象中容易
    r"别[再把]",                 # 别再把博客当笔记
    r"却(?!是)",                 # 却忘了抬头升级思维
    r"当你以为",                 # 当你以为报价是技术评估时…
    r"反而", r"其实", r"真相", r"误区", r"错了", r"未必",
    r"并不是", r"并非", r"没想到", r"多半", r"重灾区", r"翻车", r"教训",
)
_BENEFIT_PATTERNS = (
    r"怎么", r"如何", r"教你", r"学会", r"省钱", r"避坑", r"清单",
    r"一步", r"分钟", r"个方法", r"这几", r"照做", r"能用", r"可用", r"指南",
    r"能赚多少", r"值不值", r"要不要", r"更贵", r"影响有多大", r"红利", r"第一笔",
)
# 具体信息点：版本号、带单位的数字、金额时间等硬信息，以及"全票/满分"这类
# 可核对的量化说法
_SPECIFIC_PATTERNS = (
    r"\d+\.\d+",                       # GLM-5.4 / 0.2%
    r"[A-Za-z]+-\d",                   # GPT-5 / GLM-5
    r"\d+(?:%|％|万|亿|元|块|个|条|篇|倍|号|分|天|周|月|年|秒|人|家)",
    r"[一二三四五六七八九十百千]+(?:亿|万|元|倍|年|月|天|个|条|步|成|次)",
    r"全票|满分|第一|首个|唯一",
)


def load_records() -> list[dict[str, Any]]:
    """读取复盘记录，文件不存在或损坏时返回空列表。"""
    if not DATA_PATH.exists():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_records(records: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def guess_formula(title: str) -> str:
    """按标题特征猜公式标签；猜不准归「其他」，用户可以手动改。

    以前是「命中第一个词就返回」，结果 8 篇里 6 篇判成「其他」：
    「二十年逆向老手」「影响有多大」这类明显有特征的标题全漏了。
    改成三档打分取最高——标题常常同时踩「反差」和「数字」两类特征，
    比的是哪一类特征更集中。平手时按 反差 > 具体 > 利益 取，
    因为反差词是作者主动设计的钩子，数字很多时候只是顺带出现。
    """
    text = title or ""

    def score(patterns: tuple[str, ...]) -> int:
        return sum(1 for p in patterns if re.search(p, text))

    scores = {
        FORMULA_CONTRAST: score(_CONTRAST_PATTERNS),
        FORMULA_SPECIFIC: score(_SPECIFIC_PATTERNS),
        FORMULA_BENEFIT: score(_BENEFIT_PATTERNS),
    }
    best = max(scores.values())
    if best == 0:
        return FORMULA_OTHER
    for name in (FORMULA_CONTRAST, FORMULA_SPECIFIC, FORMULA_BENEFIT):
        if scores[name] == best:
            return name
    return FORMULA_OTHER


def word_grade(body: str) -> str:
    """字数档：短(<900) / 中(900-1500) / 长(>1500)。

    正文没带回来（队列被清理后补录、或从截图手录）时返回「未采集」，
    不能默认当「短」——那会让「短」这一档凭空多出几篇 0 字的稿。
    """
    raw = re.sub(r"<[^>]+>", "", body or "").strip()
    if not raw:
        return "未采集"
    try:
        from pipeline import word_count
        n = word_count(body or "")
    except Exception:
        n = len(raw)
    if n < 900:
        return "短"
    if n <= 1500:
        return "中"
    return "长"


def genre_of(article: dict[str, Any]) -> str:
    """品类标签：优先用调用方显式给的 _genre，其次按队列 column 归类。

    column 是后来才加进队列的，2026-11 之前的稿子基本是空的，全靠 column
    会有一大半落在「未标注」里、品类对照没法做。所以留两道兜底：
    标题带「副业/变现/起号」的归副业选题，其余按 AI 资讯算——
    这个号已发的绝大多数就是 AI 资讯，比堆一坨「未标注」有用。
    """
    explicit = str(article.get("_genre") or "").strip()
    if explicit:
        return explicit
    column = str(article.get("column") or "").strip()
    if column in COLUMN_LABELS:
        return COLUMN_LABELS[column]
    title = str(article.get("title") or "")
    if any(w in title for w in ("副业", "变现", "起号")):
        return "副业选题"
    if "github" in str(article.get("source") or "").lower():
        return "GitHub专栏"
    return "AI资讯"


def source_bucket(source: str) -> str:
    """把具体来源归并成大类，避免样本太散看不出规律。"""
    s = source or ""
    if "公众号" in s or "微信" in s:
        return "公众号系"
    if "知乎" in s:
        return "知乎"
    if "头条" in s:
        return "头条"
    if "微博" in s:
        return "微博"
    if "GitHub" in s.lower() or "掘金" in s:
        return "技术社区"
    if "热榜" in s or "DailyHot" in s:
        return "综合热榜"
    return s or "其他"


def upsert(
    article: dict[str, Any],
    metrics: dict[str, Any],
    formula: str = "",
    channel: str = CHANNEL_ARTICLE,
) -> dict[str, Any]:
    """录入/更新一篇稿在某个渠道的复盘数据，返回写入的记录。

    article：来自队列的稿件，用于带出标题/来源/字数档；
    metrics：views/likes/shares/followers/date 等用户填的数字；
    formula：用户手动指定的标题公式，留空则自动打标；
    channel：文章 / 贴图，同一篇两个渠道各存一条。
    """
    article_id = article.get("id", "")
    if not article_id:
        raise ValueError("稿件缺少 id")

    channel = channel if channel in CHANNELS else CHANNEL_ARTICLE
    title = article.get("title", "")
    records = load_records()
    old = next(
        (
            r
            for r in records
            if r.get("article_id") == article_id
            # 老记录没有 channel 字段，都是当年按「文章」录的，按文章匹配，
            # 否则补录贴图时会找不到旧值、把手工指定的公式冲掉
            and (r.get("channel") or CHANNEL_ARTICLE) == channel
        ),
        None,
    )
    chosen = (formula or "").strip()

    # 公式标签分两类：用户手动定的、程序自动打的。
    # 手动定的要一直保留（只改数字时不能把他改过的标签冲掉）；
    # 自动打的要允许跟着打标规则一起升级——否则规则改好了，老记录还是
    # 当年的错标签。2026-11-24 踩过：8 条老记录全是「其他」，改完规则重录
    # 一遍数字，标签一个都没变，因为旧值优先。
    if chosen in FORMULAS:
        formula_manual = True
    elif (old or {}).get("formula_manual"):
        chosen = old.get("formula", "")
        formula_manual = True
    else:
        chosen = guess_formula(title)
        formula_manual = False

    def num(key: str) -> int:
        try:
            return max(0, int(metrics.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    views = num("views")
    likes = num("likes")
    shares = num("shares")
    followers = num("followers")
    interactions = likes + shares + followers
    # 传播效率：每千次阅读带来的互动数。阅读为 0 时不算，避免除零虚高。
    efficiency = round(interactions * 1000 / views, 1) if views else 0.0

    record: dict[str, Any] = {
        "article_id": article_id,
        "channel": channel,
        "title": title,
        "source": article.get("source", ""),
        "source_bucket": source_bucket(article.get("source", "")),
        "genre": genre_of(article),
        "formula": chosen,
        "formula_manual": formula_manual,
        "word_grade": word_grade(article.get("body", "")),
        "views": views,
        "likes": likes,
        "shares": shares,
        "followers": followers,
        "interactions": interactions,
        "efficiency": efficiency,
        "date": (metrics.get("date") or "")[:10],
        "note": (metrics.get("note") or "")[:200],
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }

    for i, existing in enumerate(records):
        if existing.get("article_id") != article_id:
            continue
        if (existing.get("channel") or CHANNEL_ARTICLE) != channel:
            continue
        records[i] = record
        break
    else:
        records.insert(0, record)
    save_records(records)
    return record


def remove(article_id: str) -> bool:
    """删除一篇稿的全部复盘记录（文章 + 贴图一起删），返回是否删掉了。"""
    records = load_records()
    kept = [r for r in records if r.get("article_id") != article_id]
    if len(kept) == len(records):
        return False
    save_records(kept)
    return True


def merge_channels(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一条内容的「文章」「贴图」合成一条，曝光与互动相加。

    为什么合：判断「哪类标题能带量」看的是这条内容总共被看到多少，
    两个渠道是同一篇内容的分发，分开比等于把一篇稿算成两个样本。
    但渠道本身也有信息量（贴图会推给非粉丝），所以另外单出一组，
    用未合并的原始记录算，见 summarize 的 by_channel。
    """
    merged: dict[str, dict[str, Any]] = {}
    # 文章记录排前面：合并后的公式/字数档/品类标注取文章那条，
    # 贴图是同一篇的衍生稿，标注理论上一致，但以文章为准更稳
    ordered = sorted(
        records,
        key=lambda x: 0 if (x.get("channel") or CHANNEL_ARTICLE) == CHANNEL_ARTICLE else 1,
    )
    for r in ordered:
        key = r.get("article_id") or r.get("title") or ""
        item = merged.get(key)
        if item is None:
            item = dict(r)
            item["views"] = item["likes"] = item["shares"] = item["followers"] = 0
            item["by_channel"] = {}
            merged[key] = item
        ch = r.get("channel") or CHANNEL_ARTICLE
        for field in ("views", "likes", "shares", "followers"):
            item[field] += int(r.get(field, 0) or 0)
        item["by_channel"][ch] = int(r.get("views", 0) or 0)
    out = []
    for item in merged.values():
        interactions = item["likes"] + item["shares"] + item["followers"]
        item["interactions"] = interactions
        item["efficiency"] = (
            round(interactions * 1000 / item["views"], 1) if item["views"] else 0.0
        )
        out.append(item)
    out.sort(key=lambda x: (x.get("views", 0), x.get("date") or ""), reverse=True)
    return out


def summarize() -> dict[str, Any]:
    """按公式 / 来源 / 篇幅 / 渠道 / 品类聚合，给出平均表现和结论。"""
    records = load_records()
    if not records:
        return {
            "total": 0, "posts": 0, "excluded": 0, "total_views": 0, "top": [],
            "by_formula": [], "by_source": [], "by_length": [],
            "by_channel": [], "by_genre": [],
            "insights": [], "metric": "avg_views", "metric_label": "平均阅读",
        }

    merged = merge_channels(records)
    # 历史随笔（2026-04 那批程序员随笔）不参与公式/来源/篇幅的均值：
    # 那批 150、190 阅读，混进去会让「反差」组凭空变好——其实是受众基数和
    # 内容类型都不同。它自己单独出现在 by_genre 里做对照。
    base = [m for m in merged if m.get("genre") != GENRE_HISTORY]
    excluded = len(merged) - len(base)

    total_views = sum(int(m.get("views", 0) or 0) for m in merged)
    total_inter = sum(int(m.get("interactions", 0) or 0) for m in merged)
    eff_per_mille = total_inter * 1000 / total_views if total_views else 0.0

    # 互动样本太少时按互动效率排序等于随机排：一篇「30 阅读 5 转发」的效率
    # 是 167/千次，会和「150 阅读 0 互动」的稿并列比较，谁高谁低全看偶然。
    # 2026-11-24 实录：16 篇合计才 12 次互动，这种量级只够看阅读。
    use_eff = (
        total_inter >= MIN_INTERACTIONS
        and eff_per_mille >= MIN_EFFICIENCY_PER_MILLE
    )
    metric = "avg_efficiency" if use_eff else "avg_views"
    metric_label = "平均互动效率" if use_eff else "平均阅读"

    def group_by(key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            buckets[r.get(key) or "未分类"].append(r)
        out = []
        for name, group in buckets.items():
            n = len(group)
            avg_views = round(sum(r.get("views", 0) for r in group) / n)
            avg_eff = round(sum(r.get("efficiency", 0) for r in group) / n, 1)
            out.append({
                "name": name, "count": n,
                "avg_views": avg_views, "avg_efficiency": avg_eff,
            })
        # 排序必须跟结论用的指标一致，否则页面从上到下看和文字结论对不上
        out.sort(key=lambda x: (x[metric], x["count"]), reverse=True)
        return out

    by_formula = group_by("formula", base)
    by_source = group_by("source_bucket", base)
    # 没采到正文的稿子字数档是「未采集」，不能混进篇幅对比
    by_length = group_by(
        "word_grade", [m for m in base if m.get("word_grade") != "未采集"]
    )
    # 渠道用原始记录（不合并），并把老记录缺的 channel/genre 补成默认值。
    # 同样排除历史随笔：把那 3 篇 150/190 阅读的算进「文章」均值，
    # 会得出「文章远好于贴图」，其实只是两批稿的受众不同。
    raw = [
        dict(r, channel=(r.get("channel") or CHANNEL_ARTICLE),
             genre=(r.get("genre") or "未标注"))
        for r in records
    ]
    raw_base = [r for r in raw if r.get("genre") != GENRE_HISTORY]
    by_channel = group_by("channel", raw_base)
    by_genre = group_by("genre", merged)

    # 结论：每组至少 3 篇、至少两组可比，且第一名比最后一名高出 20% 才说。
    # 门槛从 2 提到 3：2 篇对 2 篇时差一篇就是 3 倍，写出来像规律其实是噪音。
    # 「其他」不参与标题公式对比——它是打标失败的垃圾筐，不是一种公式。
    MIN_GROUP = 3
    insights: list[str] = []

    def genre_mode(name: str) -> str:
        """某一组里占多数的品类。用来判断两组是不是能直接比。"""
        rows = [m for m in base if m.get("formula") == name]
        if not rows:
            return ""
        counter = Counter(m.get("genre") or "" for m in rows)
        return counter.most_common(1)[0][0]

    for groups, subject, action, skip in (
        (by_formula, "标题公式", "下次写标题可优先用前者", (FORMULA_OTHER,)),
        (by_source, "选题来源", "可考虑多看这个源", ()),
        (by_length, "篇幅", "可按这个档位来写", ()),
    ):
        valid = [
            g for g in groups
            if g["count"] >= MIN_GROUP and g["name"] not in skip
        ]
        if len(valid) < 2:
            continue
        best, worst = valid[0], valid[-1]
        if best["name"] == worst["name"]:
            continue
        bottom = worst[metric]
        if bottom <= 0 or best[metric] < bottom * 1.2:
            continue
        caveat = ""
        if subject == "标题公式":
            # 标题和品类会互相干扰：反差组那 4 篇里 3 篇是副业选题，
            # 阅读低可能主要是品类的问题，不能全算到标题头上。
            g_best, g_worst = genre_mode(best["name"]), genre_mode(worst["name"])
            if g_best and g_worst and g_best != g_worst:
                caveat = (
                    f"（注意：这两组的主体品类不同——前者多是{g_best}、后者多是"
                    f"{g_worst}，标题的影响和品类混在一起，先当倾向看）"
                )
        insights.append(
            f"{subject}里「{best['name']}」（{best['count']} 篇）的{metric_label} "
            f"{best[metric]}，高于「{worst['name']}」（{worst['count']} 篇）的 "
            f"{worst[metric]}——{action}。{caveat}"
        )

    # 品类对照：当前品类之间谁带得动量。副业稿是引流来的选题，和 AI 资讯
    # 的受众不是一批人，混在一起看不出问题，分开比才看得出该往哪边倾斜。
    genre_live = [g for g in by_genre if g["name"] != GENRE_HISTORY and g["count"] >= 3]
    if len(genre_live) >= 2:
        top, low = genre_live[0], genre_live[-1]
        if top["name"] != low["name"] and low[metric] > 0 and top[metric] >= low[metric] * 1.5:
            insights.append(
                f"品类里「{top['name']}」（{top['count']} 篇）的平均阅读 {top[metric]}，"
                f"是「{low['name']}」（{low['count']} 篇）{low[metric]} 的 "
                f"{round(top[metric] / low[metric], 1)} 倍——"
                "两类受众不一样，往哪边倾斜由你定，但比例值得调。"
            )

    # 历史随笔：只做提示，不给「优先用」的动作——那批是半年前发的，
    # 粉丝基数、内容类型都不同，照着搬会把号写偏。
    history = [m for m in merged if m.get("genre") == GENRE_HISTORY]
    if len(history) >= 3 and base:
        h_avg = round(sum(m.get("views", 0) for m in history) / len(history))
        b_avg = round(sum(m.get("views", 0) for m in base) / len(base))
        if b_avg > 0 and h_avg >= b_avg * 1.5:
            insights.append(
                f"品类对照：「{GENRE_HISTORY}」{len(history)} 篇平均阅读 {h_avg}，"
                f"是当前 AI 稿（{len(base)} 篇）平均 {b_avg} 的 "
                f"{round(h_avg / b_avg, 1)} 倍——但这批是 2026-04 发的、粉丝基数不同，"
                "只当内容类型的参考，别直接照搬。"
            )

    # 渠道对照用「同一篇两个渠道都录了」的成对数据：拿全部文章的均值去比
    # 全部贴图的均值，比的是两批不同的稿，说明不了贴图有没有带来额外曝光。
    paired = [
        m for m in base
        if len(m.get("by_channel") or {}) >= 2
    ]
    if len(paired) >= 3:
        art_v = sum((m["by_channel"].get(CHANNEL_ARTICLE) or 0) for m in paired)
        pho_v = sum((m["by_channel"].get(CHANNEL_PHOTO) or 0) for m in paired)
        if art_v > 0:
            insights.append(
                f"渠道对照：{len(paired)} 篇同时发了文章和贴图，文章合计 {art_v} 阅读、"
                f"贴图合计 {pho_v}，贴图相当于多拿了 {round(pho_v / art_v * 100)}% 的曝光"
                "——同一篇两个渠道都发是白捡的量。"
            )

    if not insights:
        insights.append(
            "各组差距还看不出规律（要么样本不足 2 篇，要么差得不多），"
            "继续录入几篇再看；录数据时把点赞、在看一起填上，能按互动效率比。"
        )

    # 单篇明细：让用户能一眼核对录进去的数字对不对，也方便看「同一篇在两个
    # 渠道各拿了多少」——标题截断到 28 字，长标题会把这一行撑爆。
    top = [
        {
            "title": (m.get("title") or "")[:28],
            "views": m.get("views", 0),
            "date": m.get("date") or "",
            "channel_note": " / ".join(
                f"{k} {v}" for k, v in (m.get("by_channel") or {}).items()
            ),
        }
        for m in merged[:10]
    ]

    return {
        "total": len(merged),
        "posts": len(records),
        "excluded": excluded,
        "total_views": total_views,
        "top": top,
        "by_formula": by_formula,
        "by_source": by_source,
        "by_length": by_length,
        "by_channel": by_channel,
        "by_genre": by_genre,
        "insights": insights,
        "metric": metric,
        "metric_label": metric_label,
    }
