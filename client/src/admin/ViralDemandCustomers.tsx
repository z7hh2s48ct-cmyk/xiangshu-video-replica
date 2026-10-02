import { useEffect, useState } from "react";
import {
  listViralDemandCustomers,
  type ViralDemandCustomer,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";

export function ViralDemandCustomers({
  from,
  to,
  keyword,
  platform,
  onCustomer,
  onClose,
}: {
  from: string;
  to: string;
  keyword: string;
  platform: string;
  onCustomer?: (userId: string) => void;
  onClose: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{
    items: ViralDemandCustomer[];
    total: number;
    countingRule: string;
  } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setPage(null);
    setError("");
    void listViralDemandCustomers({ from, to, keyword, platform, offset })
      .then((next) => {
        if (active) setPage(next);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取搜索客户失败");
      });
    return () => {
      active = false;
    };
  }, [from, to, keyword, platform, offset]);
  return (
    <section className="admin-panel" aria-label="搜索客户列表">
      <h3>「{keyword}」搜索客户</h3>
      <p>
        {from} 至 {to} · {platform === "douyin" ? "抖音" : "视频号"}
      </p>
      <button type="button" onClick={onClose}>
        关闭客户列表
      </button>
      {error ? (
        <PageBanner tone="error">{error}</PageBanner>
      ) : !page ? (
        <p role="status">正在读取搜索客户…</p>
      ) : (
        <>
          <p className="admin-hint">{page.countingRule}</p>
          <div className="admin-table-scroll">
            <table className="admin-data-table">
              <thead>
                <tr>
                  <th>搜索账号 / 公司</th>
                  <th>成功搜索页次</th>
                  <th>零结果页次</th>
                  <th>命中视频</th>
                  <th>最后搜索</th>
                  <th>客户详情</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((item) => (
                  <tr key={item.user_id}>
                    <td>
                      {item.username || "账号已删除"}
                      <br />
                      {item.display_name || "—"}
                      {item.customer_user_id &&
                      item.customer_user_id !== item.user_id ? (
                        <small> · 子账号，详情打开所属主客户</small>
                      ) : null}
                    </td>
                    <td>{item.searches ?? "历史未知"}</td>
                    <td>{item.zero_results ?? "历史未知"}</td>
                    <td>{item.videos}</td>
                    <td>
                      {new Date(item.last_searched_at).toLocaleString("zh-CN", {
                        timeZone: "Asia/Shanghai",
                      })}
                    </td>
                    <td>
                      {item.customer_user_id && onCustomer ? (
                        <button
                          type="button"
                          onClick={() =>
                            onCustomer(item.customer_user_id as string)
                          }
                        >
                          查看客户详情
                        </button>
                      ) : (
                        "无法定位客户"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.items.length ? <p>该区间暂无搜索客户。</p> : null}
          <Pagination
            offset={offset}
            limit={20}
            total={page.total}
            onPageChange={setOffset}
          />
        </>
      )}
    </section>
  );
}
