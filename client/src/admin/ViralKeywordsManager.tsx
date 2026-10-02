import { useCallback, useEffect, useRef, useState } from "react";
import {
  addViralKeywords,
  adminActivationErrorMessage,
  deleteViralKeyword,
  editViralKeyword,
  listViralKeywords,
  type ViralKeyword,
  type ViralKeywordPage,
  type ViralKeywordPerformance,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

function config(row: ViralKeyword): ViralKeyword {
  return {
    platform: row.platform,
    category: row.category,
    keyword: row.keyword,
    enabled: row.enabled,
    limit: row.limit,
  };
}

export function ViralKeywordsManager({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [page, setPage] = useState<ViralKeywordPage | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const generation = useRef(0);
  const [pending, setPending] = useState<{
    kind: "add" | "edit" | "toggle" | "delete";
    row: ViralKeywordPerformance | null;
    key: string;
  } | null>(null);
  const [platform, setPlatform] = useState<ViralKeyword["platform"]>("douyin");
  const [category, setCategory] = useState("");
  const [text, setText] = useState("");
  const [limit, setLimit] = useState("");
  const [enabled, setEnabled] = useState(true);
  const load = useCallback(async () => {
    const current = ++generation.current;
    try {
      const next = await listViralKeywords();
      if (current === generation.current) setPage(next);
    } catch (cause) {
      if (current === generation.current)
        setError(adminActivationErrorMessage(cause, "读取关键词失败"));
    }
  }, []);
  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);
  function choose(
    kind: "add" | "edit" | "toggle" | "delete",
    row: ViralKeywordPerformance | null = null,
  ) {
    setError("");
    setNotice("");
    setPlatform(row?.platform ?? "douyin");
    setCategory(row?.category ?? page?.categories[0] ?? "");
    setText(row?.keyword ?? "");
    setLimit(row?.limit == null ? "" : String(row.limit));
    setEnabled(row?.enabled ?? true);
    setPending({ kind, row, key: crypto.randomUUID() });
  }
  async function confirm(reason: string) {
    if (!pending) return;
    setSaving(true);
    setError("");
    try {
      const count = limit.trim() === "" ? null : Number(limit);
      if (
        count !== null &&
        (!Number.isInteger(count) || count < 1 || count > 50)
      )
        throw new Error("每次条数请输入1至50的整数；留空使用默认条数。");
      if (pending.kind === "add") {
        const lines = text
          .split(/\r?\n/)
          .map((word) => word.trim())
          .filter(Boolean);
        const unique = [...new Set(lines)];
        if (!unique.length || unique.some((word) => word.length > 80))
          throw new Error("请输入关键词，每行一个，每词最多80字。");
        if (!category) throw new Error("请选择分类。");
        const result = await addViralKeywords(
          unique.map((keyword) => ({
            platform,
            category,
            keyword,
            enabled,
            limit: count,
          })),
          reason,
          pending.key,
        );
        setNotice(
          `新增${result.added}个关键词，跳过${result.duplicates + lines.length - unique.length}个重复词。下一轮采集生效。`,
        );
      } else if (pending.row) {
        const original = config(pending.row);
        if (pending.kind === "delete")
          await deleteViralKeyword(original, reason, pending.key);
        else
          await editViralKeyword(
            original,
            pending.kind === "toggle"
              ? { ...original, enabled: !original.enabled }
              : {
                  platform,
                  category,
                  keyword: text.trim(),
                  enabled,
                  limit: count,
                },
            reason,
            pending.key,
          );
        setNotice(
          pending.kind === "delete"
            ? "关键词已删除。"
            : "关键词已保存，下一轮采集生效；正在进行的批次继续按原计划执行。",
        );
      }
      setPending(null);
      await load();
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "保存关键词失败，草稿仍保留"),
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <>
      <section className="admin-panel" aria-label="关键词经营">
        <h2>关键词经营</h2>
        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
        {!page ? (
          <p role="status">正在读取关键词…</p>
        ) : (
          <>
            <p className="admin-hint">
              近30天（{page.from}至{page.to}）。{page.rule}
            </p>
            {!page.coverageComplete ? (
              <PageBanner tone="notice">
                记录从
                {new Date(page.measurementStartedAt).toLocaleString("zh-CN")}
                开始。表内为已记录数量，更早历史未知，效果暂不评级。
              </PageBanner>
            ) : null}
            <p className="admin-hint">{page.effectRule}</p>
            <div className="admin-viral-keyword-actions">
              {!readOnly ? (
                <button
                  type="button"
                  onClick={() => choose("add")}
                  disabled={saving}
                >
                  新增 / 批量粘贴
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => void load()}
                disabled={saving}
              >
                刷新关键词数据
              </button>
            </div>
            <div className="admin-table-scroll admin-viral-keyword-table-scroll">
              <table className="admin-data-table">
                <thead>
                  <tr>
                    <th>关键词 / 平台</th>
                    <th>分类</th>
                    <th>每次条数 / 启停</th>
                    <th>采集 / 上首页</th>
                    <th>客户使用</th>
                    <th>效果</th>
                    <th>最后采集</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((row) => (
                    <tr key={`${row.platform}:${row.keyword}`}>
                      <td>
                        <strong>{row.keyword}</strong>
                        <div className="admin-hint">
                          {row.platform === "douyin" ? "抖音" : "视频号"}
                        </div>
                      </td>
                      <td>{row.category}</td>
                      <td>
                        {row.limit ?? "默认"}条 ·{" "}
                        <StatusBadge tone={row.enabled ? "good" : "neutral"}>
                          {row.enabled ? "已启用" : "已暂停"}
                        </StatusBadge>
                      </td>
                      <td>
                        {row.collected} / {row.featured}
                      </td>
                      <td>
                        详情 {row.details} · 文案 {row.copies}
                        <div className="admin-hint">合计 {row.uses}</div>
                      </td>
                      <td>
                        {
                          (
                            {
                              high: "高",
                              medium: "中",
                              low: "低",
                              unknown: "未知",
                            } as const
                          )[row.effect]
                        }
                      </td>
                      <td>
                        {row.lastRun ? (
                          <>
                            <span>
                              {new Date(
                                row.lastRun.finished_at ??
                                  row.lastRun.started_at,
                              ).toLocaleString("zh-CN")}
                            </span>
                            <div className="admin-hint">
                              {row.lastRun.status === "SUCCEEDED"
                                ? "搜索完成（含零结果）"
                                : row.lastRun.status === "FAILED"
                                  ? "搜索失败"
                                  : "进行中，结果待确认"}
                            </div>
                          </>
                        ) : (
                          "尚无执行记录"
                        )}
                      </td>
                      <td>
                        {readOnly ? (
                          "只读"
                        ) : (
                          <>
                            <button
                              type="button"
                              aria-label={`编辑${row.keyword}`}
                              onClick={() => choose("edit", row)}
                            >
                              编辑
                            </button>
                            <button
                              type="button"
                              aria-label={`${row.enabled ? "暂停" : "启用"}${row.keyword}`}
                              onClick={() => choose("toggle", row)}
                            >
                              {row.enabled ? "暂停" : "启用"}
                            </button>
                            <button
                              type="button"
                              aria-label={`删除${row.keyword}`}
                              onClick={() => choose("delete", row)}
                            >
                              删除
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {page.items.length === 0 ? (
                <p>暂无关键词，添加后下一轮采集生效。</p>
              ) : null}
            </div>
          </>
        )}
      </section>
      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.kind === "add"
            ? "新增采集关键词"
            : pending?.kind === "edit"
              ? "编辑采集关键词"
              : pending?.kind === "delete"
                ? "删除采集关键词"
                : "修改关键词启停"
        }
        description={
          pending?.row?.keyword ??
          "每行一个关键词，重复词会去重；所有词在同一事务保存。"
        }
        busy={saving}
        error={error}
        onConfirm={(reason) => void confirm(reason)}
        onClose={() => !saving && setPending(null)}
      >
        {pending?.kind === "add" || pending?.kind === "edit" ? (
          <div className="admin-form-grid admin-viral-keyword-form">
            <label>
              平台
              <select
                value={platform}
                onChange={(event) =>
                  setPlatform(event.target.value as ViralKeyword["platform"])
                }
              >
                <option value="douyin">抖音</option>
                <option value="wechat_channels">视频号</option>
              </select>
            </label>
            <label>
              分类
              <select
                value={category}
                onChange={(event) => setCategory(event.target.value)}
              >
                {page?.categories.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {pending.kind === "add" ? "关键词（每行一个）" : "关键词"}
              <textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
              />
            </label>
            <label>
              每次条数（留空使用默认）
              <input
                type="number"
                min={1}
                max={50}
                value={limit}
                onChange={(event) => setLimit(event.target.value)}
              />
            </label>
            <label className="admin-viral-keyword-enabled">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              启用采集
            </label>
          </div>
        ) : null}
      </ConfirmDialog>
    </>
  );
}
