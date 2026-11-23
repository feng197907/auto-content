"""内容日历 + 节日节点（Easel「内容日历」层的轻量落地）。

为什么需要：原来 cron 每天 8 点无差别生成，赶上春节/国庆也在写日常题，
既不合时宜也没流量。这里做两件事：
  1) 把队列里的稿件按日期分组，一眼看清哪天写了什么；
  2) 节假日前给出提醒，让人决定这天是跳过、还是提前写应景选题。

设计取舍：
  - 只做「提醒 + 展示」，不改 cron 主链路——自动停发风险高，且节假日往往
    反而有热点，机器不该替人拍板。要不要跳过由人在页面上决定。
  - 节日表只放固定公历日期 + 几个常见农历节日的近似日期（标注"农历，以当年为准"）。
    不引入农历库，避免加依赖；需要精确时可在 data/holidays.json 覆盖。
  - data/holidays.json 可选，格式 [{"date":"MM-DD","name":"名称"},...]，
    存在则与内置表合并（同名日期以自定义为准），换机需单独拷。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CUSTOM_PATH = ROOT / "data" / "holidays.json"

# 固定公历节日。名称即提醒文案，简短。
FIXED_HOLIDAYS: list[dict[str, str]] = [
    {"date": "01-01", "name": "元旦"},
    {"date": "02-14", "name": "情人节"},
    {"date": "03-08", "name": "妇女节"},
    {"date": "03-12", "name": "植树节"},
    {"date": "04-01", "name": "愚人节"},
    {"date": "05-01", "name": "劳动节"},
    {"date": "05-04", "name": "青年节"},
    {"date": "06-01", "name": "儿童节"},
    {"date": "07-01", "name": "建党节"},
    {"date": "08-01", "name": "建军节"},
    {"date": "09-10", "name": "教师节"},
    {"date": "10-01", "name": "国庆节"},
    {"date": "11-11", "name": "双十一"},
    {"date": "12-12", "name": "双十二"},
    {"date": "12-24", "name": "平安夜"},
    {"date": "12-25", "name": "圣诞节"},
]

# 农历节日：日期每年不同，这里给 2026 年的公历日期作参考，
# 名称带"农历"提示，避免误导；要精确就写 data/holidays.json 覆盖。
LUNAR_HOLIDAYS_2026: list[dict[str, str]] = [
    {"date": "02-17", "name": "春节（农历，以当年为准）"},
    {"date": "02-15", "name": "除夕（农历，以当年为准）"},
    {"date": "03-03", "name": "元宵（农历，以当年为准）"},
    {"date": "06-19", "name": "端午（农历，以当年为准）"},
    {"date": "08-19", "name": "七夕（农历，以当年为准）"},
    {"date": "09-25", "name": "中秋（农历，以当年为准）"},
    {"date": "10-18", "name": "重阳（农历，以当年为准）"},
]


def _custom_holidays() -> list[dict[str, str]]:
    if not CUSTOM_PATH.exists():
        return []
    try:
        data = json.loads(CUSTOM_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        d, n = str(row.get("date", ""))[:5], str(row.get("name", "")).strip()
        # 只认 MM-DD，且月日在合理范围
        if len(d) == 5 and d[2] == "-" and d[:2].isdigit() and d[3:].isdigit():
            if 1 <= int(d[:2]) <= 12 and 1 <= int(d[3:]) <= 31:
                out.append({"date": d, "name": n or "自定义节日"})
    return out


def holiday_table() -> dict[str, str]:
    """返回 MM-DD -> 节日名 的合并表，自定义覆盖内置。"""
    table: dict[str, str] = {}
    for row in FIXED_HOLIDAYS + LUNAR_HOLIDAYS_2026:
        table[row["date"]] = row["name"]
    for row in _custom_holidays():
        table[row["date"]] = row["name"]
    return table


def holiday_on(day: date) -> str:
    """某天是否是节日，返回节日名或空串。"""
    return holiday_table().get(day.strftime("%m-%d"), "")


def upcoming(days: int = 21, today: date | None = None) -> list[dict[str, Any]]:
    """未来 N 天内的节日提醒（含今天），按日期排序。"""
    today = today or date.today()
    table = holiday_table()
    out = []
    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        name = table.get(day.strftime("%m-%d"))
        if name:
            out.append({
                "date": day.isoformat(),
                "label": day.strftime("%m-%d"),
                "name": name,
                "days_away": offset,
                "when": "今天" if offset == 0 else ("明天" if offset == 1 else f"{offset} 天后"),
            })
    return out


def group_by_date(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """队列按 created_at 的日期分组，最新日期在前，组内保持原顺序。"""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in queue:
        day = str(item.get("created_at", ""))[:10] or "未知日期"
        buckets[day].append(item)
    out = []
    for day in sorted(buckets, reverse=True):
        items = buckets[day]
        out.append({
            "date": day,
            "count": len(items),
            "holiday": holiday_on(date.fromisoformat(day)) if len(day) == 10 and day[4] == "-" else "",
            "items": [
                {
                    "id": i.get("id", ""),
                    "title": i.get("title", ""),
                    "status": i.get("status", ""),
                    "source": i.get("source", ""),
                }
                for i in items
            ],
        })
    return out


def suggestion(today: date | None = None) -> str:
    """给生成工具区的一句话提醒：临近节日时提示，否则给空串。"""
    ups = upcoming(days=3, today=today)
    if not ups:
        return ""
    nearest = ups[0]
    if nearest["days_away"] == 0:
        return f"今天是{nearest['name']}，自动生成的是日常题；若要应景，先手动改选题再生成。"
    return f"{nearest['when']}是{nearest['name']}，这几天可考虑写应景选题。"
