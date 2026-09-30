import { AuditEventsPage } from "./AuditEventsPage";

/**
 * 审计中心只回答「哪位管理员在什么时候改了什么、为什么」。调账记录已迁入
 * 资金中心·人工调整（方案 P1：一件事只有一个入口），这里不再重复入口。
 */
export function AuditCenterPage() {
  return <AuditEventsPage />;
}
