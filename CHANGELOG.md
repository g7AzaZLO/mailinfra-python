# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

До `1.0.0` минорные версии могут содержать несовместимые изменения: пока не покрыт
Management API, публичная поверхность SDK ещё расширяется.

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
