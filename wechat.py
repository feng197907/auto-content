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
from typing import Any

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
    html = html.strip()

    # 可选增强：裸文本包 <span leaf="">（config.WECHAT_LEAF_WRAP 打开才生效）
    import config
    if getattr(config, "WECHAT_LEAF_WRAP", False):
        html = wrap_leaf(html)
    return html


# ---------------------------------------------------------------------------
# 排版校验：公众号会过滤哪些东西，这里逐条查一遍
#
# 参考 isjiamu/gzh-design-skill 的做法：公众号编辑器会丢弃 <style>、class、
# grid/position/flex 这类 CSS，只保留写在标签上的内联 style。以前是"粘完用
# 肉眼检查"，现在把检查前置到页面里，不合格直接标出来。
# ---------------------------------------------------------------------------

# 出现即判 error：粘过去这些内容一定消失，甚至带坏整段排版
BLOCKED_TAGS = {
    "style": "公众号整段丢弃 <style>，样式必须写在标签的 style=\"\" 里",
    "script": "脚本会被过滤",
    "iframe": "iframe 会被过滤，视频要用公众号自带的视频组件",
    "link": "外链 CSS 会被丢弃",
    "meta": "meta 会被丢弃",
    "form": "表单元素会被过滤",
    "input": "表单元素会被过滤",
    "button": "按钮会被过滤，改成普通段落",
    "video": "video 标签会被过滤，视频要用公众号视频组件",
    "audio": "audio 标签会被过滤",
}

# 能用但不稳：判 warn，粘过去大概率还行，但可能被清洗
RISKY_TAGS = {
    "div": "公众号对 <div> 的清洗不稳定，建议改用 <section>",
    "table": "表格粘过去容易变形，建议改成列表或截图",
}

# 这些 CSS 属性公众号会过滤掉，写了等于没写
BLOCKED_CSS = (
    "position:fixed",
    "position: fixed",
    "position:absolute",
    "position: absolute",
    "display:grid",
    "display: grid",
    "display:flex",
    "display: flex",
    "float:",
    "z-index",
)

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(/?)>")
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "area", "col"}


def _attrs_of(raw: str) -> dict[str, str]:
    """把标签里的属性粗解析成 dict，只用于检查，不追求完备。"""
    found: dict[str, str] = {}
    for m in re.finditer(r'([a-zA-Z_:][-\w:.]*)\s*=\s*"([^"]*)"', raw):
        found[m.group(1).lower()] = m.group(2)
    return found


def validate_html(html: str) -> list[dict[str, str]]:
    """检查富文本片段能不能安全粘进公众号。

    返回问题清单，每条 {level, code, message, sample}。
    level=error 表示粘过去一定掉格式，warn 表示可能被清洗。
    """
    issues: list[dict[str, str]] = []

    def add(level: str, code: str, message: str, sample: str = "") -> None:
        issues.append({
            "level": level,
            "code": code,
            "message": message,
            "sample": sample[:60],
        })

    if not html or not html.strip():
        add("error", "empty", "内容为空，没有可粘贴的正文")
        return issues

    if "<" not in html:
        add("warn", "no-html", "内容是纯文本，粘贴后没有任何排版")
        return issues

    # 1) 标签黑名单 / 风险标签
    stack: list[str] = []
    unbalanced: list[str] = []
    for m in _TAG_RE.finditer(html):
        closing, tag, raw_attrs, self_close = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag in BLOCKED_TAGS and not closing:
            add("error", f"blocked-tag-{tag}", BLOCKED_TAGS[tag], m.group(0))
        if tag in RISKY_TAGS and not closing:
            add("warn", f"risky-tag-{tag}", RISKY_TAGS[tag], m.group(0))

        if tag in _VOID_TAGS or self_close:
            continue
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
            elif tag in stack:
                # 关到最近的同名标签，中间那些就漏关了
                while stack and stack[-1] != tag:
                    unbalanced.append(stack.pop())
                if stack:
                    stack.pop()
            else:
                unbalanced.append(f"</{tag}>")
        else:
            stack.append(tag)
    unbalanced.extend(stack)
    if unbalanced:
        add(
            "error",
            "unbalanced",
            f"标签不配平，粘过去可能整段错位：{', '.join(sorted(set(unbalanced))[:4])}",
        )

    # 2) 内联样式：p 没有 style 就是白排版
    for m in re.finditer(r"<p(\s[^>]*)?>", html):
        attrs = _attrs_of(m.group(1) or "")
        if not attrs.get("style"):
            add("error", "p-no-style", "有段落没写内联 style，粘过去会变成无样式纯文本", m.group(0))
            break

    # 3) class / id 依赖外部样式，公众号直接丢
    if ' class="' in html:
        add("error", "class-attr", "存在 class 属性，公众号会丢弃，样式必须写在 style 里")
    if ' id="' in html:
        add("warn", "id-attr", "存在 id 属性，公众号会丢弃（不影响排版）")

    # 4) 会被过滤的 CSS
    style_text = " ".join(re.findall(r'style="([^"]*)"', html)).replace(" ", "").lower()
    hits = [css for css in BLOCKED_CSS if css.replace(" ", "") in style_text]
    if hits:
        add("warn", "blocked-css", f"使用了公众号会过滤的 CSS：{', '.join(hits[:3])}")

    # 5) 图片必须是 https，http 图片在公众号里可能不显示
    for m in re.finditer(r'<img[^>]*src="([^"]*)"', html):
        if not m.group(1).startswith("https://"):
            add("warn", "img-not-https", "图片不是 https 地址，公众号里可能加载不出来", m.group(1))
            break

    # 6) 空段落（粘过去会出现大段空白）
    if re.search(r"<p[^>]*>(?:\s|&nbsp;|<br>)*</p>", html):
        add("warn", "empty-p", "存在空段落，粘过去会出现多余空隙")

    return issues


def validate_source(body: str) -> list[dict[str, str]]:
    """检查原始正文里有没有「转换时会被丢掉」的东西。

    validate_html 查的是转换产物（一定是干净的），查不出源头的样式丢失。
    常见情况：从别的公众号/网页复制一段富文本粘进 textarea，带着 <style>
    和 class，转换器会把它们整段丢掉并统一改写——内容是保住了，但原作者
    的配色、字号、卡片样式全没了。这种该提前告诉人。
    """
    issues: list[dict[str, str]] = []
    if not body:
        return issues

    if re.search(r"<style[\s>]", body, re.I):
        issues.append({
            "level": "warn",
            "code": "src-style-tag",
            "message": "正文里带 <style> 标签，转换时会被丢弃，样式将统一改写成本站样式",
            "sample": "<style>",
        })
    if re.search(r'class\s*=', body, re.I):
        issues.append({
            "level": "warn",
            "code": "src-class",
            "message": "正文里带 class 属性，公众号不认，转换后这些类对应的样式会丢失",
            "sample": "class=",
        })
    if re.search(r"<div[\s>]", body, re.I):
        issues.append({
            "level": "warn",
            "code": "src-div",
            "message": "正文用了 <div> 布局，会被改写成 <section>，复杂卡片布局可能变形",
            "sample": "<div>",
        })
    return issues


def summarize_issues(issues: list[dict[str, str]]) -> dict[str, Any]:
    """把问题清单压成前端好用的计数。"""
    errors = sum(1 for i in issues if i["level"] == "error")
    warns = sum(1 for i in issues if i["level"] == "warn")
    return {"errors": errors, "warns": warns, "issues": issues}


class _LeafWrapper(HTMLParser):
    """把裸文本包进 <span leaf="">，公众号对裸文本的样式继承不稳定。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = "".join(
            f' {k}="{escape(v or "", quote=True)}"' for k, v in attrs
        )
        self.parts.append(f"<{tag}{attr}>")
        if tag == "pre":
            self.in_pre = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = "".join(
            f' {k}="{escape(v or "", quote=True)}"' for k, v in attrs
        )
        self.parts.append(f"<{tag}{attr}>")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag == "pre":
            self.in_pre = False

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self.in_pre or not data.strip():
            # 代码块里的空白有语义，不包；纯空白也不包
            self.parts.append(escape(data, quote=False))
            return
        self.parts.append(f'<span leaf="">{escape(data, quote=False)}</span>')


def wrap_leaf(html: str) -> str:
    """给裸文本加 <span leaf=""> 包裹（可选增强，默认不开）。"""
    if not html:
        return ""
    parser = _LeafWrapper()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


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
