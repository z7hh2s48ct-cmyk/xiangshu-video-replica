import { Icon } from "../studio/ui";

/**
 * 客户个人中心统一的「重试」按钮（审计 P1 清单 #8：RetryButton 统一）。
 *
 * 此前 8 处各写各的按钮，类名横跨 `uc-error` / `secondary-button` / `settings-error`
 * 三套，尺寸与图标也不一致。**文案仍由调用方给**——「重新加载钱包」比一个光秃秃的
 * 「重试」更能说明白要重来的是哪一块，这条是上下文信息，不该被统一掉。
 */
export function RetryButton({
  label = "重新加载",
  onClick,
  disabled = false,
}: {
  label?: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className="uc-retry"
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name="refresh" />
      {label}
    </button>
  );
}
