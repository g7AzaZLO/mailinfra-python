"""Версия пакета.

Держится отдельным модулем, чтобы `User-Agent` собирался без импорта
`importlib.metadata` (пакет может быть запущен из исходников, где метаданных
ещё нет). Значение синхронно с `version` в pyproject.toml — расхождение ловит
тест `test_version_matches_pyproject`.
"""

from __future__ import annotations

__version__ = "0.2.0"
