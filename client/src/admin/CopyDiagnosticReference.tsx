import { useState } from "react";

export function CopyDiagnosticReference({
  value,
  label,
}: {
  value: string;
  label: string;
}) {
  const [notice, setNotice] = useState("");
  return (
    <span>
      <code>{value}</code>{" "}
      <button
        type="button"
        aria-label={`复制${label} ${value}`}
        onClick={() => {
          void navigator.clipboard.writeText(value).then(
            () => setNotice("已复制"),
            () => setNotice("复制失败，请手动复制"),
          );
        }}
      >
        复制
      </button>
      {notice ? <small role="status">{notice}</small> : null}
    </span>
  );
}
