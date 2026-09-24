import { useEffect, useRef, useState } from "react";

export function CopyCustomerId({ value }: { value: string }) {
  const [notice, setNotice] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      setNotice("客户 ID 已复制");
    } catch {
      setNotice("复制失败，请选中 ID 手动复制");
    }
    timer.current = setTimeout(() => setNotice(""), 2500);
  }
  return (
    <span className="customer-id-copy">
      <button
        className="customer-id-copy__value"
        type="button"
        title={`${value} · 双击复制客户 ID`}
        onClick={() => void copy()}
        onDoubleClick={() => void copy()}
      >
        <code>{value}</code>
      </button>
      <button
        className="customer-id-copy__icon"
        type="button"
        aria-label="复制客户 ID"
        title="复制客户 ID"
        onClick={() => void copy()}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          aria-hidden="true"
        >
          <rect x="8" y="8" width="12" height="12" rx="2" />
          <path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" />
        </svg>
      </button>
      {notice && (
        <span className="customer-id-copy__notice" role="status">
          {notice}
        </span>
      )}
    </span>
  );
}
