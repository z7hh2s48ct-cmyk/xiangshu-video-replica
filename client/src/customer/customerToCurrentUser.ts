import type { CurrentUser, CustomerProfile } from "../api";
import type { CustomerWorkspaceUser } from "./useCustomerSession";

/**
 * 将客户工作台会话用户（+ 可选 profile）映射为应用统一的 CurrentUser 形状。
 *
 * CW-012：本函数原定义于旧入口 `RootApp.tsx`，客户工作台 `CustomerWorkspace`
 * 需反向依赖根入口才能取用，构成"客户域 → 旧入口"的类型/值依赖。迁出到客户域
 * 独立叶子模块后，映射职责归客户域所有，`RootApp.tsx` 不再持有该导出，解除依赖。
 *
 * 纯函数叶子：仅依赖 `../api` 与 `./useCustomerSession` 的类型，不引入运行时环。
 * 回退链保持与原实现逐字节一致：
 * - username：profile?.username ?? user.username ?? "customer"
 * - display_name：profile?.display_name ?? user.username ?? "客户"
 */
export function customerToCurrentUser(
  user: CustomerWorkspaceUser,
  profile?: CustomerProfile | null,
): CurrentUser {
  return {
    id: user.userId,
    username: profile?.username ?? user.username ?? "customer",
    display_name: profile?.display_name ?? user.username ?? "客户",
    role: "customer",
  };
}
