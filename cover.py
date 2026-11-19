"""公众号头图：稿件审核通过后自动生成一张 2.35:1 的封面图。

设计取舍：
1. 用智谱 CogView-3-Flash（永久免费，实测 8-10 秒出图），走服务器直连，
   不依赖任何外部图库，也不抓原文配图（原文图有版权风险，且多数平台带防盗链）。
2. 尺寸直接请求 1808x768，正好 2.35:1，和公众号大图 900x383 同比例，
   不用 Pillow 二次裁剪（服务器没装 Pillow，也省掉中文字体依赖）。
3. 图上不压标题文字：中文小字在生成图里容易糊，且公众号列表本身会显示标题。
   平台会自动在右下角打「AI生成」标识，符合生成式合成内容标识要求。
4. 任何一步失败都返回 None，绝不影响「审核通过」主流程。

命令行：
    .venv/bin/python cover.py --backfill    # 给所有已通过但没头图的稿件补图
"""

from __future__ import annotations

import os
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

# 标题关键词 → 画面意象。命中越靠前的规则越优先，全不命中用兜底。
VISUAL_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("漏洞", "攻击", "安全", "泄露", "入侵", "黑产", "诈骗", "勒索"),
        "网络安全危机感：深色机房与服务器机柜，冷蓝主调里点几处红色警示光",
    ),
    (
        ("融资", "投资", "估值", "上市", "IPO", "营收", "财报", "增长", "市场"),
        "资本与增长：抽象的数据光带向上延伸，深蓝与金色对比，秩序感",
    ),
    (
        ("芯片", "显卡", "处理器", "算力", "半导体", "制程", "光刻"),
        "芯片微距特写：精密电路走线与晶圆质感，冷蓝绿色反光",
    ),
    (
        ("大模型", "AI", "人工智能", "模型", "智能体", "Agent", "算法", "开源"),
        "AI 神经网络：漂浮的光点与节点连线，深蓝紫渐变，科技感",
    ),
    (
        ("手机", "电视", "屏", "折叠", "相机", "耳机", "笔记本", "平板"),
        "数码产品暗调棚拍氛围：极简背景、边缘侧光、产品留白剪影",
    ),
    (
        ("机器人", "自驾", "驾驶", "汽车", "工厂", "制造"),
        "机器与制造：金属机械结构，暖灰与冷蓝对比，工业质感",
    ),
]

DEFAULT_HINT = "通用科技意象：深色抽象科技背景，几何光带与微光粒子，简洁高级"

# 负面约束：生成图里最怕出现假文字、假 logo、真人脸
FORBID = (
    "画面中不要出现任何文字、字母、数字、代码、logo、水印、二维码；"
    "不要出现人脸特写；不要出现真实品牌与产品外观；不要卡通风格"
)


def build_cover_prompt(article: dict[str, Any]) -> str:
    """按标题关键词拼一段画面描述。"""
    title = str(article.get("title", ""))
    desc = str(article.get("desc", "") or "")[:60]

    hint = DEFAULT_HINT
    for words, text in VISUAL_RULES:
        if any(w.lower() in (title + desc).lower() for w in words):
            hint = text
            break

    return (
        f"横版公众号资讯头图背景，2.35:1 宽幅构图。主题参考：{title}。"
        f"画面：{hint}。构图上留出干净区域、不堆砌元素，整体克制、有高级感，"
        f"适合作为科技资讯文章封面。{FORBID}。"
    )


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

    api_key = config.AI_API_KEY
    if not api_key:
        print("[cover] 未配置 ZHIPU_API_KEY，跳过头图生成")
        return None

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    target = COVER_DIR / f"{article_id}.jpg"
    if target.exists() and not force:
        return target.relative_to(ROOT).as_posix()

    payload = {
        "model": MODEL,
        "prompt": build_cover_prompt(article),
        "size": COVER_SIZE,
    }
    prompt_preview = payload["prompt"][:40]

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


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        n = backfill()
        print(f"补图完成：{n} 张")
    else:
        print("用法：python cover.py --backfill")
