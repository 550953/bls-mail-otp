"""Офлайн-тест разбора писем BLS и выдачи кодов. Запуск: python test_main.py"""
import os
import sys
import types
from email.message import EmailMessage
from email.utils import formatdate

os.environ.update(API_KEY="k", CENTRAL_1="central@mail.ru", CENTRAL_1_PASS="x",
                  CENTRAL1_AK1="f_a02@mail.ru", CENTRAL1_AK2="f_a03@mail.ru")

try:
    import fastapi  # noqa: F401
except ImportError:  # нет fastapi — подставляем заглушки, тестируем только логику
    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None, headers=None):
            self.status_code, self.detail = status_code, detail

    class FastAPI:
        def __init__(self, *a, **k):
            pass

        def _d(self, *a, **k):
            return lambda f: f
        get = post = _d

    fa.FastAPI, fa.HTTPException = FastAPI, HTTPException
    fa.Depends, fa.Header = (lambda *a, **k: None), (lambda *a, **k: "")
    resp = types.ModuleType("fastapi.responses")
    resp.HTMLResponse = object
    sec = types.ModuleType("fastapi.security")
    sec.HTTPBasic, sec.HTTPBasicCredentials = (lambda **k: None), object
    pyd = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **k):
            for a, b in k.items():
                setattr(self, a, b)
    pyd.BaseModel = BaseModel
    sys.modules.update({"fastapi": fa, "fastapi.responses": resp,
                        "fastapi.security": sec, "pydantic": pyd})

import main  # noqa: E402

C = main.CENTRALS[0]
ACCEPT = ("https://appointment.blsspainrussia.ru/global/appointment/"
          "dataprotectionemailaccept?data=dnXQoxuGB6%2fFkqOUjVbGquilPK8yR%2fub6vKIX7nD4i0Ki")
DECLINE = ACCEPT.replace("accept", "decline")
_uid = [0]


def feed(subject, body, to="f_a02@mail.ru", html=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = "BLS <info@blsspainrussia.ru>", to, subject, formatdate()
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    _uid[0] += 1
    C.handle(m.as_bytes(), _uid[0])


def task(acct, kind):
    return main.create_task(main.TaskIn(email=acct, type=kind))["task_id"]


def test_application_otp_dear_fallback():
    t = task("f_a02@mail.ru", "application")
    # в To только централ — аккаунт берётся из "Dear f_a02@mail.ru"
    feed("BLS Visa Appointment - Email Verification",
         "Dear f_a02@mail.ru\nGreetings from BLS International!\n\n"
         "Your verification code is as mentioned below\n303448\n\nFor your security, copy-paste is disabled.",
         to="central@mail.ru")
    r = main.get_task(t)
    assert r["status"] == "delivered" and r["code"] == "303448" and r["type"] == "application", r


def test_registration_otp_and_password():
    t1, t2 = task("f_a03@mail.ru", "registration"), task("f_a03@mail.ru", "password")
    feed("BLS Visa Appointment - User Verification",
         "Dear f_a03@mail.ru\nGreetings from BLS International!\n"
         "Your email verification code is as mentioned below\n255449\n", to="f_a03@mail.ru")
    feed("Welcome To BLS Appointment System",
         "Dear SASHA ABRAMOV,\nWelcome to the BLS appointment system. Your account has been successfully "
         "created. Please use below password to login the system.\nPassword: 279753\n", to="f_a03@mail.ru")
    a, b = main.get_task(t1), main.get_task(t2)
    assert a["code"] == "255449" and a["type"] == "registration", a
    assert b["code"] == "279753" and b["type"] == "password", b


def test_consent_link():
    t = task("f_a02@mail.ru", "consent")
    html = (f'<p>Additional information on data protection</p><a href="{ACCEPT}">I have read and consent</a>'
            f'<a href="{DECLINE}">I disagree</a>')
    feed("BLS - Data Protection Information", "Additional information on data protection",
         to="f_a02@mail.ru", html=html)
    r = main.get_task(t)
    assert r["status"] == "delivered" and r["link"] == ACCEPT and r["code"] is None, r


def test_consent_loose_match_single_task():
    t = task("f_a03@mail.ru", "consent")
    feed("BLS - Data Protection Information", "x", to="central@mail.ru",
         html=f'<a href="{ACCEPT}">ok</a>')  # аккаунт не определился -> единственное задание централа
    assert main.get_task(t)["link"] == ACCEPT


def test_any_ignores_password_and_consent():
    t = task("f_a02@mail.ru", "any")
    feed("Welcome To BLS Appointment System", "Password: 111222", to="f_a02@mail.ru")
    feed("BLS - Data Protection Information", "x", to="f_a02@mail.ru", html=f'<a href="{ACCEPT}">ok</a>')
    assert main.get_task(t)["status"] == "waiting"
    feed("BLS Visa Appointment - Email Verification", "Your verification code is as below 424242",
         to="f_a02@mail.ru")
    assert main.get_task(t)["code"] == "424242"


def test_ack_wipes_result():
    t = task("f_a02@mail.ru", "application")
    feed("BLS Visa Appointment - Email Verification", "Your verification code is as below 515151")
    assert main.get_task(t)["code"] == "515151"
    main.ack_task(t)
    assert main.get_task(t)["code"] is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
