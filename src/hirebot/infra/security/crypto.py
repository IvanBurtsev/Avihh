"""Шифрование персональных данных (152-ФЗ) и blind index для поиска.

Что защищаем и почему именно так:

* **Резюме и контакты** (``resumes.raw_text``, ``candidates.phone/email``)
  хранятся в БД зашифрованными. Утечка дампа БД не должна превращаться в утечку
  ПДн кандидатов.
* **Поиск по телефону/email** нужен, но шифрование non-deterministic (Fernet),
  поэтому равенство шифротекстов ничего не значит. Для поиска используется
  *blind index* — детерминированный HMAC от нормализованного значения. По нему
  можно найти кандидата, но восстановить значение нельзя (в отличие от простого
  хэша, HMAC с ключом не поддаётся перебору без ключа).
* **Ключ — вне БД.** Если ключ лежит рядом с данными, шифрование не защищает
  ни от чего.

Без ``HIREBOT_SECURITY__PII_KEY`` в local-режиме шифрование отключается
(passthrough) с однократным предупреждением: демо должно запускаться без ключей.
В ``production`` такой конфиг не собирается вовсе (см. ``config.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from hirebot.config import Settings
from hirebot.domain.errors import DomainError
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Префикс шифротекста. Позволяет отличить зашифрованное значение от legacy
#: открытого (важно при миграции уже существующих данных).
CIPHER_PREFIX: Final[str] = "enc:v1:"

#: Ключ для blind index в режиме без PII_KEY. Он не секретный: это осознанный
#: компромисс демо-режима, где ПДн и так лежат открытым текстом.
_INSECURE_INDEX_KEY: Final[bytes] = b"hirebot-insecure-blind-index-v1"

_warn_lock = threading.Lock()
_warned = False


class PiiDecryptionError(DomainError):
    """Не удалось расшифровать ПДн (сменился ключ или повреждён шифротекст)."""


def _derive_index_key(secret: bytes) -> bytes:
    """Вывести ключ blind index из ключа шифрования.

    Отдельный ключ нужен, чтобы blind index и Fernet не зависели от одного и
    того же использования ключевого материала.
    """
    return hmac.new(secret, b"hirebot:blind-index:v1", hashlib.sha256).digest()


def normalize_for_index(value: str) -> str:
    """Нормализовать значение перед blind index.

    Телефон приходит из площадок в разных форматах (``+7 (999) 123-45-67``,
    ``8 999 1234567``). Без нормализации поиск по телефону не работает: одно и
    то же значение даёт разные индексы. Приводим телефоны к цифрам, e-mail — к
    нижнему регистру.
    """
    text = value.strip().casefold()
    if "@" in text:
        return text
    digits = "".join(char for char in text if char.isdigit())
    if not digits:
        return text
    # Российские номера приходят в двух форматах: «8 999 …» и «+7 999 …».
    # Без приведения к одному виду поиск по телефону промахивается.
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits


def _warn_passthrough_once() -> None:
    """Однократно предупредить, что ПДн хранятся открытыми."""
    global _warned
    with _warn_lock:
        if _warned:
            return
        _warned = True
    logger.warning(
        "pii_encryption_disabled",
        note=(
            "HIREBOT_SECURITY__PII_KEY не задан: ПДн (телефон, e-mail, текст резюме) "
            "хранятся в открытом виде. Допустимо только для local/демо."
        ),
    )


class FieldCipher:
    """Шифрование полей ПДн и blind index для поиска.

    Использование::

        cipher = FieldCipher.from_settings(settings)
        row.phone = cipher.encrypt_optional(candidate.phone)
        row.phone_index = cipher.blind_index(candidate.phone) if candidate.phone else None
        # обратно:
        candidate.phone = cipher.decrypt_optional(row.phone)
    """

    __slots__ = ("_fernet", "_index_key")

    def __init__(self, fernet: Fernet | None, *, blind_index_key: bytes) -> None:
        self._fernet = fernet
        self._index_key = blind_index_key

    @property
    def is_enabled(self) -> bool:
        """Включено ли шифрование (``False`` — passthrough-режим демо)."""
        return self._fernet is not None

    @classmethod
    def from_settings(cls, settings: Settings) -> FieldCipher:
        """Собрать шифровальщик по настройкам.

        Ключ валидируется на этапе конфигурации (32 байта base64). Здесь —
        только выбор между шифрованием и passthrough.
        """
        secret = settings.security.pii_key
        if secret is None:
            if not settings.is_local:
                msg = (
                    "HIREBOT_SECURITY__PII_KEY обязателен вне local: "
                    "хранить ПДн кандидатов в открытом виде нельзя (152-ФЗ)"
                )
                raise PiiDecryptionError(msg)
            _warn_passthrough_once()
            return cls(None, blind_index_key=_INSECURE_INDEX_KEY)
        raw = secret.get_secret_value().encode()
        return cls(Fernet(raw), blind_index_key=_derive_index_key(raw))

    @classmethod
    def generate_key(cls) -> str:
        """Сгенерировать ключ для ``.env`` (утилита для разработчика)."""
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, value: str) -> str:
        """Зашифровать значение. В passthrough-режиме вернуть как есть."""
        if self._fernet is None:
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return f"{CIPHER_PREFIX}{token}"

    def decrypt(self, token: str) -> str:
        """Расшифровать значение.

        Значение без префикса считается legacy-текстом и возвращается как есть:
        так работает чтение данных, записанных до включения шифрования.
        """
        if self._fernet is None or not token.startswith(CIPHER_PREFIX):
            return token
        payload = token[len(CIPHER_PREFIX) :].encode("ascii")
        try:
            return self._fernet.decrypt(payload).decode("utf-8")
        except InvalidToken as exc:
            msg = (
                "Не удалось расшифровать ПДн: ключ HIREBOT_SECURITY__PII_KEY "
                "не совпадает с тем, которым записаны данные"
            )
            raise PiiDecryptionError(msg) from exc

    def encrypt_optional(self, value: str | None) -> str | None:
        """Зашифровать необязательное поле (``None`` остаётся ``None``)."""
        return None if value is None else self.encrypt(value)

    def decrypt_optional(self, token: str | None) -> str | None:
        """Расшифровать необязательное поле (``None`` остаётся ``None``)."""
        return None if token is None else self.decrypt(token)

    def blind_index(self, value: str) -> str:
        """Детерминированный HMAC для поиска по телефону/email."""
        normalized = normalize_for_index(value)
        return hmac.new(self._index_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def looks_encrypted(value: str | None) -> bool:
        """Признак того, что значение уже зашифровано (защита от двойного шифрования)."""
        return value is not None and value.startswith(CIPHER_PREFIX)


__all__ = [
    "CIPHER_PREFIX",
    "FieldCipher",
    "PiiDecryptionError",
    "normalize_for_index",
]
