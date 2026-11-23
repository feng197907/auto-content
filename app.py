"""Flask 审核后台：看板、改稿、一键生成、通过、成品预览、复制。

用法：
    py -3 app.py
    浏览器打开 http://localhost:5000
"""

from __future__ import annotations

import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    Response,
    send_file,
    session,
)

import config
import analytics
import profile as profile_mod
import content_calendar
import cover as cover_mod
import wechat
import publish as publish_mod
import notify as notifier
from pipeline import (
    load_queue,
    save_queue,
    run as run_pipeline,
    optimize_article,
    regenerate_titles,
    apply_title,
    ai_tone_check,
    classify_column,
)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# 生成任务加锁，避免网页端重复点击导致队列写冲突
generate_lock = threading.Lock()

# 异步生成状态：网页点「生成」立即返回，后台线程慢慢写，前端轮询这个字典。
# 之所以要异步：一次生成多篇 AI 稿要几分钟，nginx 默认 60s 代理超时会掐断请求，
# 浏览器收到 504 就以为失败，但服务端其实还在写——用户重复点又会撞锁。
# 用进程内字典存状态足够（单进程 Flask + 单实例部署），服务重启后状态归零无副作用。
generate_state: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "created": 0,
    "demo": False,
    "error": "",
    "stage": "",       # 当前阶段（抓取选题 / 正在写第 N 篇 / 收尾），让用户看到活着
    "started_at": "",  # ISO 时间，前端算已用时
    "column": "all",   # 本次生成的目标栏目，前端可显示
}


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

    # 栏目筛选：github / ai_news / ai_sidebiz。空=全部栏目。
    # 老稿件没有 column 字段，这里用 classify_column 按标题关键词兜底归类，
    # 只在渲染副本上补，不回写队列。
    column = request.args.get("column", "").strip()
    if status == "all":
        # 「全部」tab 不参与栏目筛选：即使 URL 带了 column 也忽略，
        # 配合模板隐藏筛选栏，保证这一页看到的就是全部稿件。
        column = ""
    for item in queue:
        if not item.get("column"):
            item["column"] = classify_column(item)
    if column in config.COLUMN_LABELS:
        queue = [item for item in queue if item.get("column") == column]

    counts = compute_stats(load_queue())

    # 未读红点统计：只数 GitHub精选、待审核、带 unread 标记的稿。
    # 其他栏目不冒红点；老稿无该字段视为已读。
    # 用户在待审核 tab 看到红点，点「预览成品」后由 /mark-read 清掉。
    unread_count = sum(
        1 for a in load_queue()
        if a.get("status") == "pending"
        and a.get("unread")
        and a.get("column") == "github"
    )

    # 排版校验：把「粘到公众号会不会掉格式」提前算出来，显示在卡片上。
    # 查两层——转换产物（防转换器出 bug）+ 原始正文（防外部样式被静默丢弃）。
    # 只加在渲染用的副本里，不回写队列，避免数据污染。
    articles = []
    for item in queue:
        row = dict(item)
        body = item.get("body", "")
        checks = wechat.validate_source(body) + wechat.validate_html(wechat.to_wechat_html(body))
        row["fmt"] = wechat.summarize_issues(checks)
        row["tone"] = ai_tone_check(body)
        articles.append(row)

    # 日历：按日期分组 + 未来三周节日提醒（只展示与提醒，不改自动生成主链路）
    full_queue = load_queue()
    cal_queue = full_queue  # 日历看全部稿件，不受当前状态过滤影响
    return render_template(
        "review.html",
        articles=articles,
        counts=counts,
        daily_total=config.BATCH_SIZE,
        unread_count=unread_count,
        current=status,
        column=column,
        column_labels=config.COLUMN_LABELS,
        review_url=config.REVIEW_URL,
        publish_ready=publish_mod.configured(),
        analytics=analytics.summarize(),
        analytics_records=analytics.load_records(),
        profile=profile_mod.load(),
        profile_dims=profile_mod.DIMENSIONS,
        cal_days=content_calendar.group_by_date(cal_queue),
        cal_upcoming=content_calendar.upcoming(21),
        cal_suggestion=content_calendar.suggestion(),
        platforms=wechat.platform_list(),
        platform_metas={p: wechat.platform_meta(p) for p in wechat.platform_list()},
    )


def _column_quotas(column: str, count: int) -> dict[str, int] | None:
    """把网页选的栏目转成流水线配额。

    - all：返回 None，走旧混合抓稿逻辑（网页手动生成的默认行为）。
    - 某个栏目：返回 {栏目: count}，只抓这一类，生成 count 篇。
    """
    if column == "all" or not column:
        return None
    return {column: count}


def _generate_worker(count: int, demo: bool, column: str = "all") -> None:
    """后台线程：跑生成流水线，把进度/结果写回 generate_state。

    持锁贯穿整个生成过程（前端再点会收到 409），结束才释放。
    异常也写回状态，前端轮询能看到具体错误，不再靠 504 猜。
    """
    # systemd 下 stdout 默认块缓冲，pipeline 的 print 会攒着不刷到 journalctl，
    # 用户盯着日志只看到轮询访问、没有任何写稿进度，误以为卡死。改成行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    generate_state.update({
        "running": True, "done": 0, "total": count,
        "created": 0, "demo": demo, "error": "", "column": column,
        "stage": "正在启动", "started_at": datetime.now().isoformat(timespec="seconds"),
    })

    def on_progress(done: int, total: int, stage: str) -> None:
        generate_state["done"] = done
        generate_state["total"] = total
        generate_state["stage"] = stage
        print(f"[generate] {stage}（{done}/{total}）", flush=True)

    try:
        quotas = _column_quotas(column, count)
        created = run_pipeline(count, demo, progress=on_progress, quotas=quotas)
        generate_state["created"] = len(created)
        generate_state["stage"] = "已完成"
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把服务打挂
        generate_state["error"] = f"{type(exc).__name__}: {exc}"
        generate_state["stage"] = "出错了"
        print(f"[generate] 线程异常: {exc}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        generate_state["done"] = generate_state["total"]
        generate_state["running"] = False
        generate_lock.release()


@app.route("/generate", methods=["POST"])
def generate() -> Any:
    """网页端一键生成：立即返回，后台线程慢慢写，前端轮询 /generate/status。

    改成异步的原因：一次写多篇要几分钟，同步请求会被 nginx 60s 超时掐断成 504，
    浏览器报错但服务端还在写，用户重复点又撞锁。异步后请求秒回，彻底绕开网关超时。
    """
    data = request.get_json(silent=True) or {}
    count = int(data.get("count", config.BATCH_SIZE))
    count = max(1, min(count, 20))  # 限制 1-20，防止误操作刷爆

    # 栏目筛选：只接受 all 或已定义的栏目键，非法值直接拒绝而不是静默全量生成
    column = str(data.get("column", "all") or "all").strip()
    allowed_columns = {"all", *config.COLUMN_LABELS.keys()}
    if column not in allowed_columns:
        return jsonify({
            "ok": False,
            "error": f"未知栏目：{column}",
        }), 400

    # 没填 API key 就自动走 demo，保证网页端永远能跑
    demo = not bool(config.AI_API_KEY)

    if not generate_lock.acquire(blocking=False):
        return jsonify({
            "ok": False,
            "error": "正在生成中，请稍候",
            "state": dict(generate_state),
        }), 409

    threading.Thread(
        target=_generate_worker, args=(count, demo, column), daemon=True
    ).start()
    return jsonify({
        "ok": True, "started": True, "total": count,
        "demo": demo, "column": column,
    })


@app.route("/generate/status")
def generate_status() -> Any:
    """前端轮询：返回生成进度。页面刷新后也能重新读到当前状态。"""
    return jsonify(dict(generate_state))


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


@app.route("/retitle", methods=["POST"])
def retitle() -> Any:
    """换标题：只生成 3 个候选返回，不写回队列，由用户挑一个再调 /title/apply。

    和 /optimize 分开两步，是因为标题是打开率的关键，不能让 AI 悄悄定。
    """
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    if not article_id:
        return jsonify({"ok": False, "error": "缺少稿件 id"}), 400

    queue = load_queue()
    article = next((a for a in queue if a.get("id") == article_id), None)
    if article is None:
        return jsonify({"ok": False, "error": "稿件不存在"}), 404

    candidates = regenerate_titles(article)
    if not candidates:
        return jsonify({
            "ok": False,
            "error": "换标题失败：AI 暂时不可用或候选不合格，原标题未改动",
        })
    return jsonify({"ok": True, "id": article_id, "candidates": candidates})


@app.route("/title/apply", methods=["POST"])
def title_apply() -> Any:
    """应用用户选中的标题。只动 title 一个字段，正文/状态/头图都不碰。"""
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    title = (data.get("title") or "").strip()
    if not article_id or not title:
        return jsonify({"ok": False, "error": "缺少稿件 id 或标题"}), 400

    if not generate_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在生成或优化中，请稍候"}), 409
    try:
        updated = apply_title(article_id, title)
    finally:
        generate_lock.release()

    if updated is None:
        return jsonify({"ok": False, "error": "应用失败：稿件不存在或标题不合法"})
    return jsonify({"ok": True, "id": updated["id"], "title": updated["title"]})


@app.route("/analytics/record", methods=["POST"])
def analytics_record() -> Any:
    """录入/更新一篇稿的发布后数据。按 article_id 去重，重复录入会覆盖。"""
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    if not article_id:
        return jsonify({"ok": False, "error": "缺少稿件 id"}), 400

    queue = load_queue()
    article = next((a for a in queue if a.get("id") == article_id), None)
    if article is None:
        return jsonify({"ok": False, "error": "稿件不存在（可能已被清理）"}), 404

    try:
        record = analytics.upsert(article, data, formula=data.get("formula", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "record": record})


@app.route("/analytics/delete", methods=["POST"])
def analytics_delete() -> Any:
    """删除一条复盘记录。"""
    data = request.get_json(silent=True) or {}
    article_id = data.get("id", "")
    if not article_id or not analytics.remove(article_id):
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True})


@app.route("/profile/save", methods=["POST"])
def profile_save() -> Any:
    """保存账号画像六维。改动下次生成/改写/换标题时生效，不动已有稿件。"""
    data = request.get_json(silent=True) or {}
    profile_mod.save(data)
    return jsonify({"ok": True, "profile": profile_mod.load()})


@app.route("/profile/sync", methods=["POST"])
def profile_sync() -> Any:
    """把复盘结论写回画像的标题/来源偏好，形成闭环。"""
    before = profile_mod.load()
    updated = profile_mod.sync_from_analytics()
    # 只有真的写回了新经验才算同步成功——画像里本来就有手填偏好时不误报
    synced = (
        updated.get("title_preference", "") != before.get("title_preference", "")
        or updated.get("source_preference", "") != before.get("source_preference", "")
    )
    return jsonify({
        "ok": True,
        "synced": synced,
        "title_preference": updated.get("title_preference", ""),
        "source_preference": updated.get("source_preference", ""),
        "synced_at": updated.get("synced_at", ""),
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
        # 首次通过 → 后台自动生成公众号头图（不阻塞页面，失败也不影响通过）
        if not queue[index].get("cover"):
            queue[index]["cover_state"] = "generating"
            save_queue(queue)
            threading.Thread(
                target=_cover_worker,
                args=(article_id,),
                daemon=True,
            ).start()
            return jsonify({"ok": True, "cover": "generating"})

    save_queue(queue)
    return jsonify({"ok": True})


def _cover_worker(article_id: str) -> None:
    """后台线程：生成头图后写回队列。失败只记状态，不动稿件。"""
    try:
        queue = load_queue()
        index = find_article(queue, article_id)
        if index == -1:
            return
        path = cover_mod.generate_cover(queue[index], force=True)
        queue = load_queue()
        index = find_article(queue, article_id)
        if index == -1:
            return
        if path:
            queue[index]["cover"] = path
            queue[index]["cover_at"] = datetime.now().isoformat(timespec="seconds")
            queue[index]["cover_state"] = "done"
        else:
            queue[index]["cover_state"] = "failed"
        save_queue(queue)
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把服务打挂
        print(f"[cover] 线程异常 {article_id}: {exc}")


@app.route("/cover/<article_id>")
def cover_file(article_id: str) -> Any:
    """返回该稿件的头图文件。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404

    rel = queue[index].get("cover")
    if not rel:
        return jsonify({"error": "还没有头图"}), 404

    path = Path(__file__).resolve().parent / rel
    if not path.exists():
        return jsonify({"error": "头图文件丢失"}), 404

    # 换一张会覆盖同名文件：必须禁用缓存，否则浏览器一直显示旧图
    resp = send_file(path, mimetype="image/jpeg", max_age=0)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/cover/<article_id>/regenerate", methods=["POST"])
def cover_regenerate(article_id: str) -> Any:
    """手动换一张头图（覆盖旧的）。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"ok": False, "error": "稿件不存在"}), 404

    if queue[index].get("cover_state") == "generating":
        return jsonify({"ok": False, "error": "正在生成中，请稍候"}), 409

    queue[index]["cover_state"] = "generating"
    save_queue(queue)
    threading.Thread(target=_cover_worker, args=(article_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/publish/<article_id>", methods=["POST"])
def publish_draft(article_id: str) -> Any:
    """把稿件推进公众号草稿箱（不直接群发，人还要进后台点发表）。

    前置：.env 配了 WECHAT_APPID/WECHAT_SECRET，后台 IP 白名单加了服务器，
    这篇已经生成头图。缺哪项就报哪项，不吞错误。
    """
    if not publish_mod.configured():
        return jsonify({
            "ok": False,
            "error": "还没配公众号凭据。在 .env 里加 WECHAT_APPID 和 WECHAT_SECRET，"
                     "并把服务器 IP 加到后台 IP 白名单，然后重启服务",
        }), 400

    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"ok": False, "error": "稿件不存在"}), 404

    if queue[index].get("status") != "approved":
        return jsonify({"ok": False, "error": "先审核通过再推草稿箱"}), 400

    try:
        result = publish_mod.publish_article(queue[index])
    except publish_mod.PublishError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - 网络等意外不能把页面搞挂
        return jsonify({"ok": False, "error": f"推送失败：{exc}"}), 500

    # 回写草稿 id，重复点也不会重复创建（前端据此提示"再点会新建一条"）
    queue = load_queue()
    index = find_article(queue, article_id)
    if index != -1:
        queue[index]["draft_id"] = result["media_id"]
        queue[index]["draft_at"] = datetime.now().isoformat(timespec="seconds")
        save_queue(queue)

    return jsonify({"ok": True, "media_id": result["media_id"], "title": result["title"]})


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
    """返回可复制的成品。按 platform 选排版档位（公众号完整 / 头条百家简洁）。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404

    article = queue[index]
    platform = request.args.get("platform", "")
    # html：带内联样式的富文本，按平台切换档位；content：纯文本兜底
    # 标题单独返回：各平台都是标题、正文分开录入，正文里不必重复标题
    # title_advice：当前标题在该平台的字数是否合适（只提示不改）
    html = wechat.to_platform_html(article["body"], platform)
    return jsonify({
        "title": article["title"],
        "content": wechat.to_plain_text(article["body"]),
        "html": html,
        "platform": wechat.normalize_platform(platform),
        "title_advice": wechat.title_advice(article["title"], platform),
        "check": wechat.summarize_issues(wechat.validate_html(html)),
    })


@app.route("/preview/<article_id>")
def preview(article_id: str) -> Any:
    """返回成品预览 HTML，模拟目标平台发布后的排版效果。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404

    article = queue[index]
    platform = request.args.get("platform", "")
    return render_template(
        "preview.html",
        title=article["title"],
        # 用和"复制成品"完全相同的富文本，保证预览所见 = 粘贴所得
        body=wechat.to_platform_html(article["body"], platform),
        platform=wechat.normalize_platform(platform),
        platforms=wechat.platform_list(),
        author=config.SITE_AUTHOR,
        title_advice=wechat.title_advice(article["title"], platform),
    )


@app.route("/mark-read/<article_id>", methods=["POST"])
def mark_read(article_id: str) -> Any:
    """点「预览成品」后清除该稿的红点（unread 标记），返回剩余待审核未读数。"""
    queue = load_queue()
    index = find_article(queue, article_id)
    if index == -1:
        return jsonify({"error": "稿件不存在"}), 404
    if queue[index].get("unread"):
        queue[index]["unread"] = False
        save_queue(queue)
    remaining = sum(
        1 for a in queue
        if a.get("status") == "pending"
        and a.get("unread")
        and a.get("column") == "github"
    )
    return jsonify({"ok": True, "unread_count": remaining})


if __name__ == "__main__":
    print(f"审核后台启动：http://localhost:{config.APP_PORT}")
    app.run(
        host=config.APP_HOST,
        port=config.APP_PORT,
        debug=False,
        threaded=True,
    )
