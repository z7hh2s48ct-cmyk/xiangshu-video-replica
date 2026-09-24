#!/usr/bin/env bash
# 精简 LGPL 构建 ffmpeg/ffprobe（决策 #5，P2 客户端本地抽音轨）。
# 产出静态 Windows exe（mingw-w64 交叉编译），覆盖图片解码校验、抽音轨和
# 探测时长，不含任何 GPL 组件（x264/x265 等）。产物复制到
# client/src-tauri/resources/ffmpeg/，随 NSIS 安装包分发。
#
# 用法：scripts/ffmpeg-minimal/build.sh [输出目录]
# 依赖：Docker（BuildKit 的 local 输出）。
set -euo pipefail

OUT_DIR="${1:-$(dirname "$0")/../../client/src-tauri/resources/ffmpeg}"

# BuildKit 导出：把 export 阶段的静态 exe 直接落到输出目录。
# 镜像用固定 tag，重复构建直接覆盖（见 AGENTS.md 的镜像复用约定）。
DOCKER_BUILDKIT=1 docker build \
  --tag ffmpeg-minimal-builder:stable \
  --output "type=local,dest=$OUT_DIR" \
  "$(dirname "$0")"

# Windows 二进制跑不了在当前（Linux/macOS）宿主上，因此这里只断言产物存在；
# LGPL 断言（无 --enable-gpl、含 --enable-version3）已在 Dockerfile 内对二进制
# 做过静态检查。运行期 smoke 需在 Windows 上执行：
#   resources\ffmpeg\ffmpeg.exe -version
for binary in ffmpeg.exe ffprobe.exe; do
  if [ ! -s "$OUT_DIR/$binary" ]; then
    echo "构建未产出 $OUT_DIR/$binary" >&2
    exit 1
  fi
done

echo "── 产物（Windows 静态 exe，LGPL）──"
ls -la "$OUT_DIR"
