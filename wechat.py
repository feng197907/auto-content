"""把稿件 HTML 转成公众号能保留样式的富文本。

关键背景：公众号编辑器**只保留内联 style**，会丢掉 <style> 标签、class 和
外部 CSS。所以「复制成品」如果只给纯文本，粘过去就只剩光秃秃的段落——
小标题的竖线、加粗、列表圆点全都没了。

这里把 h2/p/li 统一翻译成带 style 的 <p>/<section>，产出可以直接塞进
剪贴板 text/html 的片段，粘贴到公众号后台即所见即所得。

用法：
    from wechat import to_wechat_html, to_plain_text
    html = to_wechat_html(article["body"])
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# 正文 17px / 行高 1.95 / 字距 .4px —— 手机上升一档更好读，段落间距给足
H_STYLE = (
    "font-size:19px;font-weight:700;color:#111827;line-height:1.6;"
    "margin:30px 0 14px;padding-left:10px;border-left:3px solid #1d4ed8;"
)
P_STYLE = (
    "font-size:17px;line-height:1.95;color:#1f2937;letter-spacing:.4px;"
    "margin:0 0 20px;"
)
LI_STYLE = (
    "font-size:17px;line-height:1.95;color:#1f2937;letter-spacing:.4px;"
    "margin:0 0 10px;padding-left:6px;"
)
A_STYLE = "color:#1d4ed8;text-decoration:none;"
CODE_STYLE = (
    "background:#f3f4f6;border-radius:4px;padding:1px 5px;"
    "font-size:14px;color:#b91c1c;"
)
QUOTE_STYLE = (
    "margin:0 0 18px;padding:12px 16px;background:#f8fafc;"
    "border-left:3px solid #cbd5e1;color:#475569;font-size:15px;line-height:1.85;"
)
# 命令块：公众号不支持 <pre> 的滚动/等宽继承，改成深色 section + 等宽字体 + <br> 换行
PRE_STYLE = (
    "margin:0 0 18px;padding:14px 16px;background:#0f172a;color:#e2e8f0;"
    "border-radius:8px;font-family:Consolas,Menlo,Monaco,monospace;"
    "font-size:13px;line-height:1.95;letter-spacing:0;word-break:break-all;"
)

_CLOSE_TAG = {
    "h1": "p",
    "h2": "p",
    "h3": "p",
    "h4": "p",
    "p": "p",
    "ul": "section",
    "ol": "section",
    "li": "p",
    "strong": "strong",
    "b": "strong",
    "em": "em",
    "i": "em",
    "a": "a",
    "code": "span",
    "pre": "section",
    "blockquote": "section",
}


class _Converter(HTMLParser):
    """把一段 HTML 重写成全内联样式版本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4"):
            self.parts.append(f'<p style="{H_STYLE}">')
            self.stack.append("p")
        elif tag == "p":
            self.parts.append(f'<p style="{P_STYLE}">')
            self.stack.append("p")
        elif tag in ("ul", "ol"):
            self.parts.append('<section style="margin:0 0 18px;">')
            self.stack.append("section")
        elif tag == "li":
            # 不用 <ul><li>：公众号对列表标签的清洗规则不稳定，圆点会丢
            self.parts.append(f'<p style="{LI_STYLE}">•&nbsp;')
            self.stack.append("p")
        elif tag in ("strong", "b"):
            self.parts.append("<strong>")
            self.stack.append("strong")
        elif tag in ("em", "i"):
            self.parts.append("<em>")
            self.stack.append("em")
        elif tag == "a":
            href = (attr.get("href") or "").strip()
            if href.startswith(("http://", "https://")):
                self.parts.append(f'<a href="{escape(href)}" style="{A_STYLE}">')
                self.stack.append("a")
            else:
                self.parts.append("<span>")
                self.stack.append("span")
        elif tag == "code":
            if self.in_pre:
                return  # pre 里已有等宽样式，code 只当容器
            self.parts.append(f'<span style="{CODE_STYLE}">')
            self.stack.append("span")
        elif tag == "pre":
            self.parts.append(f'<section style="{PRE_STYLE}">')
            self.stack.append("section")
            self.in_pre = True
        elif tag == "blockquote":
            self.parts.append(f'<section style="{QUOTE_STYLE}">')
            self.stack.append("section")
        elif tag == "br":
            self.parts.append("<br>")
        # 其余标签（div/section/span/font…）丢掉外壳，只保留里面的文字

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self.in_pre:
            return  # pre 内的 code 没有单独开标签
        if tag == "pre":
            self.in_pre = False
        close = _CLOSE_TAG.get(tag)
        if not close:
            return
        if self.stack and self.stack[-1] == close:
            self.parts.append(f"</{close}>")
            self.stack.pop()
            return
        # 标签嵌套不规范时，关到最近的同名块为止，避免产出错位 HTML
        if close in self.stack:
            while self.stack and self.stack[-1] != close:
                self.parts.append(f"</{self.stack.pop()}>")
            if self.stack:
                self.parts.append(f"</{close}>")
                self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self.in_pre:
            # 代码块靠 <br> 换行：公众号里 <pre> 的换行会被吃掉
            self.parts.append(escape(data, quote=False).replace("\n", "<br>"))
            return
        self.parts.append(escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)


def _wrap_plain_text(text: str) -> str:
    """老稿/纯文本：按空行切段，每段包一层 <p>。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = "<br>".join(escape(line, quote=False) for line in block.splitlines())
        out.append(f'<p style="{P_STYLE}">{lines}</p>')
    return "".join(out)


def to_wechat_html(body: str) -> str:
    """稿件正文 → 公众号可直接粘贴的富文本片段。"""
    if not body or not body.strip():
        return ""

    raw = body.strip()
    if "<" not in raw:
        return _wrap_plain_text(raw)

    parser = _Converter()
    parser.feed(raw)
    parser.close()
    html = parser.result()

    # 清掉空段落（AI 有时会留下空 <p>，粘过去会出现大段空隙）
    html = re.sub(
        r'<p style="[^"]*">(?:\s|&nbsp;|<br>)*</p>',
        "",
        html,
    )
    return html.strip()


def to_plain_text(body: str) -> str:
    """正文 → 纯文本（复制到不认格式的平台时用）。"""
    if not body:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", body)
    text = re.sub(r"</(p|h1|h2|h3|h4|li|blockquote|div|section)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
