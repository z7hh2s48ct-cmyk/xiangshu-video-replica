"""P2-2 — error_code → 修复建议 runbook 契约（BILLING-OBS）。

管理端的「生成记录」失败聚合与「任务诊断」尝试历史都直接展示
``error_code``；用户/客服看到 ``ANALYSIS_PROVIDER_UNREACHABLE`` 这类内部
编号却不知道下一步做什么。``app.failure_runbook`` 是「码 → 中文修复建议」
的单一来源，本文件锁定两条契约：

1. **覆盖面**：所有会写进任务行 ``error_code`` 的字面量码（扫描落库模块，
   见 ``SCANNED_FILES``）、经变量路径写入的分析族码
   （``VARIABLE_PATH_CODES``）、图片/人物视图的变量赋值与构造式码，以及口播
   由任务状态映射出的 ``ORAL_*`` 码，都必须登记建议——新增失败码时忘记登记
   会在这里失败。即时校验码（HTTPException ``detail.code``）不在此列：调用方
   当场就拿到了 message，不属于事后诊断场景。
2. **质量**：每条建议非空、为中文、可直接执行；查询按大小写/空白归一，
   未知码返回 None（fail-open，前端显示回退文案）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from app.failure_runbook import (
    FAILURE_CLASSIFICATION,
    FAILURE_RUNBOOK,
    FailureCategory,
    FailureOwner,
    failure_advice,
    failure_classification,
    failure_explanation,
)

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

# P0-10 收尾（2026-09-29 复核）：口播的码既不是 ``error_code =`` 字面量、也不是
# 构造式，而是 control_routes 的口播状态映射（``oral_tasks.status`` → ``ORAL_*``）。
# 照原方案把 oral.py 加进 SCANNED_FILES 是空扫——那文件里一个码字面量都没有，
# 守卫会永远通过。改为扫那张映射表：新增口播状态码却忘了登记建议会在这里失败。
# 只匹配 ``"状态": "ORAL_*"`` 这种字典值位置，避免误抓 ORAL_VIDEO 等记录类型。
ORAL_STATUS_CODE_FILE = "control_routes.py"
ORAL_STATUS_CODE_PATTERN = re.compile(r'''"[A-Z_]+"\s*:\s*"(ORAL_[A-Z0-9_]+)"''')


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


def test_oral_status_mapped_failure_codes_have_advice() -> None:
    text = (APP_DIR / ORAL_STATUS_CODE_FILE).read_text(encoding="utf-8")
    scanned = {match.group(1) for match in ORAL_STATUS_CODE_PATTERN.finditer(text)}
    # 防哑弹：映射表被改名或正则失效时必须失败，而不是静默通过。
    assert {"ORAL_TASK_FAILED", "ORAL_ARCHIVE_FAILED"} <= scanned
    missing = sorted(code for code in scanned if code not in FAILURE_RUNBOOK)
    assert not missing, f"口播状态码缺少修复建议登记: {missing}"


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


def test_classification_is_keyed_exactly_like_the_runbook() -> None:
    """分类与建议同键：少一个红、多一个也红——两处不允许漂移。

    这两份映射回答的是同一个码的两个问题（「怎么说」与「谁来看、看什么」），
    拆成两张表是为了让既有建议文本不必重排；键集一致由本用例钉住。
    """
    assert set(FAILURE_CLASSIFICATION) == set(FAILURE_RUNBOOK)


def test_classification_values_are_declared_members() -> None:
    categories = set(get_args(FailureCategory))
    owners = set(get_args(FailureOwner))
    for code, (category, owner) in FAILURE_CLASSIFICATION.items():
        assert category in categories, f"{code} 的原因分类未在枚举内: {category}"
        assert owner in owners, f"{code} 的处理人未在枚举内: {owner}"


def test_classification_lookup_normalizes_and_fails_open() -> None:
    assert failure_classification(" analysis_provider_failed ") == (
        "PROVIDER_FAULT",
        "OPS",
    )
    assert failure_classification("no-such-code") is None
    assert failure_classification("") is None
    assert failure_classification(None) is None


def test_explanation_returns_codes_and_advice() -> None:
    """解释对象只带稳定代码与建议；中文标签是前端词典的事，后端不重复存一份。"""
    explanation = failure_explanation("analysis_provider_rate_limited")
    assert explanation is not None
    assert explanation.category == "PROVIDER_BUSY"
    assert explanation.owner == "OPS"
    assert explanation.advice == FAILURE_RUNBOOK["ANALYSIS_PROVIDER_RATE_LIMITED"]
    assert failure_explanation("no-such-code") is None
    assert failure_explanation(None) is None


def test_content_review_upgrade_uses_provider_message() -> None:
    """审核拒绝无专属错误码，凭服务商原话升级分类且只对扫描族生效。"""
    upgraded = failure_explanation(
        "PROVIDER_TERMINAL", provider_message="Rejected: content policy violation"
    )
    assert upgraded is not None
    assert upgraded.category == "CONTENT_REVIEW"
    assert upgraded.owner == "SUPPORT"
    # 非扫描族的码不参与升级（限流原话里出现关键词是误报温床）。
    not_scanned = failure_explanation(
        "ANALYSIS_PROVIDER_RATE_LIMITED", provider_message="content policy violation"
    )
    assert not_scanned is not None
    assert not_scanned.category == "PROVIDER_BUSY"
    # 扫描族但原话无关时保持原分类。
    plain = failure_explanation("PROVIDER_TERMINAL", provider_message="connection reset by peer")
    assert plain is not None
    assert plain.category == "PROVIDER_FAULT"
