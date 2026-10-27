"""Ресурсы публичного API MailInfra.

Сейчас это только письма: по API-ключу проекта открыт `/v1/emails*`.
Домены, шаблоны, список подавления, вебхуки и ключи живут в Management API
на сессионной аутентификации и появятся здесь, когда для них будет машинный
credential — см. README.
"""

from __future__ import annotations

from .emails import AsyncEmailsResource, EmailsResource

__all__ = ["AsyncEmailsResource", "EmailsResource"]
