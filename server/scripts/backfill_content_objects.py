"""回填内容登记表：把已经存着的字节登记进去，让后续上传可以复用它们。

内容寻址方案的第 1 阶段。只做两件事：为存量资产建立 ``content_objects``
登记行，并把 ``assets.content_object_id`` 指过去。**不搬迁任何对象** ——
存量键原样保留，因为重写键意味着复制一次数量不可控的数据，且回滚代价极高。

这样做换来的收益：改造之前就上传过的素材，从此也能被去重命中，用户再传一次
同样的文件不会再落第二份。已经各占一份的重复对象不会被自动合并，它们会一直
留到引用自然消失、被回收器带走为止。

默认只读预览；写库需要显式 ``--apply``。可重复执行：已经回填过的资产会被跳过，
反复跑会逐步收敛直到查不到活干。

用法::

    python -m scripts.backfill_content_objects             # 预览
    python -m scripts.backfill_content_objects --apply     # 写库
    python -m scripts.backfill_content_objects --apply --limit 2000
"""

from __future__ import annotations

import argparse
import json

import psycopg

from app.content_store import backfill_content_objects
from app.db_pg import resolve_cli_pg_dsn
from app.db_portable import BusinessConnection


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill the content registry from existing assets."
    )
    parser.add_argument("--apply", action="store_true", help="write; default is preview")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    dsn = resolve_cli_pg_dsn()
    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        result = backfill_content_objects(conn, limit=args.limit, apply=args.apply)
        if args.apply:
            raw.commit()
        else:
            raw.rollback()
    print(json.dumps({"apply": args.apply, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
