import { useEffect, useRef } from "react";

/**
 * 帮助中心（审计方案 6 / P2 清单 #21）。
 *
 * 只回答**客户真会问、且界面术语容易看不懂**的几条——不是把文档搬进来。条目文案刻意
 * 与界面用词对齐（「待结算」「早期版本消费」「第 N 次更新」都是界面上真实出现的词），
 * 这样客户看到不认识的词，来这里能对上号。
 */
export const HELP_ENTRIES: ReadonlyArray<{ question: string; answer: string }> =
  [
    {
      question: "Token 是什么？丢了怎么办？",
      answer:
        "Token 是给你的程序调用平台接口用的凭据。完整值只在创建或更新时显示一次，平台不再保存明文；如果丢失，用「更新」换一枚新的（旧的那枚立刻失效，历史消费仍然保留在旧版本上）。",
    },
    {
      question: "积分是怎么扣的？为什么有「待结算」？",
      answer:
        "任务开始时先按预估用量「预扣」（待结算），任务结束后按实际用量「结算」：多扣的退回，少扣的补上。所以你会看到同一笔任务先预扣、再结算或退回，最终只按结算金额计入累计消费。",
    },
    {
      question: "「早期版本消费」是什么意思？",
      answer:
        "这是平台早期还没有记录消费来源时留下的流水。这些消费确实发生过、也计入账号总额，但无法再对应到某一枚 Token。",
    },
    {
      question: "「第 N 次更新」是什么意思？",
      answer:
        "同一枚 Token 更换过几次密钥。更新只影响密钥本身：分组、历史消费和账务记录都跟着保留，所以老流水仍然能追溯到这一组。",
    },
    {
      question: "子账号花的积分算在谁头上？",
      answer:
        "算在母账号的钱包上——子账号没有独立钱包。消费记录里可以按子账号筛选，也可以打开「按子账号汇总」看每个人花了多少。",
    },
    {
      question: "怀疑账号被盗用，第一步做什么？",
      answer:
        "去「账号设置 → 账号安全」：先改密码（会让所有设备下线），再撤销全部 Token，必要时用「退出所有设备」立刻清场。最近登录记录也在那里。",
    },
  ];

export function HelpDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element || element.open) {
      return;
    }
    if (typeof element.showModal === "function") {
      element.showModal();
    } else {
      element.setAttribute("open", "");
    }
  }, []);
  return (
    <dialog
      aria-label="帮助中心"
      className="uc-dialog uc-help"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      ref={dialog}
    >
      <h2>帮助中心</h2>
      <dl>
        {HELP_ENTRIES.map((entry) => (
          <div key={entry.question}>
            <dt>{entry.question}</dt>
            <dd>{entry.answer}</dd>
          </div>
        ))}
      </dl>
      <div className="uc-dialog-actions">
        <button className="uc-primary" type="button" onClick={onClose}>
          知道了
        </button>
      </div>
    </dialog>
  );
}

/**
 * 术语上的就地说明（inline tooltip）。
 *
 * 用原生 `title`：键盘/触摸/读屏都能拿到文本，且不需要引入 tooltip 库或自己管
 * 定位与焦点。虚线下划线是「这里可以悬停看解释」的通行视觉提示。
 */
export function TermHint({ term, hint }: { term: string; hint: string }) {
  return (
    <span className="uc-term" title={hint}>
      {term}
    </span>
  );
}
