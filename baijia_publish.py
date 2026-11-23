"""百家号自动存草稿（不发布）：Playwright UI 自动化填稿 + 点「存草稿」。

能做什么、不能做什么（2026-11 核实）：
  能：持久化 profile 记住登录态；打开百家号编辑页、关弹窗、填标题、贴正文、
      选封面、点「存草稿」。全程模拟真人操作，不调用任何未公开 HTTP 接口。
  不能：无人值守点「发布」——百家号对个人无官方发布 API，且自动化触碰发布
        会稳定触发滑块人机验证。滑块不该也不必绕过，最后发表留给人。

为什么走 UI 而不是像头条那样调内部接口（2026-11 调研）：
  头条发布接口靠页面 SDK 签名，借签名可全自动；百家号风控更严，发布接口逆向
  维护成本高且发布必触发滑块。存草稿阶段用 UI 模拟填表最稳——多个公开 Skill
  （dvcrn/baijiahao-publish、acedatacloud/baijiahao）都是这条路。

正文渲染：复用 toutiao_render 的裸 HTML（P/H3/STRONG/UL/LI/BLOCKQUOTE/PRE，
零内联样式）——两个平台的富文本编辑器都认这套结构，且都不吃公众号那套内联样式。
外链图无凭据会被渲染器丢弃，避免热链失败；封面走本地文件选择。

前置条件（缺哪个报哪个）：
  1. 服务器装好 Playwright + Chromium（同头条，装一次两个平台共用）。
  2. 首次扫码登录：点「百家号登录」，用百度 App 扫码，登录态写进独立 profile。
  3. 稿件已审核通过、有标题和正文；头图可选。

用法：
    from baijia_publish import BaijiaPublisher
    pub = BaijiaPublisher()
    pub.start_login()        # 扫码登录（后台线程）
    pub.save_draft(article)  # 存草稿，返回 {ok, draft_url, message}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import config
import toutiao_render

ROOT = Path(__file__).resolve().parent

# ---- 百家号常量：前端改版时只改这里 ----
BJ_BASE = "https://baijiahao.baidu.com"
EDIT_URL = f"{BJ_BASE}/builder/rc/edit?type=news&is_from_cms=1"
HOME_URL = f"{BJ_BASE}/builder/rc/home"
PROFILE_DIR = ROOT / "data" / "baijia_profile"
STATE_FILE = ROOT / "data" / "baijia_state.json"
QR_FILE = ROOT / "data" / "baijia_qr.png"

# 登录任务状态（进程内）
login_state: dict[str, Any] = {"status": "idle", "message": "", "qr": False, "at": 0}

# 标题输入框候选（百家号前端改版时按实测补在这里）
TITLE_SELECTORS = [
    "input[placeholder*='标题']",
    "textarea[placeholder*='标题']",
    ".title-input input",
    "#title",
    "input[name='title']",
]
# 正文编辑器候选（contenteditable 富文本区）
BODY_SELECTORS = [
    ".ql-editor[contenteditable='true']",   # quill
    "[contenteditable='true'].editor",
    ".editor-content [contenteditable='true']",
    "div[contenteditable='true']",
]
# 常见弹窗关闭按钮（登录后首访的引导/广告弹窗）
POPUP_CLOSE_SELECTORS = [
    "button:has-text('知道了')",
    "button:has-text('关闭')",
    ".modal-close",
    "[class*='close']:has-text('×')",
]


class BaijiaError(RuntimeError):
    """百家号操作失败，消息已经翻成人话。"""


def configured() -> bool:
    if not playwright_ready():
        return False
    return _load_state().get("logged_in", False)


def playwright_ready() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(patch: dict[str, Any]) -> dict[str, Any]:
    state = _load_state()
    state.update(patch)
    state["checked_at"] = int(time.time())
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


class BaijiaPublisher:
    """持有浏览器会话的百家号发布器。按需开关浏览器，profile 复用登录态。"""

    def __init__(self, headless: bool | None = None) -> None:
        self.headless = config.TOUTIAO_HEADLESS if headless is None else headless

    def _launch(self, pw: Any) -> Any:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": self.headless,
            "viewport": {"width": 1440, "height": 960},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # 与头条共用同一套浏览器探测：RHEL 系走 dnf 装的系统 Chromium
        if getattr(config, "BROWSER_EXECUTABLE_PATH", ""):
            kwargs["executable_path"] = config.BROWSER_EXECUTABLE_PATH
        return pw.chromium.launch_persistent_context(**kwargs)

    def check_login(self) -> bool:
        if not playwright_ready():
            return False
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                ctx = self._launch(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(EDIT_URL, timeout=30000)
                page.wait_for_timeout(2500)
                url = page.url
                # 被踢到百度登录页说明未登录
                logged = "passport" not in url and "/login" not in url
                ctx.close()
            _save_state({"logged_in": logged})
            return logged
        except Exception as exc:  # noqa: BLE001
            raise BaijiaError(f"检查登录态失败：{exc}") from exc

    def start_login(self) -> None:
        """后台生成登录二维码，用户用百度 App 扫，轮询写登录态。"""
        if not playwright_ready():
            raise BaijiaError(
                "服务器还没装 Playwright。执行 pip install playwright 和 playwright install chromium"
            )
        if STATE_FILE.exists() and _load_state().get("logged_in"):
            return
        global login_state
        login_state = {"status": "waiting", "message": "正在打开登录页…", "qr": False, "at": int(time.time())}
        import threading

        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self) -> None:
        global login_state
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                ctx = self._launch(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(EDIT_URL, timeout=30000)
                page.wait_for_timeout(3000)
                # 未登录会弹登录浮层，二维码常在 iframe 或 img 里
                qr = page.locator(
                    "iframe[src*='passport'], img[src*='qrcode'], "
                    ".login-qrcode img, canvas[class*='qr'], [class*='qrcode'] img"
                )
                try:
                    qr.first.wait_for(state="visible", timeout=15000)
                    qr.first.screenshot(path=str(QR_FILE))
                    login_state = {
                        "status": "waiting", "message": "请用百度 App 扫码登录",
                        "qr": True, "at": int(time.time()),
                    }
                except Exception:
                    page.screenshot(path=str(QR_FILE))
                    login_state = {
                        "status": "waiting", "message": "请在截图中找到二维码扫码",
                        "qr": True, "at": int(time.time()),
                    }
                deadline = time.time() + 300
                logged = False
                while time.time() < deadline:
                    page.wait_for_timeout(2500)
                    url = page.url
                    # 登录成功后回到编辑页、URL 不再含 passport/login
                    if "passport" not in url and "/login" not in url:
                        # 再确认页面确实有编辑器，防止误判中间跳转页
                        if page.locator("div[contenteditable='true']").count() > 0:
                            logged = True
                            break
                    if login_state.get("status") == "cancelled":
                        break
                ctx.close()

            if logged:
                _save_state({"logged_in": True})
                login_state = {
                    "status": "success", "message": "登录成功，登录态已保存",
                    "qr": False, "at": int(time.time()),
                }
            else:
                login_state = {
                    "status": "timeout", "message": "扫码超时，请重新发起登录",
                    "qr": False, "at": int(time.time()),
                }
        except Exception as exc:  # noqa: BLE001
            login_state = {
                "status": "error", "message": f"登录失败：{exc}",
                "qr": False, "at": int(time.time()),
            }

    @staticmethod
    def _find_first(page: Any, selectors: list[str]) -> Any:
        """按候选顺序找第一个可见元素，找不到返回 None。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _close_popups(self, page: Any) -> None:
        """关掉登录后首访常见的引导/广告弹窗（最多点 3 个，防误点正文按钮）。"""
        for sel in POPUP_CLOSE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=2000)
                    page.wait_for_timeout(300)
            except Exception:
                continue

    def save_draft(self, article: dict[str, Any]) -> dict[str, Any]:
        """把稿件存成百家号草稿（点「存草稿」，绝不点「发布」）。"""
        if not playwright_ready():
            raise BaijiaError("服务器还没装 Playwright，无法自动存草稿")
        if not _load_state().get("logged_in"):
            raise BaijiaError("百家号还没登录。点「百家号登录」扫码后再存草稿")

        title = (article.get("title") or "").strip()
        if not title:
            raise BaijiaError("稿件标题为空")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = self._launch(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(EDIT_URL, timeout=30000)
            page.wait_for_timeout(2500)
            self._close_popups(page)

            # 1) 标题
            title_input = self._find_first(page, TITLE_SELECTORS)
            if not title_input:
                ctx.close()
                raise BaijiaError(
                    "没找到标题输入框（百家号前端可能改版）。"
                    "把新的选择器发给 AI 更新 baijia_publish.py 的 TITLE_SELECTORS"
                )
            title_input.click()
            title_input.fill(title)

            # 2) 正文：复用头条裸 HTML，用 ClipboardEvent 贴进编辑器
            body_html = toutiao_render.to_toutiao_html(article.get("body", ""))
            if len(body_html) < 30:
                ctx.close()
                raise BaijiaError("正文转出来太短，先检查稿件正文")
            editor = self._find_first(page, BODY_SELECTORS)
            if not editor:
                ctx.close()
                raise BaijiaError(
                    "没找到正文编辑器（百家号前端可能改版）。"
                    "把新的选择器发给 AI 更新 baijia_publish.py 的 BODY_SELECTORS"
                )
            editor.click()
            try:
                page.evaluate(
                    """([sel, html]) => {
                      const el = document.querySelector(sel);
                      const dt = new DataTransfer();
                      dt.setData('text/html', html);
                      el.dispatchEvent(new ClipboardEvent('paste', {
                        clipboardData: dt, bubbles: true, cancelable: true}));
                    }""",
                    [self._selector_used(page, BODY_SELECTORS), body_html],
                )
            except Exception:
                # 粘贴事件不生效时退回纯文本填入，至少不空稿
                editor.fill(toutiao_render._wrap_plain_text(article.get("body", ""))[:5000])
            page.wait_for_timeout(1500)

            # 3) 封面（可选）：本地文件直接喂给 file input
            cover_rel = article.get("cover", "")
            cover_path = self._local_cover(cover_rel)
            if cover_path:
                try:
                    file_input = page.locator("input[type='file']").first
                    if file_input.count():
                        file_input.set_input_files(str(cover_path))
                        page.wait_for_timeout(2000)
                except Exception:
                    pass  # 封面失败不阻断存草稿

            # 4) 点「存草稿」——绝不点「发布」（发布会触发滑块）
            saved = False
            for label in ("存草稿", "保存草稿"):
                try:
                    btn = page.get_by_text(label, exact=True).first
                    if btn.count() and btn.is_visible():
                        btn.click(timeout=5000)
                        saved = True
                        break
                except Exception:
                    continue
            if not saved:
                ctx.close()
                raise BaijiaError(
                    "没找到「存草稿」按钮（前端改版或弹窗遮挡）。"
                    "没有点任何发布按钮，稿件未发出，可手动检查页面"
                )
            page.wait_for_timeout(3000)
            # 存草稿成功通常会跳到草稿列表或出成功提示；URL 变化/含草稿字样都算触发成功
            final_url = page.url
            ctx.close()

        return {
            "ok": True,
            "draft_url": HOME_URL,
            "final_url": final_url,
            "title": title,
            "message": "已点「存草稿」，请进百家号后台草稿箱检查，发表仍需你手动点",
        }

    def _selector_used(self, page: Any, selectors: list[str]) -> str:
        """返回实际命中的选择器字符串，供 page.evaluate 定位编辑器。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return sel
            except Exception:
                continue
        return selectors[-1]

    @staticmethod
    def _local_cover(cover_rel: str) -> Path | None:
        if not cover_rel:
            return None
        p = ROOT / cover_rel
        return p if p.exists() else None
