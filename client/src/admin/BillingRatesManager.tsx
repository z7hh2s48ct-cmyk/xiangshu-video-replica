import { Fragment, useEffect, useRef, useState } from "react";
import { adminRead, adminWrite } from "../api.admin";
import { costInCredits, creditsToCost } from "./billingAmounts";
import { type BillingService, billingUnit } from "./billingTypes";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { formatDateTime } from "./ui/vocabulary";

/**
 * 一次已通过校验、等待运营确认的费率写入。
 *
 * 2026-09-12 管理端评审 P2：保存按钮写着"确认并保存"，但 save() 直接落库，
 * 页面对运营承诺的二次确认并不存在。校验仍在提交时立刻完成（格式错误当场报错、
 * 不必点两次），确认只覆盖"校验通过 → 真正写库"这一步。
 */
type PendingTariffSave = {
  payload: {
    service: string;
    expected_version: number;
    expected_pricing_version: number;
    tariff: {
      enabled: boolean;
      unit_credits: string | null;
      unit_cost_fen: string | null;
      unit_rounding: "ceil" | "exact";
    };
  };
  reason: string;
  costRounded: boolean;
  summary: string;
};

function describeTariff(
  service: BillingService,
  next: {
    unit_credits: string | null;
    unit_cost_fen: string | null;
    enabled: boolean;
  },
  currentCostCredits: string,
): string {
  const before = service.tariff.unit_credits ?? "未配置";
  const after = next.unit_credits ?? "未配置";
  const beforeCost = currentCostCredits || "未配置";
  const afterCost = next.unit_cost_fen ?? "未配置";
  return `售价（积分）：${before} → ${after}；成本（积分）：${beforeCost} → ${afterCost}；收费状态：${next.enabled ? "启用" : "停用"}。`;
}

type Catalog = {
  services: BillingService[];
  pricing?: { version: number; points_per_yuan: number | null };
};
type TariffHistoryItem = {
  version: number;
  enabled: boolean;
  unit_credits: string | null;
  unit_cost_fen: string | null;
  unit_rounding: string;
  source: "audit" | "current";
  current: boolean;
  actor_user_id: string | null;
  actor_username: string | null;
  reason: string | null;
  effective_at: string | null;
};
type TariffHistory = {
  service: string;
  name: string;
  unit: string;
  current_version: number;
  items: TariffHistoryItem[];
};
// 默认地址与服务端各 Provider 常量一致；配置覆盖与密钥在服务配置页管理。
const providerAddresses: Record<string, string> = {
  metaso: "https://metaso.cn",
  apilio: "https://api.apilio.ai",
  deepseek: "https://api.deepseek.com",
  hifly: "https://hfw-api.hifly.cc",
  dashscope: "https://dashscope.aliyuncs.com",
  tikhub: "https://api.tikhub.io",
  douyidou: "https://gateway.diadi.cn",
};

export function BillingRatesManager({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [catalog, setCatalog] = useState<Catalog>();
  const [selected, setSelected] = useState<BillingService>();
  const [price, setPrice] = useState("");
  const [cost, setCost] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [rounding, setRounding] = useState<"ceil" | "exact">("ceil");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [historyService, setHistoryService] = useState<string>();
  const [history, setHistory] = useState<TariffHistory>();
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [pendingSave, setPendingSave] = useState<PendingTariffSave>();
  // 写入失败留在确认框里（运营就地重试），与页面级的读取错误分开。
  const [saveError, setSaveError] = useState("");
  const saving = useRef(false);
  const historyRequest = useRef("");
  const costInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selected) costInput.current?.focus();
  }, [selected]);
  const retry = useRef<{ fingerprint: string; key: string } | undefined>(
    undefined,
  );
  useEffect(() => {
    void refresh;
    let active = true;
    setError("");
    void adminRead<Catalog>("/api/control/billing/catalog", "读取费用科目失败")
      .then((result) => {
        if (active) setCatalog(result);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取失败");
      });
    return () => {
      active = false;
    };
  }, [refresh]);
  function closeHistory() {
    historyRequest.current = "";
    setHistoryService(undefined);
    setHistoryBusy(false);
  }
  function select(service: BillingService) {
    closeHistory();
    setSelected(service);
    setPrice(service.tariff.unit_credits ?? "");
    setCost(
      costInCredits(
        service.tariff.unit_cost_fen,
        catalog?.pricing?.points_per_yuan ?? null,
      ),
    );
    setEnabled(service.tariff.enabled);
    setRounding(service.tariff.unit_rounding);
    setNotice("");
    setError("");
  }
  // P2-3：价目历史与编辑互斥展开；历史只读，审计员也能回查。
  function toggleHistory(service: BillingService) {
    if (historyService === service.service) {
      closeHistory();
      return;
    }
    setSelected(undefined);
    setHistory(undefined);
    setHistoryError("");
    setHistoryService(service.service);
    const token = service.service;
    historyRequest.current = token;
    setHistoryBusy(true);
    void adminRead<TariffHistory>(
      `/api/control/billing/tariff-history?service=${encodeURIComponent(token)}`,
      "读取价目历史失败",
    )
      .then((result) => {
        if (historyRequest.current !== token) return;
        setHistory(result);
      })
      .catch((cause: unknown) => {
        if (historyRequest.current !== token) return;
        setHistoryError(cause instanceof Error ? cause.message : "读取失败");
      })
      .finally(() => {
        if (historyRequest.current === token) setHistoryBusy(false);
      });
  }
  // 校验留在提交时立即完成：格式错误当场报错，不必先弹框再让运营发现填错。
  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || !catalog || readOnly || saving.current) return;
    if (!catalog.pricing) {
      setError("换算配置尚未读取，请取消编辑并重新读取。");
      return;
    }
    let storedPrice: string | null;
    let storedCost: string | null;
    let costRounded = false;
    let originalCredits = "";
    try {
      if (selected.customer_charge_allowed) {
        if (price && !/^\d+(\.\d{1,6})?$/.test(price))
          throw new Error("售价需为非负积分数，最多 6 位小数。");
        storedPrice = price || null;
      } else {
        // 平台科目不向客户收费：售价与启用状态在此收敛，服务端守卫会再次拒绝。
        storedPrice = null;
      }
      originalCredits = costInCredits(
        selected.tariff.unit_cost_fen,
        catalog.pricing.points_per_yuan,
      );
      if (cost === originalCredits) storedCost = selected.tariff.unit_cost_fen;
      else if (!cost) storedCost = null;
      else {
        const converted = creditsToCost(
          cost,
          catalog.pricing.points_per_yuan ?? 0,
        );
        storedCost = converted.value;
        costRounded = !converted.exact;
      }
      if (selected.customer_charge_allowed && enabled) {
        if (storedPrice === null) throw new Error("启用收费必须填写售价。");
        // 单价 0 与未配置同罪：服务端 fail-closed 守卫会把 0 判为「资费未配置」
        // 并 503。放它入库只会让「明明配了却用不了」拖到运行时才暴露。
        if (Number(storedPrice) <= 0)
          throw new Error("启用收费必须填写大于 0 的售价（0 与未配置同罪）。");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "金额格式不正确。");
      return;
    }
    const payload = {
      service: selected.service,
      expected_version: selected.tariff.version,
      expected_pricing_version: catalog.pricing.version,
      tariff: {
        enabled: selected.customer_charge_allowed ? enabled : false,
        unit_credits: storedPrice,
        unit_cost_fen: storedCost,
        unit_rounding: rounding,
      },
    };
    setError("");
    setSaveError("");
    // 校验通过 ≠ 已保存：交给确认框，运营看过改了什么再落库。
    setPendingSave({
      payload,
      reason: selected.customer_charge_allowed
        ? `配置${selected.name}成本与售价`
        : `配置${selected.name}成本`,
      costRounded,
      summary: describeTariff(
        selected,
        {
          unit_credits: storedPrice,
          unit_cost_fen: storedCost,
          enabled: payload.tariff.enabled,
        },
        originalCredits,
      ),
    });
  }

  async function commitSave() {
    if (!pendingSave || saving.current) return;
    const { payload, reason, costRounded } = pendingSave;
    const fingerprint = JSON.stringify([payload, reason]);
    // 同一次提交的重试沿用同一个幂等键；改了内容才换新键。
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setSaveError("");
    try {
      const result = await adminWrite<Catalog>(
        "/api/control/billing/tariff",
        payload,
        reason.trim(),
        "保存费用科目失败",
        retry.current.key,
        "PUT",
      );
      setCatalog(result);
      setSelected(undefined);
      setPendingSave(undefined);
      setNotice(
        costRounded
          ? "已保存，成本已按支持的精度四舍五入。"
          : "已保存。新请求使用本次配置，已受理请求保留原价格。",
      );
      retry.current = undefined;
    } catch (cause) {
      // 失败留在确认框内，运营可就地重试（幂等键复用）。
      setSaveError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  const platformServices =
    catalog?.services.filter((service) => !service.customer_charge_allowed) ??
    [];
  function renderHistoryRow(service: BillingService, unit: string) {
    const pointsPerYuan = catalog?.pricing?.points_per_yuan ?? null;
    return (
      <tr className="billing-rates-history">
        <td colSpan={8}>
          <p>
            {
              "价目历史按审计记录重建，历史账单上的「费率 V{n}」对应下表版本号；「当前行（无审计）」表示该版本来自旧写入路径，只有当前快照。"
            }
          </p>
          {historyBusy && <p role="status">正在读取价目历史…</p>}
          {historyError && <p role="alert">{historyError}</p>}
          {!historyBusy && !historyError && history && (
            <div className="admin-table-scroll">
              <table
                className="admin-data-table"
                aria-label={`${service.name}价目版本历史`}
              >
                <thead>
                  <tr>
                    <th>版本</th>
                    <th>调整时间</th>
                    <th>售价（积分 / {unit}）</th>
                    <th>成本（积分 / {unit}）</th>
                    <th>用户扣分</th>
                    <th>操作人</th>
                    <th>调整原因</th>
                    <th>记录来源</th>
                  </tr>
                </thead>
                <tbody>
                  {history.items.map((item) => (
                    <tr key={item.version}>
                      <td>
                        V{item.version}
                        {item.current && <small>当前</small>}
                      </td>
                      <td>
                        {item.effective_at
                          ? formatDateTime(item.effective_at)
                          : "—"}
                      </td>
                      <td>{item.unit_credits ?? "未配置"}</td>
                      <td>
                        {costInCredits(item.unit_cost_fen, pointsPerYuan) ||
                          (item.unit_cost_fen === null
                            ? "待配置"
                            : "请设置充值换算")}
                      </td>
                      <td>
                        {item.enabled && Number(item.unit_credits) > 0
                          ? "用户承担"
                          : "平台承担"}
                      </td>
                      <td>{item.actor_username ?? "—"}</td>
                      <td>{item.reason ?? "—"}</td>
                      <td>
                        {item.source === "audit"
                          ? "审计记录"
                          : "当前行（无审计）"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </td>
      </tr>
    );
  }
  function renderPlatformRow(service: BillingService) {
    const editing = selected?.service === service.service;
    const historyOpen = historyService === service.service;
    const unit = billingUnit[service.unit];
    // 云存储与支付通道按零费用核算，成本与保存入口都由平台锁定。
    const locked = service.zero_cost_platform;
    const displayedCost = costInCredits(
      service.tariff.unit_cost_fen,
      catalog?.pricing?.points_per_yuan ?? null,
    );
    return (
      <Fragment key={service.service}>
        <tr className={editing ? "is-editing" : undefined}>
          <td>
            <strong>{service.name}</strong>
            <code>{service.service}</code>
          </td>
          <td>
            <span>{service.provider}</span>
            <small>{providerAddresses[service.provider] ?? "见服务配置"}</small>
          </td>
          <td>每{unit}</td>
          <td>
            {editing ? (
              <input
                ref={costInput}
                aria-label={`成本（积分 / ${unit}，留空待核对）`}
                value={cost}
                onChange={(event) => setCost(event.target.value)}
                inputMode="decimal"
                placeholder="待配置"
                disabled={
                  busy || readOnly || !catalog?.pricing?.points_per_yuan
                }
              />
            ) : locked ? (
              "按零费用核算"
            ) : (
              displayedCost ||
              (service.tariff.unit_cost_fen === null
                ? "待配置"
                : "请设置充值换算")
            )}
          </td>
          <td>不向客户收费</td>
          <td>平台承担</td>
          <td>
            {service.updated_by ? (
              <>
                <span>{service.updated_by}</span>
                <small>{formatDateTime(service.updated_at)}</small>
              </>
            ) : (
              "—"
            )}
          </td>
          <td>
            <button
              type="button"
              disabled={readOnly || busy || locked}
              aria-expanded={editing}
              aria-label={
                editing ? `正在配置 ${service.name}` : `配置 ${service.name}`
              }
              title={locked ? "按零费用核算，无需配置" : undefined}
              onClick={() => select(service)}
            >
              {editing ? "编辑中" : "配置"}
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              aria-expanded={historyOpen}
              aria-label={
                historyOpen
                  ? `正在查看 ${service.name} 的价目历史`
                  : `查看 ${service.name} 的价目历史`
              }
              onClick={() => toggleHistory(service)}
            >
              {historyOpen ? "收起历史" : "历史"}
            </button>
          </td>
        </tr>
        {editing && (
          <tr className="billing-rates-editor">
            <td colSpan={8}>
              <div className="billing-rates-editor__actions">
                <button type="submit" disabled={busy || readOnly}>
                  {busy ? "保存中…" : "保存"}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy}
                  onClick={() => setSelected(undefined)}
                >
                  取消
                </button>
              </div>
              {error && <p role="alert">{error}</p>}
            </td>
          </tr>
        )}
        {historyOpen && renderHistoryRow(service, unit)}
      </Fragment>
    );
  }
  return (
    <section className="admin-panel" aria-label="逐项成本与售价">
      <p>
        爆款视频数据按后台采集的已确认接口请求次数，使用采集批次开始时的单次售价向客户扣分；搜索分页和视频号详情分别计次。未配置或未启用售价时不向客户收费，读取已采集视频不扣分。
      </p>
      <div className="billing-rates-toolbar">
        <span>
          单位：积分
          {catalog?.pricing?.points_per_yuan
            ? ` · 1 元 = ${catalog.pricing.points_per_yuan} 积分`
            : ""}
        </span>
        <button
          type="button"
          className="btn-secondary"
          disabled={busy || Boolean(selected)}
          onClick={() => setRefresh((value) => value + 1)}
        >
          重新读取
        </button>
      </div>
      {catalog && !catalog.pricing?.points_per_yuan && (
        <p role="status">设置充值换算后即可配置积分成本。</p>
      )}
      {error && !selected && (
        <p role="alert">
          {error}{" "}
          <button
            type="button"
            onClick={() => setRefresh((value) => value + 1)}
          >
            重新读取
          </button>
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {!catalog ? (
        <p>正在读取科目…</p>
      ) : (
        <form
          onSubmit={save}
          aria-label={selected ? `配置 ${selected.name}` : "API 端点价格表"}
        >
          <div className="admin-table-scroll admin-table-card billing-rates-table">
            <table className="admin-data-table" aria-label="API 端点成本与售价">
              <thead>
                <tr>
                  <th>费用科目 / API</th>
                  <th>服务商 / 默认地址</th>
                  <th>单位</th>
                  <th>成本（积分）</th>
                  <th>售价（积分）</th>
                  <th>收费设置</th>
                  <th>最后修改</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {catalog.services
                  .filter((service) => service.customer_charge_allowed)
                  .map((service) => {
                    const editing = selected?.service === service.service;
                    const historyOpen = historyService === service.service;
                    const unit = billingUnit[service.unit];
                    const displayedCost = costInCredits(
                      service.tariff.unit_cost_fen,
                      catalog.pricing?.points_per_yuan ?? null,
                    );
                    return (
                      <Fragment key={service.service}>
                        <tr className={editing ? "is-editing" : undefined}>
                          <td>
                            <strong>{service.name}</strong>
                            <code>{service.service}</code>
                          </td>
                          <td>
                            <span>{service.provider}</span>
                            <small>
                              {providerAddresses[service.provider] ??
                                "见服务配置"}
                            </small>
                          </td>
                          <td>每{unit}</td>
                          <td>
                            {editing ? (
                              <input
                                ref={costInput}
                                aria-label={`成本（积分 / ${unit}，留空待核对）`}
                                value={cost}
                                onChange={(event) =>
                                  setCost(event.target.value)
                                }
                                inputMode="decimal"
                                placeholder="待配置"
                                disabled={
                                  busy ||
                                  readOnly ||
                                  !catalog.pricing?.points_per_yuan
                                }
                              />
                            ) : (
                              displayedCost ||
                              (service.tariff.unit_cost_fen === null
                                ? "待配置"
                                : "请设置充值换算")
                            )}
                          </td>
                          <td>
                            {editing ? (
                              <input
                                aria-label={`售价（积分 / ${unit}）`}
                                value={price}
                                onChange={(event) =>
                                  setPrice(event.target.value)
                                }
                                inputMode="decimal"
                                placeholder="未配置"
                                disabled={busy || readOnly}
                              />
                            ) : (
                              (service.tariff.unit_credits ?? "未配置")
                            )}
                          </td>
                          <td>
                            {editing ? (
                              <>
                                <label>
                                  <input
                                    type="checkbox"
                                    checked={enabled}
                                    onChange={(event) =>
                                      setEnabled(event.target.checked)
                                    }
                                    disabled={
                                      busy ||
                                      readOnly ||
                                      !service.customer_charge_allowed
                                    }
                                  />
                                  启用用户扣分
                                </label>
                                {service.unit === "second" && (
                                  <select
                                    aria-label="不足一秒的计费方式"
                                    value={rounding}
                                    onChange={(event) =>
                                      setRounding(
                                        event.target.value as "ceil" | "exact",
                                      )
                                    }
                                    disabled={busy || readOnly}
                                  >
                                    <option value="ceil">向上取整到秒</option>
                                    <option value="exact">按实际小数秒</option>
                                  </select>
                                )}
                              </>
                            ) : service.tariff.enabled &&
                              Number(service.tariff.unit_credits) > 0 ? (
                              "用户承担"
                            ) : (
                              "平台承担"
                            )}
                          </td>
                          <td>
                            {service.updated_by ? (
                              <>
                                <span>{service.updated_by}</span>
                                <small>
                                  {formatDateTime(service.updated_at)}
                                </small>
                              </>
                            ) : (
                              "—"
                            )}
                          </td>
                          <td>
                            <button
                              type="button"
                              disabled={readOnly || busy}
                              aria-expanded={editing}
                              aria-label={
                                editing
                                  ? `正在配置 ${service.name}`
                                  : `配置 ${service.name}`
                              }
                              onClick={() => select(service)}
                            >
                              {editing ? "编辑中" : "配置"}
                            </button>
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={busy}
                              aria-expanded={historyOpen}
                              aria-label={
                                historyOpen
                                  ? `正在查看 ${service.name} 的价目历史`
                                  : `查看 ${service.name} 的价目历史`
                              }
                              onClick={() => toggleHistory(service)}
                            >
                              {historyOpen ? "收起历史" : "历史"}
                            </button>
                          </td>
                        </tr>
                        {editing && (
                          <tr className="billing-rates-editor">
                            <td colSpan={8}>
                              <div className="billing-rates-editor__actions">
                                <button
                                  type="submit"
                                  disabled={busy || readOnly}
                                >
                                  {busy ? "保存中…" : "保存"}
                                </button>
                                <button
                                  type="button"
                                  className="btn-secondary"
                                  disabled={busy}
                                  onClick={() => setSelected(undefined)}
                                >
                                  取消
                                </button>
                              </div>
                              {error && <p role="alert">{error}</p>}
                            </td>
                          </tr>
                        )}
                        {historyOpen && renderHistoryRow(service, unit)}
                      </Fragment>
                    );
                  })}
              </tbody>
            </table>
          </div>
          {platformServices.length > 0 && (
            <div className="admin-table-scroll admin-table-card billing-rates-table">
              <p>
                平台科目（不向客户收费）：质量检查等科目只核算服务商成本，不向客户扣分；
                云存储与支付通道按零费用核算。
              </p>
              <table
                className="admin-data-table"
                aria-label="平台科目成本（不向客户收费）"
              >
                <thead>
                  <tr>
                    <th>费用科目 / API</th>
                    <th>服务商 / 默认地址</th>
                    <th>单位</th>
                    <th>成本（积分）</th>
                    <th>售价（积分）</th>
                    <th>收费设置</th>
                    <th>最后修改</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>{platformServices.map(renderPlatformRow)}</tbody>
              </table>
            </div>
          )}
        </form>
      )}

      {/* 2026-09-12 评审 P2：保存按钮承诺了"二次确认"却直接落库，这里补上。
          standard 级——该写入的审计原因由页面按科目名自动生成，不额外要求运营
          手填（那会改变既有审计内容）；框内先把"改了什么"和"将记录什么原因"
          摊开，避免盲确认。 */}
      <ConfirmDialog
        busy={busy}
        confirmLabel="确认并保存"
        description={
          pendingSave ? (
            <>
              {pendingSave.summary}
              <br />
              审计原因将记录为「{pendingSave.reason}」。
            </>
          ) : undefined
        }
        error={saveError}
        level="standard"
        open={pendingSave !== undefined}
        title={selected ? `确认调整「${selected.name}」费率` : "确认调整费率"}
        onClose={() => {
          if (busy) return;
          setPendingSave(undefined);
          setSaveError("");
        }}
        onConfirm={() => void commitSave()}
      />
    </section>
  );
}
