"""微信公众号草稿箱发布。

背景：现在成品是靠「复制成品」粘到公众号网页编辑器，最后一步始终要人操作。
这里用官方草稿箱接口把「标题 + 正文 + 封面」直接推进后台，人只需进后台
预览、点发表。

能做什么、不能做什么（2026-11 核实，别被旧教程误导）：
  能：取 access_token、上传素材、draft/add 建草稿。**未认证的个人订阅号也可以**，
      实测额度 1000 次/日。个人主体 2021 年起就不能做微信认证，所以"等认证了再说"
      这条路对个人号是走不通的，不必等。
  不能：freepublish（群发/发布）返回 48001。2025-07 起微信回收了个人主体、企业未认证
      主体的发布接口权限——最后那一下"发表"必须在后台手动点，这不是代码没写好。
      另外 2026-04《运营规范》明确禁止用脚本/AI 全自动替代真人发布，
      所以"自动进草稿 + 人工审核 + 人工点发表"既是被权限限制的结果，也是合规的做法。

三个前置条件，缺一个都推不进去（代码里会明确报出来是哪一条）：
  1. .env 里配 WECHAT_APPID / WECHAT_SECRET。
     注意 2025-12-01 起入口搬家了：不在 mp.weixin.qq.com 的「基本配置」，改到
     developers.weixin.qq.com/platform →「我的业务」→ 选公众号 → 基础信息/开发密钥。
     AppSecret 只显示一次，忘了只能重置（重置会让别处的旧密钥立刻失效）。
  2. IP 白名单加服务器出口 IP（当前 82.157.4.72），否则取 token 就返回 40164。
     微信在换 token 那一步就校验来源 IP，配错了会误判成"账号没权限"。
  3. 账号已完成管理员实名。个人号要的是实名，不是微信认证，别被提示带偏。

用法：
    python publish.py --check          # 自检：凭据配了没、token 能不能取
    python publish.py --draft <id>     # 把队列里某篇推进草稿箱
    from publish import publish_article
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

import config

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "data" / "wechat_token.json"
API = "https://api.weixin.qq.com/cgi-bin"

# 常见错误码翻成人话——报错时直接说怎么办，不甩一堆数字
ERR_HINTS = {
    40001: "AppSecret 不对，或 access_token 已失效。检查 .env 里的 WECHAT_SECRET",
    40013: "AppID 不对。检查 .env 里的 WECHAT_APPID",
    40125: "AppSecret 无效。去公众号后台「基本配置」重新生成一个",
    40164: "调用 IP 不在白名单。把服务器出口 IP 加到后台「基本配置 → IP 白名单」",
    41001: "缺少 access_token 参数",
    42001: "access_token 过期，重试即可（代码会自动刷新）",
    45009: "接口调用次数超了（每日限额），明天再试",
    48001: "接口未授权。草稿箱接口未认证号也能用；若出现在发布接口，那是个人号没有发布权限（正常）",
    48002: "API 已禁用，检查公众号是否被封禁或权限被回收",
    53404: "账号存在违规，接口被限制",
    45003: "标题超长（限制 64 字）",
    45004: "摘要超长（限制 120 字）",
}


class PublishError(RuntimeError):
    """发布失败，消息已经是给人看的话。"""


def configured() -> bool:
    return bool(config.WECHAT_APPID and config.WECHAT_SECRET)


def _explain(payload: dict[str, Any]) -> str:
    """把微信的错误返回翻译成一句能照着做的提示。"""
    code = payload.get("errcode")
    msg = payload.get("errmsg", "")
    hint = ERR_HINTS.get(code)
    if hint:
        return f"{hint}（errcode {code}: {msg}）"
    return f"微信接口报错 errcode {code}: {msg}"


def _load_cached_token() -> str:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    # 提前 5 分钟过期，避免边界时刻刚好失效
    if data.get("appid") != config.WECHAT_APPID:
        return ""
    if float(data.get("expires_at", 0)) - 300 > time.time():
        return str(data.get("access_token", ""))
    return ""


def get_access_token(force: bool = False) -> str:
    """取 access_token，带本地缓存（有效期 7200 秒，每天调用次数有限，必须缓存）。"""
    if not configured():
        raise PublishError(
            "还没配公众号凭据。在 .env 里加 WECHAT_APPID 和 WECHAT_SECRET"
            "（公众号后台「设置与开发 → 基本配置」里能看到）"
        )

    if not force:
        cached = _load_cached_token()
        if cached:
            return cached

    resp = requests.get(
        f"{API}/token",
        params={
            "grant_type": "client_credential",
            "appid": config.WECHAT_APPID,
            "secret": config.WECHAT_SECRET,
        },
        timeout=20,
    )
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise PublishError(_explain(payload))

    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(
            json.dumps({
                "appid": config.WECHAT_APPID,
                "access_token": token,
                "expires_at": time.time() + int(payload.get("expires_in", 7200)),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass  # 缓存写不进去不影响使用，下次重新取
    return token


def upload_thumb(image_path: str | Path, token: str = "") -> str:
    """把封面图作为永久素材上传，返回 thumb_media_id。"""
    path = Path(image_path)
    if not path.exists():
        raise PublishError(f"封面图不存在：{path}")

    token = token or get_access_token()
    with path.open("rb") as fh:
        resp = requests.post(
            f"{API}/material/add_material",
            params={"access_token": token, "type": "thumb"},
            files={"media": (path.name, fh, "image/jpeg")},
            timeout=60,
        )
    payload = resp.json()
    media_id = payload.get("media_id")
    if not media_id:
        raise PublishError(_explain(payload))
    return str(media_id)


def create_draft(
    title: str,
    content: str,
    thumb_media_id: str,
    author: str = "",
    digest: str = "",
    source_url: str = "",
    token: str = "",
) -> str:
    """新建一条草稿，返回草稿 media_id。"""
    if not title.strip():
        raise PublishError("标题是空的，公众号不接受")
    if not content.strip():
        raise PublishError("正文是空的，公众号不接受")

    token = token or get_access_token()
    article = {
        # 官方文档：标题总长度不超过 32 字（旧文档写 64，按严的来）。
        # 我们的标题规则是 12-20 字，正常不会碰到；这里只做兜底防 45003。
        "title": title[:32],
        "author": author[:16],
        "digest": (digest or "")[:120],
        "content": content,
        "content_source_url": source_url,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    body = json.dumps(
        {"articles": [article]},
        ensure_ascii=False,  # 必须不转义，否则中文进后台会变 \uXXXX
    ).encode("utf-8")

    resp = requests.post(
        f"{API}/draft/add",
        params={"access_token": token},
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=60,
    )
    payload = resp.json()
    media_id = payload.get("media_id")
    if not media_id:
        raise PublishError(_explain(payload))
    return str(media_id)


def make_digest(body_html: str, limit: int = 100) -> str:
    """从正文里取第一段当摘要，公众号后台不填摘要时也会自动截，这里先截好。"""
    import re

    text = re.sub(r"<[^>]+>", "", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def publish_article(article: dict[str, Any]) -> dict[str, Any]:
    """把一篇稿件推进公众号草稿箱。

    返回 {media_id, thumb_media_id, title}；任何一步失败抛 PublishError。
    """
    import wechat

    title = (article.get("title") or "").strip()
    body_html = wechat.to_wechat_html(article.get("body") or "")
    if not body_html:
        raise PublishError("正文转换后是空的，检查稿件内容")

    token = get_access_token()

    cover = article.get("cover") or ""
    cover_path = ROOT / cover if cover and not os.path.isabs(cover) else Path(cover)
    if not cover or not cover_path.exists():
        raise PublishError("这篇还没有封面图。先在审核页生成头图，再推到草稿箱")

    thumb_media_id = upload_thumb(cover_path, token)
    media_id = create_draft(
        title=title,
        content=body_html,
        thumb_media_id=thumb_media_id,
        author=getattr(config, "SITE_AUTHOR", ""),
        digest=make_digest(body_html),
        source_url=(article.get("url") or "")[:1024],
        token=token,
    )
    return {
        "media_id": media_id,
        "thumb_media_id": thumb_media_id,
        "title": title,
    }


def check() -> list[tuple[str, bool, str]]:
    """自检：凭据、token、素材接口权限。返回 [(检查项, 是否通过, 说明)]。"""
    rows: list[tuple[str, bool, str]] = []

    rows.append((
        "AppID / AppSecret",
        configured(),
        "已配置" if configured() else "没配。在 .env 里加 WECHAT_APPID 和 WECHAT_SECRET",
    ))
    if not configured():
        return rows

    try:
        token = get_access_token(force=True)
        rows.append(("取 access_token", True, f"成功，长度 {len(token)}"))
    except PublishError as exc:
        rows.append(("取 access_token", False, str(exc)))
        return rows

    # 素材接口 + 草稿箱接口各探一次：这是两个独立接口，权限各自算账，
    # 只验 token 通过不代表能建草稿（实测最容易踩的就是这个差别）。
    try:
        resp = requests.get(
            f"{API}/material/get_materialcount",
            params={"access_token": token},
            timeout=20,
        )
        payload = resp.json()
        if payload.get("errcode"):
            rows.append(("素材接口权限", False, _explain(payload)))
        else:
            rows.append((
                "素材接口权限",
                True,
                f"正常，现有素材 图片{payload.get('image_count', 0)} "
                f"视频{payload.get('video_count', 0)}",
            ))
    except Exception as exc:  # noqa: BLE001
        rows.append(("素材接口权限", False, str(exc)[:120]))

    try:
        resp = requests.get(
            f"{API}/draft/count",
            params={"access_token": token},
            timeout=20,
        )
        payload = resp.json()
        if payload.get("errcode"):
            rows.append(("草稿箱接口权限", False, _explain(payload)))
        else:
            rows.append((
                "草稿箱接口权限",
                True,
                f"正常，现有草稿 {payload.get('total_count', 0)} 条",
            ))
    except Exception as exc:  # noqa: BLE001
        rows.append(("草稿箱接口权限", False, str(exc)[:120]))

    return rows


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        results = check()
        width = max(len(r[0]) for r in results) + 2
        for name, ok, note in results:
            print(f"{name.ljust(width)} {'OK' if ok else 'FAIL':<5} {note}")
        print()
        print("全部通过" if all(r[1] for r in results) else "有未通过的项，按上面的说明处理")
    elif "--draft" in sys.argv:
        from pipeline import load_queue

        target = sys.argv[sys.argv.index("--draft") + 1]
        queue = load_queue()
        hit = next((a for a in queue if a["id"] == target), None)
        if not hit:
            print(f"没找到稿件 {target}")
            raise SystemExit(1)
        try:
            result = publish_article(hit)
            print(f"已推进草稿箱：{result['title']}（草稿 media_id {result['media_id']}）")
        except PublishError as exc:
            print(f"失败：{exc}")
            raise SystemExit(1)
    else:
        print("用法：python publish.py --check | --draft <稿件id>")
