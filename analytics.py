"""发布后数据回收与复盘（Easel「归因」层的轻量落地）。

为什么需要：项目原来发完就结束，不知道哪类标题、哪个选题源的文章表现好，
每天从零开始。这里让用户把公众号后台的几个数字录进来，自动按「标题公式 /
选题源 / 字数档」聚合，积累出可指导生成的经验。

设计取舍：
  - 不接公众号 API（阅读数据没有开放接口），靠手动录入，每周 5 分钟；
  - 存储用 data/analytics.json，和 data/queue.json 同风格，无数据库依赖；
  - 一篇稿一条记录，按 article_id 去重——同一篇隔几天再看数据就更新那条；
  - 标题公式靠特征自动打标（和 config.TITLE_VARIANTS_RULE 的三类对齐），
    也允许录入时手动指定，避免自动判断不准。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
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

# 反差 / 利益的特征词，用于自动打标（命中优先级：反差 > 利益 > 数字）
_CONTRAST_WORDS = (
    "别", "误区", "错了", "其实", "并不是", "未必", "反而", "真相",
    "劝你", "不要", "并非", "恰恰", "没想到", "误区", "坑", "翻车",
)
_BENEFIT_WORDS = (
    "怎么", "如何", "教你", "学会", "省钱", "避坑", "清单", "一步",
    "分钟", "个方法", "这几", "照做", "能用", "可用", "指南",
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
    """按标题特征猜公式标签；猜不准归「其他」，用户可以手动改。"""
    text = title or ""
    if any(w in text for w in _CONTRAST_WORDS):
        return FORMULA_CONTRAST
    if re.search(r"\d", text) and any(w in text for w in _BENEFIT_WORDS):
        return FORMULA_BENEFIT
    if re.search(r"\d+(?:年|月|日|秒|分钟|元|万|块|%|％|个|条|步|版)", text):
        return FORMULA_SPECIFIC
    if any(w in text for w in _BENEFIT_WORDS):
        return FORMULA_BENEFIT
    if re.search(r"\d", text):
        return FORMULA_SPECIFIC
    return FORMULA_OTHER


def word_grade(body: str) -> str:
    """字数档：短(<900) / 中(900-1500) / 长(>1500)。"""
    try:
        from pipeline import word_count
        n = word_count(body or "")
    except Exception:
        n = len(re.sub(r"<[^>]+>", "", body or ""))
    if n < 900:
        return "短"
    if n <= 1500:
        return "中"
    return "长"


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
) -> dict[str, Any]:
    """录入/更新一篇稿的复盘数据，返回写入的记录。

    article：来自队列的稿件，用于带出标题/来源/字数档；
    metrics：views/likes/shares/followers/date 等用户填的数字；
    formula：用户手动指定的标题公式，留空则自动打标。
    """
    article_id = article.get("id", "")
    if not article_id:
        raise ValueError("稿件缺少 id")

    title = article.get("title", "")
    records = load_records()
    old = next((r for r in records if r.get("article_id") == article_id), None)
    chosen = (formula or "").strip()

    # 更新已有记录时，若这次没指定公式，沿用旧值——避免把用户上次手动
    # 选的公式（或上次自动打标）在只改数字时冲掉。全新记录才自动判断。
    if chosen not in FORMULAS:
        chosen = (old or {}).get("formula", "")
    if chosen not in FORMULAS:
        chosen = guess_formula(title)

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
        "title": title,
        "source": article.get("source", ""),
        "source_bucket": source_bucket(article.get("source", "")),
        "formula": chosen,
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
        if existing.get("article_id") == article_id:
            records[i] = record
            break
    else:
        records.insert(0, record)
    save_records(records)
    return record


def remove(article_id: str) -> bool:
    """删除一条复盘记录，返回是否删掉了。"""
    records = load_records()
    kept = [r for r in records if r.get("article_id") != article_id]
    if len(kept) == len(records):
        return False
    save_records(kept)
    return True


def summarize() -> dict[str, Any]:
    """按公式 / 来源 / 字数档聚合，给出平均表现和结论。"""
    records = load_records()
    if not records:
        return {"total": 0, "by_formula": [], "by_source": [], "by_length": [], "insights": []}

    def group_by(key: str) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            buckets[r.get(key) or "未分类"].append(r)
        out = []
        for name, rows in buckets.items():
            n = len(rows)
            avg_views = round(sum(r.get("views", 0) for r in rows) / n)
            avg_eff = round(sum(r.get("efficiency", 0) for r in rows) / n, 1)
            out.append({
                "name": name, "count": n,
                "avg_views": avg_views, "avg_efficiency": avg_eff,
            })
        out.sort(key=lambda x: x["avg_efficiency"], reverse=True)
        return out

    by_formula = group_by("formula")
    by_source = group_by("source_bucket")
    by_length = group_by("word_grade")

    # 结论：样本 >= 2 的组里，互动效率最高/最低的给提示
    insights: list[str] = []
    valid_formula = [g for g in by_formula if g["count"] >= 2]
    if len(valid_formula) >= 2:
        best, worst = valid_formula[0], valid_formula[-1]
        if best["name"] != worst["name"]:
            insights.append(
                f"「{best['name']}」标题平均互动效率 {best['avg_efficiency']}，"
                f"明显高于「{worst['name']}」的 {worst['avg_efficiency']}——"
                f"下次写标题可优先用前者。"
            )
    valid_source = [g for g in by_source if g["count"] >= 2]
    if len(valid_source) >= 2:
        best, worst = valid_source[0], valid_source[-1]
        if best["name"] != worst["name"]:
            insights.append(
                f"来自「{best['name']}」的选题表现更好（效率 {best['avg_efficiency']} vs {worst['avg_efficiency']}），"
                f"可考虑多看这个源。"
            )
    if records and not insights:
        insights.append("样本还太少（每个分组要 2 篇以上才给结论），继续录入几篇就能看出规律。")

    return {
        "total": len(records),
        "by_formula": by_formula,
        "by_source": by_source,
        "by_length": by_length,
        "insights": insights,
    }
