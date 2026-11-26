"""公众号贴图文案：从稿件提炼「金句 + 配文 + 话题标签」，纯文本产出。

不生成图片——图片由用户自己配，这里只负责产出能直接复制粘贴的文字。
与发展历程：原方案用 Pillow 画金句卡片，用户 2026-11-26 明确改为只要内容。

流程：
    1) 文本模型从标题/摘要/正文提炼金句、配文、标签（约 1-3 秒）；
    2) 模型不可用时，从正文加粗句降级，绝不空手返回；
    3) 文案存 data/sticker_copies/{id}.json，刷新页面直接展示，不再重复调用模型；
    4) 点「换一条」才重新生成（force=True）。

命令行：
    .venv/bin/python sticker_copy.py --article <稿件id>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import config

ROOT = Path(__file__).resolve().parent
COPY_DIR = ROOT / "data" / "sticker_copies"

_TAG_RE = re.compile(r"<[^>]+>")
_MARK_RE = re.compile(r"[#*`>]+")


def _plain(text: Any, limit: int) -> str:
    """把 HTML 标签和 Markdown 记号清掉，压成一行再截断。"""
    value = _TAG_RE.sub(" ", str(text or ""))
    value = _MARK_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _clean_quote(text: str) -> str:
    """清洗金句：去前缀/引号，太长就截到最后一个逗号。"""
    # 注意：末尾的问号/问号要保留——新规则允许设问式钩子（"…谁最先慌？"），
    # strip 掉就把悬念磨平了。只清引号和句号类标点，不清 ?/？。
    quote = _plain(text, 200).strip("\"'“”‘’「」『』《》.!！")
    quote = re.sub(r"^(金句|配文|标题|一句话)\s*[:：]?\s*", "", quote)
    if len(quote) > 34:
        clipped = quote[:34]
        for separator in ("，", "；", "、", "。"):
            position = clipped.rfind(separator)
            if position >= 12:
                clipped = clipped[:position]
                break
        quote = clipped.strip("，。；、： ")
    return quote.strip()


def _compose_ai(article: dict[str, Any]) -> dict[str, str] | None:
    """让文本模型提炼金句/配文/标签；任一步不可用返回 None 走降级。"""
    try:
        from pipeline import call_ai, classify_column
    except Exception as exc:  # noqa: BLE001 - 降级路径必须兜得住
        print(f"[sticker_copy] 无法调用文本模型，改用加粗句：{exc}")
        return None

    # 配文第 4 步「关我什么事」按栏目分流：副业落到能不能上手/投多少/多久见钱，
    # 早报与 github 工具稿落到对普通人工作生活的影响。判据跟取题、审核页共用
    # classify_column，不另起一套，避免"页面归副业、文案写早报口吻"串台。
    is_sidebiz = classify_column(article) == "ai_sidebiz"
    column_hint = (
        config.STICKER_COLUMN_HINT_SIDEBIZ
        if is_sidebiz
        else config.STICKER_COLUMN_HINT_NEWS
    )
    prompt = config.STICKER_QUOTE_PROMPT.format(
        title=_plain(article.get("title"), 80),
        desc=_plain(article.get("desc"), 150) or "（无）",
        body=_plain(article.get("body"), 900) or "（无）",
        column_hint=column_hint,
    )
    raw = call_ai(prompt)
    if not raw:
        return None

    quote = caption = tags = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("金句"):
            quote = _clean_quote(line.split("：", 1)[-1] if "：" in line else line)
        elif line.startswith("配文"):
            caption = line.split("：", 1)[-1].strip()
        elif line.startswith("标签"):
            tags = line.split("：", 1)[-1].strip()
    if not quote or len(quote) < 10:
        return None
    # 配文新规则要求 150-260 字（类比+事件+判断标准+关我什么事+互动提问），
    # 旧的 120 字上限会把互动收尾那句切掉，放宽到 300 留余量。
    return {"quote": quote, "caption": caption[:300], "tags": tags[:80]}


def _compose_fallback(article: dict[str, Any]) -> dict[str, str]:
    """无模型时用正文第一个加粗句当金句；没有加粗句就用标题。"""
    body = str(article.get("body") or "")
    # 队列里的正文是 HTML（<strong>），不是 Markdown（**），两种都要认
    bold_sentences = re.findall(r"<strong[^>]*>(.*?)</strong>", body, re.S)
    if not bold_sentences:
        bold_sentences = re.findall(r"<b[^>]*>(.*?)</b>", body, re.S)
    if not bold_sentences:
        bold_sentences = re.findall(r"\*\*(.+?)\*\*", body)
    title = _plain(article.get("title"), 60)
    quote = _clean_quote(bold_sentences[0]) if bold_sentences else ""
    if len(quote) < 8:
        quote = title or "把复杂的事说清楚，本身就是一种能力。"
    caption = _plain(article.get("desc"), 100)
    tags = (
        "#AI副业 #副业避坑"
        if article.get("column") == "ai_sidebiz"
        else "#AI工具 #效率提升"
    )
    return {"quote": quote, "caption": caption, "tags": tags}


def format_copy(meta: dict[str, str]) -> str:
    """拼成可直接粘贴到公众号贴图配文区的纯文本。"""
    parts = [meta.get("quote", "").strip(), meta.get("caption", "").strip()]
    tags = meta.get("tags", "").strip()
    if tags:
        parts.append(tags)
    return "\n\n".join(p for p in parts if p)


def load_copy(article_id: str) -> dict[str, str] | None:
    """读已生成的文案；没有或损坏返回 None。"""
    path = COPY_DIR / f"{str(article_id).strip()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("quote") else None
    except Exception:  # noqa: BLE001 - 坏文件当没生成过
        return None


def generate_copy(article: dict[str, Any], force: bool = False) -> dict[str, str] | None:
    """给单篇稿件产出贴图文案，成功返回含 text 的字典。"""
    if not getattr(config, "USE_STICKER_COPY", True):
        return None
    article_id = str(article.get("id", "")).strip()
    if not article_id:
        return None

    if not force:
        existing = load_copy(article_id)
        if existing:
            return existing

    meta = _compose_ai(article) or _compose_fallback(article)
    meta["text"] = format_copy(meta)
    COPY_DIR.mkdir(parents=True, exist_ok=True)
    (COPY_DIR / f"{article_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[sticker_copy] {article_id} 金句：{meta['quote']}")
    return meta


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--article" in arguments:
        index = arguments.index("--article")
        if index + 1 >= len(arguments):
            print("用法：python sticker_copy.py --article <稿件id>")
            raise SystemExit(1)
        from pipeline import find_article, load_queue

        article_id = arguments[index + 1]
        queue = load_queue()
        position = find_article(queue, article_id)
        if position == -1:
            print(f"没找到稿件 {article_id}")
            raise SystemExit(1)
        result = generate_copy(queue[position], force=True)
        print(result["text"] if result else "生成失败")
    else:
        print("用法：python sticker_copy.py --article <稿件id>")
