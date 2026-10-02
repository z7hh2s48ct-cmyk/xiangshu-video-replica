import { type FormEvent, useEffect, useState } from "react";
import {
  type CustomerAnnotation,
  type CustomerOwnerCandidate,
  fetchCustomerAnnotation,
  listCustomerOwnerCandidates,
  updateCustomerAnnotation,
} from "../api.admin";
import { CustomerTagPills } from "./CustomerIdentity";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";
import "./admin-customer-detail.css";

function splitTagInput(raw: string): string[] {
  const tags: string[] = [];
  for (const piece of raw.split(/[,，、]/)) {
    const tag = piece.trim();
    if (tag && !tags.includes(tag)) {
      tags.push(tag);
    }
  }
  return tags;
}

/** 与迁移 / 服务端同口径（20260929T1000：jsonb_array_length <= 10）。 */
const MAX_ANNOTATION_TAGS = 10;

/**
 * 客户标注（方案 P2-3）：标签 / 备注 / 负责人。
 *
 * 整体替换语义，表单即现状（不做展示 / 编辑双态）——三项全空时服务端删行，
 * 列表回到「未标注」。保存走 ConfirmDialog + 固定事由，与定价 / 调账同写契约。
 */
export function CustomerAnnotationSection({
  userId,
  readOnly,
  onChanged,
}: {
  userId: string;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [annotation, setAnnotation] = useState<CustomerAnnotation | null>(null);
  const [candidates, setCandidates] = useState<CustomerOwnerCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tagsDraft, setTagsDraft] = useState("");
  const [noteDraft, setNoteDraft] = useState("");
  const [ownerDraft, setOwnerDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    void Promise.all([
      fetchCustomerAnnotation(userId),
      listCustomerOwnerCandidates(),
    ])
      .then(([current, ownerCandidates]) => {
        if (cancelled) {
          return;
        }
        setAnnotation(current);
        setCandidates(ownerCandidates.items);
        setTagsDraft(current.tags.join("，"));
        setNoteDraft(current.note);
        setOwnerDraft(current.owner_user_id);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户标注失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (splitTagInput(tagsDraft).length > MAX_ANNOTATION_TAGS) {
      setError(`标签最多 ${MAX_ANNOTATION_TAGS} 个`);
      return;
    }
    setError("");
    setDialogError("");
    setDialogOpen(true);
  }

  async function persistAnnotation(reason: string) {
    if (saving) {
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerAnnotation(
        userId,
        {
          tags: splitTagInput(tagsDraft),
          note: noteDraft.trim(),
          owner_user_id: ownerDraft || null,
        },
        reason,
      );
      setAnnotation(updated);
      setTagsDraft(updated.tags.join("，"));
      setNoteDraft(updated.note);
      setOwnerDraft(updated.owner_user_id);
      setNotice(
        updated.tags.length === 0 && !updated.note && !updated.owner_user_id
          ? "客户标注已清空"
          : "客户标注已保存",
      );
      setDialogOpen(false);
      onChanged();
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户标注失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="客户标注" className="customer-detail-section">
      <h3>客户标注</h3>
      {loading ? <p className="admin-hint">正在读取客户标注…</p> : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!loading && annotation ? (
        readOnly ? (
          <div className="customer-annotation-view">
            <p>
              <span className="customer-annotation-label">标签</span>
              <CustomerTagPills tags={annotation.tags} />
            </p>
            <p>
              <span className="customer-annotation-label">负责人</span>
              {annotation.owner_username || "未指定"}
            </p>
            <p>
              <span className="customer-annotation-label">备注</span>
              <span className="customer-annotation-note">
                {annotation.note || "—"}
              </span>
            </p>
            {annotation.updated_at ? (
              <p className="admin-hint">
                最后更新 {formatDateTime(annotation.updated_at)}
              </p>
            ) : null}
            <p className="admin-hint">审计员仅可查看标注，不能修改。</p>
          </div>
        ) : (
          <form className="admin-form" onSubmit={requestSave}>
            <label>
              标签（逗号分隔，最多 {MAX_ANNOTATION_TAGS} 个）
              <input
                placeholder="如：VIP，重点客户"
                type="text"
                value={tagsDraft}
                onChange={(event) => setTagsDraft(event.target.value)}
              />
            </label>
            <label>
              负责人
              <select
                value={ownerDraft}
                onChange={(event) => setOwnerDraft(event.target.value)}
              >
                <option value="">未指定</option>
                {candidates.map((candidate) => (
                  <option key={candidate.user_id} value={candidate.user_id}>
                    {candidate.display_name || candidate.username}
                  </option>
                ))}
              </select>
            </label>
            <label>
              备注（最多 2000 字，仅运营可见）
              <textarea
                rows={3}
                value={noteDraft}
                onChange={(event) => setNoteDraft(event.target.value)}
              />
            </label>
            {annotation.updated_at ? (
              <p className="admin-hint">
                最后更新 {formatDateTime(annotation.updated_at)}
              </p>
            ) : null}
            <div className="admin-actions">
              <button disabled={saving} type="submit">
                {saving ? "正在保存" : "保存标注"}
              </button>
            </div>
          </form>
        )
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel="确认保存"
        description="将整体替换该客户的标签、负责人与备注；三项全部清空会删除标注记录。"
        error={dialogError}
        level="standard"
        open={dialogOpen}
        title="保存客户标注"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={() => void persistAnnotation("更新客户标注")}
      />
    </section>
  );
}
