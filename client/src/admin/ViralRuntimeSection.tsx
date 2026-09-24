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
import { StatusBadge } from "./ui/StatusBadge";

type PendingAction =
  | "collection"
  | "import"
  | "availability"
  | "keywords"
  | null;

export function ViralRuntimeSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [controls, setControls] = useState<ViralRuntimeControls>();
  const [keywords, setKeywords] = useState<
    Array<
      NonNullable<ViralRuntimeControls["keywords"]>[number] & {
        draftId: string;
      }
    >
  >([]);
  const [perKeywordLimit, setPerKeywordLimit] = useState(10);
  const [intervalDays, setIntervalDays] = useState(7);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null);
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
      const value = await fetchViralRuntimeControls();
      setControls(value);
      setKeywords(
        (value.keywords ?? []).map((item) => ({
          ...item,
          draftId: crypto.randomUUID(),
        })),
      );
      setPerKeywordLimit(value.per_keyword_limit ?? 10);
      setIntervalDays(value.collection_interval_days ?? 7);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取爆款视频运行状态失败"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function confirm() {
    if (!controls || !pending) return;
    const reason =
      pending === "availability"
        ? "更新视频可用状态"
        : pending === "collection"
          ? "更新爆款视频采集开关"
          : pending === "keywords"
            ? "更新爆款视频采集设置"
            : "更新爆款视频导入开关";
    setSaving(true);
    setError("");
    setNotice("");
    try {
      if (pending === "availability") {
        if (!videoId.trim()) throw new Error("请输入源平台视频 ID");
        await updateViralVideoAvailability(
          platform,
          videoId.trim(),
          availability,
          reason,
        );
        setNotice("视频可用状态已更新。");
      } else {
        const next = await updateViralRuntimeControls(
          {
            collection_enabled:
              pending === "collection"
                ? !controls.collection_enabled
                : controls.collection_enabled,
            import_enabled:
              pending === "import"
                ? !controls.import_enabled
                : controls.import_enabled,
            ...(pending === "keywords"
              ? {
                  keywords: keywords.map(({ platform, category, keyword }) => ({
                    platform,
                    category,
                    keyword,
                  })),
                  per_keyword_limit: perKeywordLimit,
                  collection_interval_days: intervalDays,
                }
              : {}),
          },
          reason,
        );
        setControls(next);
        setNotice(
          pending === "keywords"
            ? "定时采集设置已更新。"
            : "爆款视频运行开关已更新。",
        );
      }
      setPending(null);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新爆款视频配置失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="爆款视频运行控制" className="admin-panel">
      <h2>爆款视频</h2>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!controls ? (
        <p className="admin-hint">读取中…</p>
      ) : (
        <>
          <p className="admin-hint">
            采集{" "}
            <StatusBadge
              tone={controls.collection_enabled ? "good" : "neutral"}
            >
              {controls.collection_enabled ? "已开启" : "已暂停"}
            </StatusBadge>
            　导入{" "}
            <StatusBadge tone={controls.import_enabled ? "good" : "neutral"}>
              {controls.import_enabled ? "已开启" : "已暂停"}
            </StatusBadge>
          </p>
          <p className="admin-hint">
            导入任务：排队 {controls.pending_imports} / 执行{" "}
            {controls.running_imports} / 失败 {controls.failed_imports}
          </p>
          <p className="admin-hint">
            数据源：
            {controls.source_configured ? "已配置（未实时探测）" : "未配置"}
            ；刷新任务： 排队 {controls.pending_refreshes} / 执行{" "}
            {controls.running_refreshes} / 失败 {controls.failed_refreshes}
          </p>
          <p className="admin-hint">
            {controls.collection_interval_days === 1 ? "每天" : "每周"}
            采集一次。首次配置后由后台启动首轮采集，关键词修改在下一轮生效。
            下一次：
            {controls.next_collection_at
              ? new Date(controls.next_collection_at).toLocaleString("zh-CN")
              : "等待首次配置或后台调度"}
            。 列表和播放读取已入库的云端素材；未配置关键词时不会采集。
          </p>
          <div className="admin-form-grid">
            <label>
              刷新周期
              <select
                disabled={readOnly || saving}
                value={intervalDays}
                onChange={(event) =>
                  setIntervalDays(Number(event.target.value))
                }
              >
                <option value={1}>每天</option>
                <option value={7}>每周</option>
              </select>
            </label>
            {keywords.map((item, index) => (
              <fieldset key={item.draftId}>
                <legend>采集关键词 {index + 1}</legend>
                <label>
                  平台 {index + 1}
                  <select
                    disabled={readOnly || saving}
                    value={item.platform}
                    onChange={(event) =>
                      setKeywords(
                        keywords.map((row, i) =>
                          i === index
                            ? {
                                ...row,
                                platform: event.target.value as
                                  | "douyin"
                                  | "wechat_channels",
                              }
                            : row,
                        ),
                      )
                    }
                  >
                    <option value="douyin">抖音</option>
                    <option value="wechat_channels">视频号</option>
                  </select>
                </label>
                <label>
                  分类 {index + 1}
                  <input
                    disabled={readOnly || saving}
                    maxLength={32}
                    value={item.category}
                    onChange={(event) =>
                      setKeywords(
                        keywords.map((row, i) =>
                          i === index
                            ? { ...row, category: event.target.value }
                            : row,
                        ),
                      )
                    }
                  />
                </label>
                <label>
                  关键词 {index + 1}
                  <input
                    disabled={readOnly || saving}
                    maxLength={80}
                    value={item.keyword}
                    onChange={(event) =>
                      setKeywords(
                        keywords.map((row, i) =>
                          i === index
                            ? { ...row, keyword: event.target.value }
                            : row,
                        ),
                      )
                    }
                  />
                </label>
                {!readOnly && (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() =>
                      setKeywords(keywords.filter((_, i) => i !== index))
                    }
                  >
                    删除关键词 {index + 1}
                  </button>
                )}
              </fieldset>
            ))}
            <label>
              每个关键词最多采集
              <input
                type="number"
                min={1}
                max={50}
                disabled={readOnly || saving}
                value={perKeywordLimit}
                onChange={(event) =>
                  setPerKeywordLimit(Number(event.target.value))
                }
              />
            </label>
            {!readOnly && (
              <div className="admin-actions">
                <button
                  type="button"
                  disabled={saving || keywords.length >= 20}
                  onClick={() =>
                    setKeywords([
                      ...keywords,
                      {
                        platform: "douyin",
                        category: "",
                        keyword: "",
                        draftId: crypto.randomUUID(),
                      },
                    ])
                  }
                >
                  添加关键词
                </button>
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => {
                    if (
                      keywords.some(
                        (row) => !row.keyword.trim() || !row.category.trim(),
                      ) ||
                      !Number.isInteger(perKeywordLimit) ||
                      perKeywordLimit < 1 ||
                      perKeywordLimit > 50
                    ) {
                      setError(
                        "请填写分类和关键词，采集数量须为 1 至 50 的整数。",
                      );
                      return;
                    }
                    setPending("keywords");
                  }}
                >
                  保存采集设置
                </button>
              </div>
            )}
          </div>
          {controls.platforms.map((item) => (
            <p className="admin-hint" key={item.platform}>
              {item.platform === "douyin" ? "抖音" : "视频号"}：已缓存{" "}
              {item.cached_videos}
              条，最后采集 {item.last_fetched_at ?? "暂无"}，状态{" "}
              {item.refresh_status === "ok"
                ? "正常"
                : item.refresh_status === "refreshing"
                  ? "采集中"
                  : item.refresh_status === "error"
                    ? (item.last_refresh_error ?? "刷新失败")
                    : item.refresh_status === "configured_only"
                      ? "已配置，未验证"
                      : "未配置"}
            </p>
          ))}
          {!readOnly ? (
            <div className="admin-actions">
              <button type="button" onClick={() => setPending("collection")}>
                {controls.collection_enabled ? "暂停采集" : "恢复采集"}
              </button>
              <button type="button" onClick={() => setPending("import")}>
                {controls.import_enabled ? "暂停导入" : "恢复导入"}
              </button>
            </div>
          ) : null}
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
          <button type="button" onClick={() => setPending("availability")}>
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
        open={pending !== null}
        title="更新爆款视频运行状态"
        onClose={() => setPending(null)}
        onConfirm={() => void confirm()}
      />
    </section>
  );
}
