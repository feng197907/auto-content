"""公众号头图：稿件审核通过后自动生成一张 2.35:1 的封面图。

设计取舍：
1. 用智谱 CogView-3-Flash（永久免费，实测 8-10 秒出图），走服务器直连，
   不依赖任何外部图库，也不抓原文配图（原文图有版权风险，且多数平台带防盗链）。
2. 画面内容先由文本模型按文章写一句具象描述（见 _visual_brief），
   拿不到才退回关键词规则——旧版纯规则会让所有科技稿共用同一张发光粒子图。
3. 尺寸直接请求 1808x768，正好 2.35:1，和公众号大图 900x383 同比例，
   不用 Pillow 二次裁剪（服务器没装 Pillow，也省掉中文字体依赖）。
4. 图上不压标题文字：中文小字在生成图里容易糊，且公众号列表本身会显示标题。
   平台会自动在右下角打「AI生成」标识，符合生成式合成内容标识要求。
5. 任何一步失败都返回 None，绝不影响「审核通过」主流程。

命令行：
    .venv/bin/python cover.py --backfill         # 给所有已通过但没头图的稿件补图
    .venv/bin/python cover.py --prompt <稿件id>  # 只打印 prompt，不出图（不花钱，调试用）
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

import config

ROOT = Path(__file__).resolve().parent
COVER_DIR = ROOT / "data" / "covers"
COVER_SIZE = "1808x768"  # 2.35:1，与公众号头图 900x383 同比例
API_URL = "https://open.bigmodel.cn/api/paas/v4/images/generations"
MODEL = "cogview-3-flash"

# ---- 画面脚本：先让文本模型读懂文章，再交给画图模型 ----
# 旧做法是「标题命中关键词 → 固定意象」，只有 5 条规则，而"AI"那条几乎命中一切
# 科技稿，结果所有文章都出同一张紫蓝光点图（实测：讲副业起号的稿子配了星云图）。
# 现在分两层：
#   1) 文本模型按标题+摘要+开头写一句**具象、可拍摄**的画面描述（约 1-3 秒、几厘钱）；
#   2) 文本模型不可用（限流/超时/没 key）时，退回关键词规则，宁可具体但重复，
#      也不要抽象的发光粒子——那是"与文章无关"的根源。
BRIEF_PROMPT = """你是公众号头图的美术指导。读下面这篇文章，写一句紧扣文章具体主题的、
能直接交给画图模型的画面描述。

最重要的一条：画面必须来自这篇文章讲的具体行业或事件。先自问"这篇在讲什么领域"，
再把这个领域的一个真实场景画出来，而不是套用万能图。绝不允许写成
"笔记本电脑＋咖啡/水杯＋窗外城市"这类通用办公桌图——除非文章主题本身就是职场办公。
主题到场景的参考映射（是短语，不是让你照抄完整句子）：
算力/数据中心→机房服务器机柜或超算中心；芯片→晶圆或处理器特写；
大会/论坛→会场演讲台或观众席；机器人→车间机械臂；汽车→车身局部或公路；
医疗→诊室器械；理财/收入→账本或计算器；副业/自媒体→手机拍摄支架。

其余硬性要求：
1. 只写看得见的东西：场景、主体、物体、光线、色调。不要写观点、不要比喻，
   不要出现"象征／体现／意味着／代表"这类词。
2. 一个主体 + 最多两个陪体。不许堆叠、拼贴、罗列：不要出现"散落""堆积""无数"
   "成堆""拼贴""碎片""废墟"这类词——画图模型会把它们画成垃圾堆。
3. 必须是具体、真实、可拍摄的场景（静物、室内环境、人物局部），不要概念图。
4. 光线明确、背景不要纯黑：写清是白天窗光、阴天柔光还是室内暖光。
5. 40-70 个汉字，一句话说完，不要分点、不要引号、不要解释。
6. 画面里不能有文字、字母、数字、屏幕上的具体内容、logo、二维码、人脸。

文章标题：{title}
文章摘要：{desc}
文章开头：{lead}
关键词参考场景（仅当你实在想不出更贴切画面时才用，通常应能写出更贴合这篇的描述）：{scene_hint}

只输出那一句画面描述，不要任何前缀。"""

# 画面脚本的硬闸门：命中这些词的脚本，画图模型基本都会画成抽象垃圾/垃圾堆
_BRIEF_BANNED = (
    "散落", "堆积", "成堆", "堆满", "拼贴", "碎片", "废墟", "无数", "大量",
    "黑色背景", "纯黑", "虚空", "粒子", "光斑", "星云", "隐喻", "象征",
    "渐变光谱", "发光", "能量流",
)

# 兜底规则：文本模型不可用时才用。每条只写**一个主体 + 环境**，
# 元素一多 cogview 就画糊，宁可朴素也不要堆砌。
VISUAL_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("副业", "自媒体", "变现", "起号", "涨粉", "流量", "账号", "粉丝",
         "内容创作", "运营", "私域", "选题", "爆款"),
        "木质桌面上一部支起的手机和小补光灯，暖色侧光",
    ),
    (
        ("芯片", "显卡", "处理器", "算力", "半导体", "制程", "光刻", "数据中心", "服务器"),
        "机房走廊里成排的服务器机柜，冷白色指示灯",
    ),
    (
        ("机器人", "工厂", "制造", "自动化", "产线"),
        "车间里的一只机械臂，冷白工业照明",
    ),
    (
        ("汽车", "自驾", "驾驶", "新能源", "充电", "出行"),
        "黄昏的公路向远处延伸，路灯初亮",
    ),
    (
        ("手机", "数码", "耳机", "电脑", "硬件", "评测", "开箱", "拍照"),
        "深色桌面上的一部手机和一副耳机，侧逆光",
    ),
    (
        ("学习", "教育", "考试", "课程", "学生", "考研", "读书", "知识"),
        "书桌上摊开的一本书和一支荧光笔，台灯暖光",
    ),
    (
        ("职场", "打工", "上班", "简历", "面试", "会议", "效率", "加班", "裁员", "管理"),
        "办公桌上一本摊开的纸质笔记本和一支笔，顶灯白光",
    ),
    (
        ("消费", "电商", "购物", "价格", "促销", "优惠", "涨价", "性价比"),
        "浅灰背景前的一只快递纸箱，柔和侧光",
    ),
    (
        ("健康", "睡眠", "运动", "减肥", "养生", "医院", "医疗"),
        "窗台上一双跑鞋和一盆绿植，清晨柔光",
    ),
    (
        ("安全", "漏洞", "攻击", "诈骗", "隐私", "泄露", "黑产"),
        "深色桌面上一台笔记本电脑和一个亮着灯的路由器",
    ),
    (
        ("城市", "房产", "政策", "经济", "就业", "收入", "融资", "上市"),
        "薄雾中的城市天际线，玻璃幕墙反射天光",
    ),
]

# 全不命中时的兜底场景（同样只留一个主体）
DEFAULT_SCENE = "简洁桌面上放着一部智能手机和一副耳机，柔和侧光"

# 统一风格锚：短句。cogview-3-flash 是轻量免费模型，prompt 越长越容易画糊，
# 实测把"构图/曝光/留白/色调"堆成一大段后，桌面物件直接糊成一团。
STYLE_ANCHOR = "真实摄影感，浅景深，自然光，画面干净，右侧留白"

# 负面约束：只留最必要的。太长的禁止清单会稀释主体描述
FORBID = "画面里不要有文字、水印、logo、人脸，不要发光粒子和霓虹光效"

_TAG_RE = re.compile(r"<[^>]+>")
_MARK_RE = re.compile(r"[#*`>]+")
_PREFIX_RE = re.compile(r"^(画面描述|画面|描述|prompt)\s*[:：]\s*", re.IGNORECASE)
_QUOTES = "\"'“”‘’「」『』《》 \t\r\n"


def _plain(text: Any, limit: int) -> str:
    """把正文里的 HTML 标签、Markdown 记号清掉，压成一行，截断到 limit 字。"""
    value = _TAG_RE.sub(" ", str(text or ""))
    value = _MARK_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _clean_brief(text: str) -> str:
    """清洗模型返回的画面描述：只取第一行、去前缀、去引号。"""
    first = str(text or "").strip().splitlines()
    line = first[0] if first else ""
    line = _PREFIX_RE.sub("", line.strip())
    return line.strip(_QUOTES)


def _rule_scene(article: dict[str, Any]) -> str:
    """关键词兜底：标题 + 摘要 + 正文开头 命中规则就用，全不命中给默认场景。"""
    text = " ".join(
        [
            _plain(article.get("title"), 120),
            _plain(article.get("desc"), 200),
            _plain(article.get("body"), 200),
        ]
    ).lower()
    for words, scene in VISUAL_RULES:
        if any(word.lower() in text for word in words):
            return scene
    return DEFAULT_SCENE


# 万能办公桌三件套：模型偷懒时的默认输出，与绝大多数文章主题无关。
# 「电脑/笔记本」+「窗/咖啡/水杯」同时出现，基本可判定是套模板而非贴题。
_GENERIC_OFFICE = ("笔记本电脑", "电脑", "笔电")  # 不收"笔记本"，纸质本子是正常主体
_GENERIC_PROPS = ("窗外", "窗边", "窗光", "咖啡", "水杯", "一杯水", "玻璃杯", "城市天际线")


def _brief_ok(brief: str) -> tuple[bool, str]:
    """校验画面脚本是否可用，返回 (是否通过, 不通过原因)。"""
    if not 8 <= len(brief) <= 140:
        return False, f"长度 {len(brief)} 字不合适"
    for word in _BRIEF_BANNED:
        if word in brief:
            return False, f"出现堆叠/抽象词「{word}」"
    # 泛化闸门：电脑 + 窗景/饮品，多半是模型照抄万能办公桌模板，不是贴题画面
    if any(w in brief for w in _GENERIC_OFFICE) and any(w in brief for w in _GENERIC_PROPS):
        return False, "疑似万能办公桌模板（电脑+窗景/饮品），与文章主题无关"
    return True, ""


def _visual_brief(article: dict[str, Any]) -> str:
    """让文本模型按文章写一句画面描述；失败返回空串，由调用方退回规则。

    .env 里 COVER_BRIEF=0 可强制关掉（回到纯关键词模式，不额外花钱）。
    """
    if os.getenv("COVER_BRIEF", "1").strip() == "0":
        return ""
    prompt = BRIEF_PROMPT.format(
        title=_plain(article.get("title"), 80),
        desc=_plain(article.get("desc"), 120) or "（无）",
        lead=_plain(article.get("body"), 220) or "（无）",
        scene_hint=_rule_scene(article),
    )
    try:
        # 延迟导入：cover 模块被 app.py 在启动时引入，放模块级会加重启动依赖
        from pipeline import call_ai
    except Exception as exc:  # noqa: BLE001 - 拿不到模型就走规则兜底
        print(f"[cover] 无法调用文本模型，改用规则：{exc}")
        return ""
    # 最多两次：第一次套模板/泛化时，把不合格原因明确回给模型让它重画一次。
    # 两次都不合格才退回关键词规则（规则贴题但场景有限，好过万能办公桌）。
    current = prompt
    for attempt in range(2):
        raw = call_ai(current)
        if not raw:
            return ""
        brief = _clean_brief(raw)
        ok, reason = _brief_ok(brief)
        if ok:
            return brief
        print(f"[cover] 画面脚本不合用（{reason}），重试：{brief[:40]}")
        if attempt == 0:
            current = (
                f"{prompt}\n\n你上次写的「{brief}」不合格：{reason}。"
                "请只根据这篇文章的标题和开头，写一句贴题的具体画面，"
                "禁止套用电脑+窗景/咖啡的通用办公桌场景。"
            )
    return ""


def build_cover_prompt(article: dict[str, Any], brief: str = "") -> str:
    """拼最终 prompt。brief 优先（文字模型写的），空则退回关键词规则。

    刻意保持短：轻量生图模型对长 prompt 的遵循度反而更差。
    """
    scene = (brief or "").strip() or _rule_scene(article)
    return f"横版 2.35:1 摄影头图。{scene}。{STYLE_ANCHOR}。{FORBID}。"


def _download(url: str) -> bytes:
    """下载生成结果，带一次重试。"""
    last: Exception | None = None
    for _ in range(2):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200 and resp.content:
                return resp.content
            last = RuntimeError(f"下载失败 HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 - 网络问题统一重试
            last = exc
        time.sleep(1)
    raise RuntimeError(str(last))


def generate_cover(article: dict[str, Any], force: bool = False) -> str | None:
    """为单篇稿件生成头图，成功返回相对路径，失败返回 None。

    文件落到 data/covers/{id}.jpg，重复调用会覆盖（换一张）。
    """
    article_id = str(article.get("id", "")).strip()
    if not article_id:
        return None

    # 生图只走智谱 CogView，跟写作模型解耦：主力换成 DeepSeek 时头图照样能出
    api_key = config.ZHIPU_IMAGE_KEY
    if not api_key:
        print("[cover] 未配置 ZHIPU_API_KEY，跳过头图生成")
        return None

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    target = COVER_DIR / f"{article_id}.jpg"
    if target.exists() and not force:
        return target.relative_to(ROOT).as_posix()

    brief = _visual_brief(article)
    prompt = build_cover_prompt(article, brief)
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "size": COVER_SIZE,
    }
    print(
        f"[cover] {article_id} 画面脚本来源："
        f"{'文本模型（' + brief[:28] + '…）' if brief else '关键词规则'}"
    )
    prompt_preview = prompt[:40]

    for attempt in range(2):
        try:
            started = time.time()
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:160]}")
            data = resp.json()
            url = data["data"][0]["url"]
            content = _download(url)
            target.write_bytes(content)
            print(
                f"[cover] {article_id} 生成成功 {len(content) // 1024}KB "
                f"{round(time.time() - started, 1)}s | {prompt_preview}"
            )
            return target.relative_to(ROOT).as_posix()
        except Exception as exc:  # noqa: BLE001 - 生图失败不能拖垮主流程
            print(f"[cover] {article_id} 第 {attempt + 1} 次失败：{exc}")
            time.sleep(2)

    return None


def backfill(limit: int = 20) -> int:
    """给已通过但还没有头图的稿件补图，返回成功张数。"""
    from pipeline import load_queue, save_queue

    queue = load_queue()
    done = 0
    for item in queue:
        if done >= limit:
            break
        if item.get("status") != "approved":
            continue
        rel = str(item.get("cover") or "")
        if rel and (ROOT / rel).exists():
            continue
        path = generate_cover(item)
        if path:
            item["cover"] = path
            item["cover_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            item["cover_state"] = "done"
            save_queue(queue)
            done += 1
    return done


def preview_prompt(article_id: str) -> None:
    """只打印这篇稿件会用的画面脚本与 prompt，不出图（不消耗生图额度）。"""
    from pipeline import load_queue

    article = next(
        (item for item in load_queue() if str(item.get("id")) == str(article_id)),
        None,
    )
    if not article:
        print(f"没找到稿件 {article_id}")
        raise SystemExit(1)

    brief = _visual_brief(article)
    print(f"稿件：{article.get('title', '')}")
    print(f"画面脚本来源：{'文本模型' if brief else '关键词规则'}")
    print(f"画面：{brief or _rule_scene(article)}")
    print("完整 prompt：")
    print(build_cover_prompt(article, brief))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args:
        count = backfill()
        print(f"补图完成：{count} 张")
    elif "--prompt" in args:
        index = args.index("--prompt")
        if index + 1 >= len(args):
            print("用法：python cover.py --prompt <稿件id>")
            raise SystemExit(1)
        preview_prompt(args[index + 1])
    else:
        print("用法：python cover.py --backfill | --prompt <稿件id>")
