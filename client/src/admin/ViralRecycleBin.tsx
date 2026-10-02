import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  listViralRecycleBin,
  restoreViralVideo,
  type ViralRecycleVideo,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { Pagination } from "./ui/Pagination";

export function ViralRecycleBin({ readOnly }: { readOnly: boolean }) {
  const [items, setItems] = useState<ViralRecycleVideo[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<ViralRecycleVideo | null>(null);
  const request = useRef(0);
  const key = useRef("");
  const load = useCallback(async () => {
    const version = ++request.current;
    try {
      const result = await listViralRecycleBin(offset);
      if (version !== request.current) return;
      setItems(result.items);
      setTotal(result.total);
      setError("");
    } catch (cause) {
      if (version === request.current)
        setError(adminActivationErrorMessage(cause, "回收站暂时无法读取"));
    }
  }, [offset]);
  useEffect(() => {
    void load();
    return () => {
      ++request.current;
    };
  }, [load]);
  async function restore(reason: string) {
    if (!selected || busy) return;
    setBusy(true);
    try {
      await restoreViralVideo(selected, reason, key.current);
      setSelected(null);
      setNotice("视频已恢复到库中，不会自动上首页。");
      await load();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "恢复失败，请刷新后重试"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-label="视频回收站" className="admin-viral-recycle">
      <h3>回收站</h3>
      <p className="admin-hint">
        删除后30天内可恢复，恢复不会自动上首页。到期保留删除标记，防止采集重新带回；永久清理需单独批准。
      </p>
      {error ? <p role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
      <button type="button" disabled={busy} onClick={() => void load()}>
        刷新回收站
      </button>
      {items.length ? (
        <div className="admin-table-scroll">
          <table className="admin-data-table">
            <thead>
              <tr>
                <th>视频</th>
                <th>删除时间</th>
                <th>恢复期限</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((video) => (
                <tr key={`${video.platform}:${video.video_id}`}>
                  <td>
                    {video.title || "未命名视频"}
                    <small>
                      {video.platform === "douyin" ? "抖音" : "视频号"} ·{" "}
                      {video.author || "未知作者"}
                    </small>
                  </td>
                  <td>{new Date(video.deleted_at).toLocaleString("zh-CN")}</td>
                  <td>
                    {new Date(video.restore_before).toLocaleString("zh-CN")}
                    {!video.restorable ? "（已到期）" : ""}
                  </td>
                  <td>
                    {!readOnly ? (
                      <button
                        type="button"
                        disabled={busy || !video.restorable}
                        onClick={() => {
                          key.current = crypto.randomUUID();
                          setError("");
                          setSelected(video);
                        }}
                      >
                        恢复到视频库
                      </button>
                    ) : (
                      "只读"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p>回收站暂无视频。</p>
      )}
      <Pagination
        disabled={busy}
        limit={25}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
      <ConfirmDialog
        open={selected !== null}
        title="恢复视频"
        description="恢复后不会自动上首页，原有排期也会清除。"
        level="reason"
        busy={busy}
        error={error}
        confirmLabel="确认恢复"
        onClose={() => setSelected(null)}
        onConfirm={(reason) => void restore(reason)}
      />
    </section>
  );
}
