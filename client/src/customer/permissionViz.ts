/**
 * 批次2（CW-062 方案 B 差距 3）：功能权限矩阵可视化纯函数。
 *
 * 与后端 `sub_account_permissions.py` 的 BUSINESS_FEATURES 同源对齐——12 类
 * 业务键 + 中文标签，顺序即 Modal 勾选格子顺序（原型 v3「功能权限（12 类）」
 * 的客户口径）。`permissions === null` = 全允许（无权限行），与额度表
 * 「无行 = 不限」对称：前端只做展示与草稿整理，不放行任何操作；权威校验
 * 在服务端预扣/端点入口（403 三个稳定错误码）。
 */

import type { CustomerSubAccountPermissions } from "../api";

/** 12 类业务权限（key + 中文标签）；顺序与后端 BUSINESS_FEATURES 一致。 */
export const BUSINESS_FEATURES = [
  { key: "video", label: "视频生成" },
  { key: "oral", label: "数字人口播" },
  { key: "character", label: "人物形象" },
  { key: "first_frame", label: "首帧图片" },
  { key: "analysis", label: "视频拆解" },
  { key: "rewrite", label: "文案改写" },
  { key: "asr", label: "语音识别" },
  { key: "link_resolution", label: "链接解析" },
  { key: "prompt_optimize", label: "提示词优化" },
  { key: "avatar_clone", label: "形象克隆" },
  { key: "voice_clone", label: "声音克隆" },
  { key: "viral_data", label: "爆款数据" },
] as const;

/** 业务权限键（与服务端 BUSINESS_KEYS 同集合）。 */
export type BusinessFeatureKey = (typeof BUSINESS_FEATURES)[number]["key"];

/** Modal 的受控草稿：全部显式，避免「半勾选」歧义。 */
export interface PermissionDraft {
  businesses: BusinessFeatureKey[];
  allowApiKeys: boolean;
  allowPublishAccounts: boolean;
}

/** 服务端权限 → 草稿；null（全允许）展开为全部勾选 + 两个开关打开。 */
export function permissionDraft(
  permissions: CustomerSubAccountPermissions | null,
): PermissionDraft {
  if (permissions === null) {
    return {
      businesses: BUSINESS_FEATURES.map((feature) => feature.key),
      allowApiKeys: true,
      allowPublishAccounts: true,
    };
  }
  return {
    businesses: BUSINESS_FEATURES.filter((feature) =>
      permissions.businesses.includes(feature.key),
    ).map((feature) => feature.key),
    allowApiKeys: permissions.allow_api_keys,
    allowPublishAccounts: permissions.allow_publish_accounts,
  };
}

/** 草稿 → PUT 请求体：业务键按声明顺序规范化（与服务端 normalize 同口径）。 */
export function permissionsPayload(
  draft: PermissionDraft,
): CustomerSubAccountPermissions {
  return {
    businesses: BUSINESS_FEATURES.filter((feature) =>
      draft.businesses.includes(feature.key),
    ).map((feature) => feature.key),
    allow_api_keys: draft.allowApiKeys,
    allow_publish_accounts: draft.allowPublishAccounts,
  };
}

/** 切换一个业务键（保持声明顺序的不可变更新）。 */
export function toggleBusiness(
  draft: PermissionDraft,
  key: BusinessFeatureKey,
): PermissionDraft {
  const has = draft.businesses.includes(key);
  return {
    ...draft,
    businesses: has
      ? draft.businesses.filter((existing) => existing !== key)
      : BUSINESS_FEATURES.filter(
          (feature) =>
            feature.key === key || draft.businesses.includes(feature.key),
        ).map((feature) => feature.key),
  };
}

/** 是否全开（12 业务 + 两开关）：全开保存 = 删行，「全开行」不存在。 */
export function isAllGranted(draft: PermissionDraft): boolean {
  return (
    draft.allowApiKeys &&
    draft.allowPublishAccounts &&
    draft.businesses.length === BUSINESS_FEATURES.length
  );
}

/**
 * 卡片权限摘要：`permissions === null`（全允许）返回空串——默认态不需要
 * 徽章噪音；受限时列出禁用项，让母账号不打开 Modal 也能看出差异。
 */
export function permissionSummary(
  permissions: CustomerSubAccountPermissions | null,
): string {
  if (permissions === null) {
    return "";
  }
  const granted = BUSINESS_FEATURES.filter((feature) =>
    permissions.businesses.includes(feature.key),
  ).length;
  const parts = [`${granted}/${BUSINESS_FEATURES.length} 类业务`];
  if (!permissions.allow_api_keys) {
    parts.push("Token 禁用");
  }
  if (!permissions.allow_publish_accounts) {
    parts.push("发布账号禁用");
  }
  return parts.join(" · ");
}

/** 受限判定：有权限行且非「全开行」（防御全开 row 的展示口径）。 */
export function isPermissionRestricted(
  permissions: CustomerSubAccountPermissions | null,
): boolean {
  if (permissions === null) {
    return false;
  }
  return !(
    permissions.allow_api_keys &&
    permissions.allow_publish_accounts &&
    BUSINESS_FEATURES.every((feature) =>
      permissions.businesses.includes(feature.key),
    )
  );
}
