"""头条号专用正文渲染：把稿件 body 转成头条编辑器认的「裸 HTML」。

为什么不能复用公众号的排版（2026-11 实测）：
  头条只保留 P / DIV / IMG / H3 / STRONG / UL / LI / BLOCKQUOTE / PRE，
  所有 style="" 全部吃掉，section/a/em 连标签带样式一起丢。
  公众号那套内联样式渐变、卡片底色、01/02 序号、细分隔线在头条 100% 失效，
  所以这里另起一条渲染分支：只输出头条认的结构标签，零内联样式。

图片：头条正文里不能出现外链图片（会报 7115 图片uri非法），必须先把图片
上传到头条 CDN 拿到 (image_url, image_uri, w, h)，再用带 web_uri 的 <img>。
本模块只做「结构渲染 + 图片占位」，上传由 toutiao_publish.py 负责并回填。

用法：
    to_toutiao_html(body)                       # 无图稿
    to_toutiao_html(body, image_map={...})      # 有图稿，image_map: 原src -> 上传凭据
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Any

# 头条回读后仍保留的标签（调研实测 2026-09）
_KEEP = {"p", "div", "img", "h3", "strong", "ul", "ol", "li", "blockquote", "pre", "br"}
_INLINE = {"strong", "b"}


class _ToutiaoConverter(HTMLParser):
    """把 body 里的 HTML 重写成头条裸标签，丢弃一切样式与不支持的标签。"""

    def __init__(self, image_map: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.image_map = image_map or {}
        self.quote_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4"):
            # 头条层级会被压平，统一用 h3 最稳（实测 H1/H2/H3 回读都变 H1）
            self.parts.append("<h3>")
            self.stack.append("h3")
        elif tag == "p":
            self.parts.append("<p>")
            self.stack.append("p")
        elif tag in ("ul", "ol"):
            self.parts.append(f"<{tag}>")
            self.stack.append(tag)
        elif tag == "li":
            self.parts.append("<li>")
            self.stack.append("li")
        elif tag in _INLINE:
            self.parts.append("<strong>")
            self.stack.append("strong")
        elif tag == "blockquote":
            self.parts.append("<blockquote>")
            self.stack.append("blockquote")
            self.quote_depth += 1
        elif tag == "pre":
            self.parts.append("<pre>")
            self.stack.append("pre")
        elif tag == "img":
            src = (attr.get("src") or "").strip()
            cred = self.image_map.get(src)
            if cred:
                self._emit_uploaded_img(cred)
            # 没有上传凭据的外链图：直接丢弃，绝不留在正文里（头条会报 7115）
        elif tag == "br":
            self.parts.append("<br>")
        # section / a / em / span / div / font 等一律丢外壳，只留里面文字

    def _emit_uploaded_img(self, cred: dict[str, Any]) -> None:
        url = str(cred.get("image_url") or "")
        uri = str(cred.get("image_uri") or "")
        if not (url and uri):
            return
        w = int(cred.get("image_width") or 960)
        h = int(cred.get("image_height") or 540)
        self.parts.append(
            '<div class="pgc-img">'
            f'<img src="{escape(url, quote=True)}" class="" ic-uri="" '
            f'image_type="image/jpeg" mime_type="" web_uri="{escape(uri, quote=True)}" '
            f'img_width="{w}" img_height="{h}">'
            '<p class="pgc-img-caption"></p>'
            "</div>"
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "blockquote" and self.quote_depth:
            self.quote_depth -= 1
        close = {
            "h1": "h3", "h2": "h3", "h3": "h3", "h4": "h3",
            "p": "p", "ul": "ul", "ol": "ol", "li": "li",
            "strong": "strong", "b": "strong", "blockquote": "blockquote", "pre": "pre",
        }.get(tag)
        if not close or not self.stack:
            return
        if self.stack[-1] == close:
            self.parts.append(f"</{close}>")
            self.stack.pop()
            return
        if close in self.stack:
            while self.stack and self.stack[-1] != close:
                self.parts.append(f"</{self.stack.pop()}>")
            if self.stack:
                self.parts.append(f"</{close}>")
                self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if "pre" in self.stack:
            self.parts.append(escape(data, quote=False).replace("\n", "<br>"))
        else:
            self.parts.append(escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)


def _strip_source_note(body: str) -> str:
    """去掉正文末尾的「来源/信息来源」段，避免把抓取痕迹带进头条。

    覆盖两种写法：markdown 加粗（**信息来源**：xxx）与 HTML 标签
    （<h2>信息来源</h2>）。要求独立成行 + 加粗/标题标签，正文中间的
    「据某站报道」这类引用不会命中，所以不需要按位置再卡阈值。
    """
    text = body or ""
    patterns = [
        r"\n\s*\*\*(?:信息来源与声明|信息来源|原文出处|参考资料|来源|出处)\*\*\s*[:：]?",
        r"\n\s*<\s*(?:h[1-6]|p|div)[^>]*>\s*(?:信息来源与声明|信息来源|原文出处|参考资料|来源|出处)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return text[: m.start()].rstrip()
    return text


def _wrap_plain_text(text: str) -> str:
    """纯文本（无 HTML 标签）：按空行切段，每段包 <p>，段内换行换 <br>。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = "<br>".join(escape(line, quote=False) for line in block.splitlines())
        out.append(f"<p>{lines}</p>")
    return "".join(out)


def to_toutiao_html(
    body: str, image_map: dict[str, dict[str, Any]] | None = None
) -> str:
    """稿件正文 → 头条可直接提交的裸 HTML（零内联样式）。

    image_map: 原图片 src -> /spice/image 上传返回的凭据字典，用于让图片合规落地。
    """
    if not body or not body.strip():
        return ""
    raw = _strip_source_note(body).strip()
    if "<" not in raw:
        return _wrap_plain_text(raw)
    parser = _ToutiaoConverter(image_map)
    parser.feed(raw)
    parser.close()
    html = parser.result()
    # 清掉空段落，避免头条编辑器出现大段空隙
    html = re.sub(r"<p>(?:\s|&nbsp;|<br>)*</p>", "", html)
    return html.strip()


def image_srcs(body: str) -> list[str]:
    """列出正文里所有 <img src>，供上传环节按顺序处理。"""
    if not body or "<img" not in body:
        return []
    found: list[str] = []

    class _ImgCollector(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "img":
                src = dict(attrs).get("src")
                if src and src not in found:
                    found.append(src)

    p = _ImgCollector()
    p.feed(body)
    p.close()
    return found
