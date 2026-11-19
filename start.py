"""一键启动脚本：检查环境 -> 生成样稿 -> 启动审核后台。

用法：
    py -3 start.py

作用：新手不用记三条命令，跑这一个文件就能看到完整服务。
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

import config
from pipeline import load_queue, run

ROOT = Path(__file__).resolve().parent


def check_dependencies() -> bool:
    """检查依赖是否装好，缺了给出安装命令。"""
    try:
        import flask  # noqa: F401
        import feedparser  # noqa: F401
        import requests  # noqa: F401
        return True
    except ImportError:
        print("依赖未安装，请先执行：")
        print(f"  {sys.executable} -m pip install -r requirements.txt")
        return False


def main() -> None:
    """启动流程。"""
    print("=" * 50)
    print("  自动化内容矩阵 · 一键启动")
    print("=" * 50)

    if not check_dependencies():
        sys.exit(1)

    queue = load_queue()
    if not queue:
        print("\n队列为空，先生成 5 篇样稿...")
        demo = not bool(config.AI_API_KEY)
        if demo:
            print("（未检测到 API key，用演示样稿）")
        run(5, demo)

    demo_note = "演示样稿" if not config.AI_API_KEY else "真实 AI 稿件"
    url = f"http://localhost:{config.APP_PORT}"
    print(f"\n当前模式：{demo_note}")
    print(f"审核后台：{url}")
    print("按 Ctrl+C 停止服务\n")

    # 延迟打开浏览器，避免服务还没起来
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 浏览器打不开不影响服务
        pass

    subprocess.run([sys.executable, str(ROOT / "app.py")], check=True)


if __name__ == "__main__":
    main()
