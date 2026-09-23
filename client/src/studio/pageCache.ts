/** 页间 SWR 缓存（MATERIAL-PERF-C-20260917，P1-1）。
 *
 * 页面组件的数据请求在 mount effect 里执行且 state 是组件局部的——每次切回
 * 页面都要白屏等一轮网络。本模块提供极简的模块级缓存：切入页面先同步回放
 * 上次数据（readPageCache），再后台刷新并回写（writePageCache）。账号维度
 * 隔离由调用方拼进 key；clearPageCache 用于登出/会话切换。
 */

const cache = new Map<string, { at: number; value: unknown }>();
const PAGE_CACHE_LIMIT = 64;

export function readPageCache<T>(key: string): T | undefined {
  return cache.get(key)?.value as T | undefined;
}

export function writePageCache<T>(key: string, value: T): void {
  if (cache.size >= PAGE_CACHE_LIMIT && !cache.has(key)) {
    // 粗粒度淘汰：删最早的键（Map 迭代序即插入序）。
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(key, { at: Date.now(), value });
}

export function clearPageCache(prefix?: string): void {
  if (prefix === undefined) {
    cache.clear();
    return;
  }
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
}
