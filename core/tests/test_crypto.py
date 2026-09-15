"""M9 加密测试：字节/文件/目录 加解密往返、口令错误、篡改检测。"""
from __future__ import annotations

import pytest

from snapvault.crypto import (
    decrypt_bytes, decrypt_dir, decrypt_file, encrypt_bytes, encrypt_dir,
    encrypt_file,
)
from snapvault.exceptions import CryptoError

PW = "正确的口令 123!@#"


class TestBytes:
    def test_roundtrip(self):
        data = b"hello snapvault \xe4\xbd\xa0\xe5\xa5\xbd" * 100
        enc = encrypt_bytes(data, PW)
        assert enc != data
        assert decrypt_bytes(enc, PW) == data

    def test_randomized_ciphertext(self):
        d1 = encrypt_bytes(b"same", PW)
        d2 = encrypt_bytes(b"same", PW)
        assert d1 != d2  # 随机盐/IV

    def test_wrong_password(self):
        enc = encrypt_bytes(b"secret", PW)
        with pytest.raises(CryptoError, match="口令错误|损坏"):
            decrypt_bytes(enc, "wrong")

    def test_tampered(self):
        enc = bytearray(encrypt_bytes(b"secret data", PW))
        enc[-1] ^= 0xFF
        with pytest.raises(CryptoError):
            decrypt_bytes(bytes(enc), PW)

    def test_not_encrypted(self):
        with pytest.raises(CryptoError, match="不是"):
            decrypt_bytes(b"plain", PW)

    def test_different_keys_same_salt_diff(self):
        """同口令同数据应因盐不同而密文不同（已由 randomized 覆盖）。"""


class TestFile:
    def test_roundtrip(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_bytes(b"x" * (2 * 1024 * 1024 + 123))  # 跨块
        enc = tmp_path / "a.enc"
        decrypt = tmp_path / "a.out"
        encrypt_file(src, enc, PW, chunk_size=1024 * 1024)
        assert enc.exists() and enc.stat().st_size != src.stat().st_size
        decrypt_file(enc, decrypt, PW)
        assert decrypt.read_bytes() == src.read_bytes()

    def test_wrong_password_file(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        enc = tmp_path / "a.enc"
        encrypt_file(src, enc, PW)
        with pytest.raises(CryptoError):
            decrypt_file(enc, tmp_path / "x.out", "nope")

    def test_truncated(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello world")
        enc = tmp_path / "a.enc"
        encrypt_file(src, enc, PW)
        data = bytearray(enc.read_bytes())
        enc.write_bytes(data[:-5])  # 截断
        with pytest.raises(CryptoError):
            decrypt_file(enc, tmp_path / "x.out", PW)


class TestDir:
    def test_roundtrip(self, tmp_path):
        src = tmp_path / "vault"
        (src / "sub").mkdir(parents=True)
        (src / "f1.txt").write_text("内容一")
        (src / "sub" / "f2.bin").write_bytes(b"\x00\x01\x02" * 100)
        enc_dir = tmp_path / "enc"
        dec_dir = tmp_path / "dec"
        n = encrypt_dir(src, enc_dir, PW)
        assert n == 2
        m = decrypt_dir(enc_dir, dec_dir, PW)
        assert m == 2
        assert (dec_dir / "f1.txt").read_text() == "内容一"
        assert (dec_dir / "sub" / "f2.bin").read_bytes() == b"\x00\x01\x02" * 100
