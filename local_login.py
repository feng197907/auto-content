"""本地真人登录一次，导出明文登录态（storage_state）给服务器复用。

为什么导出 JSON 而不是直接搬 profile 目录：
  Windows 上 Edge/Chrome 把 cookie 用 DPAPI 加密，整个 profile 目录搬到
  Linux 后用 Chromium 打开会解不开——登录 cookie 全丢，只剩几个不加密的
  普通 cookie，表现为"传上去了却显示未登录"。storage_state 是 Playwright
  官方的明文导出格式（cookie + localStorage），跨平台通用，绕开加密。

  头条短信登录会弹滑块拼图（真人才能过），服务器无头浏览器过不去；
  百家号虽然短信能到，但服务器端的表单识别不稳。
  本脚本只做「在 Windows 本地用真人身份登录一次」，不绕过任何风控。

用法（在项目根目录执行）：
    py -3 -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
    py -3 local_login.py toutiao      # 弹浏览器 → 手动拖滑块/收短信登录 → 回命令行按回车
    py -3 local_login.py baijia
  脚本会自动用系统自带的 Edge/Chrome，不需要再下载 Chromium（省 100 多 MB）。

登录成功后，只需把导出的 JSON 传到服务器（不用再传整个 profile 目录）：
    data/toutiao_cookies.json   → /opt/auto-content/data/toutiao_cookies.json
    data/baijia_cookies.json    → /opt/auto-content/data/baijia_cookies.json
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
            # 关键：导出明文 storage_state（跨平台），而不是靠加密的 profile 目录
            cookie_file = m.COOKIES_FILE
            ctx.storage_state(path=str(cookie_file))
            m._save_state({"logged_in": True})
            print(f"\n[OK] 登录态已导出到 {cookie_file}")
            print("只需把这一个 JSON 文件传到服务器，不用再传整个 profile 目录：")
            print(f"  scp {cookie_file.name} root@服务器:/opt/auto-content/data/")
            print("传完在服务器执行 systemctl restart auto-content")
            # 顺带确认导出的 JSON 里确实有登录 cookie（防止误判"登录成功"但没凭据）
            try:
                import json as _json

                data = _json.loads(cookie_file.read_text(encoding="utf-8"))
                names = [c.get("name", "") for c in data.get("cookies", [])]
                print(f"  导出 cookie 共 {len(names)} 个：{names[:12]}")
            except Exception:
                pass
        else:
            print("\n[!] 看起来还没登录成功（URL 仍在登录页）。")
            print("   没有导出登录态，请重新运行本脚本再登录一次。")
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
