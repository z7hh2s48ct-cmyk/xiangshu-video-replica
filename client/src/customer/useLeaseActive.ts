import { useEffect, useState } from "react";

/** 租约是否还在有效期内——设备「本机在线」的唯一判据。 */
export function leaseIsActive(expiresAt: string | null): boolean {
  if (!expiresAt) {
    return false;
  }
  const expiry = Date.parse(expiresAt);
  return Number.isFinite(expiry) && expiry > Date.now();
}

/**
 * 跟随租约到期自动翻面的在线态。
 *
 * 这份判据曾被已下线的旧个人中心（`CustomerProfilePanel`）与新中心的「设备管理」
 * 页签共用，故提到本模块；旧面板删除后新中心是唯一使用者，独立成模块仍然值得，
 * 否则「设备是否在线」会在多处各留一份实现并迟早分叉。到期时刻本身由服务端给，
 * 这里只负责在那一刻把状态翻到 false，不轮询。
 */
export function useLeaseActive(expiresAt: string | null): boolean {
  const [isActive, setIsActive] = useState(() => leaseIsActive(expiresAt));

  useEffect(() => {
    const expiry = expiresAt ? Date.parse(expiresAt) : Number.NaN;
    const delayMs = expiry - Date.now();
    if (!Number.isFinite(expiry) || delayMs <= 0) {
      setIsActive(false);
      return;
    }
    setIsActive(true);
    const timer = window.setTimeout(() => setIsActive(false), delayMs + 1);
    return () => window.clearTimeout(timer);
  }, [expiresAt]);

  return isActive;
}
