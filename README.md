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
| Идемпотентность | На каждую отправку ставится `Idempotency-Key`, и он **переиспользуется при повторах** — обрыв сети не превращается в два письма |
| Повторы | `429`, `408/425`, `5xx` и таймауты повторяются с экспоненциальным backoff и half-jitter |
| Rate limit | `Retry-After` уважается как есть; слишком долгий (>60 с) не блокирует поток, а приходит как `RateLimitError.retry_after` |
| Типизированные ошибки | `DomainNotVerifiedError`, `RateLimitError`, `UnprocessableError.fields` и ещё ~20 классов |
| Локальная валидация | Лимиты API (50 получателей, 10 тегов, 998 символов темы, 10 МиБ вложение) проверяются до сетевого вызова |
| Вебхуки | `mailinfra.webhooks.construct_event()` — проверка HMAC-подписи и разбор события |
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
> `error` у каждого элемента. `Idempotency-Key` этот эндпоинт не принимает, поэтому
> SDK **не** повторяет его после `5xx`: повтор мог бы отправить часть писем дважды.
> Повторяется только `429` — единственный отказ, который случается до приёма
> писем. Обрыв соединения и таймаут батча **не** повторяются: сервер мог уже
> закоммитить часть писем, поэтому решение о повторе за вами.

## Чтение

```python
email = client.emails.get(result.id)
print(email.status, email.delivered_at, email.final_smtp_code)

for event in client.emails.events(result.id):
    print(event.timestamp, event.type, event.data)

recent = client.emails.list(status="BOUNCED", tag="onboarding", limit=50)
```

Фильтры `list()`: `status`, `from_email`, `to_email`, `tag`, `from_date`, `to_date`,
`limit` (1..100). Enum-поля — подклассы `str`, поэтому `email.status == "DELIVERED"`
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
        mark_bounced(event.email_id, event.data["event"])

    return {"status": "ok"}
```

`construct_event` проверяет HMAC-SHA256 подпись и её возраст (по умолчанию 5 минут,
как на стороне MailInfra) и только потом разбирает JSON. Нужен просто флаг — есть
`webhooks.verify(...) -> bool`.

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
