import { useEffect, useRef, useState } from "react";

import { getAdminGenerationRecordThumbnail } from "../api.admin";

/** 生成结果的派生缩略图（方案 P2-1）。
 *
 * 只在详情展开时取地址（`active`）：服务端每签出一次就写一条审计，管理员「真的看了
 * 这张图」才是留痕的语义；在列表渲染时批量拉既会多出 N 次签发，也会把那批审计淹没。
 *
 * 三种情况显示占位而不是报错——没有媒体的记录类型（拆解、取帧）、历史记录（抽帧写入点
 * 上线前入库）、以及本地盘存储（服务端不发外链）。它们都不是故障，不该让运营以为坏了。
 */
export function RecordThumbnail({
  active,
  recordId,
  recordType,
}: {
  active: boolean;
  recordId: string;
  recordType: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [status, setStatus] = useState<
    "idle" | "loading" | "ready" | "empty" | "error"
  >("idle");
  // 只在第一次展开时取一次：展开→收起→再展开不该重复签发（那会重复写审计）。
  // 用 ref 而不是把 status 放进依赖——后者会让 setStatus 触发 effect 重跑，
  // 重跑的 cleanup 会把在途请求的结果丢掉（组件会永远停在"加载中"）。
  const requested = useRef(false);

  useEffect(() => {
    if (!active || requested.current) {
      return;
    }
    requested.current = true;
    let cancelled = false;
    setStatus("loading");
    getAdminGenerationRecordThumbnail(recordType, recordId)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setUrl(result.url);
        setStatus(result.url ? "ready" : "empty");
      })
      .catch(() => {
        if (!cancelled) {
          setStatus("error");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [active, recordId, recordType]);

  if (!active) {
    return null;
  }
  if (status === "ready" && url) {
    return (
      <img
        alt={`生成结果缩略图 ${recordId}`}
        className="admin-generation-records__thumbnail"
        src={url}
      />
    );
  }
  return (
    <p className="admin-generation-records__thumbnail-note" role="status">
      {status === "error"
        ? "缩略图读取失败，可稍后重试。"
        : status === "empty"
          ? "无缩略图（历史记录，或当前存储不提供派生小图）。"
          : "缩略图加载中…"}
    </p>
  );
}
