# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

До `1.0.0` минорные версии могут содержать несовместимые изменения: пока не покрыт
Management API, публичная поверхность SDK ещё расширяется.

## [0.2.0] — 2026-10-28

### Добавлено

- `metadata` у письма: ваши пары «ключ — значение» в `send()` и в элементах
  `send_batch()`; возвращаются в `EmailDetail.metadata`, `EmailListItem.metadata` и
  в вебхуках (`WebhookEvent.metadata`). Лимиты API (20 ключей, ключ из
  `[A-Za-z0-9_.-]` до 40 символов, значение до 500 символов без NUL, весь объект до
  4096 байт JSON) проверяются до сетевого вызова: в спеке они записаны только в
  описании поля, поэтому генератор моделей их не видит.
- Отложенная отправка: параметр `send_at` (`datetime` с часовым поясом или ISO 8601
  строка со смещением), статус `SCHEDULED`, методы `emails.cancel(email_id)` и
  `emails.reschedule(email_id, send_at=...)`, возвращающие `EmailScheduleData`.
  Время без часового пояса отклоняется на клиенте с кодом `invalid_request` — у него
  нет однозначного момента, и письмо ушло бы на часовой пояс раньше или позже.
- `ttl` — срок жизни письма в секундах (60..86400); диапазон проверяется на клиенте.
- `Attachment.content_id` — inline-картинки, на которые HTML ссылается как
  `cid:<content_id>`.
- Курсорная пагинация: параметр `cursor` у `emails.list()` и обход всех страниц
  `emails.list_all(...)` — генератор у синхронного клиента, асинхронный итератор у
  `AsyncMailInfra`. Следующая страница запрашивается, только когда прочитана
  предыдущая.
- `Idempotency-Key` у `send_batch()`: SDK ставит его сам и переиспользует на
  повторах, свой ключ (до 252 символов) — `send_batch(..., idempotency_key=...)`.
  Результат батча — `SendBatchResult` с `replayed`, `idempotency_key` и
  `request_id`; он наследует `BatchSendResponseData`, так что прежний код с
  `results`, `accepted_count` и `failed_count` продолжает работать. Как и
  `SendEmailResult`, результат неизменяем.
- Исключения `EmailNotScheduledError` (409), `SendAtTooFarError` и
  `SendAtInvalidError` (400), `IPNotAllowedError` (403 — ключ использован с IP вне
  его белого списка).
- Значения перечислений из спеки: статусы `SCHEDULED` и `CANCELED`, события
  `scheduled` и `canceled` (вебхуки `email.scheduled` и `email.canceled`), категория
  bounce `delivery-expired`; поля писем `scheduled_at` и `expires_at`.

### Изменено

- **Несовместимо:** `emails.list()` возвращает страницу `EmailListResponse` (`data`,
  `has_more`, `next_cursor`) вместо голого списка, как теперь отвечает
  `GET /v1/emails`. Было `for email in client.emails.list(...)`, стало
  `client.emails.list(...).data` или `client.emails.list_all(...)` для всех страниц.
- `send_batch()` с ключом идемпотентности повторяется после `5xx`, таймаута и обрыва
  соединения, как одиночная отправка: бэкенд выводит из ключа батча ключ каждого
  письма, и повтор не дублирует уже принятые. Без ключа (`auto_idempotency=False`)
  батч по-прежнему повторяется только на `429`.
- Проверка подписи вебхука принимает событие, если совпала любая из подписей `v1`:
  во время ротации секрета их в заголовке две — новым секретом и прежним. Раньше
  бралась только последняя, и обработчик с новым секретом отвергал события сразу
  после ротации. Каждая подпись сравнивается за постоянное время.
- Элемент батча с `idempotency_key` по-прежнему отклоняется (`invalid_batch_item`),
  но сообщение подсказывает ключ на весь батч.

## [0.1.0] — 2026-10-27

Первый релиз.

### Добавлено

- Клиенты `MailInfra` и `AsyncMailInfra` поверх httpx.
- Ресурс писем: `send`, `send_batch`, `get`, `list`, `events`.
- Автоматический `Idempotency-Key` на отправке, переиспользуемый при повторах;
  флаг `replayed` в результате для идемпотентных повторов.
- Повторы с экспоненциальным backoff и half-jitter, уважение `Retry-After`,
  раздельная политика для идемпотентных и неидемпотентных запросов.
- Типизированные исключения по HTTP-статусу и по `error.code`; неизвестный код
  деградирует до класса по статусу.
- Локальная валидация письма по OpenAPI-спеке (лимиты получателей, тегов, темы,
  вложений) до сетевого вызова. Нарушение приходит как `MailInfraError` с кодом
  `invalid_request`, а не как `pydantic.ValidationError`: README учит ловить
  первое, и голый `ValidationError` пролетал бы мимо такого `except`.
- `Idempotency-Key` принимается только как печатаемый ASCII: httpx кодирует
  значение заголовка в ascii и на кириллице падает `UnicodeEncodeError` —
  голым исключением мимо `MailInfraError`. CR и LF отбиваются заодно.
- Модели pydantic v2, сгенерированные из <https://docs.mailinfra.ru/openapi/mailinfra-public.json>.
- `mailinfra.webhooks`: `verify`, `construct_event`, `sign` — проверка HMAC-подписи
  с защитой от replay.
- Определение окружения по префиксу ключа (`mi_live_` / `mi_test_`) и опция
  `require_environment`.

### Не входит

- Management API (домены, шаблоны, список подавления, вебхуки, API-ключи) — у него
  нет машинной аутентификации, см. README.
