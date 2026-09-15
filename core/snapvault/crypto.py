"""AES-256 加密（M9）：密钥派生 PBKDF2-HMAC-SHA256，AES-256-GCM 分块文件加密。

文件格式（分块 AEAD）：
    header = MAGIC(7) + VERSION(1) + salt(16) + iv(12) + chunk_size(4)
    之后每块: tag(16) + 该块密文
    每块以「块序号」为 AAD，防重排；任一块校验失败即判定口令错误或数据损坏。

内存数据格式（单发 AEAD）：
    MAGIC(7) + VERSION(1) + salt(16) + iv(12) + tag(16) + 密文
"""
from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

from .exceptions import CryptoError
from .util import atomic_write

MAGIC = b"SNPVLT1"
VERSION = 1
SALT_LEN = 16
IV_LEN = 12
TAG_LEN = 16
CHUNK_LEN_FIELD = 4
DEFAULT_CHUNK = 1 << 20  # 1MB

PBKDF2_ITERATIONS = 600_000


def derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256 派生 32 字节 AES-256 密钥。"""
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32
    )


def _aes(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


# ======================================================================
# 内存数据（单发）
# ======================================================================
def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_LEN)
    iv = os.urandom(IV_LEN)
    ct = _aes(derive_key(passphrase, salt)).encrypt(iv, data, None)
    return MAGIC + bytes([VERSION]) + salt + iv + ct[:TAG_LEN] + ct[TAG_LEN:]


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    if not data.startswith(MAGIC):
        raise CryptoError("不是 SnapVault 加密数据")
    pos = len(MAGIC) + 1
    salt = data[pos:pos + SALT_LEN]
    iv = data[pos + SALT_LEN:pos + SALT_LEN + IV_LEN]
    tag = data[pos + SALT_LEN + IV_LEN:pos + SALT_LEN + IV_LEN + TAG_LEN]
    body = data[pos + SALT_LEN + IV_LEN + TAG_LEN:]
    try:
        return _aes(derive_key(passphrase, salt)).decrypt(iv, tag + body, None)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("解密失败：口令错误或数据已损坏") from exc


# ======================================================================
# 分块文件（流式）
# ======================================================================
def encrypt_file(src: str | Path, dst: str | Path, passphrase: str,
                 chunk_size: int = DEFAULT_CHUNK, on_progress=None) -> int:
    salt = os.urandom(SALT_LEN)
    iv = os.urandom(IV_LEN)
    aes = _aes(derive_key(passphrase, salt))
    total = Path(src).stat().st_size
    written = 0
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(out, "wb") as fout:
        fout.write(MAGIC + bytes([VERSION]) + salt + iv + struct.pack("<I", chunk_size))
        idx = 0
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            ct = aes.encrypt(iv, chunk, b"chunk:%08d" % idx)  # GCM 输出 = tag(16) + 密文
            fout.write(ct)
            idx += 1
            written += len(chunk)
            if on_progress:
                on_progress(written, total)
    return out.stat().st_size


def decrypt_file(src: str | Path, dst: str | Path, passphrase: str,
                 on_progress=None) -> int:
    raw = Path(src).read_bytes()
    if not raw.startswith(MAGIC):
        raise CryptoError("不是 SnapVault 加密文件")
    pos = len(MAGIC) + 1
    salt = raw[pos:pos + SALT_LEN]
    iv = raw[pos + SALT_LEN:pos + SALT_LEN + IV_LEN]
    chunk_size = struct.unpack("<I", raw[pos + SALT_LEN + IV_LEN:
                                         pos + SALT_LEN + IV_LEN + CHUNK_LEN_FIELD])[0]
    body = raw[pos + SALT_LEN + IV_LEN + CHUNK_LEN_FIELD:]
    aes = _aes(derive_key(passphrase, salt))
    parts = []
    p = 0
    idx = 0
    try:
        while p < len(body):
            if p + TAG_LEN > len(body):
                raise CryptoError("加密文件截断")
            tag = body[p:p + TAG_LEN]
            p += TAG_LEN
            remaining = len(body) - p
            ct_len = chunk_size if remaining >= chunk_size else remaining
            ct = body[p:p + ct_len]
            parts.append(aes.decrypt(iv, tag + ct, b"chunk:%08d" % idx))
            p += ct_len
            idx += 1
    except CryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("解密失败：口令错误或数据已损坏") from exc
    plain = b"".join(parts)
    atomic_write(dst, plain)
    if on_progress:
        on_progress(len(plain), len(plain))
    return len(plain)


def encrypt_dir(src_dir: str | Path, dst_dir: str | Path, passphrase: str,
                on_progress=None) -> int:
    """加密整个目录（逐文件，同名+.enc）。返回文件数。"""
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    files = [p for p in src_dir.rglob("*") if p.is_file()]
    for i, f in enumerate(files, start=1):
        rel = f.relative_to(src_dir)
        encrypt_file(f, dst_dir / f"{rel}.enc", passphrase)
        if on_progress:
            on_progress(i, len(files))
    return len(files)


def decrypt_dir(src_dir: str | Path, dst_dir: str | Path, passphrase: str,
                on_progress=None) -> int:
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    files = [p for p in src_dir.rglob("*.enc") if p.is_file()]
    for i, f in enumerate(files, start=1):
        rel = f.relative_to(src_dir)
        target = dst_dir / rel.with_suffix("")
        target.parent.mkdir(parents=True, exist_ok=True)
        decrypt_file(f, target, passphrase)
        if on_progress:
            on_progress(i, len(files))
    return len(files)
