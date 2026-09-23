import {
  type CSSProperties,
  type RefCallback,
  useCallback,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

/** 左栏宽度上限占行宽的比例。超过后右栏会被挤垮（实测 77.7% 时右栏仅剩 291px）。 */
const DEFAULT_CAP_PERCENT = 40;
/** 媒体框所在 .panel 的内边距与边框总补偿：padding 20×2 + border 1×2 = 42。 */
const DEFAULT_PAD_COMPENSATION = 42;
/** 媒体基准高度 clamp(240px, 34dvh, 360px) 的三个端点。 */
const BASE_MIN_PX = 240;
const BASE_VIEWPORT_FRACTION = 0.34;
const BASE_MAX_PX = 360;
/**
 * 列宽与媒体高度互为因果（列宽由高度推出，高度又由右栏宽度决定），单次求值有残差，
 * 故迭代。3 次实测已收敛，相邻两次差 < 0.5px 即停。
 *
 * 这是个正反馈环，收敛不是数学保证，靠两个护栏兜住：
 *  1) 媒体框高度由 aspect-ratio 从列宽反推时（creation.css 的 max-height 上限生效，
 *     或 flex 没有把它拉高），测量高度 = (列宽 - 42) / 比例，代回 target 恒等于当前
 *     列宽，第 1 轮即 < 0.5px 退出；
 *  2) 媒体框被 flex 拉高时高度与列宽脱钩，target 会一路推动列宽增长（列宽变大 →
 *     右栏变窄 → 右栏内容变高 → 媒体更高），此时由 40% 上限截断，最迟第 3 轮停。
 * 也就是说护栏 1 失效时护栏 2 独占收敛，代价是右栏被挤到 40% 上限；1440×900 实测
 * （review 入口）该场景下 2.5s 空闲 + 一次 resize 信号，行内联样式改写 0 次，无可见抖动。
 */
const MAX_FIT_ITERATIONS = 3;
const FIT_EPSILON_PX = 0.5;
/** hook 与 creation.css 成对交付，媒体框类名即测量锚点。 */
const MEDIA_FRAME_SELECTOR = ".media-frame";

/** CSS 自定义属性不在 React 的闭合 CSSProperties 里，需显式补上。 */
export type MediaRowStyle = CSSProperties & Record<`--${string}`, string>;

export interface UseMediaRowFitOptions {
  /** 素材宽高比（宽 / 高），如 9:16 传 9 / 16。 */
  ratio: number;
  /** 左栏宽度上限，占行宽百分比。 */
  capPercent?: number;
  /** 面板内边距 + 边框补偿（px）。 */
  padCompensation?: number;
  /** 媒体基准高度下限（px）。省略时按视口复刻 clamp(240px, 34dvh, 360px)。 */
  minHeight?: number;
}

export interface UseMediaRowFitResult {
  rowRef: RefCallback<HTMLElement>;
  rowStyle: MediaRowStyle;
}

/**
 * 复刻 CSS clamp(240px, 34dvh, 360px)。
 *
 * 不能用 parseFloat(getComputedStyle(el).getPropertyValue("--base-h"))：自定义属性取到
 * 的是原始 token 串 "clamp(240px,34dvh,360px)"，parseFloat 得 NaN，会静默退化成兜底值。
 */
function resolveBaseHeight(minHeight: number | undefined): number {
  if (typeof minHeight === "number" && Number.isFinite(minHeight)) {
    return minHeight;
  }
  // 非浏览器环境（jsdom、纯函数调用）拿不到视口，退回下限。
  const viewport = typeof window === "undefined" ? 0 : window.innerHeight;
  return Math.min(
    Math.max(BASE_MIN_PX, viewport * BASE_VIEWPORT_FRACTION),
    BASE_MAX_PX,
  );
}

function measureRowWidth(row: HTMLElement): number {
  return row.clientWidth > 0
    ? row.clientWidth
    : row.getBoundingClientRect().width;
}

/** 媒体框被 flex 拉伸后的实际高度，即本行可用的媒体高度。 */
function measureMediaHeight(row: HTMLElement): number {
  const frame = row.querySelector<HTMLElement>(MEDIA_FRAME_SELECTOR);
  return frame ? frame.getBoundingClientRect().height : 0;
}

interface ResolvedFitOptions {
  ratio: number;
  capPercent: number;
  padCompensation: number;
  minHeight: number | undefined;
}

function fitColumnWidth(row: HTMLElement, options: ResolvedFitOptions): number {
  const { ratio, padCompensation } = options;
  const base = resolveBaseHeight(options.minHeight);
  const rowWidth = measureRowWidth(row);

  // 行尚未布局（首次绘制前、jsdom）时没有可用的上限约束，先按基准比例给值。
  if (!(rowWidth > 0)) {
    return base * ratio + padCompensation;
  }

  const cap = rowWidth * (options.capPercent / 100);
  let columnWidth = Math.min(base * ratio + padCompensation, cap);

  for (let i = 0; i < MAX_FIT_ITERATIONS; i++) {
    const mediaHeight = Math.max(measureMediaHeight(row), base);
    const target = Math.min(mediaHeight * ratio + padCompensation, cap);
    if (Math.abs(target - columnWidth) < FIT_EPSILON_PX) {
      break;
    }
    columnWidth = target;
    // 把中间值写回 DOM，下一轮测量才会反映新列宽；否则循环读的是同一份旧布局。
    row.style.setProperty("--media-col", `${columnWidth}px`);
  }

  return columnWidth;
}

/**
 * 媒体行左栏宽度由素材宽高比驱动，而非网格均分。
 *
 * height:100% + aspect-ratio 是循环依赖——媒体宽度由行高推出，行高由右栏内容推出，
 * 右栏宽度又依赖左栏宽度，CSS Grid 不做迭代，实测四种纯 CSS 写法全部失败。
 * 故用 useLayoutEffect（绘制前完成测量，避免可见闪动）+ ResizeObserver（容器变化后重算）。
 */
export function useMediaRowFit({
  ratio,
  capPercent = DEFAULT_CAP_PERCENT,
  padCompensation = DEFAULT_PAD_COMPENSATION,
  minHeight,
}: UseMediaRowFitOptions): UseMediaRowFitResult {
  const [row, setRow] = useState<HTMLElement | null>(null);
  const [columnWidth, setColumnWidth] = useState<number | null>(null);

  const rowRef = useCallback<RefCallback<HTMLElement>>((node) => {
    setRow(node ?? null);
  }, []);

  useLayoutEffect(() => {
    if (!row) {
      return;
    }
    const recompute = () => {
      setColumnWidth(
        fitColumnWidth(row, { ratio, capPercent, padCompensation, minHeight }),
      );
    };
    recompute();

    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(recompute);
    observer?.observe(row);
    // 基准高度取自视口高度（34dvh），行宽不变而视口变高时 observer 不触发。
    window.addEventListener("resize", recompute);

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", recompute);
    };
  }, [row, ratio, capPercent, padCompensation, minHeight]);

  const rowStyle = useMemo<MediaRowStyle>(() => {
    const style: MediaRowStyle = {
      "--row-ratio": `${ratio}`,
      // 让媒体框的 min-height 与 JS 的基准高度同源，否则 CSS 的 320px 兜底会与基准打架。
      "--media-min-h": `${resolveBaseHeight(minHeight)}px`,
    };
    if (columnWidth !== null) {
      style["--media-col"] = `${columnWidth}px`;
    }
    return style;
  }, [columnWidth, ratio, minHeight]);

  return { rowRef, rowStyle };
}
