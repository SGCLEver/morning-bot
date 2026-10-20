#!/usr/bin/env python3
"""Личный Telegram-бот: утренняя сводка (Todoist, курс, погода, почта mail.ru)
и вечерний итог с переносом невыполненного на завтра после подтверждения.

Запуск:  python bot.py morning   |   python bot.py evening
Только стандартная библиотека Python, никаких зависимостей.
"""
import email
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

# ---------- настройки ----------
TZ = timezone(timedelta(hours=int(os.environ.get("UTC_OFFSET", "5"))))  # Москва +2
CITY = os.environ.get("CITY_NAME", "Уфа")
LAT = os.environ.get("CITY_LAT", "54.7348")
LON = os.environ.get("CITY_LON", "55.9579")
COURSE_PROJECT = os.environ.get("COURSE_PROJECT", "Курс по истории")
MORNING_AT = os.environ.get("MORNING_AT", "06:30")
EVENING_AT = os.environ.get("EVENING_AT", "21:45")
# до какого часа вечером бот ждёт нажатия кнопки (дальше нажатие обработает утренний запуск)
EVENING_WAIT_UNTIL_HOUR = int(os.environ.get("EVENING_WAIT_UNTIL_HOUR", "24"))
NO_WAIT = os.environ.get("NO_WAIT") == "1"

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TD_TOKEN = os.environ.get("TODOIST_TOKEN", "")
MAIL_USER = os.environ.get("MAILRU_EMAIL", "")
MAIL_PASS = os.environ.get("MAILRU_APP_PASSWORD", "")

# Отправители, которые показываются всегда (адреса или домены через запятую)
MAIL_VIP = [s.strip().lower() for s in os.environ.get("MAIL_VIP", "").split(",") if s.strip()]
# Слова в теме, при которых письмо показывается даже от робота (если это не реклама)
MAIL_KEYWORDS = [s.strip().lower() for s in os.environ.get(
    "MAIL_KEYWORDS",
    "олимпиад,пробник,призёр,призер,победител,диплом,результаты участ,пропуск,госуслуг",
).split(",") if s.strip()]
# Отправители, которые не показываются никогда
MAIL_BLOCK = [s.strip().lower() for s in os.environ.get("MAIL_BLOCK", "").split(",") if s.strip()]

WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def now():
    return datetime.now(TZ)


def today():
    return now().date()


def esc(s):
    return html.escape(str(s or ""), quote=False)


def clean(s):
    """Убирает markdown-ссылки Todoist: [текст](url) -> текст."""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s or "")
    return s.replace("**", "").strip()


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_day(d):
    return f"{WD_SHORT[d.weekday()]}, {d.day} {MONTHS_SHORT[d.month - 1]}"


def fmt_long(d):
    return f"{WD[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- HTTP ----------
def http(method, url, headers=None, data=None, timeout=70):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if isinstance(data, (dict, list)) and headers.get("Content-Type") == "application/json":
            body = json.dumps(data).encode()
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {e.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise


# ---------- Telegram ----------
def tg(method, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    res = http("POST", url, {"Content-Type": "application/json"}, params)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}: {res}")
    return res["result"]


def send(text, keyboard=None):
    params = dict(chat_id=TG_CHAT, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True})
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", **params)


# ---------- Todoist ----------
TD = "https://api.todoist.com/api/v1"


def td(method, path, params=None, data=None):
    url = TD + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    h = {"Authorization": f"Bearer {TD_TOKEN}"}
    if data is not None:
        h["Content-Type"] = "application/json"
    return http(method, url, h, data)


def td_all(path, params=None):
    params = dict(params or {}, limit=200)
    out = []
    while True:
        res = td("GET", path, params)
        out += res.get("results", res.get("items", []))
        cur = res.get("next_cursor")
        if not cur:
            return out
        params["cursor"] = cur


def due_date(task):
    d = (task.get("due") or {}).get("date")
    return date.fromisoformat(d[:10]) if d else None


def due_time(task):
    d = (task.get("due") or {}).get("date") or ""
    if "T" not in d:
        return ""
    if d.endswith("Z"):  # время с фиксированным поясом хранится в UTC
        dt = datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%H:%M")
    return d[11:16]


RU_MONTH_RE = "|".join(m[:3] for m in MONTHS)


def deadline_of(task):
    """Настоящий дедлайн: поле deadline Todoist, либо «Дедлайн: 24 октября 2026» в описании."""
    dl = (task.get("deadline") or {}).get("date")
    if dl:
        return date.fromisoformat(dl[:10])
    m = re.search(r"[Дд]едлайн[:\s]+(\d{1,2})\s+(" + RU_MONTH_RE + r")[а-я]*\s*(\d{4})?",
                  task.get("description") or "")
    if m:
        month = [x[:3] for x in MONTHS].index(m.group(2)) + 1
        year = int(m.group(3)) if m.group(3) else today().year
        try:
            return date(year, month, int(m.group(1)))
        except ValueError:
            return None
    if "ДЕДЛ" in (task.get("content") or "").upper():
        return due_date(task)
    return None


def load_todoist():
    tasks = td_all("/tasks")
    projects = {p["id"]: p for p in td_all("/projects")}
    sections = {s["id"]: s for s in td_all("/sections")}
    return tasks, projects, sections


def task_line(t, show_date=False):
    """Одна задача одной строкой: время/дата жирным, затем название."""
    name = esc(clean(t["content"]))
    if t.get("priority") == 4:
        name = "❗️" + name
    parts = []
    if show_date:
        parts.append(fmt_day(due_date(t)))
    if due_time(t):
        parts.append(due_time(t))
    if parts:
        return f"<b>{' · '.join(parts)}</b>  {name}"
    return f"▫️ {name}"


def sort_key(t):
    return (due_date(t) or date.max, due_time(t) or "99", -t.get("priority", 1), t.get("child_order", 0))


# ---------- Погода (Open-Meteo, без ключа) ----------
WMO = {
    0: ("☀️", "ясно"), 1: ("🌤", "преимущественно ясно"), 2: ("⛅️", "переменная облачность"),
    3: ("☁️", "пасмурно"), 45: ("🌫", "туман"), 48: ("🌫", "изморозь"),
    51: ("🌦", "лёгкая морось"), 53: ("🌦", "морось"), 55: ("🌧", "сильная морось"),
    56: ("🌧", "ледяная морось"), 57: ("🌧", "ледяная морось"),
    61: ("🌧", "небольшой дождь"), 63: ("🌧", "дождь"), 65: ("🌧", "сильный дождь"),
    66: ("🌧", "ледяной дождь"), 67: ("🌧", "ледяной дождь"),
    71: ("🌨", "небольшой снег"), 73: ("🌨", "снег"), 75: ("❄️", "сильный снег"), 77: ("🌨", "снежная крупа"),
    80: ("🌦", "ливень"), 81: ("🌧", "ливни"), 82: ("⛈", "сильные ливни"),
    85: ("🌨", "снегопад"), 86: ("❄️", "сильный снегопад"),
    95: ("⛈", "гроза"), 96: ("⛈", "гроза с градом"), 99: ("⛈", "гроза с градом"),
}


def t_fmt(x):
    x = round(x)
    return f"+{x}" if x > 0 else str(x).replace("-", "−")


def weather():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "auto", "forecast_days": 2,
        "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
    })
    return http("GET", "https://api.open-meteo.com/v1/forecast?" + q)


def weather_line(w, i):
    d = w["daily"]
    ico, txt = WMO.get(d["weather_code"][i], ("🌡", ""))
    s = f"{ico} {t_fmt(d['temperature_2m_min'][i])}…{t_fmt(d['temperature_2m_max'][i])}°, {txt}"
    p = d["precipitation_probability_max"][i]
    if p is not None and p >= 40:
        s += ", возьми зонт ☂️"
    return s


# ---------- Почта mail.ru (только важное) ----------
ROBOT_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|noreply|mailer|mailer-daemon|news|newsletter|digest|info|"
    r"hello|marketing|promo|offers?|sale|shop|notify|notifications?|robot|bot|subscribe|"
    r"rassylka|mail|mailing|team|updates?|bounce|support|service)([._+-][\w.+-]*)?@", re.I)
PROMO_RE = re.compile(r"скидк|акци[яи]|распродаж|промокод|кешбэк|кэшбэк|%|бесплатн|розыгрыш|вебинар|"
                      r"успей|только сегодня|подписк|предложени", re.I)
BULK_MAILERS = ("mailchimp", "sendpulse", "unisender", "sendsay", "dashamail", "mindbox",
                "esputnik", "mailganer", "sendgrid", "mailgun", "amazonses", "mandrill")


def mail_is_important(msg):
    def h(name, default=""):
        v = msg.get(name)
        return str(v) if v is not None else default

    name, addr = email.utils.parseaddr(h("From"))
    addr = addr.lower()
    domain = addr.split("@")[-1]
    subj = h("Subject")
    if any(b == addr or b == domain or domain.endswith("." + b) for b in MAIL_BLOCK):
        return False
    if any(v == addr or v == domain or domain.endswith("." + v) for v in MAIL_VIP):
        return True
    keyword = any(k in subj.lower() for k in MAIL_KEYWORDS) and not PROMO_RE.search(subj)
    bulk = (
        h("List-Unsubscribe") or h("List-Id") or h("List-Post")
        or (h("Precedence", "").lower() in ("bulk", "list", "junk"))
        or any(m in (h("X-Mailer", "") + h("Message-ID", "") + h("Return-Path", "")).lower()
               for m in BULK_MAILERS)
        or h("X-Campaign") or h("X-Campaign-Id") or h("X-MC-User")
    )
    auto = h("Auto-Submitted", "no").lower() not in ("", "no")
    robot = bool(ROBOT_RE.match(addr))
    if keyword:
        return True
    return not (bulk or auto or robot)


def important_mail(since):
    if not (MAIL_USER and MAIL_PASS):
        return None
    m = imaplib.IMAP4_SSL("imap.mail.ru", 993)
    try:
        m.login(MAIL_USER, MAIL_PASS)
        m.select("INBOX", readonly=True)  # readonly: письма остаются непрочитанными
        crit = "(UNSEEN SINCE {})".format((since - timedelta(days=1)).strftime("%d-%b-%Y"))
        typ, data = m.search(None, crit)
        ids = data[0].split() if data and data[0] else []
        out = []
        for i in ids[-200:]:
            typ, msg = m.fetch(i, "(BODY.PEEK[HEADER])")
            raw = next((p[1] for p in msg if isinstance(p, tuple)), b"")
            h = email.message_from_bytes(raw, policy=email.policy.default)
            try:
                when = email.utils.parsedate_to_datetime(h.get("Date"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except Exception:
                when = None
            if when and when < since:
                continue
            try:
                if not mail_is_important(h):
                    continue
            except Exception as e:  # кривой заголовок не должен ронять сводку
                log("mail header error:", e)
                continue
            name, addr = email.utils.parseaddr(str(h.get("From", "")))
            out.append({"from": name or addr, "subject": str(h.get("Subject", "") or "(без темы)"),
                        "when": when})
        out.sort(key=lambda x: x["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------- Утро ----------
def wait_until(hhmm):
    if NO_WAIT:
        return
    h, mnt = map(int, hhmm.split(":"))
    target = now().replace(hour=h, minute=mnt, second=0, microsecond=0)
    delay = (target - now()).total_seconds()
    if 0 < delay < 3600:
        log(f"жду {int(delay)} с до {hhmm}")
        time.sleep(delay)


_NAME = None


def user_name():
    """Имя для обращения: USER_NAME или имя из профиля Telegram."""
    global _NAME
    if _NAME is None:
        _NAME = os.environ.get("USER_NAME", "")
        if not _NAME:
            try:
                _NAME = tg("getChat", chat_id=TG_CHAT).get("first_name", "")
            except Exception as e:
                log("getChat:", e)
    return _NAME


def hello(greeting):
    n = user_name()
    return f"{greeting}, {esc(n)}" if n else greeting


def strip_dedl(s):
    return re.sub(r",?\s*ДЕДЛ\.?\s*$", "", clean(s), flags=re.I)


def when_left(left):
    if left == 0:
        return "сегодня"
    if left == 1:
        return "завтра"
    return f"через {left} {plural(left, 'день', 'дня', 'дней')}"


def morning():
    process_updates(wait=False)  # вдруг вчера кнопку нажали поздно
    wait_until(MORNING_AT)
    d0 = today()
    tasks, projects, sections = load_todoist()

    head = f"☀️ <b>{hello('Доброе утро')}!</b>\n{fmt_long(d0)}"
    try:
        head += f"\n{weather_line(weather(), 0)}"
    except Exception as e:
        log("weather:", e)
    blocks = [head]

    # Сегодня
    todays = sorted([t for t in tasks if due_date(t) == d0], key=sort_key)
    overdue = sorted([t for t in tasks if due_date(t) and due_date(t) < d0], key=sort_key)
    if todays:
        b = [f"📋 <b>СЕГОДНЯ · {len(todays)}</b>"] + [task_line(t) for t in todays]
    else:
        b = ["📋 <b>СЕГОДНЯ</b>", "Дел нет, свободный день"]
    if overdue:
        b.append(f"\n<i>Хвосты с прошлых дней · {len(overdue)}</i>")
        b += [task_line(t, show_date=True) for t in overdue[:7]]
        if len(overdue) > 7:
            b.append(f"…и ещё {len(overdue) - 7}")
    blocks.append("\n".join(b))

    # Дедлайны на 30 дней вперёд
    dls = sorted([(deadline_of(t), t) for t in tasks
                  if deadline_of(t) and 0 <= (deadline_of(t) - d0).days <= 30], key=lambda x: x[0])
    if dls:
        b = ["🎯 <b>ДЕДЛАЙНЫ</b>"]
        for dl, t in dls[:6]:
            left = (dl - d0).days
            mark = "🔥" if left <= 3 else "▫️"
            b.append(f"{mark} {esc(strip_dedl(t['content']))}\n      <b>{when_left(left)}</b> · {fmt_day(dl)}")
        blocks.append("\n".join(b))

    # Почта: только важное за сутки
    try:
        mails = important_mail(now() - timedelta(hours=24))
        if mails:
            b = [f"✉️ <b>ПОЧТА · {len(mails)}</b>"]
            b += [f"▫️ <b>{esc(x['from'])}</b>\n      {esc(x['subject'])}" for x in mails[:6]]
            if len(mails) > 6:
                b.append(f"…и ещё {len(mails) - 6}")
            blocks.append("\n".join(b))
        elif mails is not None:
            blocks.append("✉️ Важных писем нет")
    except Exception as e:
        log("mail:", e)
        blocks.append("✉️ Почту проверить не удалось")

    send("\n\n".join(blocks))

    # Отдельное напоминание только накануне и в день дедлайна
    for dl, t in dls:
        left = (dl - d0).days
        if left in (0, 1):
            send(f"⏰ <b>Дедлайн {when_left(left)}!</b>\n{esc(strip_dedl(t['content']))}")


# ---------- Вечер ----------
def completed_between(since, until):
    params = {"since": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "until": until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 200}
    out = []
    while True:
        res = td("GET", "/tasks/completed/by_completion_date", params)
        out += res.get("items", res.get("results", []))
        if not res.get("next_cursor"):
            return out
        params["cursor"] = res["next_cursor"]


def short(s, n=38):
    s = clean(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def evening():
    process_updates(wait=False)
    wait_until(EVENING_AT)
    d0 = today()
    midnight = datetime(d0.year, d0.month, d0.day, tzinfo=TZ)
    tasks, projects, sections = load_todoist()
    done = completed_between(midnight, now())
    left = sorted([t for t in tasks if due_date(t) and due_date(t) <= d0], key=sort_key)

    head = f"🌙 <b>{hello('Добрый вечер')}!</b>\nИтоги дня · {fmt_day(d0)}"
    blocks = [head]

    if done:
        b = [f"✅ <b>СДЕЛАНО · {len(done)}</b>"]
        b += [f"▫️ {esc(clean(t.get('content', '')))}" for t in done[:10]]
        if len(done) > 10:
            b.append(f"…и ещё {len(done) - 10}")
    else:
        b = ["✅ <b>СДЕЛАНО</b>", "Сегодня ничего не отмечено"]
    blocks.append("\n".join(b))

    if d0.weekday() == 6:
        blocks.append(weekly_stats(d0))

    try:
        blocks.append(f"<i>Завтра:</i> {weather_line(weather(), 1)}")
    except Exception as e:
        log("weather:", e)

    if not left:
        blocks.append("🎉 <b>Все дела на сегодня закрыты!</b>")
        send("\n\n".join(blocks))
        return

    blocks.append(f"⏳ <b>ОСТАЛОСЬ · {len(left)}</b>"
                  "\nНажми на дело, чтобы оставить его на месте (✖️).\nОтмеченные ✅ перенесу на завтра.")
    shown = left[:40]
    kb = [[{"text": "✅ " + short(t["content"]), "callback_data": "t:" + t["id"]}] for t in shown]
    kb.append([{"text": "➡️ Перенести на завтра", "callback_data": "go:" + d0.isoformat()}])
    kb.append([{"text": "Ничего не переносить", "callback_data": "no"}])
    msg = send("\n\n".join(blocks), kb)

    # Ждём нажатия до полуночи (если позже, обработает утренний запуск)
    end = datetime(d0.year, d0.month, d0.day, tzinfo=TZ) + timedelta(hours=EVENING_WAIT_UNTIL_HOUR)
    if NO_WAIT:  # ручной запуск: ждём кнопку не дольше 30 минут
        end = min(end, now() + timedelta(minutes=30))
    while now() < end:
        if process_updates(wait=True, until=end, stop_on_msg=msg["message_id"]):
            break


def weekly_stats(d0):
    monday = d0 - timedelta(days=d0.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=TZ)
    done = completed_between(start, now())
    per = [0] * 7
    for t in done:
        ts = t.get("completed_at")
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
            per[dt.weekday()] += 1
    total = sum(per)
    top = max(per) or 1
    lines = [f"📊 <b>НЕДЕЛЯ · {total} {plural(total, 'дело', 'дела', 'дел')}</b>"]
    rows = []
    for i, n in enumerate(per):
        bar = "█" * round(n / top * 8) if n else "·"
        rows.append(f"{WD_SHORT[i]} {bar} {n}")
    lines.append("<code>" + "\n".join(rows) + "</code>")
    return "\n".join(lines)


# ---------- Обработка кнопок ----------
OFFSET_FILE = os.environ.get("OFFSET_FILE", "")


def process_updates(wait, until=None, stop_on_msg=None):
    """Читает нажатия кнопок. Возвращает True, если вечерний вопрос stop_on_msg решён."""
    resolved, offset = False, None
    while True:
        timeout = 0
        if wait:
            remaining = int((until - now()).total_seconds())
            if remaining <= 0:
                break
            timeout = min(50, remaining)
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            ups = tg("getUpdates", **params)
        except Exception as e:
            log("getUpdates:", e)
            time.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq:
                continue
            try:
                if handle_callback(cq) and stop_on_msg and cq["message"]["message_id"] == stop_on_msg:
                    resolved = True
            except Exception as e:
                log("callback:", e)
                try:
                    tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ошибка, попробуй ещё раз")
                except Exception:
                    pass
        if resolved or (not wait and not ups):
            break
    if offset is not None:  # подтверждаем обработанное, чтобы не получить повторно
        tg("getUpdates", offset=offset, timeout=0)
    return resolved


MARK_RE = re.compile(r"^(✅|✖️|✖)\s*")


def handle_callback(cq):
    """True, если вопрос закрыт (перенесено или отказ)."""
    msg = cq.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return False
    data = cq.get("data", "")
    kb = (msg.get("reply_markup") or {}).get("inline_keyboard", [])

    if data.startswith("t:"):
        for row in kb:
            for btn in row:
                if btn.get("callback_data") == data:
                    on = btn["text"].startswith("✅")
                    btn["text"] = ("✖️ " if on else "✅ ") + MARK_RE.sub("", btn["text"])
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=msg["message_id"],
           reply_markup={"inline_keyboard": kb})
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return False

    if data == "no":
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=msg["message_id"],
           reply_markup={"inline_keyboard": []})
        send("👌 Ничего не переношу, всё осталось на своих местах.")
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Оставил как есть")
        return True

    if data.startswith("go:"):
        evening_day = date.fromisoformat(data[3:])
        target = max(evening_day + timedelta(days=1), today())
        ids = [btn["callback_data"][2:] for row in kb for btn in row
               if btn.get("callback_data", "").startswith("t:") and btn["text"].startswith("✅")]
        moved, skipped = reschedule(ids, target)
        word = "завтра" if target == today() + timedelta(days=1) else "сегодня" if target == today() else fmt_day(target)
        note = f"➡️ Перенёс на {word} ({fmt_day(target)}): <b>{moved}</b>"
        kept = sum(1 for row in kb for b in row if b.get("callback_data", "").startswith("t:")) - len(ids)
        if kept:
            note += f"\nОставил на месте: {kept}"
        if skipped:
            note += f"\nУже сделано или сдвинуто вручную: {skipped}"
        tg("editMessageReplyMarkup", chat_id=TG_CHAT, message_id=msg["message_id"],
           reply_markup={"inline_keyboard": []})
        send(note + "\n\nСпокойной ночи 🌙")
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=f"Перенесено: {moved}")
        return True

    tg("answerCallbackQuery", callback_query_id=cq["id"])
    return False


def reschedule(ids, target):
    """Переносит задачи на дату target, сохраняя время и повторяемость."""
    cmds, skipped = [], 0
    for tid in ids:
        try:
            t = td("GET", f"/tasks/{tid}")
        except RuntimeError:
            skipped += 1
            continue
        due = t.get("due")
        if t.get("checked") or not due or due_date(t) >= target:
            skipped += 1
            continue
        old = due["date"]
        if "T" in old and old.endswith("Z"):
            local = datetime.fromisoformat(old.replace("Z", "+00:00")).astimezone(TZ)
            new = datetime.combine(target, local.time(), TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif "T" in old:
            new = target.isoformat() + old[10:]
        else:
            new = target.isoformat()
        nd = {"date": new}
        if due.get("timezone"):
            nd["timezone"] = due["timezone"]
        if due.get("is_recurring"):
            # строка правила + новая дата: Todoist сдвигает текущий повтор и сохраняет правило
            nd.update(string=due.get("string"), lang=due.get("lang") or "ru", is_recurring=True)
        cmds.append({"type": "item_update", "uuid": str(uuid.uuid4()), "args": {"id": tid, "due": nd}})
    moved = 0
    for i in range(0, len(cmds), 50):
        chunk = cmds[i:i + 50]
        res = http("POST", TD + "/sync", {"Authorization": f"Bearer {TD_TOKEN}"},
                   {"commands": json.dumps(chunk)})
        st = res.get("sync_status", {})
        moved += sum(1 for c in chunk if st.get(c["uuid"]) == "ok")
        bad = {k: v for k, v in st.items() if v != "ok"}
        if bad:
            log("sync errors:", bad)
            skipped += len(bad)
    return moved, skipped


def whoami():
    """Отвечает каждому, кто написал боту, его числовым ID (для секрета TELEGRAM_CHAT_ID)."""
    ups = tg("getUpdates", timeout=0, allowed_updates=["message"])
    chats = {}
    for u in ups:
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats[chat["id"]] = chat
    for cid in chats:
        tg("sendMessage", chat_id=cid, parse_mode="HTML",
           text=f"Твой ID: <code>{cid}</code>\n\nНажми на число, чтобы скопировать, и вставь его "
                f"в секрет TELEGRAM_CHAT_ID на GitHub.")
    if ups:
        tg("getUpdates", offset=ups[-1]["update_id"] + 1, timeout=0)
    log(f"ответил в {len(chats)} чат(ов)")
    if not chats:
        sys.exit("Боту никто не писал за последние сутки: напиши ему любое сообщение и запусти снова")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode == "whoami":
        return whoami()
    missing = [k for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TODOIST_TOKEN") if not os.environ.get(k)]
    if missing:
        sys.exit("Не заданы секреты: " + ", ".join(missing))
    try:
        {"morning": morning, "evening": evening}[mode]()
    except Exception as e:
        log("FATAL:", repr(e))
        try:
            send(f"⚠️ Бот споткнулся ({mode}): <code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
