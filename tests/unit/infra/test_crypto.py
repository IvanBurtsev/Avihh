"""Тесты шифрования ПДн и blind index."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from cryptography.fernet import Fernet

from hirebot.config import Settings
from hirebot.infra.security.crypto import (
    CIPHER_PREFIX,
    FieldCipher,
    PiiDecryptionError,
    normalize_for_index,
)


def test_roundtrip_encrypt_decrypt(cipher: FieldCipher) -> None:
    token = cipher.encrypt("+7 999 123-45-67")

    assert token != "+7 999 123-45-67"
    assert token.startswith(CIPHER_PREFIX)
    assert cipher.decrypt(token) == "+7 999 123-45-67"


def test_encryption_is_non_deterministic(cipher: FieldCipher) -> None:
    """Fernet добавляет случайный IV: одинаковый вход даёт разные шифротексты.

    Именно поэтому для поиска нужен отдельный blind index.
    """
    assert cipher.encrypt("secret") != cipher.encrypt("secret")


def test_resume_text_roundtrip(cipher: FieldCipher) -> None:
    raw_text = "Иван Петров\nОпыт: 5 лет в продажах\nОжидания: 100 000 ₽"

    assert cipher.decrypt(cipher.encrypt(raw_text)) == raw_text


def test_optional_helpers(cipher: FieldCipher) -> None:
    assert cipher.encrypt_optional(None) is None
    assert cipher.decrypt_optional(None) is None

    token = cipher.encrypt_optional("ivan@example.com")
    assert token is not None
    assert cipher.decrypt_optional(token) == "ivan@example.com"


def test_legacy_plaintext_is_returned_as_is(cipher: FieldCipher) -> None:
    """Данные, записанные до включения шифрования, не должны ломать чтение."""
    assert cipher.decrypt("+7 999 000-00-00") == "+7 999 000-00-00"


def test_wrong_key_raises() -> None:
    first = FieldCipher(Fernet(Fernet.generate_key()), blind_index_key=b"k" * 32)
    second = FieldCipher(Fernet(Fernet.generate_key()), blind_index_key=b"k" * 32)
    token = first.encrypt("+7 999 123-45-67")

    with pytest.raises(PiiDecryptionError, match="PII_KEY"):
        second.decrypt(token)


def test_looks_encrypted(cipher: FieldCipher) -> None:
    assert cipher.looks_encrypted(cipher.encrypt("x")) is True
    assert cipher.looks_encrypted("plain") is False
    assert cipher.looks_encrypted(None) is False


def test_blind_index_is_deterministic(cipher: FieldCipher) -> None:
    assert cipher.blind_index("+7 999 123-45-67") == cipher.blind_index("+7 999 123-45-67")


def test_blind_index_matches_different_phone_formats(cipher: FieldCipher) -> None:
    """Один номер в разных записях должен находиться одним поиском."""
    variants = [
        "+7 999 123-45-67",
        "+79991234567",
        "8 (999) 123-45-67",
        "89991234567",
        "7 999 123 45 67",
    ]

    indexes = {cipher.blind_index(value) for value in variants}

    assert len(indexes) == 1


def test_blind_index_is_case_insensitive_for_email(cipher: FieldCipher) -> None:
    assert cipher.blind_index("Ivan@Example.COM") == cipher.blind_index("ivan@example.com")


def test_blind_index_differs_for_different_values(cipher: FieldCipher) -> None:
    assert cipher.blind_index("+7 999 123-45-67") != cipher.blind_index("+7 999 123-45-68")


def test_blind_index_is_keyed() -> None:
    """Без ключа blind index нельзя воспроизвести перебором по словарю."""
    key = Fernet.generate_key()
    first = FieldCipher(Fernet(key), blind_index_key=b"a" * 32)
    second = FieldCipher(Fernet(key), blind_index_key=b"b" * 32)

    assert first.blind_index("+79991234567") != second.blind_index("+79991234567")


def test_from_settings_uses_configured_key(settings: Settings) -> None:
    cipher = FieldCipher.from_settings(settings)

    assert cipher.is_enabled is True


def test_passthrough_in_local_without_key(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(env="local", security={})

    cipher = FieldCipher.from_settings(settings)

    assert cipher.is_enabled is False
    assert cipher.encrypt("открыто") == "открыто"
    assert cipher.decrypt("открыто") == "открыто"
    # Blind index остаётся детерминированным и в passthrough-режиме.
    assert cipher.blind_index("+79991234567") == cipher.blind_index("89991234567")


def test_missing_key_outside_local_raises(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(env="staging", security={})

    with pytest.raises(PiiDecryptionError, match="PII_KEY"):
        FieldCipher.from_settings(settings)


def test_generate_key_is_valid_base64() -> None:
    import base64

    assert len(base64.urlsafe_b64decode(FieldCipher.generate_key().encode())) == 32


def test_normalize_for_index_handles_non_phone_values() -> None:
    assert normalize_for_index("  Иванов  ") == "иванов"
    assert normalize_for_index("не телефон") == "не телефон"
