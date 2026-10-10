# OTP broker

Запуск локально: `pip install -r requirements.txt && uvicorn main:app --reload` (переменные из `.env.example`).
Проверка разбора писем без почты и без fastapi: `python test_main.py`.

## Типы писем BLS (по темам)

| type | Тема письма | Что отдаём |
|---|---|---|
| `registration` | BLS Visa Appointment - User Verification | `code` |
| `application` (алиас `form`) | BLS Visa Appointment - Email Verification | `code` |
| `password` | Welcome To BLS Appointment System | `code` (временный пароль) |
| `consent` (алиас `link`) | BLS - Data Protection Information | `link` (кнопка согласия `...dataprotectionemailaccept?data=...`) |
| `activation` (алиас `activate`) | VFS Global: тема `Welcome`, в теле ссылка `.../activateemail?q=...` | `link` (ссылка активации аккаунта) |
| `any` | любое письмо с кодом, кроме `password`, `consent` и `activation` | `code` |

## Отправители
По умолчанию читаются письма от `info@blsspainrussia.ru` (BLS) и `donotreply@vfsglobal.com` (VFS Global).
Список меняется переменной `SENDERS=a@x.com,b@y.com`. Ссылка активации ищется по фрагменту `activateemail`
(`ACTIVATION_LINK_KEY`). Ссылка VFS действует 2 дня.

## API (заголовок `X-API-Key: <API_KEY>`)

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/api/tasks` `{"email":"alias@mail.ru","type":"registration"}` | создаёт задание, возвращает `task_id` |
| GET | `/api/tasks/{task_id}` | `waiting` — ждём; `delivered` + `code` или `link` — забрано; `expired` / `not_collected` |
| POST | `/api/tasks/{task_id}/ack` | (необязательно) клиент подтверждает получение, код/ссылка стираются из памяти |

Статусы: `waiting` → `ready` (письмо пришло, виден на дашборде) → `delivered` (клиент забрал) ·
`expired` (5 мин без письма) · `not_collected` (письмо пришло, клиент не вернулся за 5 мин).

```python
import requests, time
H = {"X-API-Key": KEY}
tid = requests.post(URL + "/api/tasks", json={"email": acc, "type": "consent"}, headers=H).json()["task_id"]
while True:
    time.sleep(5)
    s = requests.get(f"{URL}/api/tasks/{tid}", headers=H).json()
    if s["status"] == "delivered": result = s.get("link") or s["code"]; break
    if s["status"] in ("expired", "not_collected"): raise RuntimeError(s["status"])
requests.post(f"{URL}/api/tasks/{tid}/ack", headers=H)
```

## Дочитывание старых писем
Письма с кодами живут минуты (`MAX_MAIL_AGE_SECONDS`, 15 мин), а **ссылки активации VFS — до двух суток**
(`ACTIVATION_MAX_AGE_SECONDS`, 172800). Поэтому:
- при старте сервис читает ящики за последние `BACKFILL_HOURS` (по умолчанию 48 ч) и сохраняет письма активации,
  даже если они пришли до запуска или до создания задания;
- задание `activation` получит такую ссылку сразу, независимо от того, когда пришло письмо
  (если писем несколько, берётся самое свежее);
- повторно прочитать ящики без перезапуска: `POST /api/rescan` (заголовок `X-API-Key`), уже учтённые письма не дублируются.

## Как определяется аккаунт письма
1. Заголовки `To`, `Delivered-To`, `X-Original-To` и т.п. (совпадение с известным подчинённым).
2. Запасной вариант: `Dear alias@mail.ru` в тексте письма (так начинаются письма с кодами).
3. Если не определился (например, письмо согласия): код/ссылка уйдёт заданию, только если на этом централе
   ровно одно подходящее ожидающее задание. Иначе письмо видно на дашборде со знаком `?`.

## Дашборд
Метка «ссылка ↗» в заданиях и письмах кликабельна: открывает ссылку активации/согласия в новой вкладке.
Ссылка одноразовая, открывайте её один раз. Она видна только после входа по Basic Auth.

`GET /` — логин/пароль `DASH_USER` / `DASH_PASS` (Basic Auth).

Вёрстка лежит в `static/dashboard.html` (правится как обычный HTML), логотип и значки — в `static/`
(`logo.png`, `mark.png`, `favicon.png`; открыты без пароля, остальное в `static/` не раздаётся).
График за 24 часа, лента событий и средняя задержка хранятся только в памяти и обнуляются при перезапуске.
