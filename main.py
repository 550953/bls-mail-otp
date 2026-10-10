"""OTP broker: читает письма BLS с центральных ящиков, раздаёт коды по заданиям.

Всё берётся из переменных окружения (Render). Если API_KEY не задан —
сервис считается не запущенным: воркеры не стартуют, API отвечает 503.
"""
import email
import html
import imaplib
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("otp")


# ============================================================ настройки
def env(k, d=""):
    return os.environ.get(k, d).strip()


def csv(k, d):
    return [x.strip().lower() for x in env(k, d).split(",") if x.strip()]


API_KEY = env("API_KEY")
DASH_USER, DASH_PASS = env("DASH_USER"), env("DASH_PASS")
IMAP_SERVER = env("IMAP_SERVER", "imap.mail.ru")
# Отправители писем: BLS и VFS Global. Можно переопределить: SENDERS=a@x.com,b@y.com
# (старая переменная BLS_SENDER тоже работает и добавляется в список)
SENDERS = csv("SENDERS", "info@blsspainrussia.ru,donotreply@vfsglobal.com")
if env("BLS_SENDER") and env("BLS_SENDER").lower() not in SENDERS:
    SENDERS.append(env("BLS_SENDER").lower())
POLL = int(env("POLL_SECONDS", "5"))
TASK_TTL = int(env("TASK_TTL_SECONDS", "300"))      # задание протухает
CLAIM_TTL = int(env("CLAIM_TTL_SECONDS", "300"))    # код пришёл, а браузер не забрал
GRACE = int(env("MATCH_GRACE_SECONDS", "30"))       # письмо могло прийти чуть раньше задания
MAX_MAIL_AGE = int(env("MAX_MAIL_AGE_SECONDS", "900"))
# Ссылки активации живут ~2 суток: такие письма читаем «задним числом» и держим для будущих заданий
ACTIVATION_MAX_AGE = int(env("ACTIVATION_MAX_AGE_SECONDS", "172800"))
# На старте (и при /api/rescan) дочитываем ящики за столько часов назад
BACKFILL_HOURS = int(env("BACKFILL_HOURS", "48"))
# Темы писем BLS (по реальным письмам):
#   "BLS Visa Appointment - User Verification"  -> код при регистрации
#   "BLS Visa Appointment - Email Verification" -> код при заполнении анкеты
#   "Welcome To BLS Appointment System"         -> временный пароль
#   "BLS - Data Protection Information"         -> ссылка согласия (consent)
#   VFS Global: тема "Welcome", в теле ссылка .../activateemail?q=...  -> ссылка активации (activation)
KW_REG = csv("KW_REGISTRATION", "user verification,registration,регистрац")
KW_APP = csv("KW_APPLICATION", "email verification,application,анкет")
KW_CONSENT = csv("KW_CONSENT", "data protection,защита данных")
# ссылка согласия определяется по этому фрагменту URL
CONSENT_LINK_KEY = env("CONSENT_LINK_KEY", "dataprotectionemailaccept").lower()
# ссылка активации аккаунта (VFS Global) определяется по этому фрагменту URL
ACTIVATION_LINK_KEY = env("ACTIVATION_LINK_KEY", "activateemail").lower()
MAX_ACTIVE = int(env("MAX_ACTIVE_TASKS", "2000"))

KINDS = ("registration", "application", "password", "consent", "activation")
LINK_KINDS = ("consent", "activation")   # эти письма отдают link, а не code
ALIASES = {"form": "application", "link": "consent", "data_protection": "consent",
           "activate": "activation", "activate_account": "activation"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
STOP = threading.Event()
LOCK = threading.RLock()
TASKS: dict = {}
MAILS: deque = deque(maxlen=1500)
SEEN: dict = {}
START = time.time()
EVENTS: deque = deque(maxlen=80)    # лента событий для дашборда (только в памяти)
HOURLY: dict = {}                   # час -> {ready, delivered, failed}: график за 24 ч
LAT: deque = deque(maxlen=100)      # сколько секунд от задания до письма
BASE = Path(__file__).resolve().parent


def event(kind, who="", note=""):
    EVENTS.appendleft({"ts": time.time(), "kind": kind, "who": who, "note": note})


def bump(field):
    with LOCK:
        h = HOURLY.setdefault(int(time.time() // 3600), {"ready": 0, "delivered": 0, "failed": 0})
        h[field] += 1


# ============================================================ разбор писем
def dec(v):
    out = ""
    for p, enc in decode_header(v or ""):
        if isinstance(p, bytes):
            try:
                out += p.decode(enc or "utf-8", "ignore")
            except LookupError:
                out += p.decode("utf-8", "ignore")
        else:
            out += p
    return out


def parse_body(msg):
    chunks = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        raw = part.get_payload(decode=True)
        if not raw:
            continue
        try:
            chunks.append(raw.decode(part.get_content_charset() or "utf-8", "ignore"))
        except LookupError:
            chunks.append(raw.decode("utf-8", "ignore"))
    t = "\n".join(chunks)
    links = [html.unescape(u).rstrip(".,;)") for u in
             re.findall(r"""href\s*=\s*["']([^"']+)["']""", t, re.I)
             + re.findall(r"https?://[^\s\"'<>]+", t)]
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)), links


CODE_PATTERNS = [
    r"(?:verification\s+code|code|otp)[^\d]{0,40}(\d{4,8})",
    r"(?:код|код\s+подтверждения)[^\d]{0,40}(\d{4,8})",
    r"(?:password|пароль)[^\d]{0,40}(\d{4,8})",
]


def find_code(text):
    for p in CODE_PATTERNS:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    m = re.search(r"(?<!\d)\d{6}(?!\d)", text)
    return m.group(0) if m else ""


def find_link(links):
    """Первая ссылка согласия (BLS) или активации (VFS). Возвращает (ссылка, вид)."""
    for u in links:
        if ACTIVATION_LINK_KEY in u.lower():
            return u, "activation"
    for u in links:
        if CONSENT_LINK_KEY in u.lower():
            return u, "consent"
    return "", ""


DEAR_RE = re.compile(r"Dear\s+([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)


def classify(subject, body, link="", link_kind=""):
    s, b = subject.lower(), body.lower()
    if link_kind == "activation":   # раньше "password": фраза "account has been successfully created" есть и тут
        return "activation"
    if (re.search(r"welcome to (the )?bls appointment system", s)
            or "please use below password" in b
            or "your account has been successfully created" in b):
        return "password"
    if link_kind == "consent" or any(k in s for k in KW_CONSENT):
        return "consent"
    if any(k in s for k in KW_REG):
        return "registration"
    if any(k in s for k in KW_APP):
        return "application"
    return "unknown"


# ============================================================ брокер заданий
def fits(t, m, loose=False):
    """loose=True: аккаунт в письме не определён, сверяем только централ."""
    if m["used_by"] or t["status"] != "waiting":
        return False
    if loose:
        if t["central"] != m["central"]:
            return False
    elif t["email"] != m["account"]:
        return False
    if m["kind"] != "activation" and m["ts"] < t["created"] - GRACE:
        return False   # у ссылки активации срок жизни сутки+, письмо могло прийти до задания
    k = m["kind"]
    return (t["type"] == k
            or (t["type"] == "any" and k not in ("password",) + LINK_KINDS)
            or (k == "unknown" and t["type"] not in ("password",) + LINK_KINDS))


def assign(t, m):
    t.update(status="ready", code=m["code"], link=m["link"], kind=m["kind"], ready_at=time.time())
    m["used_by"] = t["id"]
    bump("ready")
    LAT.append(max(0.0, t["ready_at"] - t["created"]))
    event("ready", t["email"], m["kind"])
    log.info("task %s <- %s for %s", t["id"], m["kind"], t["email"])


def ingest(m):
    with LOCK:
        if m["id"] in SEEN:
            return
        SEEN[m["id"]] = time.time()
        MAILS.appendleft(m)
        waiting = sorted((t for t in TASKS.values() if t["status"] == "waiting"),
                         key=lambda t: t["created"])
        for t in waiting:
            if fits(t, m):
                assign(t, m)
                break
        else:
            # аккаунт не определился: отдаём, только если задание на этом централе одно
            if m["account"] in ("", CENTRAL_LOGINS.get(m["central"])):
                cand = [t for t in waiting if fits(t, m, loose=True)]
                if len(cand) == 1:
                    assign(cand[0], m)


def sweep():
    now = time.time()
    with LOCK:
        for t in TASKS.values():
            if t["status"] == "waiting" and now - t["created"] > TASK_TTL:
                t["status"] = "expired"
                bump("failed")
                event("expired", t["email"])
            elif t["status"] == "ready" and now - t["ready_at"] > CLAIM_TTL:
                t["status"] = "not_collected"
                bump("failed")
                event("not_collected", t["email"])
        for k in [k for k, t in TASKS.items() if now - t["created"] > 3 * 3600]:
            del TASKS[k]
        for k in [k for k, v in SEEN.items() if now - v > ACTIVATION_MAX_AGE + 3600]:
            del SEEN[k]
        for k in [k for k in HOURLY if k < now // 3600 - 26]:
            del HOURLY[k]


def sweeper():
    while not STOP.wait(2):
        sweep()


# ============================================================ центральные ящики
class Central:
    def __init__(self, n, login, pw, subs):
        self.n, self.login, self.pw, self.subs = n, login.lower(), pw, subs
        self.accounts = set(subs) | {self.login}
        self.conn = None
        self.last_uid = None
        self.status, self.error, self.last_ok = "idle", "", 0.0
        self.fails, self.mails = 0, 0

    def rescan(self):
        """Заново прочитать окно BACKFILL_HOURS (уже учтённые письма пропускаются по SEEN)."""
        self.last_uid = None

    def start(self):
        threading.Thread(target=self.loop, name=f"central-{self.n}", daemon=True).start()

    def close(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def loop(self):
        STOP.wait(0.3 * self.n)  # не стартуем все 10 коннектов разом
        while not STOP.is_set():
            try:
                if not self.conn:
                    c = imaplib.IMAP4_SSL(IMAP_SERVER, 993, timeout=30)
                    c.login(self.login, self.pw)
                    self.conn = c
                self.poll()
                if self.status == "error":
                    event("central_ok", self.login)
                self.status, self.error, self.last_ok, self.fails = "online", "", time.time(), 0
                STOP.wait(POLL)
            except Exception as e:
                self.fails += 1
                if self.status != "error":
                    event("central_error", self.login, type(e).__name__)
                self.status, self.error = "error", f"{type(e).__name__}: {e}"[:200]
                log.warning("central %s: %s", self.n, self.error)
                self.close()
                STOP.wait(min(60, POLL * 2 ** min(self.fails, 4)))

    def search(self, *crit):
        typ, d = self.conn.uid("SEARCH", None, *crit)
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        return [int(x) for x in (d[0] or b"").split()]

    def poll(self):
        c = self.conn
        if c.select("INBOX", readonly=True)[0] != "OK":
            raise RuntimeError("IMAP select failed")
        if self.last_uid is None:  # первый заход / rescan: дочитываем письма за BACKFILL_HOURS
            self.last_uid = max(self.search("ALL"), default=0)
            # SINCE работает по датам, берём с запасом в сутки; точный возраст проверяет handle()
            since = (datetime.now(timezone.utc) - timedelta(hours=BACKFILL_HOURS + 24)).strftime("%d-%b-%Y")
            uids = sorted({u for s in SENDERS for u in self.search("FROM", s, "SINCE", since)})
        else:
            uids = sorted({u for s in SENDERS
                           for u in self.search("UID", f"{self.last_uid + 1}:*", "FROM", s)
                           if u > self.last_uid})
            allnew = self.search("UID", f"{self.last_uid + 1}:*")
            self.last_uid = max([self.last_uid] + [u for u in allnew if u > self.last_uid])
        for u in sorted(uids):
            _, data = c.uid("FETCH", str(u), "(BODY.PEEK[])")
            raw = next((i[1] for i in data or [] if isinstance(i, tuple)), None)
            if raw:
                self.handle(raw, u)

    def detect(self, msg, body=""):
        found = []
        for h in ("Delivered-To", "X-Original-To", "X-Forwarded-To", "Resent-To",
                  "Envelope-To", "X-Envelope-To", "Original-Recipient", "To", "Cc"):
            for v in msg.get_all(h, []):
                found += [a.lower() for a in EMAIL_RE.findall(str(v))]
        for a in found:
            if a in self.subs:
                return a
        d = DEAR_RE.search(body)  # "Dear alias@mail.ru" в теле письма — запасной вариант
        if d and d.group(1).lower() in self.subs:
            return d.group(1).lower()
        return self.login if self.login in found else ""

    def handle(self, raw, uid):
        msg = email.message_from_bytes(raw)
        m = EMAIL_RE.search(str(msg.get("From", "")))
        if not m or m.group(0).lower() not in SENDERS:
            return
        try:
            d = parsedate_to_datetime(msg.get("Date"))
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            d = datetime.now(timezone.utc)
        age = (datetime.now(timezone.utc) - d).total_seconds()
        if age > ACTIVATION_MAX_AGE:
            return
        subject = dec(msg.get("Subject", ""))
        body, links = parse_body(msg)
        link, link_kind = find_link(links)
        kind = classify(subject, body, link, link_kind)
        if age > MAX_MAIL_AGE and kind != "activation":
            return   # старые коды бесполезны, дочитываем только ссылки активации
        code = "" if kind in LINK_KINDS else find_code(subject + " " + body)
        if not (code or link):
            return
        self.mails += 1
        ingest({"id": f"{self.n}:{uid}", "central": self.n, "account": self.detect(msg, body),
                "code": code, "link": link if kind in LINK_KINDS else "", "kind": kind,
                "subject": subject[:80], "ts": d.timestamp(), "used_by": None})


def load_centrals():
    out = []
    for n in range(1, 51):
        login = env(f"CENTRAL_{n}")
        if not login:
            continue
        subs = [env(f"CENTRAL{n}_AK{k}").lower() for k in range(1, 51) if env(f"CENTRAL{n}_AK{k}")]
        out.append(Central(n, login, env(f"CENTRAL_{n}_PASS"), subs))
    return out


CENTRALS = load_centrals()
ACCOUNT_CENTRAL = {a: c.n for c in CENTRALS for a in c.accounts}
ACCOUNTS = set(ACCOUNT_CENTRAL)
CENTRAL_LOGINS = {c.n: c.login for c in CENTRALS}


# ============================================================ API
@asynccontextmanager
async def lifespan(_):
    if API_KEY:
        for c in CENTRALS:
            c.start()
        threading.Thread(target=sweeper, daemon=True).start()
        log.info("started: %d centrals, %d accounts", len(CENTRALS), len(ACCOUNTS))
    else:
        log.warning("API_KEY не задан — сервис не запущен")
    yield
    STOP.set()


app = FastAPI(title="OTP broker", lifespan=lifespan)
basic = HTTPBasic(auto_error=False)


def _eq(a: str, b: str):
    return secrets.compare_digest(a.encode(), b.encode())


def require_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(503, "service not configured")
    if not _eq(x_api_key, API_KEY):
        raise HTTPException(401, "bad api key")


def require_dash(cred: Optional[HTTPBasicCredentials] = Depends(basic)):
    if not (DASH_USER and DASH_PASS):
        raise HTTPException(503, "dashboard not configured")
    if not (cred and _eq(cred.username, DASH_USER) and _eq(cred.password, DASH_PASS)):
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": 'Basic realm="otp"'})


class TaskIn(BaseModel):
    email: str
    type: str = "any"   # registration | application | password | consent | activation | any


@app.get("/health")
def health():
    return {"ok": True, "configured": bool(API_KEY)}


@app.post("/api/tasks", status_code=201, dependencies=[Depends(require_key)])
def create_task(body: TaskIn):
    sweep()
    acct, kind = body.email.strip().lower(), (body.type.strip().lower() or "any")
    kind = ALIASES.get(kind, kind)
    if kind not in KINDS + ("any",):
        raise HTTPException(422, f"type must be one of {KINDS + ('any',)}")
    if acct not in ACCOUNTS:
        raise HTTPException(404, "unknown account")
    t = {"id": secrets.token_hex(4), "email": acct, "central": ACCOUNT_CENTRAL[acct],
         "type": kind, "status": "waiting", "created": time.time(), "code": "", "link": "",
         "kind": None, "ready_at": None, "delivered_at": None}
    with LOCK:
        if sum(1 for x in TASKS.values() if x["status"] in ("waiting", "ready")) >= MAX_ACTIVE:
            raise HTTPException(429, "too many active tasks")
        TASKS[t["id"]] = t
        for m in MAILS:  # письмо могло прийти за секунды до задания
            if fits(t, m):
                assign(t, m)
                break
    return {"task_id": t["id"], "status": t["status"], "expires_in": TASK_TTL, "poll_interval": POLL}


@app.get("/api/tasks/{tid}", dependencies=[Depends(require_key)])
def get_task(tid: str):
    sweep()
    with LOCK:
        t = TASKS.get(tid)
        if not t:
            raise HTTPException(404, "task not found")
        out = {"task_id": tid, "email": t["email"], "status": t["status"]}
        if t["status"] in ("ready", "delivered"):
            if t["status"] == "ready":
                t.update(status="delivered", delivered_at=time.time())
                bump("delivered")
                event("delivered", t["email"], t["kind"] or "")
            out.update(status="delivered", type=t["kind"], code=t["code"] or None,
                       link=t["link"] or None)
        elif t["status"] == "waiting":
            out["expires_in"] = max(0, int(TASK_TTL - (time.time() - t["created"])))
        return out


@app.post("/api/tasks/{tid}/ack", dependencies=[Depends(require_key)])
def ack_task(tid: str):
    """Клиент подтверждает, что результат получен: код/ссылка стираются из памяти."""
    with LOCK:
        t = TASKS.get(tid)
        if not t:
            raise HTTPException(404, "task not found")
        if t["status"] in ("waiting", "ready"):
            raise HTTPException(409, "result not fetched yet")
        if t["status"] == "delivered":
            t.update(code="", link="")
        else:
            raise HTTPException(410, f"task is {t['status']}")
        return {"task_id": tid, "status": "delivered", "acked": True}


@app.post("/api/rescan", dependencies=[Depends(require_key)])
def rescan():
    """Заставить все централы заново дочитать письма за BACKFILL_HOURS."""
    for c in CENTRALS:
        c.rescan()
    event("rescan", "", f"{BACKFILL_HOURS} ч")
    return {"ok": True, "centrals": len(CENTRALS), "hours": BACKFILL_HOURS}


@app.get("/dash/state", dependencies=[Depends(require_dash)])
def dash_state():
    sweep()
    now = time.time()
    with LOCK:
        tasks = sorted(TASKS.values(), key=lambda t: -t["created"])
        stats = {k: sum(1 for t in tasks if t["status"] == k)
                 for k in ("waiting", "ready", "delivered", "expired", "not_collected")}
        cur = int(now // 3600)
        hourly = [{"h": (cur - i) * 3600, **HOURLY.get(cur - i, {"ready": 0, "delivered": 0, "failed": 0})}
                  for i in range(23, -1, -1)]
        return {
            "configured": bool(API_KEY), "accounts": len(ACCOUNTS), "stats": stats,
            "now": now, "uptime": now - START, "hourly": hourly,
            "latency": (sum(LAT) / len(LAT)) if LAT else None,
            "events": list(EVENTS)[:30],
            "centrals": [{"n": c.n, "login": c.login, "subs": len(c.subs), "status": c.status,
                          "error": c.error, "poll_age": (now - c.last_ok) if c.last_ok else None,
                          "mails": c.mails} for c in CENTRALS],
            "tasks": [{"id": t["id"], "email": t["email"], "type": t["kind"] or t["type"],
                       "status": t["status"], "code": t["code"], "has_link": bool(t["link"]), "link": t["link"],
                       "age": now - t["created"]}
                      for t in tasks[:100]],
            "mails": [{"ts": m["ts"], "central": m["central"], "account": m["account"] or "?",
                       "kind": m["kind"], "code": m["code"], "has_link": bool(m["link"]), "link": m["link"],
                       "used_by": m["used_by"],
                       "subject": m["subject"]} for m in list(MAILS)[:50]],
        }


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_dash)])
def dashboard():
    return DASH_HTML


# логотип и значки открыты без пароля: браузер просит фавикон до логина
STATIC_FILES = {"logo-wide.png", "favicon.png"}


@app.get("/static/{name}")
def static_file(name: str):
    if name not in STATIC_FILES:
        raise HTTPException(404, "not found")
    return FileResponse(BASE / "static" / name, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(BASE / "static" / "favicon.png", media_type="image/png")


DASH_HTML = (BASE / "static" / "dashboard.html").read_text(encoding="utf-8")
