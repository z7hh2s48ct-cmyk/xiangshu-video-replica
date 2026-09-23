# 精简 LGPL 构建 ffmpeg/ffprobe（静态 Windows exe，mingw-w64 交叉编译）。
# 只保留图片解码校验和"已缓存视频 → 抽音轨（AAC/m4a）→ ffprobe 探测时长"所需能力；
# 不含 x264/x265 等 GPL 库，产物按 LGPL v3 分发（见
# client/src-tauri/resources/ffmpeg/README.md）。
#
# 目标是 Windows NSIS 安装包，因此用 mingw-w64 交叉编译产出 .exe——
# 早先用 musl 交叉编译产出的是 Linux ELF，与"随 NSIS 分发"的用途不符。
FROM debian:bookworm-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config curl ca-certificates xz-utils make \
        nasm yasm \
        gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Windows 交叉工具链（gcc-mingw-w64 提供 x86_64-w64-mingw32-* 前缀）。
ENV CROSS_PREFIX=x86_64-w64-mingw32-

ARG ZLIB_VERSION=1.3.1
WORKDIR /tmp/zlib
RUN curl -fsSL "https://zlib.net/fossils/zlib-${ZLIB_VERSION}.tar.gz" \
      | tar -xz --strip-components=1 && \
    CC="${CROSS_PREFIX}gcc" AR="${CROSS_PREFIX}ar" RANLIB="${CROSS_PREFIX}ranlib" \
      ./configure --static --prefix=/opt/zlib && \
    make -j"$(nproc)" && make install

ARG FFMPEG_VERSION=7.1.1
WORKDIR /src
RUN curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
      | tar -xJ --strip-components=1

# 精简配置：图片校验、音视频容器/解码、AAC 原生编码、无 GPL 组件。
# -static -static-libgcc 让 exe 自包含，安装包里不需要额外 DLL。
RUN ./configure \
        --enable-cross-compile \
        --target-os=mingw32 --arch=x86_64 \
        --cross-prefix="${CROSS_PREFIX}" \
        --pkg-config-flags=--static \
        --extra-cflags="-I/opt/zlib/include -Os -ffunction-sections -fdata-sections" \
        --extra-ldflags="-L/opt/zlib/lib -static -static-libgcc -Wl,--gc-sections" \
        --disable-everything --disable-autodetect --disable-debug \
        --disable-doc --disable-programs --disable-network --disable-iconv \
        --disable-bzlib --disable-lzma --disable-sdl2 --disable-xlib \
        --disable-libxcb --disable-vaapi --disable-vdpau --disable-d3d11va \
        --enable-small \
        --enable-zlib \
        --enable-version3 \
        --enable-ffmpeg --enable-ffprobe \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,mp4,m4a,matroska,webm,avi,flv,ogg,wav,mp3,aac,image_jpeg_pipe,image_png_pipe,image_webp_pipe \
        --enable-parser=aac,ac3,dca,flac,h264,hevc,mjpeg,mpegaudio,opus,png,vorbis,vp8,vp9,webp \
        --enable-decoder=aac,mp3,mp2,flac,opus,vorbis,pcm_s16le,pcm_u8,ac3,eac3,mjpeg,png,webp \
        --enable-encoder=aac,flac,pcm_s16le,wrapped_avframe \
        --enable-muxer=mp4,ipod,adts,flac,wav,null \
        --enable-bsf=aac_adtstoasc \
        --enable-filter=aresample,aformat,anull,volume \
        && make -j"$(nproc)"

# Windows 二进制无法在 Linux 构建容器里执行，因此这里做**静态断言**而不是
# 跑一次 ffmpeg：ffmpeg 会把整条 configure 命令行嵌进产物，可以直接验证它
# 没有 --enable-gpl、且带 --enable-version3（LGPL v3）。真正的运行期 smoke
# （解码一帧 PNG/JPEG/WebP）由 Windows CI 在 NSIS 门禁里做。
RUN test -s ffmpeg.exe && test -s ffprobe.exe && \
    for binary in ffmpeg.exe ffprobe.exe; do \
        config_line="$(tr -d '\0' < "$binary" | grep -ao -- '--enable-version3[^\r\n]*' | head -1)"; \
        if [ -z "$config_line" ]; then \
            echo "missing embedded --enable-version3 in $binary (not an LGPL build?)" >&2; \
            exit 1; \
        fi; \
        if printf '%s' "$config_line" | grep -q -- '--enable-gpl'; then \
            echo "found --enable-gpl in $binary; this build must stay LGPL" >&2; \
            exit 1; \
        fi; \
    done

# type=local 导出的是本阶段的**根文件系统**，因此产物必须放在根目录下，
# 否则会多出一层 out/（原写法写到 /out/ 就是这个毛病，导致 build.sh 的自检
# 一直找不到文件而静默跳过）。
FROM scratch AS export
COPY --from=build /src/ffmpeg.exe /ffmpeg.exe
COPY --from=build /src/ffprobe.exe /ffprobe.exe
