"""Flask 审核后台：看板、改稿、一键生成、通过、成品预览、复制。

用法：
    py -3 app.py
    浏览器打开 http://localhost:5000
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template, request, Response, session

import config
import notify as notifier
from pipeline import (
    load_queue,
    save_queue,
    run as run_pipeline,
    optimize_article,
)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# 生成任务加锁，避免网页端重复点击导致队列写冲突
generate_lock = threading.Lock()


def _password_ok() -> bool:
    """检查请求是否通过了密码验证。

    支持 Basic Auth / query / JSON 三种入口。验证成功后写入 session（签名 cookie），
    之后同一浏览器内的所有操作（复制/预览/保存/删除/生成）自动免密，
    不再每次弹密码框。
    """
    password = config.APP_PASSWORD
    if not password:
        return True  # 没设密码则不限制（本地开发用）

    # 0. 本会话已经验证过（关键：后续 ajax 请求不再重复索要密码）
    if session.get("authed"):
        return True

    ok = False
    # 1. query 参数（微信推送链接里带 ?pwd=xxx，点开即进）
    if request.args.get("pwd") == password:
        ok = True
    # 2. HTTP Basic Auth（浏览器弹窗输入）
    if not ok:
        import base64

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                if decoded.split(":", 1)[-1] == password:
                    ok = True
            except (ValueError, UnicodeDecodeError):
                pass
    # 3. JSON body（网页内的 ajax 请求兜底）
    if not ok:
        data = request.get_json(silent=True) or {}
        if data.get("pwd") == password:
            ok = True

    # 任一入口通过 → 记住本次会话，后续不再要密码
    if ok:
        session["authed"] = True
    return ok


@app.before_request
def require_password() -> Response | None:
    """公网部署时的密码保护；没设密码则不限制。"""
    if _password_ok():
        return None
    return Response(
        "请输入访问密码",
        status=401,
        # realm 必须是 ASCII：Werkzeug 用 latin-1 编码 header，中文会抛
        # UnicodeEncodeError 导致请求挂起（生产环境设密码后实测踩到）。
        headers={"WWW-Authenticate": 'Basic realm="AutoContent Review"'},
    )


@app.template_filter("hot_fmt")
def hot_fmt(value: int) -> str:
    """把热度数字格式化成易读形式，如 1185万。"""
    if not value:
        return ""
    if value >= 10000:
        return f"{value / 10000:.0f}万"
    return str(value)


def strip_html(text: str) -> str:
    """把 HTML 转成可复制的纯文本。"""
    text = re.sub(r"</(p|li|h2|h3|ul)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def find_article(queue: list[dict[str, Any]], article_id: str) -> int:
    """按 id 找稿件下标，找不到返回 -1。"""
    for index, item in enumerate(queue):
        if item["id"] == article_id:
            return index
    return -1


def compute_stats(queue: list[dict[str, Any]]) -> dict[str, int]:
    """计算看板统计数据。"""
    counts = Counter(item["status"] for item in queue)
    today = datetime.now().date().isoformat()
    today_count = sum(
        1 for item in queue if item["created_at"][:10] == today
    )
    total = len(queue)
    approved = counts.get("approved", 0)
    return {
        "pending": counts.get("pending", 0),
        "approved": approved,
        "rejected": counts.get("rejected", 0),
        "all": total,
        "today": today_count,
        "rate": round(approved / total * 100) if total else 0,
    }


@app.route("/")
def index() -> str:
    """审核主页，按状态过滤显示稿件。"""
    queue = load_queue()
    status = request.args.get("status", "pending")
    if status != "all":
        queue = [item for item in queue if item["status"] == status]

    counts = compute_stats(load_queue())
    return render_template(
        "review.html",
        articles=queue,
        counts=counts,
        current=status,
        review_url=config.REVIEW_URL,
    )


@app.route("/generate", methods=["POST"])
def generate() -> Any:
    """网页端一键生成：按配置调用流水线，返回新增篇数。"""
    data = request.get_json(silent=True) or {}
    count = int(data.get("count", config.BATCH_SIZE))
    count = max(1, min(count, 20))  # 限制 1-20，防止误操作刷爆

    # 没填 API key 就自动走 demo，保证网页端永远能跑
    demo = not bool(config.AI_API_KEY)

    if not generate_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在生成中，请稍候"}), 409

    try:
        created = run_pipeline(count, demo)
        return jsonify({
            "ok": True,
            "created": len(created),
            "demo": demo,
        })
    finally:
        generate_lock.release()


@app.route("/optimize", methods=["POST"])
def optimize() -> Any:
    """AI优化：基于原稿真实素材重写，覆盖当前稿件（保持已通过状态）。

    与生成共用锁，避免并发写队列；AI 失败时原稿不变。
    """
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    if not article_id:
        return jsonify({"ok": False, "error": "缺少稿件 id"}), 400

    if not generate_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在生成或优化中，请稍候"}), 409

    try:
        updated = optimize_article(article_id)
    finally:
        generate_lock.release()

    if updated is None:
        return jsonify({
            "ok": False,
            "error": "优化失败：稿件不存在，或 AI 暂时不可用（限流/无 key），请稍后重试",
        })
    return jsonify({
        "ok": True,
        "id": updated["id"],
        "title": updated["title"],
        "body": updated["body"],
    })


@app.route("/notify", methods=["POST"])
def push_notify() -> Any:
    """把当前待审稿推送到微信。没配 key 时返回明确提示。"""
    data = request.get_json(silent=True) or {}
    demo = bool(data.get("demo", False))
    ok = notifier.notify(review_url=config.REVIEW_URL, demo=demo)
    if demo:
        return jsonify({"ok": True, "demo": True})
    if not ok:
        return jsonify({
            "ok": False,
            "error": "推送失败：检查 .env 的 SERVERCHAN_KEY 是否已配置",
        })
    return jsonify({"ok": True})


@app.route("/update", methods=["POST"])
def update() -> Any:
    """更新稿件标题、正文或状态。"""
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    queue = load_queue()
    index = find_article(queue, article_id)

    if index == -1:
        return jsonify({"ok": False, "error": "稿件不存在"}), 404

    old_status = queue[index].get("status")
    for field in ("title", "body", "status"):
        if field in data:
            queue[index][field] = data[field]
    queue[index]["updated_at"] = datetime.now().isoformat(timespec="seconds")

    # 真正切换到"已通过"时记录 approved_at 作为 30 天保留期起算点。
    # 已是 approved 的稿件再次编辑不刷新，避免频繁小改让它永远不过期。
    new_status = queue[index].get("status")
    if new_status == "approved" and old_status != "approved":
        queue[index]["approved_at"] = datetime.now().isoformat(timespec="seconds")

    save_queue(queue)
    return jsonify({"ok": True})


@app.route("/delete", methods=["POST"])
def delete() -> Any:
    """删除一篇稿件。"""
    data = request.get_json(silent=True) or {}
    queue = load_queue()
    index = find_article(queue, data.get("id", ""))

    if index == -1:
        return jsonify({"ok": False, "error": "稿件不存在"}), 404

    queue.pop(index)
    save_queue(queue)
    return jsonify({"ok": True})


@app.route("/plain/<article_id>")
def plain(article_id: str) -> Any:
    """返回可复制的纯文本成品。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404

    article = queue[index]
    content = f"{article['title']}\n\n{strip_html(article['body'])}"
    return jsonify({"title": article["title"], "content": content})


@app.route("/preview/<article_id>")
def preview(article_id: str) -> Any:
    """返回成品预览 HTML，模拟平台发布后的排版效果。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404

    article = queue[index]
    return render_template(
        "preview.html",
        title=article["title"],
        body=article["body"],
        platforms=config.TARGET_PLATFORMS,
        author=config.SITE_AUTHOR,
    )


if __name__ == "__main__":
    print(f"审核后台启动：http://localhost:{config.APP_PORT}")
    app.run(
        host=config.APP_HOST,
        port=config.APP_PORT,
        debug=False,
        threaded=True,
    )
