"""每日任务：生成稿件 + 推送到微信。给 cron 定时调用。

流程：
    1. 调 pipeline 生成 N 篇（真实 AI 模式）
    2. 调 notify 把待审稿推送到微信
    3. 全程写日志，失败不影响下一步

用法：
    py -3 daily_job.py                    # 用配置默认篇数
    py -3 daily_job.py --count 3          # 指定篇数
    py -3 daily_job.py --demo             # 演示：不真调 AI 也不真推送
"""

from __future__ import annotations

import argparse

import config
import notify as notifier
from pipeline import run as run_pipeline


def daily(count: int, demo: bool = False) -> None:
    """执行一次每日任务。"""
    print("=== 每日任务开始 ===")

    # 1. 生成稿件
    mode = "演示" if demo else "真实"
    print(f"[1/2] 生成 {count} 篇（{mode}模式）")
    created = run_pipeline(count=count, demo=demo)
    print(f"  新增 {len(created)} 篇")

    # 2. 推送微信
    print("[2/2] 推送待审稿到微信")
    notifier.notify(review_url=config.REVIEW_URL, demo=demo)

    print("=== 每日任务结束 ===")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="每日生成+微信推送")
    parser.add_argument(
        "--count",
        type=int,
        default=config.BATCH_SIZE,
        help="本次生成篇数",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="演示模式：不调 AI、不真推送",
    )
    args = parser.parse_args()
    daily(args.count, args.demo)


if __name__ == "__main__":
    main()
