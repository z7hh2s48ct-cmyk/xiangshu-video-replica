"""视频号解密算法已知答案测试（tests fixtures 为公开样本，不含任何凭据）.

向量来源：微信官方 wasm 密钥流生成器对两个公开 decode_key 的输出，
与服务端纯 Python 实现逐字节比对通过；解密结果经 ffprobe 确认可播放。
"""

from __future__ import annotations

import pytest

from app.viral_decrypt import (
    KEYSTREAM_SIZE,
    decrypt_head,
    is_encrypted_mp4,
    keystream,
)

# decode_key=1789473271 的密钥流头 32 字节（官方 WASM 输出，消费序大端）。
_KEYSTREAM_A_32 = "f967803db4c3e5223a664885b3f51e255c52abb800cb7950e2f4854f993e0af7"
# decode_key=2136343393 的密钥流头 32 字节（第二个独立样本交叉验证）。
_KEYSTREAM_B_32 = "23766a3699fb876a75d5a232994844ab4000d68b7ccc551740606596bf535525"
# 真实加密 CDN 文件头 48 字节（对应 decode_key=1789473271）。
_ENCRYPTED_HEAD_48 = (
    "f967801dd2b79c52531527e8b3f51c253521c4d569b816628382e67ef44e3ec"
    "671edd0c23345fdd30f455645015eb310"
)
# 解密后应为标准 MP4：ftyp(isom/iso2/avc1/mp41) + moov box。
_DECRYPTED_HEAD_48 = (
    "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
    "00002a806d6f6f760000006c6d766864"
)


def test_keystream_known_answer_first_sample() -> None:
    assert keystream("1789473271", 32) == bytes.fromhex(_KEYSTREAM_A_32)


def test_keystream_known_answer_second_sample() -> None:
    assert keystream("2136343393", 32) == bytes.fromhex(_KEYSTREAM_B_32)


def test_keystream_accepts_int_and_whitespace() -> None:
    assert keystream(1789473271, 32) == bytes.fromhex(_KEYSTREAM_A_32)
    assert keystream(" 1789473271 ", 32) == bytes.fromhex(_KEYSTREAM_A_32)


def test_keystream_rejects_non_decimal_key() -> None:
    with pytest.raises(ValueError):
        keystream("not-a-number", 8)


def test_is_encrypted_mp4_detects_missing_ftyp() -> None:
    assert is_encrypted_mp4(bytes.fromhex(_ENCRYPTED_HEAD_48))
    assert not is_encrypted_mp4(bytes.fromhex(_DECRYPTED_HEAD_48))
    assert is_encrypted_mp4(b"short")


def test_decrypt_head_restores_ftyp_box() -> None:
    decrypted = decrypt_head(bytes.fromhex(_ENCRYPTED_HEAD_48), "1789473271")
    assert decrypted == bytes.fromhex(_DECRYPTED_HEAD_48)


def test_decrypt_head_is_idempotent_on_plaintext() -> None:
    plaintext = bytes.fromhex(_DECRYPTED_HEAD_48)
    assert decrypt_head(plaintext, "1789473271") == plaintext


def test_decrypt_head_xors_exactly_keystream_size() -> None:
    # 真实加密头 + 加密至 128 KiB 边界的中段 + 边界之后的明文尾：
    # 仅前 KEYSTREAM_SIZE 字节被变换，边界之后透传。
    mid_len = KEYSTREAM_SIZE - 48
    plain_mid = bytes(range(256)) * ((mid_len // 256) + 1)
    plain_mid = plain_mid[:mid_len]
    plain_tail = b"PLAINTEXT-TAIL-after-128KiB" * 4
    stream = keystream("1789473271", KEYSTREAM_SIZE)
    encrypted = (
        bytes.fromhex(_ENCRYPTED_HEAD_48)
        + bytes(p ^ s for p, s in zip(plain_mid, stream[48:]))
        + plain_tail
    )
    decrypted = decrypt_head(encrypted, "1789473271")
    assert decrypted[:48] == bytes.fromhex(_DECRYPTED_HEAD_48)
    assert decrypted[48:KEYSTREAM_SIZE] == plain_mid
    assert decrypted[KEYSTREAM_SIZE:] == plain_tail


def test_decrypt_head_short_input() -> None:
    encrypted = bytes.fromhex(_ENCRYPTED_HEAD_48)[:12]
    decrypted = decrypt_head(encrypted, "1789473271")
    assert decrypted == bytes.fromhex(_DECRYPTED_HEAD_48)[:12]
