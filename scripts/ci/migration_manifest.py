#!/usr/bin/env python3
"""MIGRATION-GUARD-20260912 — 迁移链清单生成器与守卫。

本脚本把《并行开发迁移 Head 与 PR 冲突处置手册》（``docs/并行开发迁移Head与PR冲突处置手册.md``）
里的人工步骤机械化，特别是：

- §2 家族 #2/#3：head 常量与 CW-056 冻结矩阵（``HEAD_SCHEMA_COUNTS`` / ``HEAD_TABLE_NAMES`` /
  ``HEAD_SCHEMA_DIGEST``）在 head 移动后要「逐项手改」——本脚本改为重新计算并给出可直接
  粘贴的值，且断言当前树与已记录值一致。
- §4 冻结事实探针：原本是一段要手工复制粘贴的 heredoc，本脚本以 ``--print-schema`` 提供同一
  能力（**复用 CW-056 模块自身的 helper**，因此与测试零漂移，不重写等价 SQL）。
- §5 push 前预检：``--check`` 是那条清单的可执行版本，纯静态、免 PG，可放在 CI 早期步骤。

三个设计约束（都是刻意的）：

1. **静态半部只用标准库 ``ast`` 解析迁移文件**，不 import alembic、不 import 测试模块。
   这样 CI 用裸 ``python3`` 就能跑（与 ``scripts/ci/build-test-shards.py`` 同款），也不受
   ``pg_test_kit`` 的 POSIX-only ``fcntl`` 影响。
2. **不拥有业务常量**。``PUBLISHED_HEAD_REVISION`` / ``HEAD_REVISION`` / 冻结矩阵等真源仍在
   ``server/tests/test_cw056_supported_head_matrix.py``；本脚本从该文件 AST 读取它们并**独立
   重算后比对**。这就是「双跑」：两套实现必须同时正确，任何一边漂移都会红。
3. **诚实声明边界**。清单证明的是「当前树与最后一次记录一致」，不是「没人改写过历史」。
   后者仍由 CW-056 的已发布链哈希（不可变段）与手册的流程铁律承担。该边界写进 ``basis``。

用法::

    python3 scripts/ci/migration_manifest.py --check          # CI 守卫（免 PG）
    python3 scripts/ci/migration_manifest.py --record         # 重新生成清单（静态半部）
    python3 scripts/ci/migration_manifest.py --print-schema   # 打印可粘贴的冻结字面量（需 PG）
    python3 scripts/ci/migration_manifest.py --check-schema   # 真实 PG 与字面量比对（需 PG）
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"
MIGRATIONS_DIR = SERVER_DIR / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"
CW056_MODULE = SERVER_DIR / "tests" / "test_cw056_supported_head_matrix.py"
MANIFEST_PATH = MIGRATIONS_DIR / "manifest.json"

MANIFEST_SCHEMA_VERSION = 1
TASK_ID = "MIGRATION-GUARD-20260912"

# 新迁移命名策略（手册 §7 的增量段）：时间戳前缀，天然唯一，消除「抢下一个号」。
# 存量文件一律不改名 —— 已发布段（base..055）字节被 CW-056 冻结，且
# test_customer_ha_smoke 断言了若干具体 ``0??_*.py`` 路径存在。顺序由 down_revision
# 决定，文件名只是标签（main 现链 081→082→086→083→089 早已不按数字序）。
NAMING_PATTERN = re.compile(r"^\d{8}T\d{4}_[a-z0-9_]+$")

# 命名策略锚点：一经采纳**不再前移**，且刻意写成常量而不是从清单里读。
# 若允许 --record 把锚点推到当前 head，违规命名只要「先 --record 再提交」就能把锚点
# 变成它自己，从而落进豁免集——自我豁免。写成常量后，唯一能移动锚点的动作是改这一行，
# 那是一个显式、可评审的代码改动。--check 会断言清单里记录的锚点等于本常量。
NAMING_POLICY_ADOPTION_HEAD = "089_customer_api_keys"

# 需要在 CW-056 模块里读的真源常量。全部用 ast.literal_eval 读取，不执行该模块。
CW056_SCALAR_CONSTANTS = (
    "HEAD_REVISION",
    "PUBLISHED_HEAD_REVISION",
    "PUBLISHED_CHAIN_LENGTH",
    "PUBLISHED_CHAIN_CONTENT_SHA256",
    "PUBLISHED_CHAIN_RELATION_SHA256",
    "HEAD_SCHEMA_COUNTS",
    "HEAD_TABLE_NAMES",
    "HEAD_SCHEMA_DIGEST",
    "SUPPORTED_RELEASE_HEADS",
    "MATRIX_STARTING_HEADS",
)


class GuardFailure(RuntimeError):
    """守卫判定失败；消息面向修复者，必须能直接指出下一步动作。"""


# ---------------------------------------------------------------------------
# 静态半部：只用 ast，不 import 任何东西
# ---------------------------------------------------------------------------


def _module_level_assignments(
    path: Path, wanted: set[str] | None = None
) -> dict[str, Any]:
    """取模块级字面量赋值（``Assign`` 与 ``AnnAssign`` 都认）。非字面量记为
    ``<unevaluable>``。

    与 ``deploy/operator/build_operator_package.py`` 同款技法：merge revision 的
    ``down_revision`` 必须是**字面量元组**才能被读到，写成变量会 fail-closed。
    ``wanted=None`` 表示只取迁移文件关心的四个名（revision/down_revision/
    branch_labels/depends_on）。
    """
    if wanted is None:
        wanted = {"revision", "down_revision", "branch_labels", "depends_on"}

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - 语法错会在别处先炸
        raise GuardFailure(f"{path.name}: cannot parse ({exc})") from exc

    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # CW-056 的真源常量多为带注解形式：`HEAD_SCHEMA_COUNTS: dict[str, int] = {...}`
            targets = [node.target]
        else:
            continue
        for target in targets:
            if target.id not in wanted:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except ValueError:
                values[target.id] = "<unevaluable>"
    return values


def load_revisions() -> dict[str, dict[str, Any]]:
    """扫描 ``versions/*.py``，返回 revision -> {down_revision, file, sha256}。"""
    revisions: dict[str, dict[str, Any]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("__"):
            continue
        values = _module_level_assignments(path)
        rev = values.get("revision")
        if not isinstance(rev, str):
            raise GuardFailure(
                f"{path.name}: module-level `revision` must be a string literal, "
                f"got {rev!r}"
            )
        if rev in revisions:
            raise GuardFailure(
                f"duplicate revision id {rev!r}: "
                f"{revisions[rev]['file']} and {path.name}"
            )
        down = values.get("down_revision")
        if down is not None and not isinstance(down, (str, tuple)):
            raise GuardFailure(
                f"{path.name}: `down_revision` must be a string literal, a tuple of "
                f"string literals, or None; got {down!r}"
            )
        if values.get("branch_labels") is not None:
            raise GuardFailure(
                f"{path.name}: `branch_labels` must be None (this repo uses a single "
                f"unnamed chain; branch labels change revision addressing)"
            )
        revisions[rev] = {
            "down_revision": down,
            "file": f"versions/{path.name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if not revisions:
        raise GuardFailure(f"no migrations found under {VERSIONS_DIR}")
    return revisions


def parents_of(entry: dict[str, Any]) -> tuple[str, ...]:
    down = entry["down_revision"]
    if down is None:
        return ()
    return down if isinstance(down, tuple) else (down,)


def compute_graph(revisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    referenced: set[str] = set()
    for entry in revisions.values():
        for parent in parents_of(entry):
            if parent not in revisions:
                raise GuardFailure(
                    f"{entry['file']}: down_revision {parent!r} is not a known revision"
                )
            referenced.add(parent)

    heads = sorted(set(revisions) - referenced)
    bases = sorted(rev for rev, e in revisions.items() if e["down_revision"] is None)

    if not heads:
        raise GuardFailure("migration chain has no head (cycle or empty graph)")
    if not bases:
        raise GuardFailure(
            "migration chain has no base revision (down_revision is None)"
        )

    return {
        "heads": heads,
        "bases": bases,
        "revision_count": len(revisions),
        "parents": {
            rev: (
                list(entry["down_revision"])
                if isinstance(entry["down_revision"], tuple)
                else entry["down_revision"]
            )
            for rev, entry in sorted(revisions.items())
        },
    }


def walk_published_chain(
    revisions: dict[str, dict[str, Any]], published_head: str
) -> list[tuple[str, str, str | None]]:
    """base → published_head 的 (revision, 文件 sha256, down_revision)。

    与 CW-056 ``_published_chain()`` 同序同义。该段**必须线性**：出现分支点即抛，
    否则「已发布链哈希唯一」这个前提就不成立了。
    """
    if published_head not in revisions:
        raise GuardFailure(
            f"published head {published_head!r} is not a known revision; "
            f"check PUBLISHED_HEAD_REVISION in {CW056_MODULE.name}"
        )
    chain: list[tuple[str, str, str | None]] = []
    current: str | None = published_head
    while current is not None:
        entry = revisions[current]
        down = entry["down_revision"]
        chain.append((current, entry["sha256"], down))
        if isinstance(down, tuple):
            raise GuardFailure(
                f"published chain must stay linear, but {current} has multiple parents "
                f"{down}; a merge revision is only ever allowed AFTER the published head"
            )
        current = down
    chain.reverse()
    return chain


def published_chain_hashes(chain: list[tuple[str, str, str | None]]) -> tuple[str, str]:
    content = "\n".join(f"{rev}<-{down or ''}:{digest}" for rev, digest, down in chain)
    relation = "\n".join(f"{rev}<-{down or ''}" for rev, _, down in chain)
    return (
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        hashlib.sha256(relation.encode("utf-8")).hexdigest(),
    )


def load_cw056_constants() -> dict[str, Any]:
    """用 ast 从 CW-056 测试模块读取真源常量（不执行、不 import）。"""
    values = _module_level_assignments(CW056_MODULE, set(CW056_SCALAR_CONSTANTS))
    missing = [name for name in CW056_SCALAR_CONSTANTS if name not in values]
    if missing:
        raise GuardFailure(
            f"{CW056_MODULE.name}: expected module-level constants are missing: "
            f"{', '.join(missing)}. If they were renamed, update "
            f"CW056_SCALAR_CONSTANTS in this script."
        )
    return {name: values[name] for name in CW056_SCALAR_CONSTANTS}


def check_naming_policy(
    revisions: dict[str, dict[str, Any]], adoption_head: str
) -> list[str]:
    """返回违反命名策略的 revision 列表（空 = 通过）。

    ``adoption_head`` 及其祖先属存量段（``0NN_*.py``），豁免；其后新增的必须匹配
    ``NAMING_PATTERN``。用「一个锚点 + 祖先闭包」代替上百条白名单。

    锚点未知时 **fail-closed 抛错**而不是返回空列表：静默返回空会让整条策略在所有
    检查里失效，而「策略失效」和「没有违规」在结果上无法区分。
    """
    if adoption_head not in revisions:
        raise GuardFailure(
            f"naming-policy anchor {adoption_head!r} is not a known revision; "
            f"NAMING_POLICY_ADOPTION_HEAD in this script is stale"
        )

    legacy: set[str] = set()
    stack = [adoption_head]
    while stack:
        rev = stack.pop()
        if rev in legacy:
            continue
        legacy.add(rev)
        stack.extend(parents_of(revisions[rev]))

    return sorted(
        rev for rev in revisions if rev not in legacy and not NAMING_PATTERN.match(rev)
    )


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def build_manifest(
    revisions: dict[str, dict[str, Any]], cw056: dict[str, Any]
) -> dict[str, Any]:
    graph = compute_graph(revisions)
    chain = walk_published_chain(revisions, cw056["PUBLISHED_HEAD_REVISION"])
    content_sha, relation_sha = published_chain_hashes(chain)
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "task": TASK_ID,
        "basis": (
            "本清单证明「当前迁移树与最后一次记录一致」，不证明「没有历史改写」。"
            "已发布段（base..PUBLISHED_HEAD）的不可变性由 test_cw056 的内容级/关系级 "
            "sha256 承担；未合并分支重挂 down_revision 是手册 §3 允许的动作，"
            "重挂后必须重新运行 --record 并提交本文件。"
        ),
        "naming_policy": {
            "pattern": NAMING_PATTERN.pattern,
            "adoption_head": NAMING_POLICY_ADOPTION_HEAD,
            "note": (
                "adoption_head 及其祖先为存量段（0NN_*.py），豁免。其后新增的迁移 "
                "必须用 时间戳前缀，以消除并行开发下「抢下一个号」的撞名。"
                "锚点一经采纳不再前移：它由生成器里的常量决定，--check 断言本字段"
                "等于该常量，因此无法靠重新记录来把违规命名洗白。"
            ),
        },
        "graph": graph,
        "published_chain": {
            "head": cw056["PUBLISHED_HEAD_REVISION"],
            "length": len(chain),
            "content_sha256": content_sha,
            "relation_sha256": relation_sha,
        },
        "revisions": {
            rev: {"file": entry["file"], "sha256": entry["sha256"]}
            for rev, entry in sorted(revisions.items())
        },
    }


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise GuardFailure(
            f"{MANIFEST_PATH.relative_to(REPO_ROOT)} is missing; "
            f"run `python3 scripts/ci/migration_manifest.py --record`"
        )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def write_manifest(manifest: dict[str, Any]) -> None:
    # newline="\n" 是必须的：默认的 universal-newline 模式在 Windows 上会写成 CRLF，
    # 而 .gitattributes 的 `* text=auto eol=lf` 要求 LF。生成物随平台变行尾会让
    # 每次换机器重跑 --record 都产生整文件 diff（shard 清单已经因此踩过：
    # CRLF 让 run-pytest-shards.sh 读出的每个路径尾部带 \r，pytest 找不到文件）。
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# --check：手册 §5 push 前预检的可执行版本（免 PG）
# ---------------------------------------------------------------------------


def run_check() -> list[str]:
    failures: list[str] = []
    revisions = load_revisions()
    cw056 = load_cw056_constants()
    manifest = load_manifest()

    # 1) 图谱不变量：恰一个 head、恰一个 base、无悬挂父节点（compute_graph 内抛）。
    graph = compute_graph(revisions)
    if len(graph["heads"]) != 1:
        failures.append(
            f"expected exactly one migration head, got {graph['heads']}; "
            f'a merge revision (down_revision = ("a", "b")) is the way to close it — '
            f"close it with an explicit merge revision that joins every open head"
        )
    if len(graph["bases"]) != 1:
        failures.append(f"expected exactly one base revision, got {graph['bases']}")

    # 2) head 与 CW-056 字面量一致（双跑：真源在测试模块，这里独立重算）。
    if graph["heads"] != [cw056["HEAD_REVISION"]]:
        failures.append(
            f"chain head {graph['heads']} != test_cw056.HEAD_REVISION "
            f"{cw056['HEAD_REVISION']!r}"
        )

    # 3) 已发布链重算，与 CW-056 冻结字面量逐项比对。
    chain = walk_published_chain(revisions, cw056["PUBLISHED_HEAD_REVISION"])
    content_sha, relation_sha = published_chain_hashes(chain)
    if len(chain) != cw056["PUBLISHED_CHAIN_LENGTH"]:
        failures.append(
            f"published chain length {len(chain)} != "
            f"test_cw056.PUBLISHED_CHAIN_LENGTH {cw056['PUBLISHED_CHAIN_LENGTH']}"
        )
    if content_sha != cw056["PUBLISHED_CHAIN_CONTENT_SHA256"]:
        failures.append(
            "published chain CONTENT hash changed: an already-released revision was "
            "edited (bytes or topology). CW-053 §3 E1 requires append-only fixes."
        )
    if relation_sha != cw056["PUBLISHED_CHAIN_RELATION_SHA256"]:
        failures.append(
            "published chain RELATION hash changed: the base..055 topology was "
            "rewritten; that range must stay linear and frozen."
        )

    # 4) 命名策略。锚点取自本文件的常量，**不取清单里的值**——取清单会让
    #    「先 --record 把锚点推到违规 revision」变成一条自我豁免路径。
    recorded_anchor = manifest.get("naming_policy", {}).get("adoption_head")
    if recorded_anchor != NAMING_POLICY_ADOPTION_HEAD:
        failures.append(
            f"manifest naming_policy.adoption_head is {recorded_anchor!r} but this "
            f"script pins {NAMING_POLICY_ADOPTION_HEAD!r}; the anchor must not move "
            f"(moving it would exempt every revision between the two)."
        )
    offenders = check_naming_policy(revisions, NAMING_POLICY_ADOPTION_HEAD)
    if offenders:
        failures.append(
            "new revisions must use the timestamp naming policy "
            f"({NAMING_PATTERN.pattern}); offending: {offenders}. "
            "Rename the file and its `revision` literal together."
        )

    # 5) 清单未漂移：revision 集合与逐个文件字节。
    recorded = manifest.get("revisions", {})
    current_ids, recorded_ids = set(revisions), set(recorded)
    if current_ids - recorded_ids:
        failures.append(
            f"migrations added since the manifest was recorded: "
            f"{sorted(current_ids - recorded_ids)}; run `--record` and commit "
            f"{MANIFEST_PATH.relative_to(REPO_ROOT)}"
        )
    if recorded_ids - current_ids:
        failures.append(
            f"migrations present in the manifest but missing from the tree: "
            f"{sorted(recorded_ids - current_ids)}"
        )
    for rev in sorted(current_ids & recorded_ids):
        if revisions[rev]["sha256"] != recorded[rev].get("sha256"):
            failures.append(
                f"{rev}: migration file changed since the manifest was recorded "
                f"({recorded[rev]['file']}). Rewriting a landed revision is not allowed; "
                f"append a new migration instead. If this is a legitimate pre-merge "
                f"re-parent of an UNMERGED revision, re-run `--record`."
            )

    # 6) 清单的静态半部与当前树一致。
    expected = build_manifest(revisions, cw056)
    for key in ("graph", "published_chain", "naming_policy"):
        if manifest.get(key) != expected[key]:
            failures.append(
                f"manifest section {key!r} is stale; run `--record` and commit it"
            )

    return failures


# ---------------------------------------------------------------------------
# schema 半部（需真实 PG）——复用 CW-056 模块自身的 helper，与测试零漂移
# ---------------------------------------------------------------------------


def _load_cw056_module() -> Any:
    """导入 CW-056 测试模块以复用其 schema 探针（与手册 §4 同一技法）。

    ``pg_test_kit`` 顶层 ``import fcntl`` 是 POSIX-only。CI 与生产主机都是 Linux，
    所以这条路径本不需要处理；这里注入一个桩纯粹是为了让 schema 模式能在 Windows
    开发机上跑起来。**桩只在本进程内、且只在真 import 失败时生效**，不触碰
    ``server/tests/pg_test_kit.py``——那个文件的服务范围问题（Windows 上无法收集
    PG 套件）属于仓库既有状态，不是本任务要改的共享基础设施。
    """
    import importlib.util
    import types

    if "fcntl" not in sys.modules:
        try:
            import fcntl  # noqa: F401
        except ImportError:  # pragma: no cover - non-POSIX host
            stub = types.ModuleType("fcntl")
            stub.LOCK_EX = 2
            stub.LOCK_SH = 1
            stub.LOCK_NB = 4
            stub.LOCK_UN = 8
            stub.flock = lambda *a, **k: None  # noqa: ARG005
            sys.modules["fcntl"] = stub

    for extra in (str(SERVER_DIR), str(SERVER_DIR / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    spec = importlib.util.spec_from_file_location("cw056_probe", CW056_MODULE)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise GuardFailure(f"cannot load {CW056_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_head_schema(dsn: str) -> dict[str, Any]:
    import psycopg  # 局部导入：只有 schema 模式才需要

    module = _load_cw056_module()
    with psycopg.connect(dsn) as conn:
        inventory = module._schema_inventory(conn)
        counts = module._schema_counts(conn)
        digest = module._inventory_digest(inventory)
        version_rows = [
            row[0]
            for row in conn.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchall()
        ]
    return {
        "head_revision": version_rows[0] if len(version_rows) == 1 else None,
        "version_rows": version_rows,
        "counts": counts,
        "table_names": sorted(inventory["tables"]),
        "digest": digest,
    }


def resolve_dsn(explicit: str | None) -> str:
    dsn = explicit or os.environ.get("TEST_POSTGRESQL_URL")
    if not dsn:
        raise GuardFailure(
            "schema mode needs a database: pass --dsn or set TEST_POSTGRESQL_URL "
            "(e.g. `scripts/pg-fixture.sh start` prints one)"
        )
    return dsn


def run_print_schema(dsn: str) -> int:
    measured = measure_head_schema(dsn)
    cw056 = load_cw056_constants()
    if measured["version_rows"] != [cw056["HEAD_REVISION"]]:
        print(
            f"NOTE: database is at {measured['version_rows']}, test_cw056.HEAD_REVISION "
            f"is {cw056['HEAD_REVISION']!r}. Upgrade to head before trusting these values.",
            file=sys.stderr,
        )
    print("# Paste into server/tests/test_cw056_supported_head_matrix.py")
    print(f"HEAD_REVISION = {measured['head_revision']!r}")
    print(
        f"HEAD_SCHEMA_COUNTS: dict[str, int] = {json.dumps(measured['counts'], indent=4, sort_keys=True)}"
    )
    print(f"HEAD_SCHEMA_DIGEST = {measured['digest']!r}")
    print(f"# table count: {len(measured['table_names'])}")
    print("HEAD_TABLE_NAMES: tuple[str, ...] = (")
    for name in measured["table_names"]:
        print(f"    {name!r},")
    print(")")
    return 0


def run_check_schema(dsn: str) -> list[str]:
    measured = measure_head_schema(dsn)
    cw056 = load_cw056_constants()
    failures: list[str] = []

    if measured["version_rows"] != [cw056["HEAD_REVISION"]]:
        failures.append(
            f"alembic_version rows {measured['version_rows']} != "
            f"[{cw056['HEAD_REVISION']!r}]; the database is not at the single head"
        )
    if measured["counts"] != cw056["HEAD_SCHEMA_COUNTS"]:
        failures.append(
            f"HEAD_SCHEMA_COUNTS drifted: measured {json.dumps(measured['counts'], sort_keys=True)} "
            f"vs frozen {json.dumps(cw056['HEAD_SCHEMA_COUNTS'], sort_keys=True)}"
        )
    if measured["digest"] != cw056["HEAD_SCHEMA_DIGEST"]:
        failures.append(
            f"HEAD_SCHEMA_DIGEST drifted: measured {measured['digest']} "
            f"vs frozen {cw056['HEAD_SCHEMA_DIGEST']}"
        )
    if measured["table_names"] != sorted(cw056["HEAD_TABLE_NAMES"]):
        missing = sorted(set(measured["table_names"]) - set(cw056["HEAD_TABLE_NAMES"]))
        extra = sorted(set(cw056["HEAD_TABLE_NAMES"]) - set(measured["table_names"]))
        failures.append(
            f"HEAD_TABLE_NAMES drifted: missing={missing} unexpected={extra}"
        )
    return failures


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="CI 守卫（免 PG），有漂移则 exit 1"
    )
    mode.add_argument(
        "--record", action="store_true", help="重新生成 server/migrations/manifest.json"
    )
    mode.add_argument(
        "--print-schema", action="store_true", help="打印可粘贴的冻结字面量（需 PG）"
    )
    mode.add_argument(
        "--check-schema", action="store_true", help="真实 PG 与冻结字面量比对（需 PG）"
    )
    parser.add_argument(
        "--dsn", default=None, help="schema 模式的 DSN；缺省读 TEST_POSTGRESQL_URL"
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            failures = run_check()
            if failures:
                print("==> migration guard FAILED:", file=sys.stderr)
                for item in failures:
                    print(f"  - {item}", file=sys.stderr)
                return 1
            print("==> migration guard OK")
            return 0

        if args.record:
            write_manifest(build_manifest(load_revisions(), load_cw056_constants()))
            print(f"==> recorded {MANIFEST_PATH.relative_to(REPO_ROOT)}")
            return 0

        if args.print_schema:
            return run_print_schema(resolve_dsn(args.dsn))

        failures = run_check_schema(resolve_dsn(args.dsn))
        if failures:
            print("==> schema guard FAILED:", file=sys.stderr)
            for item in failures:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("==> schema guard OK")
        return 0
    except GuardFailure as exc:
        print(f"==> migration guard FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
