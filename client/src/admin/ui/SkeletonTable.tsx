/** Skeleton loading table with placeholder rows and columns */
export function SkeletonTable({ numRows = 5 }: { numRows?: number }) {
  return (
    <div className="skeleton-table">
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>显示名称</th>
            <th>用户名</th>
            <th>状态</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: numRows }).map((_, idx) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: 骨架行是固定数量的纯占位展示，无重排/增删，顺序即内容。
            <tr key={idx}>
              <td className="skeleton-cell" style={{ width: "120px" }} />
              <td className="skeleton-cell" />
              <td className="skeleton-cell" />
              <td className="skeleton-cell" style={{ width: "80px" }} />
              <td className="skeleton-cell" style={{ width: "140px" }} />
              <td className="skeleton-cell" style={{ width: "160px" }} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
