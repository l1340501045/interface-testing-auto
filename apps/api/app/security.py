"""安全原语：密码哈希（scrypt）与秘密加密（Fernet）。

密码哈希用于平台账号，与被测系统凭证分离；秘密加密用于
credential 明文，密文只入数据库，主密钥仅存本机文件/环境。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32


def hash_password(password: str) -> str:
    """scrypt 哈希，返回带参数前缀的可验证字符串，不含明文。"""
    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """恒时校验密码，避免时序侧信道。"""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError):
        return False


def new_fernet_key() -> bytes:
    return Fernet.generate_key()


def _cipher(key: bytes) -> Fernet:
    return Fernet(key)


def encrypt_secret(key: bytes, plaintext: str) -> bytes:
    return _cipher(key).encrypt(plaintext.encode("utf-8"))


def decrypt_secret(key: bytes, token: bytes) -> str:
    try:
        return _cipher(key).decrypt(token).decode("utf-8")
    except InvalidToken as error:
        raise ValueError("密文无效或主密钥不匹配") from error
