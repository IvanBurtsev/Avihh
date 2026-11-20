"""Шифрование секретов, попадающих в кэш.

Токены площадок не должны лежать в Redis открытым текстом: кэш общий для всех
сервисов, и «временное» хранение токена в нём — такая же утечка, как в БД.
Шифр передаётся в адаптеры как непрозрачный объект (``FieldCipher`` из
``infra``), поэтому вызываем его методы защищённо: имена методов не зафиксированы
портами, а падение адаптера из-за несовпадения имени метода недопустимо.
Если шифр недоступен, значение сохраняется как есть и пишется предупреждение
в лог — деградация вместо отказа (раздел 2.6 ARCHITECTURE.md).
"""

from __future__ import annotations

from typing import Any, cast

from hirebot.logging_setup import get_logger

__all__ = ["open_secret", "seal_secret"]

logger = get_logger(__name__)


def _call(cipher: object | None, method: str, value: str) -> str | None:
    if cipher is None:
        return None
    func = getattr(cipher, method, None)
    if not callable(func):
        return None
    try:
        result = cast("Any", func)(value)
    except Exception as exc:
        logger.warning("secret_cipher_failed", method=method, error=str(exc))
        return None
    return result if isinstance(result, str) else None


def seal_secret(cipher: object | None, value: str) -> str:
    """Зашифровать секрет для хранения в кэше."""
    sealed = _call(cipher, "encrypt", value)
    if sealed is None:
        if cipher is not None and value:
            logger.debug("secret_stored_unencrypted", reason="cipher_has_no_encrypt")
        return value
    return f"enc:{sealed}"


def open_secret(cipher: object | None, value: str) -> str | None:
    """Расшифровать секрет, прочитанный из кэша.

    ``None`` означает «прочитать не удалось» (битые данные или отсутствующий
    ключ шифрования) — вызывающая сторона обязана в этом случае запросить
    токены заново, а не работать с мусором.
    """
    if not value.startswith("enc:"):
        return value
    sealed = value.removeprefix("enc:")
    opened = _call(cipher, "decrypt", sealed)
    if opened is None:
        logger.warning("secret_open_failed", reason="cipher_has_no_decrypt")
        return None
    return opened
