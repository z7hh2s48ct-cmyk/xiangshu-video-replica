// 统一分页条：offset 驱动，文案固定为"第 X / Y 页（共 N 条）"。
// 两种模式：
// - 已知 total（当前**全部**调用方都走这条）：单页也展示实际总数，翻页按钮禁用；
// - 未知 total：传 hasMore，只显示"第 X 页"，"下一页"按 hasMore 启用——不伪造总数。
//
// 2026-09-12 评审 P3 把第二种模式记为「零调用」。查证后**保留**：它不是遗留，
// 而是与"聚合拉不到就降级、绝不用 0 冒充"同一套不伪造原则配套的能力（当初的
// 调用方是设备页，该页已随 PR #102 删除）。删掉它会拿掉一个已文档化的安全阀，
// 而留着的成本只是一个可选 prop —— 故此处只更正过时的调用方引用，不删模式。
export function Pagination({
  offset,
  limit,
  total,
  hasMore,
  disabled = false,
  noun = "条",
  onPageChange,
}: {
  offset: number;
  limit: number;
  total?: number;
  /** total 未知模式的续页信号：本次取满一页即视为可能还有下一页。 */
  hasMore?: boolean;
  disabled?: boolean;
  /** 计数名词：条数用"条"，客户用"位"。 */
  noun?: string;
  onPageChange: (nextOffset: number) => void;
}) {
  const knownTotal = typeof total === "number";
  const totalPages = knownTotal ? Math.max(1, Math.ceil(total / limit)) : null;
  const currentPage = Math.floor(offset / limit) + 1;
  const hasNext = knownTotal
    ? offset + limit < (total as number)
    : Boolean(hasMore);
  return (
    <nav aria-label="分页" className="pagination">
      <button
        disabled={disabled || offset <= 0}
        type="button"
        onClick={() => onPageChange(Math.max(0, offset - limit))}
      >
        上一页
      </button>
      <span>
        {totalPages !== null
          ? `第 ${currentPage} / ${totalPages} 页（共 ${total} ${noun}）`
          : `第 ${currentPage} 页`}
      </span>
      <button
        disabled={disabled || !hasNext}
        type="button"
        onClick={() => onPageChange(offset + limit)}
      >
        下一页
      </button>
    </nav>
  );
}
