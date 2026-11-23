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
from pipeline import build_daily_quotas, run as run_pipeline


def daily(count: int | None = None, demo: bool = False) -> None:
    """执行一次每日任务。

    count 不传时按三条内容线的每日配额生成（早报 5 + 副业 2，GitHub 到期当天 +1）；
    显式传 --count 则退回旧的"按总量抓稿"，方便临时补稿。
    """
    print("=== 每日任务开始 ===")

    quotas = build_daily_quotas() if count is None else None
    plan_count = sum(quotas.values()) if quotas else count
    mode = "演示" if demo else "真实"
    print(f"[1/2] 生成 {plan_count} 篇（{mode}模式）")
    if quotas:
        print(
            "  栏目排期："
            + " + ".join(
                f"{config.COLUMN_LABELS.get(k, k)} {v}"
                for k, v in quotas.items() if v > 0
            )
            or "  今日无排期"
        )
    created = run_pipeline(count=plan_count, demo=demo, quotas=quotas)
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
        default=None,
        help="本次生成篇数（不填=按三条栏目配额自动算；填了=临时按总量补稿）",
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
