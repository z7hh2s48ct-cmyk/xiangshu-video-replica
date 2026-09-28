"""P2-2 — error_code → 修复建议 runbook 契约（BILLING-OBS）。

管理端的「生成记录」失败聚合与「任务诊断」尝试历史都直接展示
``error_code``；用户/客服看到 ``ANALYSIS_PROVIDER_UNREACHABLE`` 这类内部
编号却不知道下一步做什么。``app.failure_runbook`` 是「码 → 中文修复建议」
的单一来源，本文件锁定两条契约：

1. **覆盖面**：所有会写进任务行 ``error_code`` 的字面量码（扫描落库模块，
   见 ``SCANNED_FILES``）以及经变量路径写入的分析族码
   （``VARIABLE_PATH_CODES``）都必须登记建议——新增失败码时忘记登记会在
   这里失败。即时校验码（HTTPException ``detail.code``）不在此列：调用方
   当场就拿到了 message，不属于事后诊断场景。
2. **质量**：每条建议非空、为中文、可直接执行；查询按大小写/空白归一，
   未知码返回 None（fail-open，前端显示回退文案）。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.failure_runbook import FAILURE_RUNBOOK, failure_advice

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# 会把 error_code 落进任务行/回调记录的模块清单。故意不扫全 app：请求层
# 的 detail.code 有数百个，与「事后诊断」无关。
SCANNED_FILES = (
    "analysis_routes.py",
    "generation.py",
    "image_tasks.py",
    "source_frames.py",
    "script_from_audio.py",
    "script_rewrite.py",
    "usage_billing.py",
    "viral_import.py",
    "viral_media_preparation.py",
    "viral_refresh.py",
    "settings_routes.py",
    "zpay_provider.py",
)

# Python 赋值 / SQL SET 里的 error_code 字面量（大写内部码）。
LITERAL_PATTERN = re.compile(r"""error_code\s*=\s*['"]([A-Z][A-Z0-9_]{4,})['"]""")

# 经变量（error_code=code / error_code=%s）写入的码，字面量扫描覆盖不到：
# 分析 worker 的默认兜底码与 provider 映射码，以及任务状态→码的展示映射。
VARIABLE_PATH_CODES = (
    "ANALYSIS_WORKER_FAILED",
    "ANALYSIS_PROVIDER_FAILED",
    "ANALYSIS_PROVIDER_UNREACHABLE",
    "ANALYSIS_PROVIDER_RATE_LIMITED",
    "ANALYSIS_PROVIDER_SETTINGS_REQUIRED",
    "ANALYSIS_VIDEO_URL_UNAVAILABLE",
    "APILIO_SETTINGS_REQUIRED",
    "APILIO_SETTINGS_UNAVAILABLE",
    "SCRIPT_FROM_AUDIO_PIPELINE_FAILED",
    "SCRIPT_FROM_AUDIO_PROVIDER_FAILED",
    "SCRIPT_REWRITE_TASK_FAILED",
    "SOURCE_FRAME_STORAGE_UNAVAILABLE",
    "SOURCE_FRAME_TASK_FAILED",
    "RECONCILE_OPERATION_FAILED",
    "ORAL_TASK_FAILED",
    "ORAL_SUBMISSION_UNCERTAIN",
    "ORAL_ARCHIVE_FAILED",
    # 方案 P0-9：改写服务经 detail.code 变量写入任务行的码
    # （fail_script_rewrite_task 的 ``code = detail["code"]`` 路径）。
    "DEEPSEEK_NETWORK_FAILED",
    "DEEPSEEK_REQUEST_FAILED",
    "DEEPSEEK_RESPONSE_INVALID",
    "DEEPSEEK_RESPONSE_TRUNCATED",
    "DEEPSEEK_RESPONSE_EMPTY",
)


def test_every_scanned_literal_code_has_advice() -> None:
    scanned: set[str] = set()
    missing: dict[str, list[str]] = {}
    for name in SCANNED_FILES:
        text = (APP_DIR / name).read_text(encoding="utf-8")
        for match in LITERAL_PATTERN.finditer(text):
            code = match.group(1)
            scanned.add(code)
            if code not in FAILURE_RUNBOOK:
                missing.setdefault(code, []).append(name)
    # 防哑弹：扫描器本身必须抓到已知码，否则正则或文件清单失效时会静默通过。
    assert {"VIRAL_IMPORT_FAILED", "ZPAY_SIGNATURE_MISMATCH", "PROVIDER_TERMINAL"} <= scanned
    assert not missing, f"落库 error_code 缺少修复建议登记: {missing}"


# P0-10：图片任务把码先赋给局部变量 ``code = "..."`` 再写库，人物视图用
# ``CharacterImageProviderFailed("CODE", ...)`` 构造失败——这两种写法上面的
# ``error_code =`` 正则都扫不到，漏登记的码在管理端只剩一个英文编号。
CODE_VARIABLE_FILES = ("image_tasks.py",)
CODE_VARIABLE_PATTERN = re.compile(r"""\bcode\s*=\s*['"]([A-Z][A-Z0-9_]{4,})['"]""")
CHARACTER_FAILURE_FILE = "character_image_generation.py"
CHARACTER_FAILURE_PATTERN = re.compile(
    r"""CharacterImageProviderFailed\(\s*['"]([A-Z][A-Z0-9_]{4,})['"]"""
)


def test_variable_assigned_and_constructed_failure_codes_have_advice() -> None:
    scanned: set[str] = set()
    for name in CODE_VARIABLE_FILES:
        text = (APP_DIR / name).read_text(encoding="utf-8")
        scanned.update(match.group(1) for match in CODE_VARIABLE_PATTERN.finditer(text))
    text = (APP_DIR / CHARACTER_FAILURE_FILE).read_text(encoding="utf-8")
    scanned.update(match.group(1) for match in CHARACTER_FAILURE_PATTERN.finditer(text))
    # 防哑弹：两种写法都必须真的扫到码。
    assert {"IMAGE_TASK_PROVIDER_FAILED", "CHARACTER_PROVIDER_TIMEOUT"} <= scanned
    missing = sorted(code for code in scanned if code not in FAILURE_RUNBOOK)
    assert not missing, f"图片 / 人物视图失败码缺少修复建议登记: {missing}"


def test_every_variable_path_code_has_advice() -> None:
    missing = sorted(code for code in VARIABLE_PATH_CODES if code not in FAILURE_RUNBOOK)
    assert not missing, f"变量路径 error_code 缺少修复建议登记: {missing}"


def test_every_entry_is_actionable_chinese() -> None:
    for code, advice in FAILURE_RUNBOOK.items():
        assert code == code.strip().upper(), code
        assert advice == advice.strip(), code
        assert len(advice) >= 8, f"{code} 的建议过短，无法执行"
        assert re.search(r"[\u4e00-\u9fff]", advice), f"{code} 的建议应有中文说明"


def test_lookup_normalizes_and_fails_open() -> None:
    assert failure_advice(" analysis_provider_failed ") is not None
    assert failure_advice("ANALYSIS_PROVIDER_FAILED") is not None
    assert failure_advice("no-such-code") is None
    assert failure_advice("") is None
    assert failure_advice(None) is None
