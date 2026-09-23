"""视频号加密视频解密（爆款视频参考库 / C4）.

微信视频号仅加密 MP4 文件的前 128 KiB，密钥流由接口同响应返回的
``decode_key``（十进制字符串，即 uint64 种子）经 ISAAC64 伪随机数生成器
产生。算法为 Jenkins 原版 ISAAC64：种子写入 ``Seed[0]``、其余 255 词为 0，
golden 常量 mix 4 次后做两轮 randinit，词按索引逆序消费、每词按大端输出
8 字节。该实现与官方 WASM 密钥流生成器逐字节一致（对真实样本验证，
见 tests/test_viral_decrypt.py 的已知答案向量）。

注意：``decode_key`` 每次请求都会变化，必须使用同一响应里的值。
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator

KEYSTREAM_SIZE = 131_072
_ISAAC64_MASK = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C13


def _mix(
    a: int, b: int, c: int, d: int, e: int, f: int, g: int, h: int
) -> tuple[int, int, int, int, int, int, int, int]:
    a = (a - e) & _ISAAC64_MASK
    f ^= h >> 9
    h = (h + a) & _ISAAC64_MASK
    b = (b - f) & _ISAAC64_MASK
    g ^= (a << 9) & _ISAAC64_MASK
    a = (a + b) & _ISAAC64_MASK
    c = (c - g) & _ISAAC64_MASK
    h ^= b >> 23
    b = (b + c) & _ISAAC64_MASK
    d = (d - h) & _ISAAC64_MASK
    a ^= (c << 15) & _ISAAC64_MASK
    c = (c + d) & _ISAAC64_MASK
    e = (e - a) & _ISAAC64_MASK
    b ^= d >> 14
    d = (d + e) & _ISAAC64_MASK
    f = (f - b) & _ISAAC64_MASK
    c ^= (e << 20) & _ISAAC64_MASK
    e = (e + f) & _ISAAC64_MASK
    g = (g - c) & _ISAAC64_MASK
    d ^= f >> 17
    f = (f + g) & _ISAAC64_MASK
    h = (h - d) & _ISAAC64_MASK
    e ^= (g << 14) & _ISAAC64_MASK
    g = (g + h) & _ISAAC64_MASK
    return a, b, c, d, e, f, g, h


class _Isaac64:
    """Jenkins 原版 ISAAC64：词逆序消费、大端输出的密钥流生成器."""

    def __init__(self, seed: int) -> None:
        if seed < 0 or seed > _ISAAC64_MASK:
            raise ValueError("ISAAC64 seed must be an unsigned 64-bit integer")
        self._seed: list[int] = [seed] + [0] * 255
        self._mm: list[int] = [0] * 256
        self._aa = 0
        self._bb = 0
        self._cc = 0
        self._randcnt = 255
        a = b = c = d = e = f = g = h = _GOLDEN
        for _ in range(4):
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
        state = self._seed
        for i in range(0, 256, 8):
            a = (a + state[i]) & _ISAAC64_MASK
            b = (b + state[i + 1]) & _ISAAC64_MASK
            c = (c + state[i + 2]) & _ISAAC64_MASK
            d = (d + state[i + 3]) & _ISAAC64_MASK
            e = (e + state[i + 4]) & _ISAAC64_MASK
            f = (f + state[i + 5]) & _ISAAC64_MASK
            g = (g + state[i + 6]) & _ISAAC64_MASK
            h = (h + state[i + 7]) & _ISAAC64_MASK
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            self._mm[i : i + 8] = [a, b, c, d, e, f, g, h]
        mm = self._mm
        for i in range(0, 256, 8):
            a = (a + mm[i]) & _ISAAC64_MASK
            b = (b + mm[i + 1]) & _ISAAC64_MASK
            c = (c + mm[i + 2]) & _ISAAC64_MASK
            d = (d + mm[i + 3]) & _ISAAC64_MASK
            e = (e + mm[i + 4]) & _ISAAC64_MASK
            f = (f + mm[i + 5]) & _ISAAC64_MASK
            g = (g + mm[i + 6]) & _ISAAC64_MASK
            h = (h + mm[i + 7]) & _ISAAC64_MASK
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            mm[i : i + 8] = [a, b, c, d, e, f, g, h]
        self._generate()

    def _generate(self) -> None:
        self._cc = (self._cc + 1) & _ISAAC64_MASK
        self._bb = (self._bb + self._cc) & _ISAAC64_MASK
        aa, bb, mm, seed = self._aa, self._bb, self._mm, self._seed
        for i in range(256):
            step = i & 3
            if step == 0:
                aa = ~(aa ^ (aa << 21)) & _ISAAC64_MASK
            elif step == 1:
                aa ^= aa >> 5
            elif step == 2:
                aa ^= (aa << 12) & _ISAAC64_MASK
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(i + 128) & 255]) & _ISAAC64_MASK
            x = mm[i]
            y = (mm[(x >> 3) & 255] + aa + bb) & _ISAAC64_MASK
            mm[i] = y
            bb = (mm[(y >> 11) & 255] + x) & _ISAAC64_MASK
            seed[i] = bb
        self._aa, self._bb = aa, bb

    def next_word(self) -> int:
        word = self._seed[self._randcnt]
        if self._randcnt == 0:
            self._generate()
            self._randcnt = 255
        else:
            self._randcnt -= 1
        return word


def keystream(decode_key: str | int, size: int = KEYSTREAM_SIZE) -> bytes:
    """生成 ``decode_key`` 对应的前 ``size`` 字节密钥流."""
    try:
        seed = int(str(decode_key).strip())
    except ValueError as error:
        raise ValueError("decode_key must be a decimal string") from error
    context = _Isaac64(seed)
    buf = bytearray()
    while len(buf) < size:
        buf += struct.pack(">Q", context.next_word())
    return bytes(buf[:size])


def is_encrypted_mp4(head: bytes) -> bool:
    """文件头不是标准 ``ftyp`` box 时视为视频号加密数据."""
    return len(head) < 8 or head[4:8] != b"ftyp"


def decrypt_head(head: bytes, decode_key: str | int) -> bytes:
    """解密加密数据的前 ``KEYSTREAM_SIZE`` 字节，超出部分原样返回.

    调用方在流式管线里只需对前 128 KiB 调本函数，其余字节直接透传。
    输入不足 8 字节或已解密（ftyp 头）时原样返回，便于幂等重放。
    """
    if len(head) < 8 or not is_encrypted_mp4(head):
        return head
    stream = keystream(decode_key, min(len(head), KEYSTREAM_SIZE))
    decrypted = bytearray(head)
    for index, byte in enumerate(stream):
        decrypted[index] ^= byte
    return bytes(decrypted)


def decrypt_chunks(chunks: Iterable[bytes], decode_key: str | int) -> Iterator[bytes]:
    """Buffer only the encrypted prefix; the remainder passes through unchanged."""
    head = bytearray()
    complete = False
    for chunk in chunks:
        if complete:
            yield chunk
            continue
        take = min(KEYSTREAM_SIZE - len(head), len(chunk))
        head.extend(chunk[:take])
        if len(head) == KEYSTREAM_SIZE:
            yield decrypt_head(bytes(head), decode_key)
            head.clear()
            complete = True
            if take < len(chunk):
                yield chunk[take:]
    if head:
        yield decrypt_head(bytes(head), decode_key)
