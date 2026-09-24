// 统一的数据表包装：滚动容器 + sticky 表头。各页面保留自己的列定义，
// 这里只收口"表格长得一样、大表不撑破布局"这两件事。
export function DataTable({
  ariaLabel,
  headers,
  children,
}: {
  ariaLabel: string;
  headers: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="table-scroll admin-data-table-wrap">
      <table aria-label={ariaLabel} className="internal-table admin-data-table">
        <thead>
          <tr>{headers}</tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
