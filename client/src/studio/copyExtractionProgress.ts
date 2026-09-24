import { useSyncExternalStore } from "react";

/**
 * 文案提取的跨页面进度状态。
 *
 * 提取从提交到出稿要经历几十秒到几分钟，且用户会被带到文案工坊等待，这里用一条
 * 常驻进度横幅替代 3 秒即逝的 toast，避免「点了按钮却看不到任何动静」的焦虑。
 * 转写任务暂无服务端真实进度字段，横幅按已耗时估算进度并封顶等待真实终态。
 */
export type CopyExtractionProgress = {
  active: boolean;
  startedAt: number;
  detail: string;
};

const IDLE: CopyExtractionProgress = {
  active: false,
  startedAt: 0,
  detail: "",
};

let progress: CopyExtractionProgress = IDLE;
const listeners = new Set<() => void>();

const emit = () => {
  listeners.forEach((listener) => {
    listener();
  });
};

const apply = (next: CopyExtractionProgress) => {
  progress = next;
  emit();
};

/** 开启（或刷新进行中的）一次提取；已进行中时不重置起点，耗时与进度条保持连续。 */
export const beginCopyExtractionProgress = (detail: string) => {
  const startedAt =
    progress.active && Date.now() - progress.startedAt >= 0
      ? progress.startedAt
      : Date.now();
  apply({ active: true, startedAt, detail });
};

/** 仅在已有提取进行中时更新阶段说明（例如后台轮询拿到排队/转写状态）。 */
export const updateCopyExtractionProgress = (detail: string) => {
  if (!progress.active) return;
  apply({ ...progress, detail });
};

export const endCopyExtractionProgress = () => {
  if (!progress.active) return;
  apply(IDLE);
};

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

const getSnapshot = () => progress;

export const useCopyExtractionProgress = () =>
  useSyncExternalStore(subscribe, getSnapshot);
