import { useEffect, useState } from "react";
import {
  type CustomerConsumptionByBusiness,
  type CustomerPricing,
  type CustomerSessionCredential,
  customerGetConsumptionByBusiness,
  customerGetPricing,
} from "../api";
import { ConsumptionDonut } from "./ConsumptionDonut";
import { RetryButton } from "./RetryButton";

export function CustomerPricesPage({
  credential,
}: {
  credential: () => Promise<CustomerSessionCredential>;
}) {
  const [prices, setPrices] = useState<CustomerPricing | null>(null);
  // P1#10：价格与「我实际花在哪」的关联感——单独取一次构成，失败不影响价目表。
  const [consumption, setConsumption] =
    useState<CustomerConsumptionByBusiness | null>(null);
  /**
   * 构成的失败原因。`consumption === null` 同时表示「还在读」与「读失败」，只靠它
   * 分不出两者，页面会永久停在「正在读取消费构成…」——客户既不被告知失败，也没有
   * 重试入口（重试按钮只挂在价目表的失败分支上）。
   */
  const [consumptionError, setConsumptionError] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  /** 构成自己的重试计数：价目表已经成功时不该被拉回全页 loading。 */
  const [consumptionRetry, setConsumptionRetry] = useState(0);
  useEffect(() => {
    void retry;
    let active = true;
    setPrices(null);
    setError("");
    void credential()
      .then(customerGetPricing)
      .then((value) => {
        if (active) setPrices(value);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取价格失败");
      });
    return () => {
      active = false;
    };
  }, [credential, retry]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: retry 是刻意的：用户点「重新加载价格」时构成要跟着重取。
  useEffect(() => {
    let active = true;
    setConsumption(null);
    setConsumptionError("");
    void credential()
      .then((auth) => customerGetConsumptionByBusiness(auth, 30))
      .then((value) => {
        if (active) setConsumption(value);
      })
      .catch((cause) => {
        // 构成是锦上添花：取不到不影响价目表本身可用——但要说出「取不到」，
        // 不能把失败留在「正在读取消费构成…」上骗客户一直等。
        if (active) {
          setConsumptionError(
            cause instanceof Error ? cause.message : "读取消费构成失败",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [credential, retry, consumptionRetry]);
  return (
    <section className="uc-card" aria-label="接口价格">
      <div className="uc-section-heading">
        <div>
          <h2>接口价格</h2>
          <p>按实际用量扣除账号积分，所有 Token 共用账号余额。</p>
        </div>
      </div>
      {error ? (
        <div role="alert">
          <p>{error}</p>
          <RetryButton
            label="重新加载价格"
            onClick={() => setRetry((v) => v + 1)}
          />
        </div>
      ) : !prices ? (
        <p role="status">正在读取价格…</p>
      ) : (
        <>
          <section className="uc-donut-block" aria-label="最近 30 天消费构成">
            <h3>最近 30 天消费构成</h3>
            {consumption ? (
              <ConsumptionDonut
                days={consumption.days}
                items={consumption.items}
              />
            ) : consumptionError ? (
              <div role="alert" className="uc-donut__error">
                <p>{consumptionError}</p>
                <RetryButton
                  label="重新加载消费构成"
                  onClick={() => setConsumptionRetry((value) => value + 1)}
                />
              </div>
            ) : (
              <p className="uc-donut__empty" role="status">
                正在读取消费构成…
              </p>
            )}
          </section>
          <p>
            {prices.configured
              ? `当前价格版本：V${prices.version}`
              : "未发布售价的功能不扣分，由平台承担费用。"}
          </p>
          <div className="uc-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>接口功能</th>
                  <th>模型 / 规格</th>
                  <th>积分单价</th>
                </tr>
              </thead>
              <tbody>
                {prices.prices.map((price) => (
                  <tr key={price.subject}>
                    <td>{price.name}</td>
                    <td>{price.specification}</td>
                    <td>
                      <strong>
                        {price.unit_credits} 积分 / {price.unit}
                      </strong>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {prices.config && (
            <p>
              消费按表中单价的{" "}
              {(prices.config.discount_basis_points ?? 10000) / 100}%
              计费，逐任务
              {prices.config.consumption_rounding === "floor" ? "向下" : "向上"}
              取整，最低 1 积分；标为 0 积分的功能不单独扣分。
            </p>
          )}
          {prices.config && (
            <p>
              <strong>1 元 = {prices.config.points_per_yuan} 积分</strong> ·{" "}
              {prices.recharge_rounding}
            </p>
          )}
          <p>
            任务按提交时的价格预扣，成功后结算，失败任务退回对应预扣积分，部分成功只结算成功交付数量。价格调整不影响已受理任务和已创建充值订单。
          </p>
        </>
      )}
    </section>
  );
}
