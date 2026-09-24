import { describe, expect, it } from "vitest";
import type { CustomerProfile } from "../api";
import { customerToCurrentUser } from "./customerToCurrentUser";
import type { CustomerWorkspaceUser } from "./useCustomerSession";

const profile: CustomerProfile = {
  user_id: "user-1",
  username: "customer-1",
  display_name: "李丽",
  joined_at: "2026-08-01T00:00:00Z",
  activation_code_masked: "XS04-ABCD••••WXYZ",
  activation_status: "ACTIVE",
  activated_at: "2026-08-02T00:00:00Z",
  device_slots_used: 1,
  device_slots_total: 2,
  // CW-062：母账号身份（无 parent）。
  account_type: "MASTER",
  parent_user_id: null,
  parent_display_name: null,
};

// CW-012：customerToCurrentUser 从旧入口 RootApp.tsx 迁出到客户域独立叶子模块。
// 本测试锁定迁移后行为与原实现逐字节等价（三条 ?? 回退链），并证明消费者
// 可从新路径 import——迁移前该 import 无法解析（红），创建模块后转绿。
describe("customerToCurrentUser（CW-012 迁出 RootApp 旧入口）", () => {
  it("profile 存在时优先采用 profile.username / profile.display_name", () => {
    const user: CustomerWorkspaceUser = {
      userId: "user-1",
      username: "fallback-name",
      accountType: "MASTER",
      parentUserId: null,
      parentDisplayName: null,
    };
    expect(customerToCurrentUser(user, profile)).toEqual({
      id: "user-1",
      username: "customer-1",
      display_name: "李丽",
      role: "customer",
    });
  });

  it("profile 缺省时 username 与 display_name 均回退到 user.username", () => {
    const user: CustomerWorkspaceUser = {
      userId: "user-2",
      username: "alice",
      accountType: "MASTER",
      parentUserId: null,
      parentDisplayName: null,
    };
    expect(customerToCurrentUser(user)).toEqual({
      id: "user-2",
      username: "alice",
      display_name: "alice",
      role: "customer",
    });
  });

  it("user.username 为 null 且无 profile 时回退到默认 customer/客户", () => {
    const user: CustomerWorkspaceUser = {
      userId: "user-3",
      username: null,
      accountType: null,
      parentUserId: null,
      parentDisplayName: null,
    };
    expect(customerToCurrentUser(user, null)).toEqual({
      id: "user-3",
      username: "customer",
      display_name: "客户",
      role: "customer",
    });
  });
});
