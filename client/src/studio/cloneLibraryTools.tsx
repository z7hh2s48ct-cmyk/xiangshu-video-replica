import { useEffect, useRef, useState } from "react";
import { customerVisibleErrorMessage } from "../api";
import { Button, Icon } from "./ui";

/** 与服务端名称上限一致。 */
export const CLONE_TITLE_MAX_CHARS = 60;
const SEARCH_DEBOUNCE_MS = 300;

/**
 * 口播分身 / 声音的名称搜索。
 *
 * 正式模式由服务端按名称检索（只返回当前账号创建的记录），结果只用作「哪些 id
 * 命中」：卡片仍由人物数据渲染，轮询状态与播放链接不会因搜索丢失。审核演示模式
 * 没有真实数据可查，改为本地按名称过滤。
 */
export function useCloneNameSearch({
  search,
  review,
  resetKey,
}: {
  search: (query: string) => Promise<ReadonlyArray<{ id: string }>>;
  review: boolean;
  resetKey: string;
}) {
  const [query, setQuery] = useState("");
  const [matchedIds, setMatchedIds] = useState<ReadonlySet<string>>();
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string>();
  const [nonce, setNonce] = useState(0);
  const requestRef = useRef(0);
  const searchRef = useRef(search);
  searchRef.current = search;
  const trimmed = query.trim();

  useEffect(() => {
    void resetKey;
    setQuery("");
  }, [resetKey]);

  useEffect(() => {
    void nonce;
    const request = ++requestRef.current;
    setError(undefined);
    setMatchedIds(undefined);
    if (!trimmed || review) {
      setSearching(false);
      return;
    }
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchRef
        .current(trimmed)
        .then((rows) => {
          if (request === requestRef.current)
            setMatchedIds(new Set(rows.map((row) => row.id)));
        })
        .catch((cause: unknown) => {
          if (request === requestRef.current)
            setError(customerVisibleErrorMessage(cause, "按名称搜索失败"));
        })
        .finally(() => {
          if (request === requestRef.current) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [trimmed, review, nonce]);

  const matches = (item: { id: string; name: string }) => {
    if (!trimmed) return true;
    if (review) return item.name.toLowerCase().includes(trimmed.toLowerCase());
    // 服务端结果返回前先不隐藏任何卡片，避免输入过程中列表闪空。
    return !matchedIds || matchedIds.has(item.id);
  };

  return {
    query,
    setQuery,
    trimmedQuery: trimmed,
    active: Boolean(trimmed),
    searching,
    error,
    matches,
    clear: () => setQuery(""),
    /** 改名后重新检索，让结果与新名称一致。 */
    rerun: () => setNonce((value) => value + 1),
  };
}

export function CloneNameSearch({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="clone-name-search">
      <Icon name="search" size={16} />
      <input
        aria-label={label}
        maxLength={CLONE_TITLE_MAX_CHARS}
        placeholder={label}
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {value ? (
        <button
          type="button"
          className="clone-name-search__clear"
          aria-label="清除搜索词"
          onClick={() => onChange("")}
        >
          <Icon name="close" size={14} />
        </button>
      ) : null}
    </div>
  );
}

/**
 * 搜索结果提示：有结果时给出数量与「清空搜索」，没结果时给出明确的出路，
 * 避免用户以为数据丢了。未搜索时不渲染。
 */
export function CloneSearchStatus({
  search,
  count,
  noun,
}: {
  search: ReturnType<typeof useCloneNameSearch>;
  count: number;
  noun: string;
}) {
  if (!search.active) return null;
  if (search.error)
    return (
      <p className="oral-error" role="alert">
        {search.error}
      </p>
    );
  if (search.searching)
    return (
      <p className="clone-search-status" aria-live="polite">
        正在搜索「{search.trimmedQuery}」…
      </p>
    );
  if (count > 0)
    return (
      <p className="clone-search-status" aria-live="polite">
        「{search.trimmedQuery}」找到 {count} 个{noun} ·{" "}
        <button type="button" className="clone-link" onClick={search.clear}>
          清空搜索
        </button>
      </p>
    );
  return (
    <div className="clone-search-empty" aria-live="polite">
      <Icon name="search" size={32} />
      <h3>
        没有找到名称包含「{search.trimmedQuery}」的{noun}
      </h3>
      <p>换个关键词试试，或者清空搜索查看全部{noun}。</p>
      <Button variant="outline" onClick={search.clear}>
        清空搜索
      </Button>
    </div>
  );
}

/**
 * 卡片上就地改名：点名称旁的笔形图标即可编辑，回车保存、Esc 取消，
 * 不弹窗、不离开当前卡片。名称只改本系统内的显示与搜索用名称。
 */
export function InlineCloneName({
  name,
  kindLabel,
  editing,
  busy,
  disabled,
  error,
  onStart,
  onCancel,
  onSave,
}: {
  name: string;
  kindLabel: string;
  editing: boolean;
  busy: boolean;
  disabled: boolean;
  error?: string;
  onStart: () => void;
  onCancel: () => void;
  onSave: (title: string) => void;
}) {
  const [value, setValue] = useState(name);
  useEffect(() => {
    if (editing) setValue(name);
  }, [editing, name]);
  if (!editing)
    return (
      <div className="clone-inline-name">
        <h3>{name}</h3>
        <button
          type="button"
          className="clone-icon-button"
          aria-label={`修改${name}的名称`}
          title="修改名称"
          disabled={disabled}
          onClick={onStart}
        >
          <Icon name="pen" size={15} />
        </button>
      </div>
    );
  const trimmed = value.trim();
  const canSave = !busy && Boolean(trimmed) && trimmed !== name;
  return (
    <div className="clone-inline-name is-editing">
      <div className="clone-inline-name__row">
        <input
          aria-label={`${kindLabel}名称`}
          // biome-ignore lint/a11y/noAutofocus: 用户刚点了「修改名称」，焦点应落在输入框。
          autoFocus
          disabled={busy}
          maxLength={CLONE_TITLE_MAX_CHARS}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && canSave) onSave(trimmed);
            if (event.key === "Escape") onCancel();
          }}
        />
        <Button
          variant="primary"
          disabled={!canSave}
          onClick={() => onSave(trimmed)}
        >
          {busy ? "保存中…" : "保存"}
        </Button>
        <Button variant="quiet" disabled={busy} onClick={onCancel}>
          取消
        </Button>
      </div>
      {error ? (
        <p className="oral-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** 删除改为图标按钮：低频且危险的操作不和主操作抢视线，点击后仍有确认弹窗。 */
export function DeleteIconButton({
  disabled,
  onClick,
}: {
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="clone-icon-button clone-icon-button--danger"
      aria-label="删除"
      title="删除"
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name="trash" size={16} />
    </button>
  );
}
