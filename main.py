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
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
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
SENDER = env("BLS_SENDER", "info@blsspainrussia.ru").lower()
POLL = int(env("POLL_SECONDS", "5"))
TASK_TTL = int(env("TASK_TTL_SECONDS", "300"))      # задание протухает
CLAIM_TTL = int(env("CLAIM_TTL_SECONDS", "300"))    # код пришёл, а браузер не забрал
GRACE = int(env("MATCH_GRACE_SECONDS", "30"))       # письмо могло прийти чуть раньше задания
MAX_MAIL_AGE = int(env("MAX_MAIL_AGE_SECONDS", "900"))
# Темы писем BLS (по реальным письмам):
#   "BLS Visa Appointment - User Verification"  -> код при регистрации
#   "BLS Visa Appointment - Email Verification" -> код при заполнении анкеты
#   "Welcome To BLS Appointment System"         -> временный пароль
#   "BLS - Data Protection Information"         -> ссылка согласия (consent)
KW_REG = csv("KW_REGISTRATION", "user verification,registration,регистрац")
KW_APP = csv("KW_APPLICATION", "email verification,application,анкет")
KW_CONSENT = csv("KW_CONSENT", "data protection,защита данных")
# ссылка согласия определяется по этому фрагменту URL
CONSENT_LINK_KEY = env("CONSENT_LINK_KEY", "dataprotectionemailaccept").lower()
MAX_ACTIVE = int(env("MAX_ACTIVE_TASKS", "2000"))

KINDS = ("registration", "application", "password", "consent")
ALIASES = {"form": "application", "link": "consent", "data_protection": "consent"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
STOP = threading.Event()
LOCK = threading.RLock()
TASKS: dict = {}
MAILS: deque = deque(maxlen=300)
SEEN: dict = {}


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
    return next((u for u in links if CONSENT_LINK_KEY in u.lower()), "")


DEAR_RE = re.compile(r"Dear\s+([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)


def classify(subject, body, link=""):
    s, b = subject.lower(), body.lower()
    if (re.search(r"welcome to (the )?bls appointment system", s)
            or "please use below password" in b
            or "your account has been successfully created" in b):
        return "password"
    if link or any(k in s for k in KW_CONSENT):
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
    if m["ts"] < t["created"] - GRACE:
        return False
    k = m["kind"]
    return (t["type"] == k
            or (t["type"] == "any" and k not in ("password", "consent"))
            or (k == "unknown" and t["type"] not in ("password", "consent")))


def assign(t, m):
    t.update(status="ready", code=m["code"], link=m["link"], kind=m["kind"], ready_at=time.time())
    m["used_by"] = t["id"]
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
            elif t["status"] == "ready" and now - t["ready_at"] > CLAIM_TTL:
                t["status"] = "not_collected"
        for k in [k for k, t in TASKS.items() if now - t["created"] > 3 * 3600]:
            del TASKS[k]
        for k in [k for k, v in SEEN.items() if now - v > 7200]:
            del SEEN[k]


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
                self.status, self.error, self.last_ok, self.fails = "online", "", time.time(), 0
                STOP.wait(POLL)
            except Exception as e:
                self.fails += 1
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
        if self.last_uid is None:  # первый заход: старое не трогаем, берём только свежие
            self.last_uid = max(self.search("ALL"), default=0)
            since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
            uids = self.search("FROM", SENDER, "SINCE", since)
        else:
            uids = [u for u in self.search("UID", f"{self.last_uid + 1}:*", "FROM", SENDER)
                    if u > self.last_uid]
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
        if not m or m.group(0).lower() != SENDER:
            return
        try:
            d = parsedate_to_datetime(msg.get("Date"))
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            d = datetime.now(timezone.utc)
        if (datetime.now(timezone.utc) - d).total_seconds() > MAX_MAIL_AGE:
            return
        subject = dec(msg.get("Subject", ""))
        body, links = parse_body(msg)
        link = find_link(links)
        kind = classify(subject, body, link)
        code = "" if kind == "consent" else find_code(subject + " " + body)
        if not (code or link):
            return
        self.mails += 1
        ingest({"id": f"{self.n}:{uid}", "central": self.n, "account": self.detect(msg, body),
                "code": code, "link": link if kind == "consent" else "", "kind": kind,
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
    type: str = "any"   # registration | application | password | any


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


@app.get("/dash/state", dependencies=[Depends(require_dash)])
def dash_state():
    sweep()
    now = time.time()
    with LOCK:
        tasks = sorted(TASKS.values(), key=lambda t: -t["created"])
        stats = {k: sum(1 for t in tasks if t["status"] == k)
                 for k in ("waiting", "ready", "delivered", "expired", "not_collected")}
        return {
            "configured": bool(API_KEY), "accounts": len(ACCOUNTS), "stats": stats,
            "centrals": [{"n": c.n, "login": c.login, "subs": len(c.subs), "status": c.status,
                          "error": c.error, "poll_age": (now - c.last_ok) if c.last_ok else None,
                          "mails": c.mails} for c in CENTRALS],
            "tasks": [{"id": t["id"], "email": t["email"], "type": t["kind"] or t["type"],
                       "status": t["status"], "code": t["code"], "has_link": bool(t["link"]),
                       "age": now - t["created"]}
                      for t in tasks[:100]],
            "mails": [{"ts": m["ts"], "central": m["central"], "account": m["account"] or "?",
                       "kind": m["kind"], "code": m["code"], "has_link": bool(m["link"]),
                       "used_by": m["used_by"],
                       "subject": m["subject"]} for m in list(MAILS)[:50]],
        }


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_dash)])
def dashboard():
    return DASH_HTML


DASH_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OTP Farm</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--tx:#e6e8ee;--mut:#8a93a6;--ok:#3ddc84;--warn:#f5b942;--bad:#ff5d6c;--info:#5aa9ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,sans-serif;padding:16px}
h1{font-size:18px;margin:0 0 12px;display:flex;gap:10px;align-items:center}
.pill{font-size:12px;padding:2px 10px;border-radius:99px;background:var(--line)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:22px}.card span{color:var(--mut);font-size:12px}
section{margin-bottom:16px}h2{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:0 0 6px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{width:100%;border-collapse:collapse}th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:12px}tr:last-child td{border:0}
.code{font:600 15px ui-monospace,monospace;letter-spacing:.08em}
.tag{padding:1px 8px;border-radius:6px;font-size:12px}
.w{background:#3a3220;color:var(--warn)}.r{background:#16372a;color:var(--ok)}.d{background:#1b2d46;color:var(--info)}.e{background:#3b1f25;color:var(--bad)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.empty{color:var(--mut);text-align:center;padding:14px}
</style></head><body>
<h1>OTP Farm <span class="pill" id="pill">…</span></h1>
<div class="stats" id="stats"></div>
<section><h2>Задания</h2><div id="tasks"></div></section>
<section><h2>Последние коды</h2><div id="mails"></div></section>
<section><h2>Центральные ящики</h2><div id="centrals"></div></section>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ST={waiting:["Ждёт","w"],ready:["Код пришёл","r"],delivered:["Получено","d"],expired:["Протух","e"],not_collected:["Не забрал","e"]};
const KD={registration:"Регистрация",application:"Анкета",password:"Пароль",consent:"Согласие (ссылка)",unknown:"Не определён",any:"Любой"};
const age=s=>s==null?"—":s<60?Math.round(s)+" с":Math.floor(s/60)+" мин";
const tag=s=>{const[a,b]=ST[s]||[s,"w"];return `<span class="tag ${b}">${a}</span>`};
const tbl=(h,rows)=>`<div class="wrap"><table><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr>${rows||`<tr><td class="empty" colspan="${h.length}">Пусто</td></tr>`}</table></div>`;
function render(s){
 const on=s.centrals.filter(c=>c.status==="online").length,T=s.stats;
 $("#pill").textContent=s.configured?"работает":"НЕ ЗАПУЩЕН (нет API_KEY)";
 $("#pill").style.color=s.configured?"var(--ok)":"var(--bad)";
 $("#stats").innerHTML=[["Централы онлайн",on+" / "+s.centrals.length],["Аккаунтов",s.accounts],["Ждут",T.waiting],["Код пришёл",T.ready],["Получено",T.delivered],["Протухло",T.expired],["Не забрал",T.not_collected]]
  .map(([a,b])=>`<div class="card"><b>${b}</b><span>${a}</span></div>`).join("");
 $("#tasks").innerHTML=tbl(["ID","Аккаунт","Тип","Статус","Код","Возраст"],s.tasks.map(t=>`<tr><td>${esc(t.id)}</td><td>${esc(t.email)}</td><td>${KD[t.type]||esc(t.type)}</td><td>${tag(t.status)}</td><td class="code">${esc(t.code||(t.has_link?"ссылка":""))}</td><td>${age(t.age)}</td></tr>`).join(""));
 $("#mails").innerHTML=tbl(["Время","Ц.","Аккаунт","Тип","Код","Задание","Тема"],s.mails.map(m=>`<tr><td>${new Date(m.ts*1000).toLocaleTimeString("ru-RU")}</td><td>${m.central}</td><td>${esc(m.account)}</td><td>${KD[m.kind]||esc(m.kind)}</td><td class="code">${esc(m.code||(m.has_link?"ссылка":""))}</td><td>${esc(m.used_by||"—")}</td><td>${esc(m.subject)}</td></tr>`).join(""));
 $("#centrals").innerHTML=tbl(["№","Ящик","Акк.","Статус","Опрос","Писем BLS","Ошибка"],s.centrals.map(c=>{const col=c.status==="online"?"ok":c.status==="error"?"bad":"warn";return `<tr><td>${c.n}</td><td>${esc(c.login)}</td><td>${c.subs}</td><td><span class="dot" style="background:var(--${col})"></span>${c.status}</td><td>${age(c.poll_age)} назад</td><td>${c.mails}</td><td>${esc(c.error)}</td></tr>`}).join(""));
}
async function load(){try{const r=await fetch("/dash/state");if(!r.ok)throw r.status;render(await r.json())}catch(e){$("#pill").textContent="нет связи";$("#pill").style.color="var(--bad)"}}
load();setInterval(load,3000);
</script></body></html>
"""
