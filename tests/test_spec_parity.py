"""Сверка моделей SDK с OpenAPI-спекой.

Модели генерируются из спеки, но между «сгенерировано» и «попало в пакет»
есть шаги, где можно потерять точность: реэкспорт в `mailinfra.models`,
ручные уточнения, апгрейд генератора со сменой поведения. Этот тест
сравнивает то, что реально импортируется из пакета, с тем, что написано в
`https://docs.mailinfra.ru/openapi/mailinfra-public.json`: набор полей, обязательность, ограничения
и значения перечислений.

Если тест красный — либо перегенерируйте кодоген (`bash sdk/scripts/codegen.sh`),
либо поправьте слой публичных моделей. Спека правится только через
`backend/scripts/gen_openapi.py`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import annotated_types
import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from mailinfra import _validation, models

# Модели, которые SDK отдаёт наружу, и их схемы в спеке.
MODEL_TO_SCHEMA: dict[type[BaseModel], str] = {
    models.EmailSendRequest: "EmailSendRequest",
    models.EmailAttachmentInput: "EmailAttachmentInput",
    models.EmailSendResponseData: "EmailSendResponseData",
    models.BatchSendRequest: "BatchSendRequest",
    models.BatchSendResult: "BatchSendResult",
    models.BatchSendResponseData: "BatchSendResponseData",
    models.BatchSendError: "BatchSendError",
    models.EmailListItem: "EmailListItem",
    models.EmailListResponse: "EmailListResponse",
    models.EmailDetail: "EmailDetail",
    models.EmailEventPublic: "EmailEventPublic",
    models.EmailScheduleData: "EmailScheduleData",
    models.EmailRescheduleRequest: "EmailRescheduleRequest",
    models.ErrorResponse: "ErrorResponse",
    models.ErrorDetails: "ErrorDetails",
}

ENUM_TO_SCHEMA: dict[type[Enum], str] = {
    models.EmailStatus: "EmailStatus",
    models.EmailEventType: "EmailEventType",
    models.EmailSource: "EmailSource",
    models.BounceType: "BounceType",
    models.BounceCategory: "BounceCategory",
    models.DkimSigningStatus: "DkimSigningStatus",
    models.SuppressionReason: "SuppressionReason",
}


def _schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    """Достаёт схему компонента из спеки.

    Args:
        spec: Документ спеки.
        name: Имя схемы.

    Returns:
        Схема компонента.
    """
    schemas = spec["components"]["schemas"]
    assert name in schemas, f"В спеке нет схемы {name}"
    return schemas[name]


def _wire_names(model: type[BaseModel]) -> dict[str, FieldInfo]:
    """Возвращает поля модели под теми именами, что уходят в API.

    Args:
        model: Модель SDK.

    Returns:
        Карта «имя в JSON → описание поля».
    """
    return {(field.alias or name): field for name, field in model.model_fields.items()}


def _max_len(field: FieldInfo) -> int | None:
    """Извлекает ограничение длины/размера из метаданных поля.

    Pydantic кладёт и `max_length` строки, и максимум элементов списка в одну
    и ту же метку `annotated_types.MaxLen`.

    Args:
        field: Описание поля.

    Returns:
        Значение ограничения или `None`.
    """
    for meta in field.metadata:
        if isinstance(meta, annotated_types.MaxLen):
            return meta.max_length
    return None


def _min_len(field: FieldInfo) -> int | None:
    """Извлекает минимальную длину/размер из метаданных поля.

    Args:
        field: Описание поля.

    Returns:
        Значение ограничения или `None`.
    """
    for meta in field.metadata:
        if isinstance(meta, annotated_types.MinLen):
            return meta.min_length
    return None


def _spec_limit(property_schema: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Ищет ограничение в схеме свойства, в том числе внутри nullable-типа.

    Args:
        property_schema: Схема свойства.
        keys: Имена ключей-ограничений (`maxLength`, `maxItems`).

    Returns:
        Значение ограничения или `None`.
    """
    for key in keys:
        if key in property_schema:
            value = property_schema[key]
            if isinstance(value, int):
                return value
    return None


@pytest.mark.parametrize(
    ("model", "schema_name"), MODEL_TO_SCHEMA.items(), ids=lambda value: str(value)
)
def test_model_fields_match_spec(
    spec: dict[str, Any], model: type[BaseModel], schema_name: str
) -> None:
    schema = _schema(spec, schema_name)
    spec_properties: dict[str, Any] = schema.get("properties", {})
    fields = _wire_names(model)

    assert set(fields) == set(spec_properties), (
        f"{schema_name}: набор полей модели разошёлся со спекой"
    )


@pytest.mark.parametrize(
    ("model", "schema_name"), MODEL_TO_SCHEMA.items(), ids=lambda value: str(value)
)
def test_required_fields_match_spec(
    spec: dict[str, Any], model: type[BaseModel], schema_name: str
) -> None:
    schema = _schema(spec, schema_name)
    spec_required = set(schema.get("required", []))
    fields = _wire_names(model)

    model_required = {name for name, field in fields.items() if field.is_required()}

    # Поля, обязательные в спеке, но nullable — генератор делает
    # необязательными с дефолтом None. Такое расхождение допустимо только в
    # эту сторону: обязательное поле не должно стать обязательным на клиенте,
    # если API его не требует.
    extra_required = model_required - spec_required
    assert not extra_required, (
        f"{schema_name}: модель требует поля, которых спека не требует: {sorted(extra_required)}"
    )

    missing = {
        name
        for name in spec_required - model_required
        if not _is_nullable(spec["components"]["schemas"][schema_name]["properties"][name])
    }
    assert not missing, f"{schema_name}: спека требует поля, а модель нет: {sorted(missing)}"


def _is_nullable(property_schema: dict[str, Any]) -> bool:
    """Допускает ли схема свойства значение `null`.

    Args:
        property_schema: Схема свойства.

    Returns:
        `True`, если `null` допустим.
    """
    schema_type = property_schema.get("type")
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    any_of = property_schema.get("anyOf")
    if isinstance(any_of, list):
        return any(item == {"type": "null"} for item in any_of)
    return False


@pytest.mark.parametrize(
    ("model", "schema_name"), MODEL_TO_SCHEMA.items(), ids=lambda value: str(value)
)
def test_field_limits_match_spec(
    spec: dict[str, Any], model: type[BaseModel], schema_name: str
) -> None:
    """Лимиты из `app.services.limits` должны доезжать до клиента.

    Это и есть смысл клиентской валидации: 51-й получатель или тег длиннее
    32 символов отклоняются без сетевого вызова.
    """
    schema = _schema(spec, schema_name)
    fields = _wire_names(model)

    for name, property_schema in schema.get("properties", {}).items():
        expected_max = _spec_limit(property_schema, ("maxLength", "maxItems"))
        expected_min = _spec_limit(property_schema, ("minItems",))
        field = fields[name]

        if expected_max is not None:
            assert _max_len(field) == expected_max, (
                f"{schema_name}.{name}: ожидался максимум {expected_max}, "
                f"в модели {_max_len(field)}"
            )
        if expected_min is not None:
            assert _min_len(field) == expected_min, (
                f"{schema_name}.{name}: ожидался минимум {expected_min}, в модели {_min_len(field)}"
            )


@pytest.mark.parametrize(
    ("enum_type", "schema_name"), ENUM_TO_SCHEMA.items(), ids=lambda value: str(value)
)
def test_enum_values_match_spec(
    spec: dict[str, Any], enum_type: type[Enum], schema_name: str
) -> None:
    schema = _schema(spec, schema_name)

    assert {member.value for member in enum_type} == set(schema["enum"]), (
        f"{schema_name}: значения перечисления разошлись со спекой"
    )


@pytest.mark.parametrize(
    ("enum_type", "schema_name"), ENUM_TO_SCHEMA.items(), ids=lambda value: str(value)
)
def test_enums_are_string_subclasses(enum_type: type[Enum], schema_name: str) -> None:
    """Без `str`-миксина сравнение `status == "DELIVERED"` молча ложно."""
    assert issubclass(enum_type, str), f"{schema_name} должен быть подклассом str"


def test_every_public_schema_is_covered(spec: dict[str, Any]) -> None:
    """Новая схема в спеке не должна незаметно остаться без модели SDK."""
    covered = set(MODEL_TO_SCHEMA.values()) | set(ENUM_TO_SCHEMA.values())
    # Обёртки `DataResponse[...]` SDK разворачивает сам, наружу их не отдаёт.
    ignored = {name for name in spec["components"]["schemas"] if name.startswith("DataResponse")}

    missing = set(spec["components"]["schemas"]) - covered - ignored
    assert not missing, f"Схемы без публичной модели SDK: {sorted(missing)}"


@pytest.mark.parametrize(
    ("validating", "generated"),
    [
        (_validation.SendRequest, models.EmailSendRequest),
        (_validation.RescheduleRequest, models.EmailRescheduleRequest),
    ],
    ids=["send", "reschedule"],
)
def test_validating_models_keep_spec_fields(
    validating: type[BaseModel], generated: type[BaseModel]
) -> None:
    """Валидирующий подкласс добавляет проверки, но не поля.

    Иначе клиент отправлял бы то, чего нет в спеке, — а набор полей
    сгенерированной модели уже сверен с ней тестами выше.
    """
    assert _wire_names(validating).keys() == _wire_names(generated).keys()


def test_send_request_alias_is_from(spec: dict[str, Any]) -> None:
    """Поле `from` — ключевое слово Python; алиас обязан сохраниться."""
    assert "from" in _schema(spec, "EmailSendRequest")["properties"]

    field = models.EmailSendRequest.model_fields["from_"]
    assert field.alias == "from"
