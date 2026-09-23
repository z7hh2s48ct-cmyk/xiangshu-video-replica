import { useCallback, useEffect, useState } from "react";

import {
  adminActivationErrorMessage,
  fetchViralRuntimeControls,
  updateViralRuntimeControls,
  updateViralVideoAvailability,
  type ViralRuntimeControls,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

export function ViralRuntimeSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [controls, setControls] = useState<ViralRuntimeControls>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmingAvailability, setConfirmingAvailability] = useState(false);
  const [confirmingImport, setConfirmingImport] = useState(false);
  const [platform, setPlatform] = useState<"douyin" | "wechat_channels">(
    "douyin",
  );
  const [videoId, setVideoId] = useState("");
  const [availability, setAvailability] = useState<
    "AVAILABLE" | "HIDDEN" | "UNAVAILABLE"
  >("HIDDEN");

  const load = useCallback(async () => {
    setError("");
    try {
      setControls(await fetchViralRuntimeControls());
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取爆款视频运行状态失败"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function confirmAvailability() {
    if (!videoId.trim()) {
      setError("请输入源平台视频 ID");
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await updateViralVideoAvailability(
        platform,
        videoId.trim(),
        availability,
        "更新视频可用状态",
      );
      setNotice("视频可用状态已更新。");
      setConfirmingAvailability(false);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新视频可用状态失败"));
    } finally {
      setSaving(false);
    }
  }

  async function confirmImport() {
    if (!controls) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const next = await updateViralRuntimeControls(
        {
          collection_enabled: false,
          import_enabled: !controls.import_enabled,
        },
        "更新爆款视频导入开关",
      );
      setControls(next);
      setNotice("爆款视频导入开关已更新。");
      setConfirmingImport(false);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新爆款视频导入开关失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="爆款视频运行控制" className="admin-panel">
      <h2>爆款视频内容池</h2>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!controls ? (
        <p className="admin-hint">读取中…</p>
      ) : (
        <>
          <PageBanner tone="notice">
            定时采集已停用。内容由客户关键词搜索持续沉淀，运营可在内容池中筛选首页精选。
          </PageBanner>
          <p className="admin-hint">
            任务状态：导入排队 {controls.pending_imports} / 执行{" "}
            {controls.running_imports} / 失败 {controls.failed_imports}
            ；刷新排队 {controls.pending_refreshes} / 执行{" "}
            {controls.running_refreshes} / 失败 {controls.failed_refreshes}。
          </p>
          {!readOnly ? (
            <button type="button" onClick={() => setConfirmingImport(true)}>
              {controls.import_enabled ? "暂停导入" : "恢复导入"}
            </button>
          ) : null}
          {controls.platforms.map((item) => (
            <p className="admin-hint" key={item.platform}>
              {item.platform === "douyin" ? "抖音" : "视频号"}：内容池{" "}
              {item.cached_videos} 条；历史最后采集{" "}
              {item.last_fetched_at ?? "暂无"}。
            </p>
          ))}
        </>
      )}
      {!readOnly ? (
        <div className="admin-form-grid">
          <label>
            平台
            <select
              value={platform}
              onChange={(event) =>
                setPlatform(event.target.value as typeof platform)
              }
            >
              <option value="douyin">抖音</option>
              <option value="wechat_channels">视频号</option>
            </select>
          </label>
          <label>
            视频 ID
            <input
              value={videoId}
              onChange={(event) => setVideoId(event.target.value)}
            />
          </label>
          <label>
            状态
            <select
              value={availability}
              onChange={(event) =>
                setAvailability(event.target.value as typeof availability)
              }
            >
              <option value="AVAILABLE">可用</option>
              <option value="HIDDEN">隐藏</option>
              <option value="UNAVAILABLE">不可用</option>
            </select>
          </label>
          <button type="button" onClick={() => setConfirmingAvailability(true)}>
            更新视频状态
          </button>
        </div>
      ) : null}
      <ConfirmDialog
        busy={saving}
        confirmLabel="确认更新"
        description="确认后立即生效。"
        error={error}
        level="standard"
        open={confirmingAvailability}
        title="更新爆款视频可用状态"
        onClose={() => setConfirmingAvailability(false)}
        onConfirm={() => void confirmAvailability()}
      />
      <ConfirmDialog
        busy={saving}
        confirmLabel="确认更新"
        description="导入开关用于 Web 回退和存量任务，确认后立即生效。"
        error={error}
        level="standard"
        open={confirmingImport}
        title="更新爆款视频导入状态"
        onClose={() => setConfirmingImport(false)}
        onConfirm={() => void confirmImport()}
      />
    </section>
  );
}
