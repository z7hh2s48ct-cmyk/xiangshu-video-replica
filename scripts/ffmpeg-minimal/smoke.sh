#!/usr/bin/env bash
set -euo pipefail

FFMPEG_PATH="${1:?用法：smoke.sh /path/to/ffmpeg}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

write_fixture() {
  local name="$1"
  local payload="$2"
  printf '%s' "$payload" | base64 --decode > "$WORK_DIR/$name"
}

decode_fixture() {
  local name="$1"
  "$FFMPEG_PATH" \
    -hide_banner -loglevel error -xerror -threads 1 \
    -i pipe:0 -map 0:v:0 -frames:v 1 -f null - \
    < "$WORK_DIR/$name"
}

write_fixture gray.png \
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAAAAABX3VL4AAAACXBIWXMAAAABAAAAAQBPJcTWAAAADklEQVR4nGP0YWBhYAAAAZQAUoxteykAAAAASUVORK5CYII='
write_fixture portrait.jpg \
  '/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAg+Pkk+SVVVVVVVVWRdZGhoaGRkZGRoaGhwcHCDg4NwcHBoaHBwfHyDg4+Tj4eHg4eTk5ubm7q6srLZ2eD/////xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIABAAEAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z'
write_fixture portrait.webp \
  'UklGRh4AAABXRUJQVlA4TBEAAAAvAAAAAAfQ//73v/+BiOh/AAA='

decode_fixture gray.png
decode_fixture portrait.jpg
decode_fixture portrait.webp
