"""TikHub 账号维度接口一次性探测脚本（P0 Spike）。

用途：验证「对标账号监控」所需的 4 件事，成本 ≤ $0.03。
  P1  视频号能否一步搜到账号（business_type=account 是否给 finder username）
  P2  抖音搜账号是否按文档返回 sec_uid / unique_id / follower_count
  P3  抖音按账号拉作品是否返回 play_count（决定是否必须逐条补调 → 成本翻不翻倍）
  P4  抖音端点单价（公开文档未标价，只能查接口）

安全约束：
  - key 只从环境变量 TIKHUB_API_KEY 读取，不落盘、不打印、不进 git
  - 原始响应落盘前做 key 脱敏（响应体本身不含 key，但统一过一遍保险）

用法：
  TIKHUB_API_KEY=xxx python3 /tmp/tikhub_probe/probe.py

产出：
  /tmp/tikhub_probe/*.json   原始响应（供人工核对字段）
  stdout                     结构化判读摘要
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.tikhub.io"
OUT = pathlib.Path("/tmp/tikhub_probe")
OUT.mkdir(parents=True, exist_ok=True)

# 视频号 V2 接口官方要求 timeout >= 30s，否则"已扣费收不到响应"
TIMEOUT_SLOW = 35
TIMEOUT_FAST = 20

KEY = os.environ.get("TIKHUB_API_KEY", "").strip()
if not KEY:
    sys.exit("缺少环境变量 TIKHUB_API_KEY")


def _redact(text: str) -> str:
    """响应里若意外回显 key，一律抹掉再落盘/打印。"""
    return text.replace(KEY, "<REDACTED>") if KEY else text


def call(method: str, path: str, *, body: dict | None = None, timeout: int = TIMEOUT_FAST):
    """返回 (http_status, parsed_json_or_text)。绝不抛异常，失败也要可判读。"""
    url = f"{BASE}{path}"
    data = None
    headers = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # 网络/超时：必须显式记录，不能静默
        return 0, {"_transport_error": f"{type(exc).__name__}: {exc}"}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_non_json": _redact(raw)[:2000]}


def save(name: str, payload) -> None:
    (OUT / f"{name}.json").write_text(
        _redact(json.dumps(payload, ensure_ascii=False, indent=2)), encoding="utf-8"
    )


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------------------
# P1 视频号搜账号
# ---------------------------------------------------------------------------
rule("P1  视频号搜账号  POST /api/v1/wechat_search/v2/fetch_search  business_type=account")
p1_status, p1 = call(
    "POST",
    "/api/v1/wechat_search/v2/fetch_search",
    body={"keyword": "乡墅", "business_type": "account", "raw": False},
    timeout=TIMEOUT_SLOW,
)
save("p1_wechat_search_account", p1)
print(f"HTTP {p1_status}")
print(json.dumps(p1, ensure_ascii=False)[:3000])

# 从响应里暴力搜任何疑似 finder username（形如 v2_xxx@finder）
blob = json.dumps(p1, ensure_ascii=False)
finder_hits = sorted({tok for tok in blob.replace('"', " ").replace(",", " ").split()
                      if "@finder" in tok or tok.startswith("v2_")})
print(f"\n>>> 疑似 finder username 命中: {finder_hits or '无'}")
print(">>> 判定: " + ("✅ 视频号可一步搜到账号" if finder_hits
                     else "❌ 未见 finder username —— 需走 exportId 中转或 sph 短号转换"))


# ---------------------------------------------------------------------------
# P2 抖音搜账号
# ---------------------------------------------------------------------------
rule("P2  抖音搜账号  POST /api/v1/douyin/search/fetch_user_search")
p2_status, p2 = call(
    "POST",
    "/api/v1/douyin/search/fetch_user_search",
    body={"keyword": "乡墅", "cursor": 0},
    timeout=TIMEOUT_FAST,
)
save("p2_douyin_user_search", p2)
print(f"HTTP {p2_status}")

user_list = ((p2.get("data") or {}).get("user_list")) or []
print(f"user_list 条数: {len(user_list)}")
sec_uid = ""
for idx, row in enumerate(user_list[:5]):
    info = (row or {}).get("user_info") or {}
    uid = info.get("sec_uid") or ""
    uniq = info.get("unique_id") or ""
    fans = info.get("follower_count")
    print(f"  [{idx}] nickname={info.get('nickname')!r} "
          f"sec_uid={uid[:24] + '…' if uid else 'MISSING'} "
          f"unique_id={uniq!r} follower_count={fans!r}")
    if uid and not sec_uid:
        sec_uid = uid

print(">>> 判定: " + ("✅ 抖音搜账号可用，sec_uid/unique_id/follower_count 齐备"
                     if sec_uid and user_list else "❌ 字段缺失或空列表，以落盘 JSON 为准"))


# ---------------------------------------------------------------------------
# P3 抖音按账号拉作品（关键：play_count 是否恒 0）
# ---------------------------------------------------------------------------
rule("P3  抖音按账号拉作品  GET /api/v1/douyin/app/v3/fetch_user_post_videos")
if not sec_uid:
    print("跳过：P2 未取到 sec_uid，无法继续。")
else:
    qs = urllib.parse.urlencode({"sec_user_id": sec_uid, "count": 5, "max_cursor": 0})
    p3_status, p3 = call(
        "GET", f"/api/v1/douyin/app/v3/fetch_user_post_videos?{qs}", timeout=TIMEOUT_FAST
    )
    save("p3_douyin_user_post_videos", p3)
    print(f"HTTP {p3_status}")

    awemes = ((p3.get("data") or {}).get("aweme_list")) or []
    print(f"aweme_list 条数: {len(awemes)}")
    nonzero_play = 0
    for idx, a in enumerate(awemes[:5]):
        stats = (a or {}).get("statistics") or {}
        play = stats.get("play_count")
        author = (a or {}).get("author") or {}
        if isinstance(play, int) and play > 0:
            nonzero_play += 1
        print(f"  [{idx}] play_count={play!r} digg={stats.get('digg_count')!r} "
              f"author.sec_uid={'有' if author.get('sec_uid') else '无'} "
              f"author.unique_id={author.get('unique_id')!r} "
              f"author.follower_count={author.get('follower_count')!r}")

    print(">>> 判定: " + ("✅ play_count 有真实值，无需逐条补调"
                         if nonzero_play else
                         "⚠️ play_count 恒 0 → 必须逐条补调 fetch_video_statistics（成本 ×1.5）"))
    print(">>> 附带: 上面的 author.sec_uid / unique_id / follower_count 就是"
          "「从搜索结果顺手得到账号候选」的可行性依据")


# ---------------------------------------------------------------------------
# P4 抖音端点单价（公开文档未标价）
# ---------------------------------------------------------------------------
rule("P4  端点单价  GET /api/v1/tikhub/user/get_endpoint_info")
for endpoint in (
    "/api/v1/douyin/search/fetch_user_search",
    "/api/v1/douyin/app/v3/fetch_user_post_videos",
    "/api/v1/douyin/app/v3/fetch_video_statistics",
    "/api/v1/wechat_channels/v2/fetch_user_videos",
):
    qs = urllib.parse.urlencode({"endpoint": endpoint})
    st, resp = call("GET", f"/api/v1/tikhub/user/get_endpoint_info?{qs}", timeout=TIMEOUT_FAST)
    save(f"p4_price_{endpoint.strip('/').replace('/', '_')}", resp)
    print(f"  {endpoint}  →  HTTP {st}  {json.dumps(resp, ensure_ascii=False)[:400]}")

rule("完成")
print(f"原始响应已落盘: {OUT}/")
print("注意：核对完请删除该目录。")
