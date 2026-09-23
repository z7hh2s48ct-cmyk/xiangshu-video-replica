type Intent<T> = {
  key: string;
  value: T;
  isCurrent: () => boolean;
  completion: Promise<void>;
  resolve: () => void;
  reject: (cause: unknown) => void;
};

export function createCloudDraftQueue<T>(write: (value: T) => Promise<void>) {
  let active: Intent<T> | undefined;
  let pending: Intent<T> | undefined;
  let lastSuccessKey: string | undefined;

  const start = (intent: Intent<T>) => {
    if (!intent.isCurrent()) {
      intent.resolve();
      return;
    }
    active = intent;
    void write(intent.value).then(
      () => {
        lastSuccessKey = intent.key;
        intent.resolve();
        if (active === intent) active = undefined;
        const next = pending;
        pending = undefined;
        if (next) start(next);
      },
      (cause) => {
        intent.reject(cause);
        if (active === intent) active = undefined;
        const next = pending;
        pending = undefined;
        if (next) start(next);
      },
    );
  };

  const persist = (key: string, value: T, isCurrent: () => boolean) => {
    if (!active && !pending && lastSuccessKey === key) return Promise.resolve();
    if (active?.key === key) return active.completion;

    let resolveIntent: (() => void) | undefined;
    let rejectIntent: ((cause: unknown) => void) | undefined;
    const completion = new Promise<void>((resolve, reject) => {
      resolveIntent = resolve;
      rejectIntent = reject;
    });
    const intent: Intent<T> = {
      key,
      value,
      isCurrent,
      completion,
      resolve: () => resolveIntent?.(),
      reject: (cause) => rejectIntent?.(cause),
    };

    if (active) {
      // 尚未开始的写入没有服务端副作用，只保留最新完整草稿。
      const superseded = pending;
      if (superseded) {
        void completion.then(superseded.resolve, superseded.reject);
      }
      pending = intent;
    } else {
      start(intent);
    }
    return completion;
  };

  return { persist };
}
