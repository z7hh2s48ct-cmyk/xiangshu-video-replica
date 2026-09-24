/** 创作草稿的本地残留清理（LEFTOVER-ON-OPEN）。
 *
 * 主因是云端那一行 studio_draft，这里管的是它之外的本地残留：未保存的口播稿
 * 与提示词按 账号+项目 存在 `generation.localDraft/*`（写入方是
 * useGenerationDrafts）。桌面端 localStorage 落在应用数据目录、跨版本存活，
 * 而该 hook 目前没有任何组件挂载——所以这些键只会来自旧版本升级残留。清理
 * 仍有必要：它一旦回填就是用户口中的「上次的内容」，且没有别的入口能清掉。
 *
 * 清理边界是刻意收窄的，两类记录不在范围内：
 * - `generation.idempotency/*`：批次幂等键，防重复建批（重复扣费）的安全网。
 *   清掉它，重试会生成新键并可能真的建出第二个批次；它在建批成功后自行清除。
 * - `wallet.pendingOrderNo:*`：待支付充值单的恢复凭据，与创作内容无关，
 *   清掉用户就找不回那笔充值。
 */
import { clearPageCache } from "./pageCache";

const LOCAL_DRAFT_SCRIPT_PREFIX = "generation.localDraft/script/";
const LOCAL_DRAFT_PROMPT_PREFIX = "generation.localDraft/prompt/";
const BATCH_STORAGE_KEY_PREFIX = "generation.batchId:";

/** 结尾的 `/` 不能省：账号 a/b 与 a/bc 编码后是 a%2Fb 与 a%2Fbc，
 * 少了分隔符就会把后者一起清掉。 */
function accountScope(accountId: string): string {
  return `${encodeURIComponent(accountId)}/`;
}

function removeKeys(matches: (key: string) => boolean): void {
  try {
    const storage = window.localStorage;
    // 先收集再删：边遍历边删会让后续下标整体前移、漏掉一半的键。
    const doomed: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null && matches(key)) doomed.push(key);
    }
    for (const key of doomed) storage.removeItem(key);
  } catch {
    // 存储被禁用或配额异常时静默降级：清理失败只是多留一份旧草稿，
    // 不能把「放弃上次内容」这条出路整个打断。
  }
}

/** 放弃云端草稿时同步清掉本账号未保存的口播稿与提示词。 */
export function clearCreationDraftResidue(accountId: string): void {
  const scope = accountScope(accountId);
  const prefixes = [
    `${LOCAL_DRAFT_SCRIPT_PREFIX}${scope}`,
    `${LOCAL_DRAFT_PROMPT_PREFIX}${scope}`,
  ];
  removeKeys((key) => prefixes.some((prefix) => key.startsWith(prefix)));
}

/** 登出/切账号时的清理：创作草稿残留 + 任务中心的上次批次定位 + 页间缓存。
 * 页间缓存是模块级 Map，同一进程内换账号不清就会串数据。 */
export function clearAccountResidue(accountId: string): void {
  clearCreationDraftResidue(accountId);
  const batchKey = `${BATCH_STORAGE_KEY_PREFIX}${encodeURIComponent(accountId)}`;
  removeKeys((key) => key === batchKey);
  clearPageCache();
}
