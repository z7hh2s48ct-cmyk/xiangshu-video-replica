import type { CustomerListItem } from "../api.admin";

const COMPANY_NAME_FALLBACK = "未填写";

export function companyNameOf(customer: CustomerListItem): string {
  return customer.display_name || COMPANY_NAME_FALLBACK;
}

/** 列表内标签列最多摆 3 个 pill，余量收成 +N（列宽有限，全摆会被截断）。 */
const TAG_PILL_LIMIT = 3;

export function CustomerTagPills({ tags }: { tags: string[] }) {
  if (tags.length === 0) {
    return <span className="customer-cell-muted">—</span>;
  }
  const visible = tags.slice(0, TAG_PILL_LIMIT);
  const hidden = tags.length - visible.length;
  return (
    <span className="customer-cell-tags" title={tags.join("、")}>
      {visible.map((tag) => (
        <span className="customer-tag-pill" key={tag}>
          {tag}
        </span>
      ))}
      {hidden > 0 ? <span className="customer-tag-pill">+{hidden}</span> : null}
    </span>
  );
}

export function scrollToSection(sectionId: string) {
  // jsdom 没有 scrollIntoView，必须走可选调用。
  document
    .getElementById(sectionId)
    ?.scrollIntoView?.({ behavior: "smooth", block: "start" });
}
