"""头条号自动存草稿（不发布）：浏览器自动化 + 页面上下文调内部接口。

能做什么、不能做什么（2026-11 核实）：
  能：用持久化 profile 记住登录态；上传正文图/封面；把稿件存成头条草稿。
  不能：无人值守直接点"发布"——头条没有个人可用的官方发布 API，发布动作
        必须借页面风控 SDK 的签名，且平台政策上最后一步应由人确认。
        所以本模块 save=0（存草稿），人进后台点发表。

为什么用浏览器而不是纯 requests（2026-09 实测结论）：
  头条发布接口要求 msToken / a_bogus 签名，由页面 JSVMP 现算；从 Python/curl
  发同样的请求缺签名会被拒（7050）。而在页面上下文里 fetch，风控 SDK 会自动
  把签名补上。Playwright 的 page.evaluate 跑在 MAIN world，正好能借到这个签名。
  图片上传 /spice/image 例外——实测不带签名也能成功，所以走 Python requests 更快。

三个前置条件（缺哪个报哪个，不吞错误）：
  1. 服务器装好 Playwright + Chromium（见 DEPLOYED.md 服务器步骤）。
  2. 首次扫码登录：调 login() 拿二维码，用头条 App 扫，登录态写进独立 profile。
  3. 稿件已审核通过、有标题和正文；头图可选（没有也能存草稿，只是无封面）。

用法：
    from toutiao_publish import ToutiaoPublisher
    pub = ToutiaoPublisher()
    pub.login_state()            # 是否已登录
    pub.start_login()            # 生成二维码（前端轮询扫码结果）
    pub.save_draft(article)      # 存草稿，返回草稿链接
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

import config

ROOT = Path(__file__).resolve().parent

# ---- 头条接口常量：改版时只改这里（2026-09 实测快照）----
TT_BASE = "https://mp.toutiao.com"
AID = "1231"
UPLOAD_URL = f"{TT_BASE}/spice/image"
PUBLISH_URL = f"{TT_BASE}/mp/agw/article/publish"
LOGIN_PROBE_URL = f"{TT_BASE}/mp/agw/media/get_media_info"
PROFILE_DIR = ROOT / "data" / "toutiao_profile"
STATE_FILE = ROOT / "data" / "toutiao_state.json"
QR_FILE = ROOT / "data" / "toutiao_qr.png"

# 登录任务状态（进程内，够用；服务重启归零无副作用）
login_state: dict[str, Any] = {"status": "idle", "message": "", "qr": False, "at": 0}

# 短信验证码登录：前端提交的手机号/验证码（worker 线程轮询捡取）
sms_state: dict[str, str] = {"phone": "", "code": ""}


class ToutiaoError(RuntimeError):
    """头条操作失败，消息已经翻成人话。"""


def configured() -> bool:
    """Playwright 是否可用、profile 是否已登录。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return _load_state().get("logged_in", False)


def playwright_ready() -> bool:
    """只查 Playwright 库装没装，与登录态无关（前端据此区分提示）。"""
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


def local_image_path(cover_rel: str) -> Path | None:
    """稿件的 cover 字段（相对路径）→ 绝对路径；文件不存在返回 None。"""
    if not cover_rel:
        return None
    p = ROOT / cover_rel
    return p if p.exists() else None


class ToutiaoPublisher:
    """持有 Playwright 会话的头条发布器。

    每次操作开一个独立 browser（persistent context 复用 profile），操作完关闭。
    不常驻浏览器——无头服务常驻 Chromium 容易僵死，按需开关更稳。
    """

    def __init__(self, headless: bool | None = None) -> None:
        self.headless = (
            config.TOUTIAO_HEADLESS if headless is None else headless
        )

    def _launch(self, pw: Any) -> Any:
        """启动持久化 context。登录态存在 profile 目录，跨进程复用。"""
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # RHEL 系用 dnf 装的系统 Chromium；探测到就显式指定，绕开自带包缺库的问题
        if getattr(config, "BROWSER_EXECUTABLE_PATH", ""):
            kwargs["executable_path"] = config.BROWSER_EXECUTABLE_PATH
        return pw.chromium.launch_persistent_context(**kwargs)

    def check_login(self) -> bool:
        """打开 profile 检查登录态：能进创作后台即算已登录。"""
        if not playwright_ready():
            return False
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                ctx = self._launch(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(f"{TT_BASE}/profile_v4/graphic/publish", timeout=30000)
                page.wait_for_timeout(2500)
                url = page.url
                # 被踢到登录页 / passport 说明未登录
                logged = "login" not in url and "passport" not in url
                ctx.close()
            _save_state({"logged_in": logged})
            return logged
        except Exception as exc:  # noqa: BLE001
            raise ToutiaoError(f"检查登录态失败：{exc}") from exc

    def start_login(self) -> None:
        """生成登录二维码并写入 QR_FILE，前端展示给用户扫。

        登录检测在后台线程里做：扫码成功后自动把登录态写进 profile。
        """
        if not playwright_ready():
            raise ToutiaoError(
                "服务器还没装 Playwright 或浏览器。先 pip install playwright；"
                "Debian/Ubuntu 再执行 playwright install chromium && playwright install-deps chromium；"
                "CentOS/Rocky 先试 dnf install -y chromium；Alibaba Cloud Linux 官方源无 chromium，"
                "用 playwright install chromium 后按 DEPLOYED.md 第49节补系统库"
            )
        if STATE_FILE.exists() and _load_state().get("logged_in"):
            return  # 已登录不必重复扫码

        global login_state
        login_state = {"status": "waiting", "message": "正在打开登录页…", "qr": False, "at": int(time.time())}
        import threading

        threading.Thread(target=self._login_worker, daemon=True).start()

    # 登录浮层里的二维码节点：头条前端改版时按实测补这里
    _QR_SELECTORS = (
        "iframe[src*='sso'], .login-qrcode img, img[src*='qrcode'], "
        "canvas[class*='qr'], .dui-qrcode canvas, img[class*='qrcode']"
    )
    # 二维码会定时自动刷新，静态截图几十秒就失效；轮询期间按这个间隔重截
    _QR_REFRESH_SEC = 20

    # ---- 短信验证码登录选择器（2026-11-24 服务器实测 DOM）----
    # 头条登录页默认就渲染验证码表单，手机号/验证码输入框都在，无需切 tab。
    SMS_PHONE_SELECTORS = (
        'input[name="normal-input"]',      # 实测 placeholder=手机号
        'input[placeholder="手机号"]',
        'input[type="tel"]:visible',
    )
    SMS_CODE_SELECTORS = (
        'input[name="button-input"]',      # 实测 placeholder=验证码
        'input[placeholder="验证码"]',
    )
    SMS_SEND_BTN_SELECTORS = (
        'button.web-login-button-input__button-text',  # 实测 class 含 send-input
        'button:has-text("获取验证码")',
    )
    SMS_SUBMIT_BTN_SELECTORS = (
        'button.web-login-button:not(.web-login-button__disabled)',
        'button.web-login-button',
        'button:has-text("登录")',
    )
    SMS_TAB_SELECTORS = (               # 若页面默认不是验证码登录，尝试切过去
        'text=验证码登录',
    )

    def _capture_qr(self, page: Any) -> None:
        """把当前登录页的二维码截到 QR_FILE；每轮重截，避免用户扫到过期码。"""
        global login_state
        qr = page.locator(self._QR_SELECTORS)
        try:
            qr.first.wait_for(state="visible", timeout=8000)
            qr.first.screenshot(path=str(QR_FILE))
        except Exception:
            # 选择器没命中：退而求其次截整页，用户可能要放大看
            page.screenshot(path=str(QR_FILE))
        # 打时间戳，前端据此拉最新图（防止浏览器缓存用到过期码）
        login_state["qr_at"] = int(time.time())

    def _login_worker(self) -> None:
        """后台：打开登录页 → 截二维码（周期内重截）→ 轮询扫码结果 → 写登录态。"""
        global login_state
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                ctx = self._launch(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(f"{TT_BASE}/profile_v4/graphic/publish", timeout=30000)
                page.wait_for_timeout(3000)
                login_state = {
                    "status": "waiting", "message": "请用头条 App 扫码登录（二维码会自动刷新）",
                    "qr": True, "at": int(time.time()),
                }
                self._capture_qr(page)

                # 轮询最多 8 分钟；期间每 20 秒重截二维码，保证用户扫的是有效码。
                # 前端 img 带时间戳防缓存，会跟着拉最新截图。
                deadline = time.time() + 480
                logged = False
                last_qr = time.time()
                while time.time() < deadline:
                    page.wait_for_timeout(2000)
                    url = page.url
                    if "login" not in url and "passport" not in url:
                        logged = True
                        break
                    if login_state.get("status") == "cancelled":
                        break
                    if time.time() - last_qr >= self._QR_REFRESH_SEC:
                        try:
                            self._capture_qr(page)
                            last_qr = time.time()
                        except Exception:
                            pass  # 页面正在跳转时截图失败，下轮再试
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

    # ---- 短信验证码登录（两步式：发码 → 用户填码 → 提交登录）----
    @staticmethod
    def _find_first(page: Any, selectors: tuple[str, ...] | list[str]) -> Any:
        """按候选顺序找第一个可见元素，找不到返回 None。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _wait_first(self, page: Any, selectors, timeout_ms: int = 12000) -> Any:
        """按候选顺序轮询等待第一个可见元素。

        头条登录页是重 JS 应用：domcontentloaded 后还要几秒才渲染出表单。
        固定 sleep + 瞬时 is_visible() 会扑空（实测报「找不到获取验证码按钮」），
        这里元素一出现就立刻返回，兼顾快与稳。
        """
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:
                    continue
            page.wait_for_timeout(300)
        return None

    @staticmethod
    def _page_diag(page: Any) -> str:
        """失败时抓页面摘要写进报错，下次不用再跑诊断脚本。"""
        try:
            inputs = [
                (i.get_attribute("name") or "", i.get_attribute("placeholder") or "")
                for i in page.locator("input").all()[:8]
            ]
            btns = [(b.inner_text() or "").strip()[:12]
                    for b in page.locator("button").all()[:8]]
            return " url=%s inputs=%s buttons=%s" % (page.url, inputs, btns)
        except Exception:
            return " url=%s" % getattr(page, "url", "?")

    def send_code(self, phone: str) -> None:
        """第一步：打开登录页 → 填手机号 → 点「获取验证码」。

        后台线程保持同一个浏览器会话，停在等待验证码状态；前端拿到短信后
        调 submit_code(code)，worker 在同一上下文里填码登录、保存登录态。
        手机号只做基本格式校验，不存进任何文件（只在进程内传递）。
        """
        global login_state, sms_state
        if not playwright_ready():
            raise ToutiaoError("服务器还没装 Playwright，无法自动登录")
        phone = (phone or "").strip()
        if not (phone.isdigit() and len(phone) == 11):
            raise ToutiaoError("请输入 11 位手机号")
        if login_state.get("status") in ("sending", "code_sent", "verifying"):
            raise ToutiaoError("登录正在进行中，请稍候或刷新页面查看进度")
        if STATE_FILE.exists() and _load_state().get("logged_in"):
            raise ToutiaoError("头条已登录，无需重新发码")
        sms_state = {"phone": phone, "code": ""}
        login_state = {
            "status": "sending", "message": "正在打开登录页…",
            "qr": False, "at": int(time.time()),
        }
        import threading

        threading.Thread(target=self._sms_login_worker, daemon=True).start()

    def submit_code(self, code: str) -> None:
        """第二步：把用户收到的短信验证码交给后台 worker（worker 在同一会话里登录）。"""
        global login_state
        if login_state.get("status") != "code_sent":
            raise ToutiaoError("当前不是等待验证码状态，请先点「发送验证码」")
        code = (code or "").strip()
        if not (code.isdigit() and 4 <= len(code) <= 8):
            raise ToutiaoError("验证码应为 4-8 位数字")
        # 用模块级变量传递，worker 轮询读取；同一时刻只有一个登录会话
        sms_state["code"] = code
        login_state = {
            "status": "verifying", "message": "正在提交验证码…",
            "qr": False, "at": int(time.time()),
        }

    def _sms_login_worker(self) -> None:
        """后台线程：一个浏览器会话内完成 填手机号→发码→等待→填验证码→登录。"""
        global login_state
        pw_ctx: Any = None
        browser_ctx: Any = None
        try:
            from playwright.sync_api import sync_playwright

            pw_ctx = sync_playwright().start()
            browser_ctx = self._launch(pw_ctx)
            page = browser_ctx.pages[0] if browser_ctx.pages else browser_ctx.new_page()
            page.goto(f"{TT_BASE}/profile_v4/graphic/publish", wait_until="domcontentloaded", timeout=30000)
            # 关键修复：不靠固定 sleep。登录页是重 JS 应用，domcontentloaded 后表单
            # 还要几秒才渲染；改用轮询等手机号框出现，出现即操作。
            # 同时兼容「已登录」：若 3 秒内既没出手机号框、URL 也不在登录页，视为已登录。
            phone_input = self._wait_first(page, self.SMS_PHONE_SELECTORS, 12000)
            if not phone_input:
                if "login" not in page.url and "passport" not in page.url:
                    _save_state({"logged_in": True})
                    login_state = {
                        "status": "success", "message": "头条已登录（无需验证码）",
                        "qr": False, "at": int(time.time()),
                    }
                    return
                raise ToutiaoError(
                    "没找到手机号输入框（页面未渲染完或改版）." + self._page_diag(page)
                )

            # 实测默认就在验证码登录 tab，不再点 tab（点它会触发重渲染、延长等待）

            phone_input.fill(sms_state["phone"])
            page.wait_for_timeout(300)

            send_btn = self._wait_first(page, self.SMS_SEND_BTN_SELECTORS, 8000)
            if not send_btn:
                raise ToutiaoError(
                    "没找到「获取验证码」按钮（页面未渲染完或改版）." + self._page_diag(page)
                )
            send_btn.click(timeout=8000)
            login_state = {
                "status": "code_sent", "message": "验证码已发送，请查收短信并填入",
                "qr": False, "at": int(time.time()),
            }

            # 最多等 5 分钟：前端提交 code 后轮询读取
            deadline = time.time() + 300
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                if sms_state.get("code"):
                    break
                if login_state.get("status") == "cancelled":
                    break
            if not sms_state.get("code"):
                login_state = {
                    "status": "timeout", "message": "等验证码超时，请重新发送",
                    "qr": False, "at": int(time.time()),
                }
                return

            code_input = self._wait_first(page, self.SMS_CODE_SELECTORS, 8000)
            if not code_input:
                raise ToutiaoError("没找到验证码输入框（页面可能改版）." + self._page_diag(page))
            code_input.fill(sms_state["code"])
            page.wait_for_timeout(300)

            submit_btn = self._wait_first(page, self.SMS_SUBMIT_BTN_SELECTORS, 8000)
            if not submit_btn:
                raise ToutiaoError("没找到「登录」按钮（页面可能改版）." + self._page_diag(page))
            submit_btn.click(timeout=8000)

            # 轮询登录是否成功（URL 离开登录页）
            deadline2 = time.time() + 60
            logged = False
            while time.time() < deadline2:
                page.wait_for_timeout(2000)
                if "login" not in page.url and "passport" not in page.url:
                    logged = True
                    break
            if logged:
                _save_state({"logged_in": True})
                login_state = {
                    "status": "success", "message": "登录成功，登录态已保存",
                    "qr": False, "at": int(time.time()),
                }
            else:
                login_state = {
                    "status": "error", "message": "验证码提交后未跳转，可能验证码错误，请重试",
                    "qr": False, "at": int(time.time()),
                }
        except Exception as exc:  # noqa: BLE001
            login_state = {
                "status": "error", "message": f"登录失败：{exc}",
                "qr": False, "at": int(time.time()),
            }
        finally:
            # 会话用完就关：登录态已在 persistent profile 里，下次免登录
            try:
                if browser_ctx is not None:
                    browser_ctx.close()
            except Exception:
                pass
            try:
                if pw_ctx is not None:
                    pw_ctx.stop()
            except Exception:
                pass

    def upload_image(self, page: Any, image_bytes: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
        """上传图片到头条 CDN，返回 {image_url, image_uri, width, height}。

        实测 /spice/image 不需要 a_bogus 签名，但要在页面上下文里 fetch 才稳
        （同源 + 浏览器自动带 cookie），所以走 page.evaluate，字节以 base64 传入。
        """
        import base64

        b64 = base64.b64encode(image_bytes).decode("ascii")
        js = """
        async ([b64, mime]) => {
          const bin = atob(b64);
          const buf = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
          const blob = new Blob([buf], {type: mime});
          const fd = new FormData();
          fd.append('image', blob, 'img.' + (mime.includes('png') ? 'png' : 'jpg'));
          const r = await fetch('/spice/image?upload_source=20020002&aid=1231&device_platform=web',
                                {method:'POST', body:fd, credentials:'include'});
          return await r.json();
        }
        """
        payload = page.evaluate(js, [b64, mime])
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ToutiaoError(f"图片上传失败：{payload}")
        data = payload.get("data") or {}
        if not data.get("image_url") or not data.get("image_uri"):
            raise ToutiaoError(f"图片上传返回缺字段：{data}")
        return data

    def save_draft(self, article: dict[str, Any]) -> dict[str, Any]:
        """把稿件存成头条草稿（save=0），返回 {ok, draft_url, pgc_id, title}。

        步骤：登录态检查 → 上传封面/正文图 → 渲染裸 HTML → 页面上下文调
        /mp/agw/article/publish 存草稿。任何一步失败都抛 ToutiaoError，
        绝不静默降级成"直接发布"。
        """
        if not playwright_ready():
            raise ToutiaoError("服务器还没装 Playwright，无法自动存草稿")
        if not _load_state().get("logged_in"):
            raise ToutiaoError("头条还没登录。点「头条登录」扫码后再存草稿")

        title = (article.get("title") or "").strip()
        if len(title) < 2 or len(title) > 30:
            raise ToutiaoError(f"头条标题要求 2-30 字，当前 {len(title)} 字：{title}")

        from playwright.sync_api import sync_playwright
        import toutiao_render

        with sync_playwright() as pw:
            ctx = self._launch(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{TT_BASE}/profile_v4/graphic/publish", timeout=30000)
            page.wait_for_timeout(2500)

            # 1) 封面（可选）：封面单独用 upload_source=20020003
            cover = local_image_path(article.get("cover", ""))
            cover_cred: dict[str, Any] | None = None
            if cover:
                cover_cred = self._upload_cover(page, cover.read_bytes())

            # 2) 正文图：先把正文里引用的本地图片上传，换成头条 CDN 凭据
            image_map: dict[str, dict[str, Any]] = {}
            for src in toutiao_render.image_srcs(article.get("body", "")):
                img_path = self._resolve_body_image(src, article)
                if img_path and img_path.exists():
                    try:
                        image_map[src] = self.upload_image(page, img_path.read_bytes())
                    except ToutiaoError:
                        pass  # 单张图失败不影响存草稿，正文会跳过这张图

            body_html = toutiao_render.to_toutiao_html(article.get("body", ""), image_map)
            if len(body_html) < 30:
                raise ToutiaoError("正文转出来太短，头条可能判低质，先检查稿件正文")

            # 3) 页面上下文调发布接口：save=0 存草稿，借风控 SDK 自动签名
            payload = self._build_publish_payload(title, body_html, cover_cred)
            result = page.evaluate(
                """
                async (params) => {
                  const body = new URLSearchParams();
                  for (const [k, v] of Object.entries(params)) body.append(k, v);
                  const r = await fetch(
                    '/mp/agw/article/publish?source=mp&type=article&aid=1231&mp_publish_ab_val=0',
                    {method:'POST', credentials:'include',
                     headers:{'Content-Type':'application/x-www-form-urlencoded'},
                     body: body.toString()});
                  return await r.json();
                }
                """,
                payload,
            )
            ctx.close()

        if not isinstance(result, dict) or result.get("code") != 0:
            raise ToutiaoError(f"头条存草稿失败：{result}")
        pgc_id = str((result.get("data") or {}).get("pgc_id") or "")
        return {
            "ok": True,
            "pgc_id": pgc_id,
            "draft_url": f"{TT_BASE}/profile_v4/graphic/publish?pgc_id={pgc_id}" if pgc_id else "",
            "title": title,
        }

    def _upload_cover(self, page: Any, image_bytes: bytes) -> dict[str, Any] | None:
        """上传封面图（upload_source=20020003）。失败返回 None，草稿仍可存。"""
        import base64

        b64 = base64.b64encode(image_bytes).decode("ascii")
        js = """
        async (b64) => {
          const bin = atob(b64);
          const buf = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
          const fd = new FormData();
          fd.append('image', new Blob([buf], {type:'image/jpeg'}), 'cover.jpg');
          const r = await fetch('/spice/image?upload_source=20020003&aid=1231'
                                + '&device_platform=web&need_cover_url=1',
                                {method:'POST', body:fd, credentials:'include'});
          return await r.json();
        }
        """
        try:
            payload = page.evaluate(js, b64)
            if isinstance(payload, dict) and payload.get("code") == 0:
                return payload.get("data") or None
        except Exception:
            pass
        return None

    def _resolve_body_image(self, src: str, article: dict[str, Any]) -> Path | None:
        """正文 img src → 本地文件。支持相对路径与 /cover/<id>。"""
        if src.startswith("/cover/"):
            article_id = src.split("/")[2].split("?")[0]
            rel = next(
                (a.get("cover") for a in [article] if a.get("id") == article_id), None
            )
            return local_image_path(rel or "")
        if src.startswith(("http://", "https://")):
            return None  # 外链图不下载，正文渲染时会丢掉
        p = ROOT / src.lstrip("/")
        return p if p.exists() else None

    def _build_publish_payload(
        self, title: str, body_html: str, cover_cred: dict[str, Any] | None
    ) -> dict[str, str]:
        """构造发布接口最小字段集（实测可用，字段集中在这里方便改版）。"""
        import random

        covers = []
        if cover_cred:
            covers.append({
                "url": cover_cred.get("image_url", ""),
                "uri": cover_cred.get("image_uri", ""),
                "web_uri": cover_cred.get("image_uri", ""),
                "ic_uri": "",
                "width": 960, "height": 540,
                "thumb_width": 960, "thumb_height": 540,
                "id": 0, "pos": 0,
            })
        payload: dict[str, Any] = {
            "pgc_id": "0",                    # 0=新建
            "source": "29",
            "title": title,
            "content": body_html,
            "save": "0",                      # 存草稿，不发布——绝不改成 1
            "search_creation_info": json.dumps(
                {"searchTopOne": 0, "abstract": "", "clue_id": ""}, ensure_ascii=False
            ),
            "title_id": f"{int(time.time() * 1000)}_{random.randint(10**15, 10**16 - 1)}",
            "mp_editor_stat": "{}",
            "is_refute_rumor": "0",
            "timer_status": "0",
            "timer_time": "",
            "educluecard": "",
            "draft_form_data": json.dumps(
                {"coverType": 1 if covers else 3}, ensure_ascii=False
            ),
            "article_ad_type": "3",
            "is_fans_article": "0",
            "govern_forward": "0",
            "praise": "0",
            "disable_praise": "0",
            "tree_plan_article": "0",
            "activity_tag": "0",
            "trends_writing_tag": "0",
            "claim_exclusive": "0",
            "extra": json.dumps({
                "content_source": 100000000402,
                "content_word_cnt": len(body_html),
                "is_multi_title": 0,
                "sub_titles": [],
                "gd_ext": {
                    "entrance": "", "from_page": "publisher_mp",
                    "enter_from": "PC", "device_platform": "mp", "is_message": 0,
                },
            }, ensure_ascii=False),
        }
        if covers:
            payload["pgc_feed_covers"] = json.dumps(covers, ensure_ascii=False)
        return {k: str(v) for k, v in payload.items()}
