"""AI副业「工具测评」连载系列：一次规划 N 期，逐期更新，留钩子追更。

背景（2026-11-28 用户提出）：
    副业栏每天搜索的散稿互不连贯，读者看完一篇没理由关注、没理由追更。
    改为系列连载——每期揭一个 AI 副业工具/路子，开头前情回顾、结尾下期预告，
    让读者追着看。系列形态=工具测评，大纲由 AI 一次规划，副业散稿停发。

真实口径（重要）：
    系统没法真去注册、使用每个工具，所以写「资料拆解式测评」——联网扒公开
    定价/规则/用户反馈/案例，绝不冒充亲历（"我测了""亲测有效"一律禁止）。
    素材来自 topic_research（360 搜索为主，过相关性过滤），喂给现有
    pipeline.make_article 的 prompt_override 通道，复用全部校验/扩写链路。

数据文件（data/series.json，不随 git 同步）：
    {
      "version": 1,
      "current": {                      # 当前连载中的系列
        "id": "s_xxx",
        "title": "普通人也能用的10个AI副业工具",
        "theme": "普通人能上手的AI副业工具",
        "created_at": "...",
        "episodes": [
          {"no":1, "topic":"工具名", "angle":"本期角度", "hook":"下期钩子",
           "status":"done|pending", "article_id":"...", "title":"成稿标题",
           "generated_at":"..."}
        ]
      },
      "history": [ ...已归档系列... ]
    }
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import config
import topic_research

ROOT = Path(__file__).resolve().parent
SERIES_PATH = ROOT / config.SERIES_FILE

_WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def publish_weekdays() -> list[int]:
    """读取发布日配置，返回 Python weekday 列表（默认周二、周四）。"""
    raw = str(getattr(config, "SERIES_PUBLISH_WEEKDAYS", "1,3") or "1,3")
    days: list[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return sorted(days or [1, 3])


def schedule_text() -> str:
    """给审核页展示的发布节奏，如「每周二、周四更新」。"""
    days = publish_weekdays()
    return "每" + "、".join(_WEEK_CN[d] for d in days) + "更新"


def next_publish_info(now: datetime | None = None) -> dict[str, str]:
    """下一次发布日：返回中文星期、日期和展示文案。今天就是发布日时返回今天。"""
    now = now or datetime.now()
    today = now.date()
    days = publish_weekdays()
    for delta in range(8):
        candidate = today + timedelta(days=delta)
        if candidate.weekday() in days:
            week = _WEEK_CN[candidate.weekday()]
            prefix = "今天" if delta == 0 else ("明天" if delta == 1 else week)
            return {
                "weekday": week,
                "date": candidate.isoformat(),
                "label": f"{prefix}{week}（{candidate.month}月{candidate.day}日）",
                # 文章末尾预告用短文案，避免「下次 明天周四（12月3日）更新」这种累赘
                "short_label": f"{candidate.month}月{candidate.day}日（{week}）",
            }
    return {"weekday": "", "date": "", "label": "下次发布", "short_label": "下次发布"}


def is_publish_day(now: datetime | None = None) -> bool:
    """今天是不是自动发布日。"""
    return (now or datetime.now()).weekday() in publish_weekdays()


def _empty_plan() -> dict[str, Any]:
    return {"version": 1, "current": None, "history": []}


def load_plan() -> dict[str, Any]:
    """读系列计划；文件不存在/损坏时返回空计划，绝不抛异常打断页面。"""
    if not SERIES_PATH.exists():
        return _empty_plan()
    try:
        data = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "current" not in data:
            return _empty_plan()
        data.setdefault("history", [])
        return data
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[series] 计划文件损坏，按空计划处理: {exc}")
        return _empty_plan()


def save_plan(plan: dict[str, Any]) -> None:
    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_outline(raw: str, total: int) -> list[dict[str, str]]:
    """解析 AI 大纲：每行「工具名 | 角度 | 钩子」。容错处理少列/多列/编号。"""
    episodes: list[dict[str, str]] = []
    for line in (raw or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉行首的 "1." "1、" "第1期：" 等编号
        line = line.lstrip("•-* ").strip()
        import re
        line = re.sub(r"^(第?\s*\d+\s*[期.、]?\s*[:：]?)", "", line).strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        topic = parts[0]
        angle = parts[1] if len(parts) > 1 else "怎么上手、要花多少钱"
        hook = parts[2] if len(parts) > 2 else "下期接着扒另一个路子"
        if not topic:
            continue
        episodes.append({"topic": topic, "angle": angle, "hook": hook})
        if len(episodes) >= total:
            break
    return episodes


def plan_series(theme: str, total: int | None = None) -> dict[str, Any]:
    """调 AI 一次规划整个系列的大纲，写入计划文件。

    返回 {"ok": bool, "series": dict|None, "error": str}
    """
    from pipeline import call_ai

    theme = (theme or "").strip() or "普通人能上手的AI副业工具"
    total = max(2, min(30, int(total or config.SERIES_TOTAL)))
    prompt = config.SERIES_PLAN_PROMPT.format(total=total, theme=theme)
    # 限流重试：主模型撞限流窗口时退避再试（plan 一天只做一次，值得多等几秒）
    raw = None
    for attempt in range(3):
        raw = call_ai(prompt)
        if raw:
            break
        if attempt < 2:
            import time
            time.sleep(4 * (attempt + 1))
    if not raw:
        return {
            "ok": False, "series": None,
            "error": "AI 规划失败（模型限流或兜底未配置），请稍后重试",
        }

    episodes = _parse_outline(raw, total)
    if len(episodes) < 2:
        return {
            "ok": False, "series": None,
            "error": "AI 返回的大纲格式不对，没解析出足够的期数，请重试",
        }

    series = {
        "id": "s_" + uuid.uuid4().hex[:8],
        "title": f"我扒了{len(episodes)}个AI副业工具",
        "theme": theme,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "episodes": [
            {
                "no": idx,
                "topic": ep["topic"],
                "angle": ep["angle"],
                "hook": ep["hook"],
                "status": "pending",
                "article_id": "",
                "title": "",
                "generated_at": "",
            }
            for idx, ep in enumerate(episodes, start=1)
        ],
    }
    plan = load_plan()
    # 上一个系列若还有未完成的，先归档，不直接丢弃
    if plan.get("current") and plan["current"].get("episodes"):
        plan["history"].append(plan["current"])
        plan["history"] = plan["history"][-5:]  # 最多留 5 个归档
    plan["current"] = series
    save_plan(plan)
    return {"ok": True, "series": series, "error": ""}


def _episode_prompt(series: dict[str, Any], ep: dict[str, Any]) -> str:
    """组装单期成稿提示词：连载结构 + 副业规则 + 素材 + 输出格式。

    刻意复用副业栏的标题规则与数字规则，保证风格一致；
    只在前面叠加连载三段式（前情回顾 / 本期 / 下期预告）。
    """
    import profile as profile_mod

    episodes = series.get("episodes", [])
    no = int(ep.get("no", 1))
    total = len(episodes)
    prev = [e for e in episodes if int(e.get("no", 0)) < no and e.get("status") == "done"]
    prev_topics = "、".join(e.get("topic", "") for e in prev[-3:]) or "（无，这是开篇）"
    next_ep = next((e for e in episodes if int(e.get("no", 0)) == no + 1), None)
    next_topic = next_ep.get("topic", "") if next_ep else "（本系列最后一期）"
    next_hook = ep.get("hook") or (
        f"下期扒「{next_topic}」" if next_ep else "本系列完结，想看哪个方向留言告诉我们"
    )
    next_publish = next_publish_info()["label"]

    # 素材（来自 topic_research 的汇总，措辞原样保留，喂给模型当真素材）
    desc = str(ep.get("_material", "") or "")
    topic_item = {
        "title": ep.get("topic", ""),
        "desc": desc,
        "url": ep.get("_url", ""),
        "source": "联网检索（资料拆解）",
        "column": "ai_sidebiz",
    }
    # 复用现有 build_prompt 的副业规则主体（真实性硬要求/数字要求/画像/骨架/标题规则），
    # 但把开头的任务描述换成连载结构——这是最省事又最一致的写法。
    base = _compose_series_base(topic_item)
    structure = config.SERIES_EPISODE_STRUCTURE.format(
        series_title=series.get("title", ""),
        episode_no=no,
        total=total,
        episode_topic=ep.get("topic", ""),
        episode_angle=ep.get("angle", ""),
        prev_topics=prev_topics,
        next_topic=next_topic,
        next_hook=next_hook,
        next_publish=next_publish,
    )
    return structure + "\n" + base + "\n" + profile_mod.prompt_block()


def _compose_series_base(item: dict[str, Any]) -> str:
    """拼连载稿的写作主体：素材 + 副业标题/数字规则 + 骨架 + 真实性。"""
    topic = item["title"]
    desc = item.get("desc", "")
    material = (
        f"【本期素材（联网检索到的公开资料，措辞原样保留，不得改写事实）】\n{desc}\n"
        if desc else
        "【本期素材】没搜到可用素材——只写这类工具是什么、怎么自己判断和上手，"
        "绝不编造任何具体价格、收益、用户评价或事件经过。"
    )
    return (
        f"你是公众号「AI副业工具测评」连载栏目的写手。本期测评对象：{topic}\n\n"
        f"{material}\n"
        "真实性硬要求（违反即作废）：\n"
        "1. 只能用上面素材里的信息，不得编造价格、收益、人名、平台政策或用户评价；\n"
        "2. 严禁第一人称亲历（我测了/我亲测/我做了X个月/亲测有效）——"
        "写「据公开资料」「扒了下规则」「有用户反馈」，不冒充使用者；\n"
        "3. 不许收益承诺（月入过万/躺赚/稳赚），平台政策拿不准写「以平台当前规则为准」；\n"
        "4. 通用方法可讲，但写成建议或分析，不写成事实；\n"
        "5. 文末不要写「信息来源」「原文链接」这类标注。\n\n"
        "输出格式（严格遵守）：\n"
        f"1. 第一行只写标题，12-20 字，痛点+好处两段式，不要加标记：\n"
        f"{config.SIDE_BIZ_TITLE_RULE}\n"
        f"2. {config.SIDE_BIZ_DATA_RULE}\n"
        "3. 小标题用两个井号加空格开头：## 小标题文字（3 个左右，陈述句）\n"
        "4. 需要强调的句子用**加粗**，每节最多一句\n"
        "5. 不要写导语说明、不要解释思路，直接输出标题和正文\n\n"
        f"{config.ARTICLE_SKELETON}\n"
        f"{config.INFO_GAIN_RULE}\n"
        f"{config.VIRAL_HOOK_RULE}"
    )


def _teaser_hook(hook: str, topic: str) -> str:
    """去掉钩子里和「下期主题」重复的前缀，避免同一句里扒两遍同一个工具。"""
    import re

    text = re.sub(r"^下期(?:我们)?(?:就)?(?:扒|讲|聊|说|拆解)?[「\"']?", "", hook)
    text = text.strip("」\"' ")
    if topic and text:
        n = 0
        while n < min(len(topic), len(text)) and topic[n] == text[n]:
            n += 1
        if n >= 2:  # 有实词重合才算重复，单个「A」不算
            text = text[n:].lstrip("，,、：: ")
    return text.strip()


def _wrap_episode_body(
    body: str, series: dict[str, Any], ep: dict[str, Any]
) -> str:
    """成稿后由代码强制加「前情回顾」和「下期预告」，不依赖模型听不听话。

    实测（2026-11-28）：提示词里要求模型写这两段，但 ARTICLE_SKELETON 的
    "小标题开头"结构会把它们挤掉——成稿既没有前情回顾也没有下期预告。
    连载的灵魂就是这两段，所以改成代码拼接，100% 保留。
    """
    eps = series.get("episodes", [])
    no = int(ep.get("no", 1))
    total = len(eps)
    prev = [e for e in eps if int(e.get("no", 0)) < no and e.get("status") == "done"]
    if prev:
        prev_topics = "、".join(e.get("topic", "") for e in prev[-3:])
        intro = (
            f"<p><strong>这是「{series.get('title','')}」第 {no}/{total} 期。</strong>"
            f"前面几期我们扒了 {prev_topics}，这期继续。</p>"
        )
    else:
        intro = (
            f"<p><strong>这是「{series.get('title','')}」第 1/{total} 期，我们从头扒起。</strong>"
            "想做 AI 副业又不知从哪下手？这个系列每期拆一个真实工具，"
            "讲清它能干什么、门槛和坑，帮你少走弯路。</p>"
        )

    next_ep = next((e for e in eps if int(e.get("no", 0)) == no + 1), None)
    publish = next_publish_info()["short_label"]
    hook = str(ep.get("hook", "") or "").strip()
    if next_ep:
        # 必须点名下一期主题 + 悬念钩子 + 下次发布时间，读者才知道追什么、什么时候追
        topic = str(next_ep.get("topic", "") or "").strip()
        angle = str(next_ep.get("angle", "") or "").strip()
        lead = f"下期我们就扒「{topic}」" + (f"，重点看{angle}" if angle else "") + "。"
        hook = _teaser_hook(hook, topic)
        tail = f"{lead}{hook + ' ' if hook else ''}下次 {publish} 更新，关注我，连载不迷路。"
    else:
        tail = f"{hook or '本系列到这里就完结了，想看哪个方向留言告诉我们'} 关注我，后面继续。"
    outro = (
        f"<p><strong>下期预告：</strong>{tail}</p>"
    )

    import re

    body = (body or "").strip()
    # 模型可能在正文末尾自己写一段「下期预告」，剥掉后用上面的统一版本，
    # 否则读者会看到两段预告、两个发布时间。
    body = re.sub(
        r"(?is)<p[^>]*>\s*(?:<strong>)?\s*下期预告\s*(?:：|:)?\s*(?:</strong>)?.*?</p>\s*$",
        "", body,
    ).strip()
    return intro + body + outro


def generate_next(demo: bool = False, force: bool = False) -> dict[str, Any]:
    """生成当前系列的下一期，入审核队列并标记完成。

    force=False（每日自动任务）：只在 {每周二、周四} 产出，其他日直接跳过；
    force=True（审核页手动点「写下一期」）：随时可写，不受发布日限制。

    返回 {"ok": bool, "article": dict|None, "error": str, "episode_no": int,
          "skipped": bool}
    """
    if not force and not is_publish_day():
        return {
            "ok": False, "article": None,
            "error": f"今天不是发布日（{schedule_text()}），已跳过",
            "episode_no": 0, "skipped": True,
        }

    from pipeline import (
        call_ai, make_article, load_queue, save_queue, cleanup_queue,
        parse_response,
    )

    plan = load_plan()
    series = plan.get("current")
    if not series or not series.get("episodes"):
        return {"ok": False, "article": None, "error": "还没有系列，请先创建", "episode_no": 0}

    ep = next((e for e in series["episodes"] if e.get("status") == "pending"), None)
    if not ep:
        return {
            "ok": False, "article": None,
            "error": "系列已全部写完，可以归档后创建新系列",
            "episode_no": len(series["episodes"]),
        }

    no = int(ep.get("no", 1))

    # 1. 联网搜本期素材（工具名 + 角度），复用话题搜索的相关性过滤
    query = f"{ep.get('topic', '')} {ep.get('angle', '')}"
    researched = topic_research.research(query)
    if researched["ok"]:
        ep["_material"] = researched["desc"]
        ep["_url"] = researched["url"]
        material_count = len(researched["materials"])
    else:
        # 搜不到也继续写，但提示词里明确"没素材"，模型不会编造具体价格/收益
        ep["_material"] = ""
        ep["_url"] = ""
        material_count = 0
    print(f"[series] 第 {no} 期「{ep['topic']}」搜到 {material_count} 条素材", flush=True)

    # 2. 组装连载提示词 → 复用 make_article 覆盖通道
    prompt = _episode_prompt(series, ep)
    try:
        article = make_article(
            {
                "title": ep.get("topic", ""),
                "topic": ep.get("topic", ""),
                "desc": ep.get("_material", ""),
                "url": ep.get("_url", ""),
                "source": "AI副业工具测评·资料拆解",
                "column": "ai_sidebiz",
                "origin": "series",
            },
            demo,
            prompt_override=prompt,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "article": None,
            "error": f"成稿失败: {type(exc).__name__}: {exc}",
            "episode_no": no,
        }

    # 3. 代码强制加前情回顾 / 下期预告（模型那版不可靠，见函数注释）
    article["body"] = _wrap_episode_body(article.get("body", ""), series, ep)

    # 4. 打连载标记，让审核页能显示"第N期"
    article["series_id"] = series["id"]
    article["series_title"] = series.get("title", "")
    article["series_no"] = no
    article["series_total"] = len(series["episodes"])

    # 5. 入队
    queue = load_queue()
    queue.insert(0, article)
    queue, _ = cleanup_queue(queue)
    save_queue(queue)

    # 6. 标记本期完成（清理临时素材字段，别写进 JSON）
    ep.pop("_material", None)
    ep.pop("_url", None)
    ep["status"] = "done"
    ep["article_id"] = article["id"]
    ep["title"] = article.get("title", "")
    ep["generated_at"] = datetime.now().isoformat(timespec="seconds")
    save_plan(plan)

    print(f"[series] 第 {no} 期成稿「{article.get('title','')}」({article['id']})", flush=True)
    return {"ok": True, "article": article, "error": "", "episode_no": no}


def progress() -> dict[str, Any]:
    """给审核页展示的进度摘要。"""
    plan = load_plan()
    series = plan.get("current")
    if not series:
        return {"exists": False, "title": "", "done": 0, "total": 0,
                "next": None, "episodes": [], "id": "",
                "schedule_text": schedule_text(),
                "next_publish": next_publish_info()["label"]}
    eps = series.get("episodes", [])
    done = sum(1 for e in eps if e.get("status") == "done")
    nxt = next((e for e in eps if e.get("status") == "pending"), None)
    return {
        "exists": True,
        "id": series.get("id", ""),
        "title": series.get("title", ""),
        "theme": series.get("theme", ""),
        "done": done,
        "total": len(eps),
        "schedule_text": schedule_text(),
        "next_publish": next_publish_info()["label"],
        "next": {
            "no": nxt.get("no"),
            "topic": nxt.get("topic", ""),
            "angle": nxt.get("angle", ""),
            "hook": nxt.get("hook", ""),
            "publish": next_publish_info()["label"],
        } if nxt else None,
        "episodes": [
            {
                "no": e.get("no"),
                "topic": e.get("topic", ""),
                "angle": e.get("angle", ""),
                "hook": e.get("hook", ""),
                "status": e.get("status", "pending"),
                "title": e.get("title", ""),
                "article_id": e.get("article_id", ""),
            }
            for e in eps
        ],
    }


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "plan":
        theme = sys.argv[2] if len(sys.argv) > 2 else "普通人能上手的AI副业工具"
        r = plan_series(theme)
        print("规划结果:", "成功" if r["ok"] else r["error"])
        if r["ok"]:
            for e in r["series"]["episodes"]:
                print(f"  第{e['no']}期 {e['topic']}｜{e['angle']}")
    elif action == "next":
        r = generate_next(demo="--demo" in sys.argv)
        print("生成结果:", "成功" if r["ok"] else r["error"])
        if r["ok"]:
            print("  标题:", r["article"].get("title"))
    else:
        import json
        print(json.dumps(progress(), ensure_ascii=False, indent=2))
