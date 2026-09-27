import {
  type ButtonHTMLAttributes,
  type CSSProperties,
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { VideoPreview } from "../VideoPreview";
import type { StudioAsset } from "./types";

export function Icon({ name, size = 22 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    home: "M3 10 12 3l9 7v10H3V10Zm6 10v-7h6v7",
    tasks: "M6 4h12l3 3v13H3V7l3-3Zm2 0v4h8V4M8 14l3 3 5-6",
    fire: "M13 2c2 5-2 6 2 9 2-1 3-3 3-3 6 8 1 14-6 14C3 22 0 14 7 7c0 4 3 4 3 1 0-2 2-4 3-6Z",
    pen: "m4 4 8 2 8 12-3 3L5 13 4 4Zm0 0 7 7m0 0a2 2 0 1 0 3 3 2 2 0 0 0-3-3M4 20h9",
    video: "M3 5h18v14H3V5Zm7 4 6 3-6 3V9",
    person: "M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0M4 21v-3c0-7 16-7 16 0v3H4",
    folder: "M3 6V3h6l3 3h9v14H3V6Zm0 3h18",
    upload: "M12 16V2m-5 5 5-5 5 5M3 15v6h18v-6",
    chart: "M3 21V11h4v10m3 0V3h4v18m3 0V7h4v14M1 21h22",
    search: "M16 16l6 6M2 10a8 8 0 1 0 16 0 8 8 0 0 0-16 0",
    bell: "M5 10c0-9 14-9 14 0v6l2 3H3l2-3v-6M9 22h6M12 1v2",
    plus: "M12 3v18M3 12h18",
    arrow: "M5 12h14m-6-6 6 6-6 6",
    back: "m14 4-8 8 8 8",
    chevron: "m9 4 8 8-8 8",
    down: "m5 9 7 7 7-7",
    check: "m5 12 5 5L20 7",
    close: "m5 5 14 14M19 5 5 19",
    image: "M3 3h18v18H3V3Zm0 14 6-7 5 6 3-3 4 5M15 7h1",
    play: "m7 3 14 9-14 9V3",
    pause: "M7 3v18M17 3v18",
    audio:
      "M10 18V5l10-3v13M3 18a3 3 0 1 0 6 0 3 3 0 0 0-6 0M14 15a3 3 0 1 0 6 0 3 3 0 0 0-6 0",
    link: "m9 15 6-6M8 17l-2 2c-5 4-10-4-5-7l5-5c3-3 6-1 7 1m-2 8c3 2 5 1 7-1l4-4c4-5-3-10-7-6l-2 2",
    info: "M12 10v7M12 6v1M2 12a10 10 0 1 0 20 0 10 10 0 0 0-20 0",
    warning: "M12 2 1 21h22L12 2Zm0 6v6m0 3v1",
    star: "m12 2 3 6 7 1-5 5 1 8-6-4-6 4 1-8-5-5 7-1 3-6",
    heart: "M12 21C-9 8 4-4 12 6c8-10 21 2 0 15Z",
    comment:
      "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z",
    share:
      "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12 7a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM8.6 13.5l6.8 4M15.4 6.5l-6.8 4",
    download: "M12 2v14m-5-5 5 5 5-5M3 16v6h18v-6",
    more: "M4 12h1m6 0h1m6 0h1",
    refresh: "M21 8V2l-3 3A9 9 0 1 0 3 18m18-10h-6",
    clock: "M12 6v7l4 2M2 12a10 10 0 1 0 20 0 10 10 0 0 0-20 0",
    copy: "M8 8h13v13H8V8ZM3 16V3h13",
    sparkles: "m12 1 3 8 8 3-8 3-3 8-3-8-8-3 8-3 3-8",
    shield: "M12 2 3 6v7c0 5 9 9 9 9s9-4 9-9V6l-9-4Zm-5 10 3 3 7-7",
    settings:
      "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0-6 2 1 .5 2.2 2 .8 2-1 2 2-1 2 .8 2 2.2.5 1 2-1 2-2.2.5-.8 2 1 2-2-1-2 .8-2.2-1-1-2 1-2.2-.8-2-2-2 1-2-.8-2-2.2-.5-1Z",
    save: "M3 3h15l3 3v15H3V3Zm4 0v6h10V3M7 21v-7h10v7",
  };
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="studio-icon"
    >
      <path d={paths[name] ?? paths.image} />
    </svg>
  );
}

export function Button({
  variant = "outline",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "outline" | "quiet";
}) {
  return (
    <button
      type="button"
      className={`studio-button studio-button--${variant} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
export function Panel({
  children,
  className = "",
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <section className={`studio-panel ${className}`} style={style}>
      {children}
    </section>
  );
}
export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="studio-field">
      <label htmlFor={id}>{label}</label>
      {isValidElement(children)
        ? cloneElement(children as ReactElement<{ id?: string }>, { id })
        : children}
    </div>
  );
}
export function Hint({ children }: { children: ReactNode }) {
  return (
    <div className="studio-hint">
      <Icon name="info" size={18} />
      <span>{children}</span>
    </div>
  );
}
export function Empty({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="studio-empty">
      <Icon name="image" size={46} />
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}

export function Tabs<T extends string>({
  items,
  value,
  onChange,
}: {
  items: { id: T; label: ReactNode }[];
  value: T;
  onChange: (id: T) => void;
}) {
  const id = useId();
  return (
    <div className="studio-tabs" role="tablist" aria-label="页面选项">
      {items.map((item) => (
        <button
          key={item.id}
          id={`${id}-${item.id}`}
          type="button"
          role="tab"
          aria-selected={value === item.id}
          className={value === item.id ? "is-active" : ""}
          onClick={() => onChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** 任务中心类型筛选：单行工具栏右侧的下拉选择器。首项为默认项
 * （"全部类型"），选中非默认项时触发按钮高亮；菜单项计数由调用方
 * 按另一维度（状态）联动计算。关闭交互与 RunningRowMenu 一致。 */
export function FilterSelect<T extends string>({
  label,
  items,
  value,
  onChange,
}: {
  label: string;
  items: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const selected = items.find((item) => item.id === value) ?? items[0];
  return (
    <div className="studio-filter-select" ref={rootRef}>
      <button
        type="button"
        className={`studio-filter-select-trigger${
          value === items[0].id ? "" : " is-active"
        }`}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {label}：{selected?.label}
        <Icon name="down" size={16} />
      </button>
      {open && (
        <div
          className="studio-filter-select-list"
          role="listbox"
          aria-label={label}
        >
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              role="option"
              aria-selected={item.id === value}
              onClick={() => {
                setOpen(false);
                onChange(item.id);
              }}
            >
              <span>
                {item.id === value ? "✓ " : ""}
                {item.label}
              </span>{" "}
              {item.count !== undefined && (
                <small className="studio-filter-select-count">
                  {item.count}
                </small>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Waveform() {
  const bars = Array.from({ length: 64 }, (_, index) => ({
    id: `wave-${index}`,
    height: 12 + ((index * 31 + 7) % 34),
  }));
  return (
    <div className="studio-wave" aria-hidden="true">
      {bars.map((bar) => (
        <i key={bar.id} style={{ height: `${bar.height}px` }} />
      ))}
    </div>
  );
}

export function Media({
  asset,
  alt,
  className = "",
  onError,
  presentation,
  onPlay,
  aspectRatio,
  onAspectRatioChange,
  fallback,
  fitContainer = false,
  audioControls = true,
}: {
  presentation?: "video";
  aspectRatio?: string;
  onAspectRatioChange?: (ratio: number) => void;
  fallback?: ReactNode;
  fitContainer?: boolean;
  // 卡片本体是 <button>，内嵌原生播放器属于交互元素嵌套（不合法且抢点击）；
  // 卡片形态传 false，只保留波形与常显时长角标，播放收敛到详情面板。
  audioControls?: boolean;
  onPlay?: () => void;
  asset?: StudioAsset;
  alt: string;
  className?: string;
  onError?: (failedUrl?: string) => void;
}) {
  if (!asset)
    return (
      <VideoPreview
        className={`studio-media studio-media--empty ${className}`}
        frameRatio={aspectRatio ?? "9:16"}
        fitContainer={fitContainer}
        alt={alt}
        fallback={
          fallback ?? (
            <>
              <Icon name="image" size={44} />
              <span>{alt}</span>
            </>
          )
        }
      />
    );
  if (asset.kind === "audio")
    return (
      <div className={`studio-media studio-media--audio ${className}`}>
        <Waveform />
        {asset.url ? (
          audioControls ? (
            <audio
              controls
              onPlay={onPlay}
              src={asset.url}
              aria-label={alt}
              onError={() => onError?.(asset.url)}
            >
              <track kind="captions" />
            </audio>
          ) : null
        ) : (
          <span>完整口播音频 · {asset.duration || "待读取时长"}</span>
        )}
        {asset.url && (
          <span className="studio-media-duration">
            {asset.duration || "音频"}
          </span>
        )}
      </div>
    );
  if (
    asset.kind === "video" ||
    asset.kind === "image" ||
    presentation === "video"
  )
    return (
      <VideoPreview
        onPlay={onPlay}
        onAspectRatioChange={onAspectRatioChange}
        frameRatio={
          aspectRatio ?? (asset.kind === "image" ? "adaptive" : undefined)
        }
        fitContainer={fitContainer}
        className={`studio-media ${className}`}
        controls
        src={asset.kind === "video" ? asset.url : undefined}
        poster={asset.kind === "image" ? asset.url : asset.poster}
        aria-label={alt}
        alt={alt}
        onError={() => onError?.(asset.url)}
        onPosterError={() =>
          onError?.(asset.kind === "image" ? asset.url : asset.poster)
        }
        overlay={
          // 时长角标常显：同款多视频在瓦片里主要靠时长区分，有封面的瓦片同样
          // 需要（MATERIAL-UX-02；此前时长只在无封面兜底时出现）。
          // 「视频预览图」兜底文案仍只在无封面时出现——封面已可见时不再叠加
          // 占位标记（ui.test.tsx「封面已就位时不叠加占位标记」）。发布草稿卡
          // 与封面选项场景由 CSS 兜底隐藏。
          asset.kind === "video" && (asset.duration || !asset.poster) ? (
            <span className="studio-media-duration">
              <Icon name="play" size={14} />
              {asset.duration || "视频预览图"}
            </span>
          ) : undefined
        }
      />
    );
  return null;
}

/** Localized short time for task timestamps: 今天/昨天 HH:mm, then MM-DD
 * HH:mm within the year, full date across years. Values that cannot be
 * parsed (including already-friendly sample labels) pass through untouched
 * so review fixtures keep their curated wording. */
export function formatTaskTime(value: string, now: Date = new Date()): string {
  if (!value) return value;
  const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(value)
    ? value.replace(" ", "T")
    : value;
  const normalized = timestamp
    .replace(/(\.\d{3})\d+(?=Z|[+-]\d{2}(?::?\d{2})?$)/, "$1")
    .replace(/([+-]\d{2})$/, "$1:00");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const dayKey = (d: Date) =>
    `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  if (dayKey(date) === dayKey(now)) return `今天 ${time}`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (dayKey(date) === dayKey(yesterday)) return `昨天 ${time}`;
  if (date.getFullYear() === now.getFullYear())
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

export function StudioDialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (typeof dialog?.showModal === "function") dialog.showModal();
    else dialog?.setAttribute("open", "");
    return () => {
      if (typeof dialog?.close === "function") dialog.close();
    };
  }, []);
  return (
    <dialog
      ref={dialogRef}
      className="studio-dialog"
      aria-label={title}
      onCancel={onClose}
    >
      <header>
        <h2>{title}</h2>
        <button type="button" aria-label="关闭" onClick={onClose}>
          <Icon name="close" />
        </button>
      </header>
      {children}
    </dialog>
  );
}
