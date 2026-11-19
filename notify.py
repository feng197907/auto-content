"""微信推送模块：通过 Server酱 把今日稿件推送到微信。

设计原则：
    1. 推送失败绝不影响主流程（生成稿件照常进行）
    2. key 从 .env 读，不落代码
    3. 没配 key 时走 demo 模式，只打印将要推送的内容，方便验证
    4. 推送内容带审核页链接，点开即可在手机上审核复制

用法：
    py -3 notify.py            # 推送当前待审稿到微信
    py -3 notify.py --demo     # 不真发，只打印推送内容
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import requests

import config
from pipeline import load_queue

ROOT = Path(__file__).resolve().parent
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"


def get_serverchan_key() -> str:
    """读 Server酱 的 SendKey，优先 .env，其次环境变量。"""
    return os.getenv("SERVERCHAN_KEY", "")


def build_message(queue: list[dict[str, Any]], review_url: str) -> tuple[str, str]:
    """根据待审稿构造推送的标题和正文（Markdown 格式）。"""
    pending = [item for item in queue if item.get("status") == "pending"]
    count = len(pending)

    title = f"今日 {count} 篇稿件待审核" if count else "今日无新增稿件"

    lines = ["## 待审核稿件", ""]
    if not pending:
        lines.append("今天没有新稿件。")
    else:
        for index, item in enumerate(pending[:10], 1):
            hot = f"（热度 {item['hot']:,}）" if item.get("hot") else ""
            lines.append(f"{index}. {item['title']}{hot}")
        if count > 10:
            lines.append(f"……还有 {count - 10} 篇")

    lines.append("")
    lines.append("---")
    lines.append(f"点开审核：{review_url}" if review_url else "审核地址未配置")
    lines.append("")
    lines.append("在审核页可改稿、通过、一键复制成品。")

    return title, "\n".join(lines)


def send_serverchan(title: str, content: str, key: str) -> bool:
    """调 Server酱 推送，成功返回 True。

    注意：Server酱 HTTP 层永远返回 200，成败看响应体的 code 字段。
    """
    if not key:
        print("  未配置 SERVERCHAN_KEY，跳过推送")
        return False

    try:
        response = requests.post(
            SERVERCHAN_URL.format(key=key),
            data={"title": title, "desp": content},
            timeout=15,
        )
        # 只打印打码后的信息，绝不把含 key 的 URL 写进日志
        result = response.json()
        if str(result.get("code")) == "0":
            print("  微信推送成功")
            return True
        code = result.get("code")
        message = str(result.get("message", ""))[:60]
        print(f"  微信推送失败: code={code} msg={message}")
        return False
    except (requests.RequestException, ValueError) as exc:
        print(f"  微信推送异常: {type(exc).__name__}: {exc}")
        return False


def notify(review_url: str = "", demo: bool = False) -> bool:
    """推送当前待审稿到微信，返回是否成功。"""
    queue = load_queue()
    title, content = build_message(queue, review_url)

    if demo:
        print("=== 微信推送内容预览（demo，不真发）===")
        print(f"标题: {title}")
        print(content)
        print("=====================================")
        return True

    key = get_serverchan_key()
    return send_serverchan(title, content, key)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="微信推送待审稿件")
    parser.add_argument("--demo", action="store_true", help="只预览不真发")
    parser.add_argument(
        "--url",
        default=os.getenv("REVIEW_URL", ""),
        help="审核页公网地址，也可设 REVIEW_URL 环境变量",
    )
    args = parser.parse_args()
    notify(args.url, args.demo)


if __name__ == "__main__":
    main()
