"""本地真人登录一次，把登录态留在 data/*_profile，再搬到服务器复用。

为什么需要它：
  头条短信登录会弹滑块拼图（真人才能过），服务器无头浏览器过不去；
  百家号虽然短信能到，但服务器端的表单识别不稳。
  本脚本只做「在 Windows 本地用真人身份登录一次」，不绕过任何风控。
  登录完成后把 profile 目录传到服务器，服务器从此免登录存草稿。

用法（在项目根目录执行）：
    py -3 -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
    py -3 local_login.py toutiao      # 弹浏览器 → 手动拖滑块/收短信登录 → 回命令行按回车
    py -3 local_login.py baijia
  脚本会自动用系统自带的 Edge/Chrome，不需要再下载 Chromium（省 100 多 MB）。
  只有系统没有 Edge/Chrome 时才需要：playwright install chromium（记得设国内镜像）。

登录成功后，把下面这些文件/目录整体传到服务器 /opt/auto-content/data/ 对应位置：
    data/toutiao_profile/        → /opt/auto-content/data/toutiao_profile/
    data/toutiao_state.json      → /opt/auto-content/data/toutiao_state.json
    data/baijia_profile/         → /opt/auto-content/data/baijia_profile/
    data/baijia_state.json       → /opt/auto-content/data/baijia_state.json
然后服务器执行：systemctl restart auto-content
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 服务器配置里的 Chromium 路径在本地无效。本地优先用系统自带的 Edge/Chrome，
# 省掉 playwright install chromium 那一步的下载（国内慢且容易断）。
import config  # noqa: E402

config.BROWSER_EXECUTABLE_PATH = ""
_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
for _p in _EDGE_PATHS:
    if Path(_p).exists():
        config.BROWSER_EXECUTABLE_PATH = _p
        break


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("toutiao", "baijia"):
        print("用法: py -3 local_login.py toutiao|baijia")
        sys.exit(2)
    platform = sys.argv[1]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("还没装 Playwright，先执行（国内镜像加速）：")
        print("  py -3 -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple")
        print("本脚本用系统自带的 Edge/Chrome，不需要 playwright install chromium。")
        sys.exit(1)

    if platform == "toutiao":
        import toutiao_publish as m

        Pub = m.ToutiaoPublisher
        url = f"{m.TT_BASE}/profile_v4/graphic/publish"
        logged_in = lambda p: "login" not in p.url and "passport" not in p.url  # noqa: E731
    else:
        import baijia_publish as m

        Pub = m.BaijiaPublisher
        url = m.EDIT_URL
        logged_in = lambda p: (  # noqa: E731
            "passport" not in p.url
            and "/login" not in p.url
            and "theme/bjh" not in p.url
        )

    print(f"正在打开 {url} ...")
    with sync_playwright() as pw:
        ctx = Pub(headless=False)._launch(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            print(f"打开页面时提示：{exc}（不影响，接着看浏览器窗口）")

        print("\n请在弹出的浏览器窗口里手动登录：")
        print("  头条：手机号 + 短信，滑块自己拖")
        print("  百家号：短信快捷登录或扫码，验证码自己填")
        try:
            input("\n登录成功后，回到这里按回车继续 ... ")
        except EOFError:
            pass

        try:
            ok = logged_in(page)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            m._save_state({"logged_in": True})
            print(f"\n[OK] 登录态已保存到 {m.PROFILE_DIR}")
            print("把整个 profile 目录和 state.json 传到服务器，然后重启服务。")
        else:
            print("\n[!] 看起来还没登录成功（URL 仍在登录页）。")
            print("   profile 已保留，重新运行本脚本再登录一次即可。")
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
