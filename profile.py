"""账号画像 / 长期记忆（Easel「profiles/」层的轻量落地）。

为什么需要：原来账号定位、受众、风格、红线全写死在 config.py 的两个常量里，
改一次要动代码；复盘录进来的经验也没有回流到生成，每天从零开始。
这里把画像抽成一份独立配置（data/profile.json），页面可编辑，生成时注入提示词，
并能把复盘结论一键写回画像——形成「生成 → 发布 → 复盘 → 调画像 → 再生成」闭环。

设计取舍：
  - 存储 data/profile.json，和 queue.json / analytics.json 同风格，无数据库依赖；
    被 .gitignore 的 data/*.json 覆盖，不随 git 同步（换机需单独拷）。
  - 七维画像：定位 / 受众 / 风格 / 红线 / 标题偏好 / 选题源偏好 / 不写的主题。
    前四维人工编辑，后两维由复盘写回（也可手改）；"不写的主题"是唯一会
    真正拦截选题的字段——定位/风格只是提示词，管不住抓取，必须单独硬过滤。
  - 缺字段一律用 config 里的旧常量兜底 —— 老项目首次运行行为与现在完全一致，
    不会因为多了这个文件就改变已验证过的生成质量。
  - 历史偏好只影响「标题取向 / 选题取舍」，明确要求模型不要写进正文，
    更不能当成事实数字引用（否则会把"效率 44"编成正文里的事实）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import config

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "profile.json"

# 六维画像的键与中文说明，前端也按这个顺序展示
DIMENSIONS: list[dict[str, str]] = [
    {"key": "niche", "label": "账号定位", "hint": "这个号写什么领域、什么主题"},
    {"key": "audience", "label": "目标受众", "hint": "写给谁看，他们的水平和关心什么"},
    {"key": "style", "label": "写作风格", "hint": "语气、结构、篇幅的偏好"},
    {"key": "redlines", "label": "内容红线", "hint": "绝对不写的东西（诱导词、编造、敏感话题等）"},
    {"key": "title_preference", "label": "标题偏好", "hint": "哪类标题效果好——可由复盘一键写回"},
    {"key": "source_preference", "label": "选题源偏好", "hint": "哪个来源的选题效果好——可由复盘写回"},
    {"key": "excluded_topics", "label": "不写的主题", "hint": "命中即丢弃的选题词，用逗号分隔；留空表示不过滤（如：汽车、房地产、明星八卦）"},
]
EDITABLE_KEYS = [d["key"] for d in DIMENSIONS]


def excluded_words() -> list[str]:
    """把「不写的主题」切成词表；兼容逗号/顿号/分号/换行分隔。"""
    raw = load().get("excluded_topics", "")
    parts = re.split(r"[,，、;；\n]+", raw) if raw else []
    return [p.strip() for p in parts if p.strip()]


def defaults() -> dict[str, Any]:
    """用 config 里的旧常量当默认值，保证升级前后行为一致。"""
    return {
        "niche": config.WRITING_NICHE,
        "audience": (
            "关注科技的普通读者和从业者；对 AI 工具、模型发布、数码产品感兴趣，"
            "希望看懂事实、拿到能用的判断或操作，而不是看参数堆砌。"
        ),
        "style": config.WRITING_STYLE,
        "redlines": (
            "不编造数字/人名/机构/时间；不写'揭秘/震惊/炸裂/速看'这类诱导词；"
            "不写空泛升华和营销腔；不给未经证实的归因；不写信息来源标注。"
        ),
        "title_preference": "",
        "source_preference": "",
        # 默认挡掉汽车整车类——账号定位是 AI/数码，这类稿不该再混进来
        "excluded_topics": ", ".join(
            getattr(config, "EXCLUDE_TOPIC_KEYWORDS", [])
        ),
        "updated_at": "",
        "synced_at": "",
    }


def load() -> dict[str, Any]:
    """读画像；文件不存在或损坏时返回默认值。缺字段自动补齐（兼容旧版本）。"""
    base = defaults()
    if not DATA_PATH.exists():
        return base
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return base
    if not isinstance(data, dict):
        return base
    for key in EDITABLE_KEYS:
        value = data.get(key)
        # "不写的主题"允许留空（表示不过滤），其余字段空值仍回退默认
        if key == "excluded_topics" and isinstance(value, str):
            base[key] = value.strip()
            continue
        if isinstance(value, str) and value.strip():
            base[key] = value
    base["updated_at"] = data.get("updated_at", "")
    base["synced_at"] = data.get("synced_at", "")
    return base


def save(values: dict[str, Any]) -> dict[str, Any]:
    """保存画像。只认六维里的字符串字段，其他一律忽略，避免脏数据写进去。"""
    import datetime

    current = load()
    for key in EDITABLE_KEYS:
        value = values.get(key)
        if isinstance(value, str):
            current[key] = value[:1200]
    current["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return current


def sync_from_analytics() -> dict[str, Any]:
    """把复盘结论写回画像的「标题偏好 / 选题源偏好」，形成闭环。

    只写有足够样本（>=2 篇）支撑的结论；样本不够时保留原值不动，
    避免把偶然的一次波动固化成"经验"。返回更新后的画像。
    """
    import datetime

    try:
        import analytics

        summary = analytics.summarize()
    except Exception:
        summary = {"insights": []}

    current = load()
    title_pref = ""
    source_pref = ""

    for insight in summary.get("insights", []):
        if "标题" in insight and "优先用前者" in insight:
            title_pref = insight
        elif "来自" in insight and "多看这个源" in insight:
            source_pref = insight

    if title_pref:
        current["title_preference"] = title_pref
    if source_pref:
        current["source_preference"] = source_pref

    if title_pref or source_pref:
        current["synced_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return current


def prompt_block(mode: str = "article") -> str:
    """拼成提示词片段，注入生成 / 改写 / 换标题三处。

    mode="article"：完整六维（写正文用）；
    mode="title"：只要定位 + 标题经验 + 红线，不给大段风格要求，
                  免得把正文写作规范塞进"只拟标题"的任务里干扰输出。

    历史偏好明确标注"只影响标题取向和选题取舍、不要当事实数字引用"，
    这是关键防御——复盘里的"效率 44"一旦被模型当成素材，正文就会编造。
    """
    p = load()
    if mode == "title":
        lines = [
            "【账号定位（拟标题时参考）】",
            f"账号定位：{p['niche']}",
            f"目标受众：{p['audience']}",
            f"内容红线：{p['redlines']}",
        ]
        if p.get("title_preference"):
            lines.append(f"标题经验（只影响用哪种公式，不许把里面的数字写进标题）：{p['title_preference']}")
        return "\n".join(lines)

    lines = [
        "【账号画像（写稿时遵守）】",
        f"账号定位：{p['niche']}",
        f"目标受众：{p['audience']}",
        f"写作风格：{p['style']}",
        f"内容红线：{p['redlines']}",
    ]
    if p.get("excluded_topics"):
        lines.append(
            f"不写的主题：{p['excluded_topics']}"
            "（这类素材直接跳过，不要扩写、也不要在正文里当背景提）"
        )
    exp = []
    if p.get("title_preference"):
        exp.append(f"标题经验：{p['title_preference']}")
    if p.get("source_preference"):
        exp.append(f"选题经验：{p['source_preference']}")
    if exp:
        lines.append("历史经验：" + "；".join(exp))
        lines.append(
            "（历史经验只用来决定标题取向和选题取舍，"
            "绝对不要写进正文，也不要把里面的数字当成素材事实引用。）"
        )
    return "\n".join(lines)
