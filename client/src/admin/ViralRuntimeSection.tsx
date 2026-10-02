import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  collectViralNow,
  estimateViralCollection,
  fetchViralRuntimeControls,
  updateViralRuntimeControls,
  type ViralCollectionEstimate,
  type ViralRuntimeControls,
} from "../api.admin";
import { yuanInputToFen } from "../rechargePackageDisplay";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

export function ViralRuntimeSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [controls, setControls] = useState<ViralRuntimeControls | null>(null);
  const [interval, setInterval] = useState(7);
  const [executionTime, setExecutionTime] = useState("");
  const [estimate, setEstimate] = useState<ViralCollectionEstimate | null>(
    null,
  );
  const [estimating, setEstimating] = useState(false);
  const [limit, setLimit] = useState(10);
  const [minLikes, setMinLikes] = useState("");
  const [minDuration, setMinDuration] = useState("");
  const [maxDuration, setMaxDuration] = useState("");
  const [exclude, setExclude] = useState("");
  const [budget, setBudget] = useState("");
  const [budgetInvalid, setBudgetInvalid] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<
    "settings" | "collection" | "import" | "collect" | null
  >(null);
  const generation = useRef(0);
  const load = useCallback(async (drafts = false) => {
    const current = ++generation.current;
    try {
      const next = await fetchViralRuntimeControls();
      if (current !== generation.current) return;
      setControls(next);
      if (drafts) {
        setInterval(next.collection_interval_days ?? 7);
        setExecutionTime(next.collection_time ?? "");
        setLimit(next.per_keyword_limit ?? 10);
        setMinLikes(
          next.quality_min_likes == null ? "" : String(next.quality_min_likes),
        );
        setMinDuration(
          next.quality_duration_min_ms == null
            ? ""
            : String(next.quality_duration_min_ms / 1000),
        );
        setMaxDuration(
          next.quality_duration_max_ms == null
            ? ""
            : String(next.quality_duration_max_ms / 1000),
        );
        setExclude((next.quality_exclude_words ?? []).join("、"));
        setBudget(
          next.monthly_budget_fen == null
            ? ""
            : String(next.monthly_budget_fen / 100),
        );
      }
    } catch (cause) {
      if (current === generation.current)
        setError(adminActivationErrorMessage(cause, "读取采集设置失败"));
    }
  }, []);
  useEffect(() => {
    void load(true);
    return () => {
      generation.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (!controls?.pending_refreshes && !controls?.running_refreshes) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [controls?.pending_refreshes, controls?.running_refreshes, load]);
  async function confirm(reason: string) {
    if (!pending || !controls) return;
    generation.current += 1;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      if (pending === "collect") {
        await collectViralNow(reason, undefined, estimate?.snapshot);
        setNotice("采集已入队，可查看平台进度。");
        await load();
      } else {
        const budgetFen = budget.trim() === "" ? null : yuanInputToFen(budget);
        if (
          pending === "settings" &&
          (budgetInvalid || (budget.trim() !== "" && budgetFen === null))
        )
          throw new Error(
            "月度预算请输入大于零、最多两位小数的金额；清空才会解除限额。",
          );
        const duration = (value: string) => {
          if (!value.trim()) return null;
          const seconds = Number(value);
          if (!Number.isFinite(seconds) || seconds < 0)
            throw new Error("时长请输入非负秒数。");
          return Math.round(seconds * 1000);
        };
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
            ...(pending === "settings"
              ? {
                  per_keyword_limit: limit,
                  collection_interval_days: interval,
                  collection_time: executionTime || null,
                  quality_min_likes:
                    minLikes.trim() === "" ? null : Number(minLikes),
                  quality_duration_min_ms: duration(minDuration),
                  quality_duration_max_ms: duration(maxDuration),
                  quality_exclude_words: exclude
                    .split(/[,，、\n]/)
                    .map((value) => value.trim())
                    .filter(Boolean),
                  monthly_budget_fen: budgetFen,
                }
              : {}),
          },
          reason,
        );
        generation.current += 1;
        setControls(next);
        setNotice("采集设置已保存。");
      }
      setPending(null);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "操作失败，表单内容已保留"));
    } finally {
      setSaving(false);
    }
  }
  const descriptions = {
    settings: "保存计划、质量规则和预算；关键词在下方表格逐条维护。",
    collection: controls?.collection_enabled
      ? "暂停后不再安排新批次，运行中任务会在下一次状态检查时停止。"
      : "开启后按已保存计划采集启用的关键词。",
    import: controls?.import_enabled
      ? "暂停客户粘贴视频链接创建新导入任务。"
      : "允许客户粘贴视频链接并导入创作。",
    collect: "立即安排当前启用关键词的采集批次。满预算时仍允许此次手动操作。",
  };
  async function prepareManual() {
    setEstimating(true);
    setError("");
    setEstimate(null);
    try {
      setEstimate(await estimateViralCollection());
      setPending("collect");
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "无法读取采集预估，请稍后重试"),
      );
    } finally {
      setEstimating(false);
    }
  }
  return (
    <>
      <section className="admin-panel" aria-label="采集计划与质量">
        <h2>采集计划与质量</h2>
        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
        {!controls ? (
          <p role="status">正在读取采集设置…</p>
        ) : (
          <>
            <div className="admin-form-grid admin-viral-plan-form">
              <section>
                <h3>定时采集</h3>
                <StatusBadge
                  tone={controls.collection_enabled ? "good" : "neutral"}
                >
                  {controls.collection_enabled ? "已开启" : "已暂停"}
                </StatusBadge>
                <p className="admin-hint">
                  按计划采集启用的关键词；关键词启停和条数在下一轮生效。
                </p>
                {!readOnly ? (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setPending("collection")}
                  >
                    {controls.collection_enabled ? "暂停采集" : "开启采集"}
                  </button>
                ) : null}
              </section>
              <section>
                <h3>客户链接导入</h3>
                <StatusBadge
                  tone={controls.import_enabled ? "good" : "neutral"}
                >
                  {controls.import_enabled ? "已开启" : "已暂停"}
                </StatusBadge>
                <p className="admin-hint">客户粘贴视频链接并导入创作的功能。</p>
                {!readOnly ? (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setPending("import")}
                  >
                    {controls.import_enabled ? "暂停链接导入" : "开启链接导入"}
                  </button>
                ) : null}
              </section>
              <label>
                采集频率
                <select
                  disabled={readOnly || saving}
                  value={interval}
                  onChange={(event) => setInterval(Number(event.target.value))}
                >
                  <option value={1}>每天</option>
                  <option value={7}>每周</option>
                </select>
              </label>
              <label>
                默认每词条数
                <input
                  type="number"
                  min={1}
                  max={50}
                  disabled={readOnly || saving}
                  value={limit}
                  onChange={(event) => setLimit(Number(event.target.value))}
                />
              </label>
              <label>
                执行时间（上海时间）
                <input
                  type="time"
                  disabled={readOnly || saving}
                  value={executionTime}
                  onChange={(event) => setExecutionTime(event.target.value)}
                />
              </label>
              <label>
                最低点赞数
                <input
                  type="number"
                  min={0}
                  disabled={readOnly || saving}
                  value={minLikes}
                  onChange={(event) => setMinLikes(event.target.value)}
                />
              </label>
              <label>
                最短时长（秒）
                <input
                  type="number"
                  min={0}
                  step={0.001}
                  disabled={readOnly || saving}
                  value={minDuration}
                  onChange={(event) => setMinDuration(event.target.value)}
                />
              </label>
              <label>
                最长时长（秒）
                <input
                  type="number"
                  min={0}
                  step={0.001}
                  disabled={readOnly || saving}
                  value={maxDuration}
                  onChange={(event) => setMaxDuration(event.target.value)}
                />
              </label>
              <label>
                排除词
                <input
                  disabled={readOnly || saving}
                  value={exclude}
                  onChange={(event) => setExclude(event.target.value)}
                />
              </label>
              <label>
                月度采集预算（元）
                <input
                  type="number"
                  min={0.01}
                  step={0.01}
                  disabled={readOnly || saving}
                  value={budget}
                  onChange={(event) => {
                    setBudget(event.target.value);
                    setBudgetInvalid(event.target.validity.badInput);
                  }}
                />
              </label>
            </div>
            <p className="admin-hint">
              {executionTime
                ? interval === 7
                  ? "设置或更改每周时刻时，以当日为周起点；错过周期仅补采一次，随后保持原定星期。"
                  : "每天在设定的上海时间执行。"
                : "未设固定执行时间，沿用现有间隔计划。"}
              上次采集：
              {controls.last_collection_at
                ? new Date(controls.last_collection_at).toLocaleString("zh-CN")
                : "尚无执行记录"}
              。 下一次采集：
              {controls.next_collection_at
                ? new Date(controls.next_collection_at).toLocaleString("zh-CN")
                : "等待首次配置或调度"}
              。本月已知成本
              {controls.month_spend_fen == null
                ? "待核对"
                : ` ¥${(controls.month_spend_fen / 100).toFixed(4)}`}
              。
            </p>
            {controls.budget_status === "warning" ||
            controls.budget_status === "exhausted" ? (
              <PageBanner tone="warning">
                {controls.budget_status === "exhausted"
                  ? "已知成本已达预算100%，新定时采集已暂停；手动采集仍可确认执行。"
                  : "已知成本已达预算80%，请核对剩余预算。"}
              </PageBanner>
            ) : null}
            {controls.month_unknown_cost_count ||
            controls.month_pending_cost_count ? (
              <PageBanner tone="warning">
                费用尚未完整核对：未知 {controls.month_unknown_cost_count ?? 0}{" "}
                次， 进行中 {controls.month_pending_cost_count ?? 0}{" "}
                次；当前总费用未知， 已知成本占预算比例仅为下限。
              </PageBanner>
            ) : null}
            <p className="admin-hint">{controls.budget_scope}</p>
            {!readOnly ? (
              <div className="admin-form-grid admin-viral-plan-actions">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => setPending("settings")}
                >
                  保存采集设置
                </button>
                <button
                  type="button"
                  disabled={
                    saving || estimating || !controls.collection_enabled
                  }
                  onClick={() => void prepareManual()}
                >
                  立即采集
                </button>
              </div>
            ) : null}
            <section aria-label="采集平台状态">
              <h3>平台状态</h3>
              <div className="admin-viral-platform-table-scroll">
                <table className="admin-data-table">
                  <thead>
                    <tr>
                      <th>平台</th>
                      <th>状态</th>
                      <th>上次更新</th>
                      <th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {controls.platforms.map((item) => (
                      <tr key={item.platform}>
                        <td>
                          {item.platform === "douyin" ? "抖音" : "视频号"}
                        </td>
                        <td>
                          {
                            (
                              {
                                not_configured: "未配置",
                                configured_only: "已配置，未探测",
                                refreshing: "采集中",
                                ok: "最近采集成功",
                                error: "采集异常",
                              } as const
                            )[item.refresh_status]
                          }
                        </td>
                        <td>
                          {item.last_fetched_at
                            ? new Date(item.last_fetched_at).toLocaleString(
                                "zh-CN",
                              )
                            : "暂无"}
                        </td>
                        <td>
                          {item.last_refresh_error ??
                            (item.refresh_status === "error"
                              ? "请查看采集记录并核对服务配置。"
                              : "按已记录状态展示，未进行实时探测。")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
      </section>
      <ConfirmDialog
        open={pending !== null}
        title={
          pending === "settings"
            ? "保存采集设置"
            : pending === "collect"
              ? "立即采集"
              : pending === "collection"
                ? "修改定时采集开关"
                : "修改客户链接导入开关"
        }
        description={pending ? descriptions[pending] : ""}
        busy={saving}
        error={error}
        onConfirm={(reason) => void confirm(reason)}
        onClose={() => !saving && setPending(null)}
      >
        {pending === "settings" && controls ? (
          <section aria-label="采集设置变更摘要">
            <p>
              最低点赞：{controls.quality_min_likes ?? "不限"} →{" "}
              {minLikes.trim() || "不限"}
            </p>
            <p>
              最短时长（秒）：
              {controls.quality_duration_min_ms == null
                ? "不限"
                : controls.quality_duration_min_ms / 1000}{" "}
              → {minDuration.trim() || "不限"}
            </p>
            <p>
              最长时长（秒）：
              {controls.quality_duration_max_ms == null
                ? "不限"
                : controls.quality_duration_max_ms / 1000}{" "}
              → {maxDuration.trim() || "不限"}
            </p>
            <p>
              排除词：{controls.quality_exclude_words?.join("、") || "不排除"} →{" "}
              {exclude.trim() || "不排除"}
            </p>
            <p>
              月度预算：
              {controls.monthly_budget_fen == null
                ? "不限"
                : `¥${(controls.monthly_budget_fen / 100).toFixed(2)}`}{" "}
              → {budget.trim() ? `¥${Number(budget).toFixed(2)}` : "不限"}
            </p>
          </section>
        ) : null}
        {pending === "collect" && estimate ? (
          <div className="admin-viral-estimate">
            <p>
              启用关键词 {estimate.enabledKeywords} 个，预计搜索调用{" "}
              {estimate.searchCallsMin}～{estimate.searchCallsMax} 次，最多接收{" "}
              {estimate.videoLimit} 条视频（去重和过滤前）。
            </p>
            <p>
              搜索费预估：
              {estimate.searchCostMinFen == null ||
              estimate.searchCostMaxFen == null
                ? "价格未配置，费用未知"
                : `¥${(estimate.searchCostMinFen / 100).toFixed(2)}～¥${(estimate.searchCostMaxFen / 100).toFixed(2)}`}
              。
            </p>
            <p>
              视频号详情预计 {estimate.detailCallsMin ?? "未知"}～
              {estimate.detailCallsMax ?? "未知"} 次；含失败重试的数据接口总调用
              {estimate.physicalDataCallsMin ?? "未知"}～
              {estimate.physicalDataCallsMax ?? "未知"} 次。 数据接口成本区间：
              {estimate.dataCostMinFen == null ||
              estimate.dataCostMaxFen == null
                ? "未知"
                : `¥${(estimate.dataCostMinFen / 100).toFixed(4)}～¥${(estimate.dataCostMaxFen / 100).toFixed(4)}`}
              。
            </p>
            <p>
              常规失败重试估算至{" "}
              {estimate.normalRetryPhysicalCallsMax ?? "未知"} 次数据请求，
              对应接口成本{" "}
              {estimate.normalRetryDataCostMaxFen == null
                ? "未知"
                : `¥${(estimate.normalRetryDataCostMaxFen / 100).toFixed(4)}`}
              ； 断机恢复可能产生额外调用，最终上限未知。
            </p>
            <p>
              常规重试视频下载 {estimate.mediaDownloadsMin ?? "未知"}～
              {estimate.mediaDownloadsMax ?? "未知"} 次，封面下载
              {estimate.coverDownloadsMin ?? "未知"}～
              {estimate.coverDownloadsMax ?? "未知"}{" "}
              次；缓存命中、下载流量费和存储费未知， 总费用待核对。
            </p>
            <PageBanner tone="warning">
              此操作会影响客户积分：当前符合收费条件客户
              {estimate.customerCount ?? "待核对"} 位，每位每次成功数据请求收费
              {estimate.customerCreditsPerConfirmedCall ?? "待核对"} 积分，
              常规重试估算每位至{" "}
              {estimate.normalRetryCustomerCreditsMaxEach ?? "待核对"} 积分，
              最终上限未知（实际按成功请求结算；断机恢复可能增加调用；客户停用或余额不足时不补扣）。
            </PageBanner>
            {estimate.budget?.budget_status === "exhausted" ? (
              <PageBanner tone="warning">
                月度预算已满，此次手动采集仍会产生费用。
              </PageBanner>
            ) : null}
            <p className="admin-hint">{estimate.note}</p>
          </div>
        ) : null}
      </ConfirmDialog>
    </>
  );
}
