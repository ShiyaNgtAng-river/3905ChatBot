"""Conservative time candidates; ambiguous expressions never acquire invented precision."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def normalize_time(raw: str, anchor: str, tz: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    local = datetime.fromisoformat(anchor).astimezone(ZoneInfo(tz))
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if len(raw) == 10:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.isoformat()
    except ValueError:
        pass
    if "今晚" in raw and local.hour < 5:
        return None
    day = local.date()
    explicit_day = False
    for key, offset in (("后天", 2), ("明天", 1), ("今天", 0), ("今晚", 0)):
        if key in raw:
            day += timedelta(days=offset)
            explicit_day = True
            break
    m = re.search(r"(下周|这周|本周|周|星期)([一二三四五六日天])", raw)
    if m:
        index = "一二三四五六日".index(m[2].replace("天", "日"))
        monday = local.date() - timedelta(days=local.weekday())
        day = monday + timedelta(days=index + (7 if m[1] == "下周" else 0))
        if m[1] in {"周", "星期"} and day < local.date():
            return None  # Do not silently choose this versus next week.
        explicit_day = True
    numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    m = re.search(r"(\d{1,2}|十一|十二|[一二三四五六七八九十])(?:点|[:：])(\d{1,2}|半)?", raw)
    if not m:
        return day.isoformat() if explicit_day else None
    hour = int(m[1]) if m[1].isdigit() else numbers[m[1]]
    minute = 30 if m[2] == "半" else int(m[2] or 0)
    if any(x in raw for x in ("下午", "晚上", "今晚")) and hour < 12:
        hour += 12
    if hour > 23 or minute > 59 or not explicit_day:
        return None
    return datetime.combine(day, datetime.min.time(), ZoneInfo(tz)).replace(hour=hour, minute=minute).isoformat()
