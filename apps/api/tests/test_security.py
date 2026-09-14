"""安全原语测试：密码哈希与 Fernet 加密往返。"""
from __future__ import annotations

from app.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_fernet_key,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery staple", stored) is True
    assert verify_password("wrong password", stored) is False


def test_password_hash_unique_salt() -> None:
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b, "相同密码应因随机盐产生不同哈希"
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_fernet_roundtrip() -> None:
    key = new_fernet_key()
    plaintext = "secret-token-value"
    token = encrypt_secret(key, plaintext)
    assert token != plaintext.encode()
    assert decrypt_secret(key, token) == plaintext


def test_fernet_wrong_key_rejected() -> None:
    key_a = new_fernet_key()
    key_b = new_fernet_key()
    token = encrypt_secret(key_a, "value")
    try:
        decrypt_secret(key_b, token)
        raise AssertionError("错误密钥应被拒绝")
    except ValueError:
        pass
