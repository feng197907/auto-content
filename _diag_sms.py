"""只读诊断 v2：百家号登录页 DOM 全探。

上一版只 dump 主 frame 的 input/button，遇到两种情况会全空：
  1) 直打登录页 + domcontentloaded 只等 4 秒，JS bundle 还没渲染；
  2) 登录表单嵌在跨域 iframe 里，page.locator('input') 根本看不到。
本版改：两条路径各探一次（直打登录页 / 从编辑页跳转），各等 8-10 秒，
遍历 page.frames 里每个 frame，并 dump 页面 HTML 摘要判断是否被风控拦截。
只读，不改任何项目文件。
"""
import sys

sys.path.insert(0, "/opt/auto-content")
from playwright.sync_api import sync_playwright

import baijia_publish as bj


def _brief(text, n=500):
    return (text or "").replace("\n", "|")[:n]


def dump_frame(frame, tag):
    print("\n---- FRAME [%s] url=%s ----" % (tag, frame.url))
    try:
        txt = frame.locator("body").inner_text(timeout=3000)
    except Exception:
        txt = ""
    print("  TEXT:", _brief(txt))
    inputs = frame.locator("input").all()[:20]
    print("  INPUT_COUNT(total):", frame.locator("input").count())
    for i in inputs:
        try:
            print("  INPUT", {
                "type": i.get_attribute("type"),
                "placeholder": i.get_attribute("placeholder"),
                "name": i.get_attribute("name"),
                "id": i.get_attribute("id"),
                "class": i.get_attribute("class"),
                "visible": i.is_visible(),
            })
        except Exception as exc:
            print("  INPUT_ERR", str(exc)[:60])
    print("  BUTTON_COUNT(total):", frame.locator("button, [role=button]").count())
    for b in frame.locator("button, [role=button]").all()[:30]:
        try:
            label = (b.inner_text() or "").strip()[:24]
            if label:
                print("  BTN", repr(label), "visible=", b.is_visible())
        except Exception:
            pass


def dump_page(page, tag):
    print("\n======= %s =======" % tag)
    print("URL", page.url)
    try:
        html = page.content()
        print("HTML_LEN", len(html))
        print("HTML_HEAD", _brief(html, 400))
    except Exception as exc:
        print("HTML_ERR", str(exc)[:80])
    print("FRAME_COUNT", len(page.frames))
    for idx, frame in enumerate(page.frames):
        try:
            dump_frame(frame, "%s#%d" % (tag, idx))
        except Exception as exc:
            print("  FRAME_ERR", idx, str(exc)[:80])


def probe(pw, url, tag, wait_ms):
    ctx = bj.BaijiaPublisher(headless=True)._launch(pw)
    p = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        p.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        print(tag, "GOTO_ERR", str(exc)[:120])
    p.wait_for_timeout(wait_ms)
    dump_page(p, tag)
    # 落地页若有「登录/注册百家号」入口，点开后再探一次（表单可能点后才渲染）
    for sel in ["text=登录/注册百家号", "button:has-text('登录')",
                "text=立即登录", "text=登录"]:
        try:
            loc = p.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1500):
                print("TRY_CLICK", tag, sel)
                loc.click(timeout=4000, force=True)
                p.wait_for_timeout(5000)
                dump_page(p, tag + "_AFTER_CLICK")
                break
        except Exception as exc:
            print("CLICK_FAIL", sel, str(exc)[:80])
    ctx.close()


with sync_playwright() as pw:
    probe(pw, bj.LOGIN_URL, "BJ_DIRECT_LOGIN", 10000)
    probe(pw, bj.EDIT_URL, "BJ_VIA_EDIT", 8000)
print("DONE")
