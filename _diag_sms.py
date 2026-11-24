"""只读诊断：探查头条/百家号短信验证码登录表单的真实选择器。"""
import sys

sys.path.insert(0, "/opt/auto-content")
from playwright.sync_api import sync_playwright

import toutiao_publish as tt
import baijia_publish as bj


def dump(page, tag):
    print("\n======= %s =======" % tag)
    print("URL", page.url)
    print("--- INPUTS ---")
    for i in page.locator("input").all():
        try:
            print(" input", {
                "type": i.get_attribute("type"),
                "placeholder": i.get_attribute("placeholder"),
                "name": i.get_attribute("name"),
                "id": i.get_attribute("id"),
                "class": i.get_attribute("class"),
                "visible": i.is_visible(),
                "html": (i.evaluate("e => e.outerHTML") or "")[:220],
            })
        except Exception as exc:
            print(" input err", str(exc)[:60])
    print("--- BUTTONS ---")
    for b in page.locator("button, [role=button]").all():
        try:
            print(" btn", repr(b.inner_text()[:24]),
                  "class=", b.get_attribute("class"),
                  "visible=", b.is_visible())
        except Exception:
            pass
    print("--- KEY TEXT COUNTS ---")
    for kw in ["验证码登录", "获取验证码", "发送验证码", "密码登录", "扫码登录", "登录/注册百家号"]:
        print(" text", kw, "count", page.get_by_text(kw, exact=False).count())


with sync_playwright() as pw:
    ctx = tt.ToutiaoPublisher(headless=True)._launch(pw)
    p = ctx.pages[0] if ctx.pages else ctx.new_page()
    p.goto(tt.TT_BASE + "/profile_v4/graphic/publish", timeout=30000)
    p.wait_for_timeout(4000)
    dump(p, "TOUTIAO_BEFORE")
    for sel in ["text=验证码登录", "span:has-text('验证码登录')",
                "div:has-text('验证码登录')"]:
        try:
            loc = p.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                print("CLICKED toutiao tab:", sel)
                p.wait_for_timeout(2500)
                dump(p, "TOUTIAO_AFTER")
                break
        except Exception as exc:
            print(" toutiao tab fail", sel, str(exc)[:60])
    ctx.close()

    ctx = bj.BaijiaPublisher(headless=True)._launch(pw)
    p = ctx.pages[0] if ctx.pages else ctx.new_page()
    # 编辑页未登录会跳登录页且 load 事件不触发，直接打登录页 + domcontentloaded
    p.goto(bj.LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    p.wait_for_timeout(4000)
    dump(p, "BAIJIA_LANDING")
    # 落地页要先点「登录/注册百家号」才展开手机号表单
    for sel in ["button:has-text('登录/注册百家号')", "text=登录/注册百家号",
                "button:has-text('登录')"]:
        try:
            loc = p.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1500):
                loc.click(timeout=3000, force=True)
                p.wait_for_timeout(3000)
                dump(p, "BAIJIA_AFTER_LOGIN_CLICK")
                break
        except Exception as exc:
            print(" bj click fail", sel, str(exc)[:80])
    ctx.close()
print("DONE")
