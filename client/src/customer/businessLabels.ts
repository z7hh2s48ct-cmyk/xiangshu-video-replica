/**
 * 业务键 → 客户可见中文名（消费记录筛选、消费构成环形图共用一份）。
 *
 * **中文名只有一份来源**：`permissionViz.BUSINESS_FEATURES`，它与后端
 * `sub_account_permissions.BUSINESS_FEATURES` 同源对齐，子账号权限矩阵用的就是它。
 * 此前这里自己抄了一份文案，其中 5 个键与权限矩阵不一致（`asr` 写「语音转写」而权限
 * 矩阵写「语音识别」、`analysis` 写「视频分析」而权限矩阵写「视频拆解」……），于是
 * 同一个功能在客户界面里有两个名字，客户以为是两种服务。现在从那份导出，
 * 不再允许两边漂移。
 *
 * `recharge` 与 `other` 是消费记录独有的两个桶（充值行没有计费科目；
 * 无 operation 的历史行由后端归到 `other`），不属于 12 类功能权限，单独补在这里。
 */
import { BUSINESS_FEATURES } from "./permissionViz";

export const BUSINESS_LABEL: Record<string, string> = {
  ...Object.fromEntries(
    BUSINESS_FEATURES.map(({ key, label }) => [key, label]),
  ),
  recharge: "充值 / 赠送",
  other: "其他",
};

export const businessLabel = (key: string): string =>
  BUSINESS_LABEL[key] ?? key;
