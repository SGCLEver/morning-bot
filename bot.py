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
# Свои слова через запятую: письмо с ними в теме или тексте показывается всегда
MAIL_KEYWORDS = [s.strip().lower() for s in os.environ.get("MAIL_KEYWORDS", "").split(",") if s.strip()]
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


def is_dedl(task):
    """Задание с пометкой ДЕДЛ: дата в Todoist = день публикации, сдать позже."""
    return "ДЕДЛ" in (task.get("content") or "").upper()


def deadline_of(task):
    """Когда сдавать.
    ДЕДЛ: дата задачи = публикация; пробник сдать в тот же день, остальное через 2 дня (с запасом в день от реального срока в 3 дня).
    Иначе: поле deadline Todoist или «Дедлайн: 24 октября 2026» в описании."""
    dl = (task.get("deadline") or {}).get("date")
    if dl:
        return date.fromisoformat(dl[:10])
    if is_dedl(task) and due_date(task):
        days = 0 if "пробник" in (task.get("content") or "").lower() else 2
        return due_date(task) + timedelta(days=days)
    m = re.search(r"[Дд]едлайн[:\s]+(\d{1,2})\s+(" + RU_MONTH_RE + r")[а-я]*\s*(\d{4})?",
                  task.get("description") or "")
    if m:
        month = [x[:3] for x in MONTHS].index(m.group(2)) + 1
        year = int(m.group(3)) if m.group(3) else today().year
        try:
            return date(year, month, int(m.group(1)))
        except ValueError:
            return None
    return None


def load_todoist():
    tasks = td_all("/tasks")
    projects = {p["id"]: p for p in td_all("/projects")}
    sections = {s["id"]: s for s in td_all("/sections")}
    return tasks, projects, sections


ONLINE_RE = re.compile(r"онлайн|хард|вебинар|zoom|зум|конференц|созвон|трансляц|стрим", re.I)


def is_online(t):
    return bool(ONLINE_RE.search(t.get("content") or ""))


def task_line(t, show_date=False):
    """Строка задачи по шаблону: моноширинная плашка (время, ДЗ, дата) и название.
    Время выделено жирной плашкой и значком: 🎥 онлайн-занятие, ⏰ остальное."""
    name = strip_dedl(t["content"])
    tags = []
    if show_date:
        d = due_date(t)
        tags.append(f"<code>{WD_SHORT[d.weekday()]} {d.day:02d}.{d.month:02d}</code>")
    tm = due_time(t)
    if tm:
        tags.append(f"{'🎥' if is_online(t) else '⏰'} <b><code>{tm}</code></b>")
    m = re.match(r"^ДЗ\b[\s:.,-]*", name)
    if m:
        tags.append("<code>ДЗ</code>")
        name = name[m.end():] or name
    name = esc(name)
    if tm and is_online(t):
        name = f"<b>{name}</b>"
    if t.get("priority") == 4:
        name = "❗️" + name
    plate = " ".join(tags)
    return f"{plate} {name}" if plate else name


def quote(lines):
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


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


# Что показывать ВСЕГДА, даже если письмо похоже на рассылку:
# университеты, олимпиады, приглашения и ответы на мои письма.
UNI_DOMAINS = (
    "edu", "msu.ru", "spbu.ru", "hse.ru", "fa.ru", "kpfu.ru", "mipt.ru", "ranepa.ru", "mgimo.ru",
    "bashedu.ru", "uust.ru", "bspu.ru", "ugatu.su", "urfu.ru", "nsu.ru", "tsu.ru", "rudn.ru",
    "mpgu.su", "mpgu.edu", "rsuh.ru", "spbstu.ru", "itmo.ru", "herzen.spb.ru", "sfu-kras.ru",
    "susu.ru", "psu.ru", "pstu.ru", "sgu.ru", "vsu.ru", "unn.ru", "kfu.ru", "dvfu.ru", "misis.ru",
    "bmstu.ru", "mephi.ru", "guu.ru", "rea.ru", "pgu.ru", "ssau.ru", "omsu.ru", "isu.ru", "utmn.ru",
    "olimpiada.ru", "rsr-olymp.ru", "vsosh.ru", "olymp.ru", "sirius.online", "siriusolymp.ru",
    "talantiuspeh.ru", "obrnadzor.gov.ru", "minobrnauki.gov.ru", "edu.gov.ru",
)
UNI_NAME_RE = re.compile(r"университет|институт|академи|колледж|университ|приёмн|приемн|"
                         r"деканат|кафедр|абитуриент|олимпиад|univ|college|admission", re.I)
STRONG_RE = re.compile(
    r"олимпиад|олимп\b|всош|рсош|перечнев|пробник|отборочн|заключительн|региональн\w* этап|"
    r"муниципальн\w* этап|призёр|призер|победител|диплом|апелляц|результат\w* (участ|олимп|этап)|"
    r"приглаш|пригласительн|invitation|invite|"
    r"университет|вуз\b|абитуриент|поступлен|приёмн\w* комисс|приемн\w* комисс|зачислен|"
    r"собеседован|интервью|день открытых дверей|регистрац\w* (на|участ)|"
    r"сириус|талант и успех|olymp", re.I)
REPLY_RE = re.compile(r"^\s*((re|aw|отв|ответ)\s*(\[\d+\])?\s*:|re\s*\(\d+\)\s*:)", re.I)


def _h(msg, name):
    v = msg.get(name)
    return str(v) if v is not None else ""


def _domain_match(domain, items):
    return any(domain == x or domain.endswith("." + x) for x in items)


def mail_tags(msg, body=""):
    """Метки «обязательно показать»: ВУЗ, олимп, приглаш, ответ. Пусто, если письмо обычное."""
    name, addr = email.utils.parseaddr(_h(msg, "From"))
    domain = addr.lower().split("@")[-1]
    subj = _h(msg, "Subject")
    text = f"{subj}\n{body[:5000]}"
    tags = []
    if _domain_match(domain, UNI_DOMAINS) or UNI_NAME_RE.search(name or ""):
        tags.append("ВУЗ")
    if re.search(r"олимп|всош|рсош|пробник|olymp|этап", text, re.I):
        tags.append("олимп")
    if re.search(r"приглаш|пригласительн|invitation|invite", text, re.I):
        tags.append("приглаш")
    if REPLY_RE.match(subj) or _h(msg, "In-Reply-To"):
        tags.append("ответ")
    if not tags and any(k in text.lower() for k in MAIL_KEYWORDS):
        tags.append("важно")
    # общие слова (победитель, результат...) без явной связи с вузом или олимпиадой
    # засчитываем, только если это не реклама и не «розыгрыш»
    if not tags and STRONG_RE.search(text) and not PROMO_RE.search(text[:600]):
        tags.append("важно")
    return tags


def mail_is_important(msg, body=""):
    addr = email.utils.parseaddr(_h(msg, "From"))[1].lower()
    domain = addr.split("@")[-1]
    if _domain_match(domain, MAIL_BLOCK) or addr in MAIL_BLOCK:
        return False
    if _domain_match(domain, MAIL_VIP) or addr in MAIL_VIP or mail_tags(msg, body):
        return True
    bulk = (
        _h(msg, "List-Unsubscribe") or _h(msg, "List-Id") or _h(msg, "List-Post")
        or _h(msg, "Precedence").lower() in ("bulk", "list", "junk")
        or any(m in (_h(msg, "X-Mailer") + _h(msg, "Message-ID") + _h(msg, "Return-Path")).lower()
               for m in BULK_MAILERS)
        or _h(msg, "X-Campaign") or _h(msg, "X-Campaign-Id") or _h(msg, "X-MC-User")
    )
    auto = _h(msg, "Auto-Submitted").lower() not in ("", "no")
    robot = bool(ROBOT_RE.match(addr))
    return not (bulk or auto or robot)


def body_text(raw):
    """Текст письма (plain или html без тегов) для поиска ключевых слов."""
    try:
        m = email.message_from_bytes(raw, policy=email.policy.default)
        part = m.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        t = part.get_content()
        if part.get_content_subtype() == "html":
            t = re.sub(r"(?s)<(script|style).*?</\1>", " ", t)
            t = html.unescape(re.sub(r"<[^>]+>", " ", t))
        return re.sub(r"\s+", " ", t)
    except Exception:
        return ""


def spam_folder(m):
    """Имя папки «Спам» на сервере (по флагу Junk или по названию)."""
    typ, data = m.list()
    for line in data or []:
        line = line.decode(errors="ignore") if isinstance(line, bytes) else str(line)
        mt = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
        if not mt:
            continue
        name = mt.group("name").strip()
        bare = name.strip('"')
        # «Спам» в IMAP-кодировке modified UTF-7 = &BCEEPwQwBDw-
        if "\\junk" in mt.group("flags").lower() or bare.lower() in ("spam", "junk", "&bcEepwqwbdw-".lower()):
            return name
    return None


def spam_is_relevant(msg, body):
    """Из спама достаём только то, что явно про вуз или олимпиаду, без рекламы."""
    tags = mail_tags(msg, body)
    addr = email.utils.parseaddr(_h(msg, "From"))[1].lower()
    domain = addr.split("@")[-1]
    if _domain_match(domain, UNI_DOMAINS) or _domain_match(domain, MAIL_VIP):
        return True  # настоящий адрес вуза/олимпиады
    if PROMO_RE.search(_h(msg, "Subject")) or PROMO_RE.search(body[:1500]):
        return False  # курсы «подготовка к олимпиаде со скидкой» и т.п.
    return bool({"олимп", "приглаш"} & set(tags)) or bool(STRONG_RE.search(_h(msg, "Subject")))


def scan_folder(m, folder, since, spam=False):
    typ, _ = m.select(folder, readonly=True)  # readonly: письма остаются непрочитанными
    if typ != "OK":
        return []
    crit = "(UNSEEN SINCE {})".format((since - timedelta(days=1)).strftime("%d-%b-%Y"))
    typ, data = m.search(None, crit)
    ids = data[0].split() if data and data[0] else []
    out = []
    for i in ids[-(100 if spam else 200):]:
        typ, resp = m.fetch(i, "(RFC822.SIZE BODY.PEEK[HEADER])")
        raw = next((p[1] for p in resp if isinstance(p, tuple)), b"")
        meta = b" ".join(p[0] if isinstance(p, tuple) else p for p in resp if p)
        size = int((re.search(rb"RFC822\.SIZE (\d+)", meta) or [0, b"0"])[1])
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
            addr = email.utils.parseaddr(_h(h, "From"))[1].lower()
            if _domain_match(addr.split("@")[-1], MAIL_BLOCK) or addr in MAIL_BLOCK:
                continue
            body = ""
            # если по заголовкам письмо рядовое, заглядываем в текст (не больше 1 МБ)
            if (spam or not mail_is_important(h)) and size < 1_000_000:
                typ, full = m.fetch(i, "(BODY.PEEK[])")
                body = body_text(next((p[1] for p in full if isinstance(p, tuple)), b""))
            if spam:
                if not spam_is_relevant(h, body):
                    continue
            elif not mail_is_important(h, body):
                continue
            tags = mail_tags(h, body)
            if spam:
                tags = ["из спама"] + [t for t in tags if t != "ответ"]
        except Exception as e:  # кривое письмо не должно ронять сводку
            log("mail error:", e)
            continue
        name, addr = email.utils.parseaddr(_h(h, "From"))
        out.append({"from": name or addr, "subject": _h(h, "Subject") or "(без темы)",
                    "when": when, "tags": tags})
    return out


def important_mail(since):
    if not (MAIL_USER and MAIL_PASS):
        return None
    m = imaplib.IMAP4_SSL("imap.mail.ru", 993)
    try:
        m.login(MAIL_USER, MAIL_PASS)
        out = scan_folder(m, "INBOX", since)
        try:
            sf = spam_folder(m)
            if sf:
                out += scan_folder(m, sf, since, spam=True)
            else:
                log("папка Спам не найдена")
        except Exception as e:
            log("spam:", e)
        # сначала письма с метками (ВУЗ, олимпиады, приглашения, ответы), внутри по времени
        out.sort(key=lambda x: x["when"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out.sort(key=lambda x: not x["tags"])
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def mail_block(since, limit=6):
    """Блок почты по шаблону; пустой раздел сжимается в одну строку."""
    try:
        mails = important_mail(since)
    except Exception as e:
        log("mail:", e)
        return "✉️ Почта: проверить не удалось"
    if mails is None:
        return None
    if not mails:
        return "✉️ Почта: важного нет"
    lines = []
    for x in mails[:limit]:
        plate = "".join(f"<code>{t}</code> " for t in x["tags"])
        lines.append(f"{plate}<b>{esc(x['from'])}</b>\n      {esc(x['subject'])}")
    if len(mails) > limit:
        lines.append(f"<i>…и ещё {len(mails) - limit}</i>")
    return f"<b>✉️ ПОЧТА · {len(mails)}</b>\n" + quote(lines)


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


def urgency(left):
    return "🔴" if left <= 3 else "🟠" if left <= 7 else "🟢"


def left_plate(left):
    if left == 0:
        return "<code>сегодня</code>"
    if left == 1:
        return "<code>завтра</code>"
    return f"<code>{left} {plural(left, 'день', 'дня', 'дней')}</code>"


def morning():
    process_updates(wait=False)  # вдруг вчера кнопку нажали поздно
    wait_until(MORNING_AT)
    d0 = today()
    tasks, projects, sections = load_todoist()

    head = [f"<b>☀️ {hello('Доброе утро')}!</b>",
            f"<i>{WD[d0.weekday()].capitalize()}, {d0.day} {MONTHS[d0.month - 1]}</i>"]
    try:
        head.append(weather_line(weather(), 0))
    except Exception as e:
        log("weather:", e)
    blocks = ["\n".join(head)]

    # обычные дела на сегодня + ДЕДЛ-задания, которые сегодня публикуют
    todays = sorted([t for t in tasks if due_date(t) == d0], key=sort_key)
    if todays:
        lines = []
        for t in todays:
            if is_dedl(t):
                dl = deadline_of(t)
                when = "сдать сегодня" if dl == d0 else f"сдать до {fmt_day(dl)}"
                lines.append(f"<code>вышло</code> {esc(strip_dedl(t['content']))} · {when}")
            else:
                lines.append(task_line(t))
        blocks.append(f"<b>📋 СЕГОДНЯ · {len(todays)}</b>\n" + quote(lines))
    else:
        blocks.append("📋 Сегодня: дел нет")

    # хвосты: обычные просроченные + ДЕДЛ, у которых дедлайн уже прошёл
    overdue = sorted([t for t in tasks if due_date(t) and (
        (not is_dedl(t) and due_date(t) < d0) or (is_dedl(t) and deadline_of(t) < d0))], key=sort_key)
    if overdue:
        lines = []
        for t in overdue[:7]:
            if is_dedl(t):
                dl = deadline_of(t)
                lines.append(f"<code>срок {dl.day:02d}.{dl.month:02d}</code> {esc(strip_dedl(t['content']))}")
            else:
                lines.append(task_line(t, show_date=True))
        if len(overdue) > 7:
            lines.append(f"<i>…и ещё {len(overdue) - 7}</i>")
        blocks.append(f"<b>⚠️ ХВОСТЫ · {len(overdue)}</b>\n" + quote(lines))

    dls = sorted([(deadline_of(t), t) for t in tasks
                  if deadline_of(t) and 0 <= (deadline_of(t) - d0).days <= 14], key=lambda x: x[0])
    if dls:
        lines = []
        for dl, t in dls[:6]:
            left = (dl - d0).days
            line = f"{urgency(left)} {esc(strip_dedl(t['content']))}\n      {left_plate(left)} · {fmt_day(dl)}"
            if is_dedl(t) and due_date(t) > d0:
                line += " · в день выхода" if due_date(t) == dl else f" · выйдет {fmt_day(due_date(t))}"
            lines.append(line)
        blocks.append("<b>🎯 ДЕДЛАЙНЫ</b>\n" + quote(lines))
    else:
        blocks.append("🎯 Дедлайнов в ближайшие две недели нет")

    mb = mail_block(now() - timedelta(hours=24))
    if mb:
        blocks.append(mb)

    send("\n\n".join(blocks))

    # Отдельное напоминание только накануне и в день дедлайна
    for dl, t in dls:
        left = (dl - d0).days
        if left in (0, 1):
            send(f"<b>⏰ ДЕДЛАЙН {when_left(left).upper()}</b>\n"
                 + quote([f"🔴 {esc(strip_dedl(t['content']))}\n      {left_plate(left)} · {fmt_day(dl)}"]))


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
    # ДЕДЛ-задания не переносим: их дата = день публикации, от неё считается срок сдачи
    left = sorted([t for t in tasks if due_date(t) and due_date(t) <= d0 and not is_dedl(t)], key=sort_key)
    soon = sorted([(deadline_of(t), t) for t in tasks
                   if deadline_of(t) and 0 <= (deadline_of(t) - d0).days <= 1
                   and due_date(t) and due_date(t) <= d0], key=lambda x: x[0])

    head = [f"<b>🌙 {hello('Добрый вечер')}!</b>",
            f"<i>Итоги дня · {WD[d0.weekday()]}, {d0.day} {MONTHS[d0.month - 1]}</i>"]
    try:
        head.append("Завтра: " + weather_line(weather(), 1))
    except Exception as e:
        log("weather:", e)
    blocks = ["\n".join(head)]

    if done:
        lines = [task_line({"content": t.get("content", "")}) for t in done[:10]]
        if len(done) > 10:
            lines.append(f"<i>…и ещё {len(done) - 10}</i>")
        blocks.append(f"<b>✅ СДЕЛАНО · {len(done)}</b>\n" + quote(lines))
    else:
        blocks.append("✅ Сделано: пока ничего не отмечено")

    tomorrow = d0 + timedelta(days=1)
    timed = sorted([t for t in tasks if due_date(t) == tomorrow and due_time(t)], key=sort_key)
    if timed:
        blocks.append("<b>📅 ЗАВТРА ПО ВРЕМЕНИ</b>\n" + quote([task_line(t) for t in timed]))

    if soon:
        lines = [f"🔴 {esc(strip_dedl(t['content']))}\n      {left_plate((dl - d0).days)} · {fmt_day(dl)}"
                 for dl, t in soon]
        blocks.append("<b>🎯 СКОРО СДАВАТЬ</b>\n" + quote(lines))

    if d0.weekday() == 6:
        blocks.append(weekly_stats(d0))

    # почта, пришедшая после утренней сводки
    h, mnt = map(int, MORNING_AT.split(":"))
    mb = mail_block(datetime(d0.year, d0.month, d0.day, h, mnt, tzinfo=TZ))
    if mb:
        blocks.append(mb)

    if not left:
        blocks.append("🎉 Все дела на сегодня закрыты")
        send("\n\n".join(blocks))
        if d0.weekday() == 6:
            send(week_plan(tasks, d0 + timedelta(days=1)))
        return

    # сами дела показаны кнопками ниже, отдельным списком не дублируем
    blocks.append(f"<b>⏳ ОСТАЛОСЬ · {len(left)}</b>\n"
                  "<i>Отмеченное ✅ перенесу на завтра. Нажми на дело, чтобы оставить его на месте ✖️</i>")
    shown = left[:40]
    kb = [[{"text": "✅ " + short(t["content"]), "callback_data": "t:" + t["id"]}] for t in shown]
    kb.append([{"text": "➡️ Перенести на завтра", "callback_data": "go:" + d0.isoformat()}])
    kb.append([{"text": "Ничего не переносить", "callback_data": "no"}])
    msg = send("\n\n".join(blocks), kb)
    if d0.weekday() == 6:
        send(week_plan(tasks, d0 + timedelta(days=1)))

    # Ждём нажатия до полуночи (если позже, обработает утренний запуск)
    end = datetime(d0.year, d0.month, d0.day, tzinfo=TZ) + timedelta(hours=EVENING_WAIT_UNTIL_HOUR)
    if NO_WAIT:  # ручной запуск: ждём кнопку не дольше 30 минут
        end = min(end, now() + timedelta(minutes=30))
    while now() < end:
        if process_updates(wait=True, until=end, stop_on_msg=msg["message_id"]):
            break


def week_plan(tasks, monday):
    """План на неделю: нагрузка по дням, время онлайн-занятий, выход и сдача ДЕДЛ-заданий."""
    days = [monday + timedelta(days=i) for i in range(7)]
    rows, load = [], {}
    for d in days:
        plain = [t for t in tasks if due_date(t) == d and not is_dedl(t)]
        out = [t for t in tasks if is_dedl(t) and due_date(t) == d]
        due = [t for t in tasks if is_dedl(t) and deadline_of(t) == d]
        n = len(plain) + len(out)
        load[d] = n
        parts = []
        if n:
            parts.append(f"{n} {plural(n, 'дело', 'дела', 'дел')}")
        for t in sorted([t for t in plain if due_time(t) and is_online(t)], key=sort_key):
            parts.append(f"🎥 <b><code>{due_time(t)}</code></b>")
        for t in out:
            same = deadline_of(t) == d
            parts.append(f"{'🎯' if same else '📤'} {esc(strip_dedl(t['content']))}"
                         + (" (выйдет и сдать в тот же день)" if same else " выйдет"))
        for t in due:
            if due_date(t) != d:
                parts.append(f"🎯 сдать {esc(strip_dedl(t['content']))}")
        rows.append((d, parts))
    top = max(load.values()) if load else 0
    lines = []
    for d, parts in rows:
        fire = "🔥 " if load[d] >= 5 or (load[d] >= 4 and load[d] == top) else ""
        body = " · ".join(parts) if parts else "<i>свободно</i>"
        lines.append(f"<code>{WD_SHORT[d.weekday()]} {d.day:02d}.{d.month:02d}</code> {fire}{body}")
    free = [WD_SHORT[d.weekday()] for d in days if not load[d]]
    busy = [WD_SHORT[d.weekday()] for d in days if load[d] and load[d] == top]
    summary = []
    if top:
        summary.append(f"Самый загруженный: {', '.join(busy)}")
    if free:
        summary.append(f"свободно: {', '.join(free)}")
    sunday = days[-1]
    head = (f"<b>🗓 ПЛАН НЕДЕЛИ</b>\n<i>{monday.day} {MONTHS[monday.month - 1]} – "
            f"{sunday.day} {MONTHS[sunday.month - 1]}</i>")
    text = head + "\n" + quote(lines)
    if summary:
        text += "\n<i>" + ". ".join(summary).capitalize() + "</i>"
    return text


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
    rows = [f"<code>{WD_SHORT[i]} {('█' * round(n / top * 8)) if n else '·':<8} {n}</code>"
            for i, n in enumerate(per)]
    return f"<b>📊 НЕДЕЛЯ · {total} {plural(total, 'дело', 'дела', 'дел')}</b>\n" + quote(rows)


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
        send("<b>👌 НИЧЕГО НЕ ПЕРЕНОШУ</b>\n<i>Все дела остались на своих местах</i>")
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Оставил как есть")
        return True

    if data.startswith("go:"):
        evening_day = date.fromisoformat(data[3:])
        target = max(evening_day + timedelta(days=1), today())
        ids = [btn["callback_data"][2:] for row in kb for btn in row
               if btn.get("callback_data", "").startswith("t:") and btn["text"].startswith("✅")]
        moved, skipped = reschedule(ids, target)
        word = "завтра" if target == today() + timedelta(days=1) else "сегодня" if target == today() else fmt_day(target)
        note = f"<b>➡️ ПЕРЕНЁС НА {word.upper()} · {moved}</b>\n<i>{fmt_long(target)}</i>"
        kept = sum(1 for row in kb for b in row if b.get("callback_data", "").startswith("t:")) - len(ids)
        extra = []
        if kept:
            extra.append(f"Оставил на месте: <code>{kept}</code>")
        if skipped:
            extra.append(f"Уже сделано или сдвинуто вручную: <code>{skipped}</code>")
        if extra:
            note += "\n" + quote(extra)
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
        if t.get("checked") or not due or due_date(t) >= target or is_dedl(t):
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


REMIND_BEFORE = int(os.environ.get("REMIND_BEFORE", "30"))  # минут
REMIND_EVERY = int(os.environ.get("REMIND_EVERY", "10"))    # как часто будит cron-job.org
REMIND_ALL = os.environ.get("REMIND_ALL") == "1"            # 1 = про все дела со временем


def remind():
    """Запускается каждые REMIND_EVERY минут. Напоминает об онлайн-занятиях,
    которые начнутся примерно через REMIND_BEFORE минут. Окно привязано к слоту
    расписания, поэтому одно занятие попадает ровно в один запуск."""
    n = now()
    slot = n.replace(second=0, microsecond=0) - timedelta(minutes=n.minute % REMIND_EVERY)
    lo = slot + timedelta(minutes=REMIND_BEFORE - REMIND_EVERY / 2)
    hi = slot + timedelta(minutes=REMIND_BEFORE + REMIND_EVERY / 2)
    d0 = today()
    tasks = td_all("/tasks")
    for t in sorted(tasks, key=sort_key):
        if due_date(t) != d0 or not due_time(t) or not (REMIND_ALL or is_online(t)):
            continue
        h, m = map(int, due_time(t).split(":"))
        start = datetime(d0.year, d0.month, d0.day, h, m, tzinfo=TZ)
        if lo < start <= hi:
            mins = max(1, round((start - n).total_seconds() / 60))
            icon = "🎥" if is_online(t) else "⏰"
            send(f"<b>{icon} ЧЕРЕЗ {mins} {plural(mins, 'МИНУТУ', 'МИНУТЫ', 'МИНУТ').upper()}</b>\n"
                 + quote([task_line(t)]))


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
        {"morning": morning, "evening": evening, "remind": remind}[mode]()
    except Exception as e:
        log("FATAL:", repr(e))
        try:
            send(f"⚠️ Бот споткнулся ({mode}): <code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
