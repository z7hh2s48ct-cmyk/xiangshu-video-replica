//! 视频号加密视频的本地解密（爆款模块 · P2 客户端缓存）。
//!
//! 微信视频号只加密 MP4 的**前 128 KiB**，密钥流由接口同响应返回的 `decode_key`
//! （十进制字符串，即 uint64 种子）经 ISAAC64 伪随机数生成器产生。决策 #17 要求
//! 解密在客户端本地完成，原始文件不再交由服务端处理。
//!
//! 本实现与服务端 `server/app/viral_decrypt.py` 逐字节一致，单测直接复用服务端
//! `server/tests/test_viral_decrypt.py` 的同一批已知答案向量交叉验证（那些向量来自
//! 微信官方 WASM 密钥流生成器，并对真实样本用 ffprobe 确认过可播放）。
//!
//! 注意：`decode_key` 每次请求都会变化，必须与同一响应里的 `full_url` 配对使用。

/// 视频号只加密文件前 128 KiB，其余字节为明文。
pub const KEYSTREAM_SIZE: usize = 131_072;

const GOLDEN: u64 = 0x9E37_79B9_7F4A_7C13;

/// `decode_key` 不是十进制 uint64 字符串。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidDecodeKey;

impl std::fmt::Display for InvalidDecodeKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("decode_key 必须是 uint64 十进制字符串")
    }
}

impl std::error::Error for InvalidDecodeKey {}

/// Jenkins 原版 ISAAC64 mix 步骤。全部按 u64 环绕语义计算，与 Python 侧的
/// `& MASK` 掩码等价（Rust 的 `<<` 本就截断高位）。
#[inline]
#[allow(clippy::too_many_arguments)]
fn mix(
    mut a: u64,
    mut b: u64,
    mut c: u64,
    mut d: u64,
    mut e: u64,
    mut f: u64,
    mut g: u64,
    mut h: u64,
) -> [u64; 8] {
    a = a.wrapping_sub(e);
    f ^= h >> 9;
    h = h.wrapping_add(a);
    b = b.wrapping_sub(f);
    g ^= a << 9;
    a = a.wrapping_add(b);
    c = c.wrapping_sub(g);
    h ^= b >> 23;
    b = b.wrapping_add(c);
    d = d.wrapping_sub(h);
    a ^= c << 15;
    c = c.wrapping_add(d);
    e = e.wrapping_sub(a);
    b ^= d >> 14;
    d = d.wrapping_add(e);
    f = f.wrapping_sub(b);
    c ^= e << 20;
    e = e.wrapping_add(f);
    g = g.wrapping_sub(c);
    d ^= f >> 17;
    f = f.wrapping_add(g);
    h = h.wrapping_sub(d);
    e ^= g << 14;
    g = g.wrapping_add(h);
    [a, b, c, d, e, f, g, h]
}

/// Jenkins 原版 ISAAC64：词按索引逆序消费、每词按大端输出 8 字节。
///
/// `state` 一开始承载种子（`state[0] = seed`、其余为 0），randinit 之后
/// `generate` 会把结果直接写回同一个数组——这与参考实现里 randrsl 既是输入
/// 又是输出的行为一致，不能拆成两个数组。
struct Isaac64 {
    state: [u64; 256],
    mm: [u64; 256],
    aa: u64,
    bb: u64,
    cc: u64,
    randcnt: usize,
}

impl Isaac64 {
    fn new(seed: u64) -> Self {
        let mut state = [0u64; 256];
        state[0] = seed;
        let mut mm = [0u64; 256];
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = [GOLDEN; 8];
        for _ in 0..4 {
            [a, b, c, d, e, f, g, h] = mix(a, b, c, d, e, f, g, h);
        }
        for i in (0..256).step_by(8) {
            a = a.wrapping_add(state[i]);
            b = b.wrapping_add(state[i + 1]);
            c = c.wrapping_add(state[i + 2]);
            d = d.wrapping_add(state[i + 3]);
            e = e.wrapping_add(state[i + 4]);
            f = f.wrapping_add(state[i + 5]);
            g = g.wrapping_add(state[i + 6]);
            h = h.wrapping_add(state[i + 7]);
            [a, b, c, d, e, f, g, h] = mix(a, b, c, d, e, f, g, h);
            mm[i..i + 8].copy_from_slice(&[a, b, c, d, e, f, g, h]);
        }
        for i in (0..256).step_by(8) {
            a = a.wrapping_add(mm[i]);
            b = b.wrapping_add(mm[i + 1]);
            c = c.wrapping_add(mm[i + 2]);
            d = d.wrapping_add(mm[i + 3]);
            e = e.wrapping_add(mm[i + 4]);
            f = f.wrapping_add(mm[i + 5]);
            g = g.wrapping_add(mm[i + 6]);
            h = h.wrapping_add(mm[i + 7]);
            [a, b, c, d, e, f, g, h] = mix(a, b, c, d, e, f, g, h);
            mm[i..i + 8].copy_from_slice(&[a, b, c, d, e, f, g, h]);
        }
        let mut context = Self {
            state,
            mm,
            aa: 0,
            bb: 0,
            cc: 0,
            randcnt: 255,
        };
        context.generate();
        context
    }

    fn generate(&mut self) {
        self.cc = self.cc.wrapping_add(1);
        self.bb = self.bb.wrapping_add(self.cc);
        let mut aa = self.aa;
        let mut bb = self.bb;
        for i in 0..256 {
            match i & 3 {
                0 => aa = !(aa ^ (aa << 21)),
                1 => aa ^= aa >> 5,
                2 => aa ^= aa << 12,
                _ => aa ^= aa >> 33,
            }
            aa = aa.wrapping_add(self.mm[(i + 128) & 255]);
            let x = self.mm[i];
            let y = self.mm[((x >> 3) & 255) as usize]
                .wrapping_add(aa)
                .wrapping_add(bb);
            self.mm[i] = y;
            bb = self.mm[((y >> 11) & 255) as usize].wrapping_add(x);
            self.state[i] = bb;
        }
        self.aa = aa;
        self.bb = bb;
    }

    /// 先取当前 `randcnt` 处的词，再决定是否推进——顺序与参考实现一致。
    fn next_word(&mut self) -> u64 {
        let word = self.state[self.randcnt];
        if self.randcnt == 0 {
            self.generate();
            self.randcnt = 255;
        } else {
            self.randcnt -= 1;
        }
        word
    }
}

fn parse_decode_key(decode_key: &str) -> Result<u64, InvalidDecodeKey> {
    decode_key
        .trim()
        .parse::<u64>()
        .map_err(|_| InvalidDecodeKey)
}

/// 生成 `decode_key` 对应的前 `size` 字节密钥流。
pub fn keystream(decode_key: &str, size: usize) -> Result<Vec<u8>, InvalidDecodeKey> {
    let mut context = Isaac64::new(parse_decode_key(decode_key)?);
    let mut buffer = Vec::with_capacity(size + 8);
    while buffer.len() < size {
        buffer.extend_from_slice(&context.next_word().to_be_bytes());
    }
    buffer.truncate(size);
    Ok(buffer)
}

/// 文件头不是标准 `ftyp` box 时视为视频号加密数据。
pub fn is_encrypted_mp4(head: &[u8]) -> bool {
    head.len() < 8 || &head[4..8] != b"ftyp"
}

/// 解密前 [`KEYSTREAM_SIZE`] 字节，超出部分原样返回。
///
/// 流式管线里只需对前 128 KiB 调本函数，其余字节直接透传。输入不足 8 字节或
/// 已解密（`ftyp` 头）时原样返回，便于幂等重放。
pub fn decrypt_head(head: &[u8], decode_key: &str) -> Result<Vec<u8>, InvalidDecodeKey> {
    if head.len() < 8 || !is_encrypted_mp4(head) {
        return Ok(head.to_vec());
    }
    let stream = keystream(decode_key, head.len().min(KEYSTREAM_SIZE))?;
    let mut decrypted = head.to_vec();
    for (byte, key) in decrypted.iter_mut().zip(stream) {
        *byte ^= key;
    }
    Ok(decrypted)
}

#[cfg(test)]
mod tests {
    use super::*;

    // 与服务端 `server/tests/test_viral_decrypt.py` 完全相同的已知答案向量。
    const KEYSTREAM_A_32: &str = "f967803db4c3e5223a664885b3f51e255c52abb800cb7950e2f4854f993e0af7";
    const KEYSTREAM_B_32: &str = "23766a3699fb876a75d5a232994844ab4000d68b7ccc551740606596bf535525";
    const ENCRYPTED_HEAD_48: &str = "f967801dd2b79c52531527e8b3f51c253521c4d569b816628382e67ef44e3ec671edd0c23345fdd30f455645015eb310";
    const DECRYPTED_HEAD_48: &str = "000000206674797069736f6d0000020069736f6d69736f32617663316d70343100002a806d6f6f760000006c6d766864";

    fn hex(value: &str) -> Vec<u8> {
        assert!(value.len() % 2 == 0, "hex 长度必须是偶数");
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).expect("合法 hex"))
            .collect()
    }

    #[test]
    fn keystream_known_answer_first_sample() {
        assert_eq!(keystream("1789473271", 32).unwrap(), hex(KEYSTREAM_A_32));
    }

    #[test]
    fn keystream_known_answer_second_sample() {
        assert_eq!(keystream("2136343393", 32).unwrap(), hex(KEYSTREAM_B_32));
    }

    #[test]
    fn keystream_accepts_whitespace() {
        assert_eq!(keystream(" 1789473271 ", 32).unwrap(), hex(KEYSTREAM_A_32));
        assert_eq!(keystream("1789473271", 32).unwrap(), hex(KEYSTREAM_A_32));
    }

    #[test]
    fn keystream_rejects_non_decimal_key() {
        assert_eq!(keystream("not-a-number", 8), Err(InvalidDecodeKey));
        assert_eq!(keystream("-1", 8), Err(InvalidDecodeKey));
        // 超出 uint64 的上界同样拒绝。
        assert_eq!(keystream("18446744073709551616", 8), Err(InvalidDecodeKey));
    }

    #[test]
    fn is_encrypted_mp4_detects_missing_ftyp() {
        assert!(is_encrypted_mp4(&hex(ENCRYPTED_HEAD_48)));
        assert!(!is_encrypted_mp4(&hex(DECRYPTED_HEAD_48)));
        assert!(is_encrypted_mp4(b"short"));
    }

    #[test]
    fn decrypt_head_restores_ftyp_box() {
        let decrypted = decrypt_head(&hex(ENCRYPTED_HEAD_48), "1789473271").unwrap();
        assert_eq!(decrypted, hex(DECRYPTED_HEAD_48));
    }

    #[test]
    fn decrypt_head_is_idempotent_on_plaintext() {
        let plaintext = hex(DECRYPTED_HEAD_48);
        assert_eq!(
            decrypt_head(&plaintext, "1789473271").unwrap(),
            plaintext,
            "已解密的输入必须原样返回"
        );
    }

    #[test]
    fn decrypt_head_transforms_exactly_keystream_size() {
        // 真实加密头 + 加密至 128 KiB 边界的中段 + 边界之后的明文尾：
        // 仅前 KEYSTREAM_SIZE 字节被变换，边界之后透传。
        let mid_len = KEYSTREAM_SIZE - 48;
        let mut plain_mid = Vec::with_capacity(mid_len);
        while plain_mid.len() < mid_len {
            plain_mid.extend(0u8..=255);
        }
        plain_mid.truncate(mid_len);
        let plain_tail: Vec<u8> = b"PLAINTEXT-TAIL-after-128KiB".repeat(4);

        let stream = keystream("1789473271", KEYSTREAM_SIZE).unwrap();
        let mut encrypted = hex(ENCRYPTED_HEAD_48);
        encrypted.extend(
            plain_mid
                .iter()
                .zip(&stream[48..])
                .map(|(plain, key)| plain ^ key),
        );
        encrypted.extend_from_slice(&plain_tail);

        let decrypted = decrypt_head(&encrypted, "1789473271").unwrap();
        assert_eq!(decrypted[..48], hex(DECRYPTED_HEAD_48)[..]);
        assert_eq!(decrypted[48..KEYSTREAM_SIZE], plain_mid[..]);
        assert_eq!(decrypted[KEYSTREAM_SIZE..], plain_tail[..]);
    }

    #[test]
    fn decrypt_head_short_input() {
        let encrypted = hex(ENCRYPTED_HEAD_48);
        let decrypted = decrypt_head(&encrypted[..12], "1789473271").unwrap();
        assert_eq!(decrypted, hex(DECRYPTED_HEAD_48)[..12]);
    }

    #[test]
    fn decrypt_head_plaintext_does_not_need_valid_key() {
        // 与 Python 一致：已解密的输入提前返回，不会因密钥非法而报错。
        let plaintext = hex(DECRYPTED_HEAD_48);
        assert_eq!(decrypt_head(&plaintext, "not-a-number").unwrap(), plaintext);
    }
}
