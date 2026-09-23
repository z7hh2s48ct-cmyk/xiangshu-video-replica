"""MIGRATION-GUARD-20260912 — 迁移链守卫的静态契约与变异测试。

对应《并行开发迁移 Head 与 PR 冲突处置手册》§2 家族 #2/#3（head 常量与 CW-056 冻结
矩阵靠人手改）与 §5（push 前预检清单）。本文件把那些人工步骤变成可执行断言。

**本文件不需要 PostgreSQL，也不需要 Docker**：全部是文件集运算、AST 解析与对
``scripts/ci/migration_manifest.py`` 的进程内调用（与 ``test_cw061_shard_coverage_guard.py``
同款边界声明）。

分两类用例：

1. **真实树契约**（不经 monkeypatch）：守卫在真实迁移树上必须零 failure；且守卫的独立
   重算结果必须与 ``test_cw056_supported_head_matrix.py`` 的冻结字面量逐项相等。
   这就是「双跑」：两套实现必须同时正确，任何一边漂移都会红。
2. **变异用例**（合成树 + monkeypatch）：守卫必须**被触发**。一个从不失败的守卫等于没有
   守卫，所以每条不变量都配一个反例。合成树里的 CW-056 常量由本文件自算——这对变异用例
   是充分的（测的是「变异被抓到」而非哈希公式本身；公式正确性由第 1 类的真实字面量承担）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "scripts" / "ci" / "migration_manifest.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MIGRATE_SH = REPO_ROOT / "deploy" / "postgres" / "migrate.sh"
ROLLOUT_SH = REPO_ROOT / "deploy" / "customer-git-rollout.sh"

_MIGRATION_TEMPLATE = """\
revision = {rev!r}
down_revision = {down!r}
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
"""


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("migration_manifest_under_test", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


# ---------------------------------------------------------------------------
# 1. 真实树契约
# ---------------------------------------------------------------------------


def test_guard_reports_no_failures_on_the_real_tree() -> None:
    """当前树必须零 failure —— 即 CI 上 ``--check`` 应当通过。"""
    failures = gen.run_check()
    assert failures == [], "migration guard reported:\n  - " + "\n  - ".join(failures)


def test_viral_copy_cache_migration_is_pg_only_and_has_registered_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    migration = REPO_ROOT / "server/migrations/versions/20260915T1600_viral_copy_cache.py"
    spec = importlib.util.spec_from_file_location("viral_copy_cache_migration", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20260915T1600_viral_copy_cache"
    assert module.down_revision == "20260914T0000_local_joint_merge"
    calls: list[object] = []
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            execute=calls.append,
            drop_table=calls.append,
        ),
    )
    module.upgrade()
    module.downgrade()
    assert calls == []


def test_guard_recomputation_agrees_with_cw056_frozen_literals() -> None:
    """双跑核心：守卫独立重算的结果必须等于 CW-056 的冻结字面量。

    守卫不拥有这些常量（真源在测试模块），它只做 AST 读取 + 独立重算 + 比对。
    任一侧漂移都会在这里或 ``test_cw056`` 里红，不会两边一起错。
    """
    cw056 = gen.load_cw056_constants()
    revisions = gen.load_revisions()

    graph = gen.compute_graph(revisions)
    assert graph["heads"] == [cw056["HEAD_REVISION"]], (
        "chain head and test_cw056.HEAD_REVISION disagree; one of them is stale"
    )
    assert len(graph["bases"]) == 1, f"expected a single base, got {graph['bases']}"

    chain = gen.walk_published_chain(revisions, cw056["PUBLISHED_HEAD_REVISION"])
    assert len(chain) == cw056["PUBLISHED_CHAIN_LENGTH"]
    content, relation = gen.published_chain_hashes(chain)
    assert content == cw056["PUBLISHED_CHAIN_CONTENT_SHA256"], (
        "published chain content hash disagrees with the frozen literal"
    )
    assert relation == cw056["PUBLISHED_CHAIN_RELATION_SHA256"], (
        "published chain relation hash disagrees with the frozen literal"
    )


def test_manifest_on_disk_matches_a_fresh_recomputation() -> None:
    """清单不得是陈旧的：磁盘上的清单 == 现在重算出来的清单（静态半部）。"""
    manifest = gen.load_manifest()
    expected = gen.build_manifest(gen.load_revisions(), gen.load_cw056_constants())
    for key in ("graph", "published_chain", "naming_policy", "revisions"):
        assert manifest.get(key) == expected[key], (
            f"manifest section {key!r} is stale; run "
            f"`python3 scripts/ci/migration_manifest.py --record` and commit it"
        )


# ---------------------------------------------------------------------------
# 2. 变异用例（合成树）
# ---------------------------------------------------------------------------

_BASE_CHAIN: dict[str, str | None] = {
    "000_base": None,
    "001_early": "000_base",
    "002_published": "001_early",
    "003_tip": "002_published",
}
_PUBLISHED_HEAD = "002_published"
_HEAD = "003_tip"


def _cw056_stub_for(
    versions_dir: Path,
    revisions: dict[str, str | None],
    published_head: str,
    head: str,
) -> str:
    """生成一份只含常量、与合成链自洽的 CW-056 桩。

    链哈希用与守卫**相同**的公式自算——含 ``sha256(迁移文件字节)``，否则 content 哈希
    永远对不上。对变异用例充分（见模块 docstring）。
    """
    chain: list[tuple[str, str | None, str]] = []
    current: str | None = published_head
    while current is not None:
        digest = hashlib.sha256((versions_dir / f"{current}.py").read_bytes()).hexdigest()
        chain.append((current, revisions[current], digest))
        current = revisions[current]
    chain.reverse()

    content = "\n".join(f"{rev}<-{down or ''}:{digest}" for rev, down, digest in chain)
    relation = "\n".join(f"{rev}<-{down or ''}" for rev, down, _ in chain)
    return (
        "from __future__ import annotations\n"
        f"HEAD_REVISION = {head!r}\n"
        f"PUBLISHED_HEAD_REVISION = {published_head!r}\n"
        f"PUBLISHED_CHAIN_LENGTH = {len(chain)}\n"
        f"PUBLISHED_CHAIN_CONTENT_SHA256 = {hashlib.sha256(content.encode()).hexdigest()!r}\n"
        f"PUBLISHED_CHAIN_RELATION_SHA256 = {hashlib.sha256(relation.encode()).hexdigest()!r}\n"
        "HEAD_SCHEMA_COUNTS: dict[str, int] = {}\n"
        "HEAD_TABLE_NAMES: tuple[str, ...] = ()\n"
        "HEAD_SCHEMA_DIGEST = ''\n"
        "SUPPORTED_RELEASE_HEADS: dict[str, tuple[str, str]] = {}\n"
        "MATRIX_STARTING_HEADS: tuple[str, ...] = ('',)\n"
    )


def _build_synthetic_tree(root: Path) -> None:
    versions = root / "server" / "migrations" / "versions"
    versions.mkdir(parents=True)
    for rev, down in _BASE_CHAIN.items():
        (versions / f"{rev}.py").write_text(
            _MIGRATION_TEMPLATE.format(rev=rev, down=down), encoding="utf-8"
        )
    tests_dir = root / "server" / "tests"
    tests_dir.mkdir(parents=True)
    # 桩必须在迁移文件落盘之后生成：它的 content 哈希含那些文件的字节。
    (tests_dir / "test_cw056_supported_head_matrix.py").write_text(
        _cw056_stub_for(versions, _BASE_CHAIN, _PUBLISHED_HEAD, _HEAD), encoding="utf-8"
    )


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """把守卫指向一棵合成迁移树，并先记录一份基线清单。"""
    _build_synthetic_tree(tmp_path)
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(gen, "MIGRATIONS_DIR", tmp_path / "server" / "migrations")
    monkeypatch.setattr(gen, "VERSIONS_DIR", tmp_path / "server" / "migrations" / "versions")
    monkeypatch.setattr(
        gen, "CW056_MODULE", tmp_path / "server" / "tests" / "test_cw056_supported_head_matrix.py"
    )
    monkeypatch.setattr(gen, "MANIFEST_PATH", tmp_path / "server" / "migrations" / "manifest.json")
    # 合成树里没有真实的 089_customer_api_keys，把命名锚点指向合成链的链尾，
    # 于是「其后新增的迁移」就是本次变异加进去的那些文件。
    monkeypatch.setattr(gen, "NAMING_POLICY_ADOPTION_HEAD", _HEAD)

    # 先记录再断言：run_check 要求清单存在，所以「基线干净」只能在记录之后建立。
    gen.write_manifest(gen.build_manifest(gen.load_revisions(), gen.load_cw056_constants()))
    assert gen.run_check() == [], (
        "synthetic baseline must be clean before mutating; a failing baseline would "
        "make every mutation case below pass for the wrong reason"
    )
    return gen


def _add_revision(rev: str, down: str | tuple[str, ...]) -> None:
    (gen.VERSIONS_DIR / f"{rev}.py").write_text(
        _MIGRATION_TEMPLATE.format(rev=rev, down=down), encoding="utf-8"
    )


def test_second_head_is_rejected(synthetic: Any) -> None:
    """变体 A：从已发布点分叉出第二个 head —— 手册 §1 描述的正是这个失效链条。"""
    _add_revision("004_sibling", _PUBLISHED_HEAD)
    failures = synthetic.run_check()
    assert any("exactly one migration head" in item for item in failures), failures


def test_merge_revision_closes_a_second_head(synthetic: Any) -> None:
    """变体 A 的修复侧：``alembic merge`` 形态的 tuple 父节点必须被接受为合法收口。

    这是本任务引入的核心能力——多头是合法的瞬时状态，用 merge revision 收口，
    而不是重写某个已经合并的 revision。
    """
    # 旁支必须挂在**已有后代**的点上（002_published 之下已有 003_tip），
    # 挂在链尾只会把 head 前移，不产生第二个 head。
    _add_revision("004_sibling", _PUBLISHED_HEAD)
    assert any("exactly one migration head" in i for i in synthetic.run_check())
    _add_revision("004_merge", ("004_sibling", _HEAD))
    failures = synthetic.run_check()
    assert not any("exactly one migration head" in i for i in failures), failures
    # head 与 CW-056 字面量现在必然不一致（真源要跟着走），但那不是本用例的命题。
    assert not any("published chain" in i for i in failures), failures
    synthetic.write_manifest(
        synthetic.build_manifest(synthetic.load_revisions(), synthetic.load_cw056_constants())
    )
    assert not any("section 'graph' is stale" in i for i in synthetic.run_check())
    manifest = synthetic.load_manifest()
    manifest["graph"]["parents"]["004_merge"] = [_HEAD]
    synthetic.write_manifest(manifest)
    assert any("section 'graph' is stale" in i for i in synthetic.run_check())


def test_branch_point_inside_the_published_range_is_rejected(synthetic: Any) -> None:
    """已发布段必须保持线性 —— 否则「已发布链哈希唯一」这个前提就不成立。

    构造要点：分支点必须落在**从已发布 head 向下的行走路径上**。一个挂在路径之外的
    旁支（``001b_fork`` 挂 ``000_base``）不会被这条检查抓到，只会被单 head 检查抓到——
    那是另一条不变量，用另一个用例覆盖。
    """
    _add_revision("001b_other", "001_early")
    # 把已发布点本身改成 merge revision：从它向下的行走立刻遇到 tuple。
    (synthetic.VERSIONS_DIR / "002_published.py").write_text(
        _MIGRATION_TEMPLATE.format(rev="002_published", down=("001_early", "001b_other")),
        encoding="utf-8",
    )
    with pytest.raises(synthetic.GuardFailure, match="published chain must stay linear"):
        synthetic.run_check()


def test_new_revision_must_follow_the_naming_policy(synthetic: Any) -> None:
    """新迁移必须用时间戳前缀；存量段豁免。"""
    _add_revision("090_badly_named", _HEAD)
    failures = synthetic.run_check()
    assert any("timestamp naming policy" in item for item in failures), failures
    assert any("090_badly_named" in item for item in failures), failures


def test_timestamp_named_revision_does_not_trip_the_naming_policy(synthetic: Any) -> None:
    """正向对照：符合策略的新迁移**不应**产生命名类 failure。

    没有这条，命名检查可能只是「凡新增皆报错」而看起来在正常工作。
    """
    _add_revision("20260912T1200_customer_discounts", _HEAD)
    failures = synthetic.run_check()
    assert not any("timestamp naming policy" in item for item in failures), failures


def test_the_naming_anchor_cannot_be_laundered_by_re_recording(synthetic: Any) -> None:
    """锚点不可前移：`--record` 不能把违规命名洗白。

    这是一个真实的自我豁免路径：若锚点取自清单，那么「加一个违规命名的迁移 →
    跑 --record（锚点随之推到它）→ 提交」就能让它成为锚点本身、落进豁免集，
    此后再怎么 --check 都查不出来。
    """
    _add_revision("090_badly_named", _HEAD)
    assert any("timestamp naming policy" in i for i in synthetic.run_check())

    # 重新记录（会写出一份包含该违规 revision 的清单）……
    synthetic.write_manifest(
        synthetic.build_manifest(synthetic.load_revisions(), synthetic.load_cw056_constants())
    )
    # ……违规仍然必须被报出来，而不是因为「现在它是锚点」而消失。
    failures = synthetic.run_check()
    assert any("timestamp naming policy" in i for i in failures), failures


def test_manifest_anchor_tampering_is_detected(synthetic: Any) -> None:
    """清单里被手改的锚点必须被发现（锚点真源是本文件的常量）。"""
    manifest = synthetic.load_manifest()
    manifest["naming_policy"]["adoption_head"] = "090_badly_named"
    synthetic.write_manifest(manifest)
    failures = synthetic.run_check()
    assert any("adoption_head" in item for item in failures), failures


def test_rewriting_a_recorded_revision_is_rejected(synthetic: Any) -> None:
    """改动一个已记录 revision 的文件而不重新记录 —— 捕获「重写已落地迁移」。"""
    target = gen.VERSIONS_DIR / "001_early.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    failures = synthetic.run_check()
    assert any("changed since the manifest was recorded" in item for item in failures), failures


def test_duplicate_revision_id_is_rejected(synthetic: Any) -> None:
    """两个文件声明同一个 revision id —— 直接 fail-closed，不做「取其一」。"""
    (gen.VERSIONS_DIR / "001_early_dupe.py").write_text(
        _MIGRATION_TEMPLATE.format(rev="001_early", down="000_base"), encoding="utf-8"
    )
    with pytest.raises(synthetic.GuardFailure, match="duplicate revision id"):
        synthetic.load_revisions()


# ---------------------------------------------------------------------------
# 3. 接线契约（守卫必须真的在 CI 与部署脚本里生效）
# ---------------------------------------------------------------------------


def test_ci_runs_the_guard_before_the_sharded_pytest() -> None:
    """CI 必须在跑分片 pytest 之前执行守卫。

    顺序是命题的一部分：守卫的作用是「在昂贵且难定位的分片阶段之前拦住漂移」，
    排在 pytest 之后等于白装。照 ``test_build_contracts.py`` 里 CW-061 接线契约的写法。
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "migration_manifest.py --check" in workflow
    assert workflow.index("migration_manifest.py --check") < workflow.index(
        "bash scripts/ci/run-pytest-shards.sh"
    )


def test_migrate_sh_fails_closed_on_multiple_heads() -> None:
    """``deploy/postgres/migrate.sh`` 必须显式要求恰一个 head。

    守的是 ``alembic current`` 在库里留下多行 ``alembic_version`` 的情形：原来
    ``awk 'NR == 1'`` 只取首行，可能把一个多头库判成「已到达预期 head」而报成功。
    这里锁定计数守卫存在，与 ``customer-git-rollout.sh`` 的既有写法对齐。
    """
    body = MIGRATE_SH.read_text(encoding="utf-8")
    assert "alembic current" in body
    assert "HEAD_COUNT" in body, "migrate.sh must count heads rather than take the first"
    assert "CURRENT_COUNT" in body, "migrate.sh must count applied versions too"
    # 升级行不得被 exec 掉（exec 之后的检查永远不会执行）。
    upgrade_lines = [line for line in body.splitlines() if "alembic upgrade head" in line]
    assert upgrade_lines, "migrate.sh must run `alembic upgrade head`"
    assert all(not line.strip().startswith("exec ") for line in upgrade_lines)


def test_rollout_guard_idiom_is_the_one_we_mirror() -> None:
    """防守性断言：被抄的那个既有守卫仍在原地。

    若 ``customer-git-rollout.sh`` 的写法变了，本任务的 migrate.sh 就失去了对齐基准，
    应该一起复核而不是各自漂移。
    """
    body = ROLLOUT_SH.read_text(encoding="utf-8")
    assert "HEAD_COUNT" in body
    assert "EXPECTED_DB_HEAD" in body


def _revisions_at_or_after(graph_parents: dict[str, Any], head: str) -> set[str]:
    """head 及其祖先闭包，兼容迁移元组与 JSON 数组父节点。"""
    seen: set[str] = set()
    stack = [head]
    while stack:
        rev = stack.pop()
        if rev in seen:
            continue
        seen.add(rev)
        down = graph_parents.get(rev)
        if down is None:
            continue
        stack.extend(down if isinstance(down, (tuple, list)) else [down])
    return seen


def test_manifest_is_valid_json_with_the_expected_shape() -> None:
    manifest = json.loads(gen.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["manifest_schema_version"] == gen.MANIFEST_SCHEMA_VERSION
    assert manifest["task"] == gen.TASK_ID
    assert "basis" in manifest, "the manifest must state what it does and does not prove"
    # 结构性不变量：恰一个 head，且命名锚点是该 head 自身或其祖先。
    # 不能断言 head == adoption_head：锚点恒定 089，而并行分支（如 CW-075 的折扣迁移）
    # 合法地在 089 之上追加时间戳命名的新 head；旧的头等断言对任何 089 之后的新迁移
    # 都会误红（merge 驱动的过拟合修正，与 run_check 的单 head + 命名策略不变量对齐）。
    heads = manifest["graph"]["heads"]
    assert len(heads) == 1, f"expected a single head, got {heads}"
    adoption = manifest["naming_policy"]["adoption_head"]
    assert adoption in _revisions_at_or_after(manifest["graph"]["parents"], heads[0]), (
        f"naming-policy adoption_head {adoption!r} must equal the head or be an "
        f"ancestor of it; recorded head is {heads[0]!r}"
    )
