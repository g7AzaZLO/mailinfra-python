# MailInfra SDK для Python

Официальный клиент [MailInfra](https://mailinfra.ru) — email API для разработчиков.

```bash
pip install mailinfra
```

```python
from mailinfra import MailInfra

client = MailInfra()  # ключ из MAILINFRA_API_KEY

result = client.emails.send(
    from_email="hello@mail.example.ru",
    to="user@example.com",
    subject="Добро пожаловать!",
    html="<p>Привет!</p>",
)
print(result.id, result.status)
```

Требования: Python 3.10+.

## Что клиент делает за вас

| Возможность | Подробности |
|---|---|
| Идемпотентность | На каждую отправку — и письма, и батча — ставится `Idempotency-Key`, и он **переиспользуется при повторах** — обрыв сети не превращается в два письма |
| Повторы | `429`, `408/425`, `5xx` и таймауты повторяются с экспоненциальным backoff и half-jitter |
| Rate limit | `Retry-After` уважается как есть; слишком долгий (>60 с) не блокирует поток, а приходит как `RateLimitError.retry_after` |
| Типизированные ошибки | `DomainNotVerifiedError`, `RateLimitError`, `UnprocessableError.fields` и ещё ~20 классов |
| Локальная валидация | Лимиты API (50 получателей, 10 тегов, 998 символов темы, 10 МиБ вложение, лимиты `metadata`, диапазон `ttl`, часовой пояс у `send_at`) проверяются до сетевого вызова |
| Пагинация | `list_all()` сам идёт по курсорам и отдаёт письма по одному, запрашивая страницы по мере чтения |
| Вебхуки | `mailinfra.webhooks.construct_event()` — проверка HMAC-подписи (в том числе двух подписей в окне ротации секрета) и разбор события |
| Типы | Модели pydantic v2, сгенерированные из OpenAPI-спеки MailInfra; пакет типизирован (`py.typed`) |

## Аутентификация

```python
client = MailInfra(api_key="mi_live_…")  # явно
client = MailInfra()  # из MAILINFRA_API_KEY
client = MailInfra(require_environment="live")  # упасть, если подсунули тестовый ключ
```

`mi_live_…` отправляет письма по-настоящему. `mi_test_…` — sandbox: письмо сохраняется
со статусом `SIMULATED`, не доставляется и не расходует месячный объём плана. Окружение
ключа доступно как `client.environment` (`"live"` / `"test"`).

Базовый URL меняется параметром `base_url=` или переменной `MAILINFRA_BASE_URL`.

## Отправка

### По шаблону

```python
client.emails.send(
    from_email="no-reply@mail.example.ru",
    to=["anna@example.com", "boris@example.com"],
    template="welcome",  # тема берётся из шаблона
    variables={"user_name": "Анна", "app_name": "MyApp"},
    tags=["onboarding"],
)
```

### С вложением

SDK сам закодирует байты в base64:

```python
from mailinfra import Attachment

client.emails.send(
    from_email="billing@mail.example.ru",
    to="user@example.com",
    subject="Счёт за май",
    text="Счёт во вложении.",
    attachments=[
        Attachment(
            filename="invoice.pdf",
            content=open("invoice.pdf", "rb").read(),
            content_type="application/pdf",
        )
    ],
)
```

#### Inline-картинки

Вложение с `content_id` встраивается в тело письма, а не прикладывается файлом; в HTML
на него ссылаются как `cid:<content_id>`:

```python
client.emails.send(
    from_email="hello@mail.example.ru",
    to="user@example.com",
    subject="Добро пожаловать!",
    html='<p><img src="cid:logo" alt="MyApp"></p><p>Привет!</p>',
    attachments=[
        Attachment(
            filename="logo.png",
            content=open("logo.png", "rb").read(),
            content_type="image/png",
            content_id="logo",
        )
    ],
)
```

### Свои метаданные

`metadata` — ваши пары «ключ — значение» (номер заказа, id пользователя). На доставку
они не влияют, а возвращаются в `get()`, `list()` и в каждом вебхуке письма — по ним
удобно связать письмо и события со своей системой:

```python
result = client.emails.send(
    from_email="billing@mail.example.ru",
    to="user@example.com",
    template="order_paid",
    metadata={"order_id": "4242", "user_id": "u-17"},
)
client.emails.get(result.id).metadata  # {"order_id": "4242", "user_id": "u-17"}
```

Лимиты проверяются до сетевого вызова: не больше 20 ключей; ключ — 1–40 символов
`[A-Za-z0-9_.-]`; значение — строка до 500 символов без NUL; весь объект — до 4096 байт
JSON в UTF-8.

### Отложенная отправка

`send_at` — когда отправить письмо: `datetime` **с часовым поясом** или ISO 8601 строка
со смещением, не дальше 7 дней вперёд. Письмо получает статус `SCHEDULED`; время в
прошлом — отправить сразу. Время без часового пояса SDK отклоняет до сетевого вызова
(`MailInfraError` с кодом `invalid_request`): у него нет однозначного момента.

```python
from datetime import datetime, timedelta, timezone

reminder = client.emails.send(
    from_email="billing@mail.example.ru",
    to="user@example.com",
    template="payment_reminder",
    send_at=datetime.now(timezone.utc) + timedelta(days=3),
)
print(reminder.status)  # SCHEDULED

# Пользователь оплатил — напоминание больше не нужно; резерв квоты вернётся.
client.emails.cancel(reminder.id)

# …или перенести на другое время.
client.emails.reschedule(reminder.id, send_at="2026-11-01T09:00:00+03:00")
```

`cancel()` и `reschedule()` возвращают `EmailScheduleData` (`id`, `status`,
`scheduled_at`) и требуют права на отправку. Горизонт переноса — 7 дней от приёма
письма, а не от переноса: дальше — `SendAtTooFarError`. Письмо, которое уже ушло в
очередь, отправлено или отменено, — `EmailNotScheduledError` (текущий статус в
`details["status"]`). Оба вызова SDK повторяет после `5xx` и таймаута, и если
`EmailNotScheduledError` пришёл на повторе, первая попытка, скорее всего, уже
выполнила операцию.

### Срок жизни письма

`ttl` — сколько секунд письмо остаётся актуальным (60..86400), считая от `send_at` или от
приёма. Не доставленное за это время письмо больше не отправляется и не повторяется:
статус `FAILED` с причиной `expired` или soft bounce с категорией `delivery-expired`.
Подходит для кодов входа и всего, что поздно уже не нужно:

```python
client.emails.send(
    from_email="auth@mail.example.ru",
    to="user@example.com",
    subject="Код входа: 271828",
    text="Код действует 10 минут.",
    ttl=600,
)
```

### Свой ключ идемпотентности

Если у вас есть естественный идентификатор операции — передайте его: повторный вызов
с тем же ключом не создаст второе письмо в течение 24 часов — столько бэкенд хранит
ключ. Позже тот же ключ — уже новый запрос.

```python
result = client.emails.send(
    from_email="no-reply@mail.example.ru",
    to="user@example.com",
    template="order_paid",
    variables={"order_id": 4242},
    idempotency_key="order_4242_paid",
)
if result.replayed:
    print("Письмо уже было отправлено раньше")
```

### Пакетная отправка

До 100 писем одним запросом:

```python
batch = client.emails.send_batch(
    [
        {
            "from_email": "no-reply@mail.example.ru",
            "to": "a@example.com",
            "template": "welcome",
            "variables": {"user_name": "Анна"},
        },
        {
            "from_email": "no-reply@mail.example.ru",
            "to": "b@example.com",
            "template": "welcome",
            "variables": {"user_name": "Борис"},
        },
    ]
)

print(batch.accepted_count, batch.failed_count)
for item in batch.results:
    if item.error:
        print(item.index, item.error.code, item.error.message)
```

> **Важно.** Батч возвращает `202`, даже если часть писем отклонена — проверяйте
> `error` у каждого элемента.

Элементы батча принимают те же параметры, что `send()` (`metadata`, `send_at`, `ttl`,
вложения), кроме `idempotency_key`: у батча ключ один на весь запрос. SDK ставит его сам
и переиспользует на повторах, а бэкенд выводит из него ключ каждого письма. Поэтому
повтор после `5xx`, таймаута или обрыва соединения не дублирует уже принятые письма, а
отклонённые пробует принять заново. Свой ключ (до 252 символов) — когда батч может
быть отправлен повторно уже вашим кодом, например после перезапуска задачи:

```python
batch = client.emails.send_batch(emails, idempotency_key="newsletter_2026_11")
if batch.replayed:
    print("Все письма этого батча уже были приняты раньше")
```

`replayed` — `True`, если ни одно письмо не принято заново (ответ `200` с
`Idempotent-Replayed: true`). С `auto_idempotency=False` и без своего ключа батч
неидемпотентен, и SDK повторяет его только на `429` — единственном отказе, который
случается до приёма писем.

## Чтение

```python
email = client.emails.get(result.id)
print(email.status, email.delivered_at, email.final_smtp_code)

for event in client.emails.events(result.id):
    print(event.timestamp, event.type, event.data)
```

### Список писем и пагинация

Письма отдаются страницами, новые сначала. `list()` возвращает одну страницу —
`EmailListResponse` с письмами в `data`, признаком `has_more` и курсором `next_cursor`:

```python
page = client.emails.list(status="BOUNCED", tag="onboarding", limit=50)
for email in page.data:
    print(email.id, email.status, email.metadata)

if page.has_more:
    page = client.emails.list(status="BOUNCED", tag="onboarding", limit=50, cursor=page.next_cursor)
```

Курсор непрозрачный: передавайте его как есть и с теми же фильтрами. Обойти все страницы
проще через `list_all()` — он сам идёт по `next_cursor`, пока `has_more` не станет
`False`, и запрашивает следующую страницу, только когда прочитана предыдущая:

```python
for email in client.emails.list_all(status="BOUNCED", limit=100):
    mark_bounced(email.id, email.metadata.get("user_id"))
```

У `list_all()` те же фильтры, что у `list()`; `limit` — размер одной страницы, а не общее
число писем, `cursor` — с какой страницы начать (например, сохранённый `next_cursor`).
В асинхронном клиенте это `async for email in client.emails.list_all(...)`.

Фильтры: `status`, `from_email`, `to_email`, `tag`, `from_date`, `to_date`, `limit`
(1..100), `cursor`. Enum-поля — подклассы `str`, поэтому `email.status == "DELIVERED"`
работает наравне с `email.status is EmailStatus.DELIVERED`.

## Ошибки

```python
from mailinfra import (
    DomainNotVerifiedError,
    RateLimitError,
    UnprocessableError,
    MailInfraError,
)

try:
    client.emails.send(...)
except DomainNotVerifiedError:
    ...  # домен ещё не подтверждён в DNS
except UnprocessableError as exc:
    for field in exc.fields:  # [{"field": "to", "message": "…"}]
        print(field["field"], field["message"])
except RateLimitError as exc:
    print("повторить через", exc.retry_after)
except MailInfraError as exc:  # базовый класс всех остальных
    print(exc.code, exc.message, exc.request_id)
```

У любой ошибки есть `.code`, `.message`, `.details`, `.status_code` и `.request_id` —
последний стоит логировать: с ним поддержка найдёт запрос в своих логах.

Конкретный класс всегда наследует класс своего HTTP-статуса: `DomainNotVerifiedError`
— это и `UnprocessableError` (422), `SandboxTrialExhaustedError` — и
`PermissionDeniedError` (403). Ловить можно на любом уровне: `MailInfraError`,
класс статуса или конкретная причина.

Коды отложенной отправки и доступа: `EmailNotScheduledError` (409, `email_not_scheduled`)
— отменить или перенести можно только письмо в статусе `SCHEDULED`;
`SendAtTooFarError` и `SendAtInvalidError` (400, `send_at_too_far` и `send_at_invalid`)
— время дальше 7 дней или без часового пояса; `IPNotAllowedError` (403,
`ip_not_allowed`) — ключ использован с IP-адреса вне его белого списка.

Неизвестный код ошибки (новый на бэкенде) приходит как класс по HTTP-статусу, а не
падает — обновление API не ломает старый SDK.

## Настройка повторов

```python
from mailinfra import MailInfra, RetryPolicy

client = MailInfra(
    max_retries=4,  # быстрый вариант
    timeout=10.0,
)

client = MailInfra(  # полный контроль
    retry_policy=RetryPolicy(
        max_retries=3,
        initial_backoff=0.5,
        backoff_factor=2.0,
        max_backoff=8.0,
        max_retry_after=60.0,  # выше этого не ждём, отдаём RateLimitError
    ),
)
```

Отключить авто-идемпотентность: `MailInfra(auto_idempotency=False)`.
Свой HTTP-клиент (прокси, mTLS, пул): `MailInfra(http_client=httpx.Client(...))`.

## Асинхронный клиент

```python
from mailinfra import AsyncMailInfra

async with AsyncMailInfra() as client:
    result = await client.emails.send(
        from_email="hello@mail.example.ru",
        to="user@example.com",
        subject="Привет",
        text="Привет!",
    )
```

API и поведение совпадают с синхронным клиентом.

## Вебхуки

```python
from fastapi import FastAPI, Request
from mailinfra import webhooks

app = FastAPI()


@app.post("/mailinfra/events")
async def handle(request: Request) -> dict[str, str]:
    event = webhooks.construct_event(
        body=await request.body(),  # именно сырые байты
        secret=os.environ["MAILINFRA_WEBHOOK_SECRET"],
        signature_header=request.headers["X-MailInfra-Signature"],
    )

    if event.type == "email.bounced":
        mark_bounced(event.email_id, event.metadata.get("user_id"), event.data["event"])

    return {"status": "ok"}
```

`construct_event` проверяет HMAC-SHA256 подпись и её возраст (по умолчанию 5 минут,
как на стороне MailInfra) и только потом разбирает JSON. Нужен просто флаг — есть
`webhooks.verify(...) -> bool`.

`event.metadata` — `metadata`, переданный при отправке письма (пустой словарь у событий
без письма). У отложенных писем есть события `email.scheduled` и `email.canceled`.

Ротация секрета не требует одновременного деплоя: пока прежний секрет действует,
MailInfra подписывает событие обоими, и в заголовке две подписи —
`t=…,v1=<новым секретом>,v1=<прежним>`. SDK принимает событие, если совпала **любая**
`v1`, поэтому обработчик со старым секретом продолжает работать, пока вы не выкатите
новый.

Тело обязательно брать **до** разбора JSON: пересериализованный JSON даст другие байты
и подпись не сойдётся.

## Что пока не покрыто

v1 покрывает публичный API писем (`/v1/emails*`) — всё, что доступно по API-ключу
проекта. Управление доменами, шаблонами, списком подавления, вебхуками и ключами — это
Management API на сессионном токене пользователя; для машинной интеграции у него пока
нет подходящего credential, поэтому в SDK этих ресурсов нет. Следите за
[CHANGELOG](CHANGELOG.md).

## Ссылки

- Документация: <https://docs.mailinfra.ru>
- Формат ошибок: <https://docs.mailinfra.ru/errors>
- Вебхуки: <https://docs.mailinfra.ru/webhooks/signature>
- OpenAPI-спека: <https://docs.mailinfra.ru/openapi/mailinfra-public.json>
- Исходный код и issues: <https://github.com/g7AzaZLO/mailinfra-python>. Репозиторий
  обновляется при каждом релизе SDK; ошибки и предложения — в issues, pull request'ы
  не принимаются: SDK разрабатывается вместе с API MailInfra.

## Лицензия

MIT
