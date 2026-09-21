import { type FormEvent, useEffect, useState } from "react";
import {
  type CustomerListItem,
  createSubAccount,
  deleteSubAccount,
  listMasterAccounts,
  listSubAccounts,
  type SubAccountListItem,
  updateSubAccount,
} from "../api.admin";
import { SkeletonTable } from "./ui/SkeletonTable";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";
import "./sub-accounts.css";

interface SubAccountsPageProps {
  embedded?: boolean;
  operatorId?: string;
  readOnly?: boolean;
}

type CreateSubAccountForm = {
  username: string;
  display_name: string;
  parent_user_id: string;
  reason: string;
};

export function SubAccountsPage({
  embedded: _embedded = false,
  operatorId: _operatorId = "standalone-admin",
  readOnly = false,
}: SubAccountsPageProps = {}) {
  const [subAccounts, setSubAccounts] = useState<SubAccountListItem[]>([]);
  const [parents, setParents] = useState<CustomerListItem[]>([]);
  const [parentLoading, setParentLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState<
    "all" | "active" | "inactive"
  >("all");
  const [selectedParent, setSelectedParent] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editData, setEditData] = useState<{
    display_name: string;
    is_active: boolean;
  }>({
    display_name: "",
    is_active: true,
  });
  const [_deletingId, setDeletingId] = useState<string | null>(null);
  const [form, setForm] = useState<CreateSubAccountForm>({
    username: "",
    display_name: "",
    parent_user_id: selectedParent || "",
    reason: "",
  });
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [checkUsernameLoading, setCheckUsernameLoading] = useState(false);
  const [sortBy, setSortBy] = useState<"created_at" | "name">("created_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");

  // Load master accounts on mount
  useEffect(() => {
    async function loadParents() {
      try {
        const result = await listMasterAccounts(100, 0);
        setParents(result.items);
      } catch (err) {
        console.error("Failed to load master accounts:", err);
        setError(
          err instanceof Error ? err.message : "Failed to load master accounts",
        );
      } finally {
        setParentLoading(false);
      }
    }
    loadParents();
  }, []);

  // Debounced username uniqueness check
  useEffect(() => {
    if (!form.username || form.username.length < 3 || !selectedParent) {
      setUsernameError(null);
      return;
    }

    let cancelled = false;
    const debounceTimer = setTimeout(async () => {
      setCheckUsernameLoading(true);
      try {
        const data = await listSubAccounts(selectedParent);
        const existingUsernames = new Set(
          data.sub_accounts.map((sa) => sa.username),
        );
        if (!cancelled && existingUsernames.has(form.username)) {
          setUsernameError(`用户名 "${form.username}" 已被使用`);
        }
      } catch (err) {
        console.error("Failed to check username:", err);
      } finally {
        if (!cancelled) {
          setCheckUsernameLoading(false);
        }
      }
    }, 500); // 500ms debounce

    return () => {
      cancelled = true;
      clearTimeout(debounceTimer);
    };
  }, [form.username, selectedParent]);

  // Load sub-accounts on mount or when filter changes
  useEffect(() => {
    async function load() {
      if (!selectedParent) {
        setLoading(false);
        return;
      }
      setLoading(true);
      setError("");
      try {
        const data = await listSubAccounts(selectedParent);
        setSubAccounts(data.sub_accounts);
      } catch (err) {
        console.error("Failed to load sub-accounts:", err);
        setError(
          err instanceof Error ? err.message : "Failed to load sub-accounts",
        );
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [selectedParent]);

  // Filter sub-accounts based on status
  const filteredSubAccounts = subAccounts.filter((sa) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "active") return sa.is_active;
    if (statusFilter === "inactive") return !sa.is_active;
    return true;
  });

  // Sort filtered sub-accounts
  const sortedSubAccounts = [...filteredSubAccounts].sort((a, b) => {
    let comparison = 0;
    if (sortBy === "name") {
      comparison = (a.display_name || a.username).localeCompare(
        b.display_name || b.username,
        "zh",
      );
    } else {
      comparison =
        new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
    }
    return sortOrder === "asc" ? comparison : -comparison;
  });

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (creating) return;

    // Basic validation
    if (!form.username.trim()) {
      setError("请输入用户名");
      return;
    }
    if (usernameError) {
      setError(usernameError);
      return;
    }
    if (!form.display_name.trim()) {
      setError("请输入显示名称");
      return;
    }
    if (!form.reason.trim()) {
      setError("请输入创建原因");
      return;
    }

    setCreating(true);
    try {
      await createSubAccount(form);
      setForm({
        username: "",
        display_name: "",
        parent_user_id: selectedParent,
        reason: "",
      });
      setUsernameError(null);
      // Reload list
      if (selectedParent) {
        const data = await listSubAccounts(selectedParent);
        setSubAccounts(data.sub_accounts);
      }
    } catch (err) {
      console.error("Failed to create sub-account:", err);
      setError(err instanceof Error ? err.message : "创建子账号失败，请重试");
    } finally {
      setCreating(false);
    }
  }

  async function handleStartEdit(sa: SubAccountListItem) {
    setEditingId(sa.id);
    setEditData({ display_name: sa.display_name, is_active: sa.is_active });
  }

  async function handleSaveEdit(id: string) {
    try {
      await updateSubAccount(id, editData, "编辑子账号信息");
      setEditingId(null);
      if (selectedParent) {
        const data = await listSubAccounts(selectedParent);
        setSubAccounts(data.sub_accounts);
      }
    } catch (err) {
      console.error("Failed to update sub-account:", err);
      alert(
        err instanceof Error ? err.message : "Failed to update sub-account",
      );
      setEditingId(null);
    }
  }

  async function handleCancelEdit() {
    setEditingId(null);
  }

  async function handleDelete(id: string, username: string) {
    const confirmed = window.confirm(
      `确定要删除子账号"${username}"吗？\n\n注意：删除后将同时移除该账号关联的所有设备和发布账号，此操作不可恢复。`,
    );
    if (!confirmed) return;

    setDeletingId(id);
    try {
      await deleteSubAccount(id);
      // Reload list
      if (selectedParent) {
        const data = await listSubAccounts(selectedParent);
        setSubAccounts(data.sub_accounts);
      }
    } catch (err) {
      console.error("Failed to delete sub-account:", err);
      setError(err instanceof Error ? err.message : "删除子账号失败，请重试");
    } finally {
      setDeletingId(null);
    }
  }

  function handleSortChange(field: "created_at" | "name") {
    if (sortBy === field) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc");
    } else {
      setSortBy(field);
      setSortOrder("desc");
    }
  }

  function handleParentSelect(e: React.ChangeEvent<HTMLSelectElement>) {
    const parentId = e.target.value;
    setSelectedParent(parentId);
    setForm((prev) => ({ ...prev, parent_user_id: parentId }));
  }

  if (error) {
    return (
      <div className="admin-page">
        <div className="error-message">{error}</div>
      </div>
    );
  }

  return (
    <div className="admin-page">
      <h2>子账号管理</h2>
      <p>为母账号创建和管理子账号，支持分布式视频生成操作</p>

      {/* Parent selector */}
      <div className="filter-bar">
        <label htmlFor="parent-account-select">
          选择母账号：
          {parentLoading ? (
            <span style={{ color: "#6b7280", fontSize: "14px" }}>
              加载中...
            </span>
          ) : (
            <select
              id="parent-account-select"
              value={selectedParent}
              onChange={handleParentSelect}
            >
              <option value="">请选择母账号</option>
              {parents.map((parent) => (
                <option key={parent.user_id} value={parent.user_id}>
                  {parent.display_name || parent.username}
                </option>
              ))}
            </select>
          )}
        </label>
      </div>

      {/* Creation form */}
      {selectedParent && (
        <form className="create-form" onSubmit={handleSubmit}>
          <h3>创建新子账号</h3>
          <div className="form-row">
            <input
              type="text"
              placeholder="用户名 (username)"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              disabled={creating}
            />
            {checkUsernameLoading && (
              <span
                className="loading-indicator"
                role="status"
                aria-label="检查可用性..."
              ></span>
            )}
            {usernameError && !checkUsernameLoading && (
              <span className="error-text" role="alert">
                {usernameError}
              </span>
            )}
            <input
              type="text"
              placeholder="显示名称 (display_name)"
              value={form.display_name}
              onChange={(e) =>
                setForm({ ...form, display_name: e.target.value })
              }
              disabled={creating}
            />
          </div>
          <textarea
            placeholder="创建原因 (reason)"
            rows={2}
            value={form.reason}
            onChange={(e) => setForm({ ...form, reason: e.target.value })}
            disabled={creating}
          />
          <button type="submit" disabled={creating}>
            {creating ? "创建中..." : "创建子账号"}
          </button>
        </form>
      )}

      {/* List section */}
      {selectedParent && (
        <>
          {/* Filters and Sort */}
          <div className="filter-bar">
            <label>
              状态筛选:
              <select
                value={statusFilter}
                onChange={(e) =>
                  setStatusFilter(
                    e.target.value as "all" | "active" | "inactive",
                  )
                }
              >
                <option value="all">全部</option>
                <option value="active">活跃</option>
                <option value="inactive">已禁用</option>
              </select>
            </label>
            {!readOnly && (
              <label>
                排序方式:
                <select
                  value={`${sortBy}-${sortOrder}`}
                  onChange={(e) => {
                    const [field, order] = e.target.value.split("-");
                    handleSortChange(field as "created_at" | "name");
                    setSortOrder(order as "asc" | "desc");
                  }}
                >
                  <option value="created_at-desc">按时间倒序</option>
                  <option value="created_at-asc">按时间正序</option>
                  <option value="name-asc">按名称升序</option>
                  <option value="name-desc">按名称降序</option>
                </select>
              </label>
            )}
          </div>

          {/* Table */}
          {loading ? (
            <SkeletonTable numRows={5} />
          ) : filteredSubAccounts.length === 0 ? (
            <div className="empty-state">暂无子账号</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th
                    onClick={() => handleSortChange("name")}
                    style={{ cursor: "pointer" }}
                  >
                    显示名称{" "}
                    {sortBy === "name" && (sortOrder === "asc" ? "↑" : "↓")}
                  </th>
                  <th>用户名</th>
                  <th>状态</th>
                  <th
                    onClick={() => handleSortChange("created_at")}
                    style={{ cursor: "pointer" }}
                  >
                    创建时间{" "}
                    {sortBy === "created_at" &&
                      (sortOrder === "asc" ? "↑" : "↓")}
                  </th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {sortedSubAccounts.map((sa) => (
                  <tr key={sa.id}>
                    <td>{sa.id.slice(0, 8)}...</td>
                    <td>{sa.username}</td>
                    {editingId === sa.id ? (
                      <td>
                        <input
                          type="text"
                          value={editData.display_name}
                          onChange={(e) =>
                            setEditData({
                              ...editData,
                              display_name: e.target.value,
                            })
                          }
                          className="edit-input"
                        />
                      </td>
                    ) : (
                      <td>{sa.display_name}</td>
                    )}
                    <td>
                      {editingId === sa.id ? (
                        <select
                          value={editData.is_active ? "true" : "false"}
                          onChange={(e) =>
                            setEditData({
                              ...editData,
                              is_active: e.target.value === "true",
                            })
                          }
                        >
                          <option value="true">活跃</option>
                          <option value="false">已禁用</option>
                        </select>
                      ) : (
                        <StatusBadge tone={sa.is_active ? "good" : "neutral"}>
                          {sa.is_active ? "活跃" : "已禁用"}
                        </StatusBadge>
                      )}
                    </td>
                    <td>{formatDateTime(sa.created_at)}</td>
                    <td className="actions-cell">
                      {editingId === sa.id ? (
                        <>
                          <button
                            type="button"
                            onClick={() => handleSaveEdit(sa.id)}
                          >
                            保存
                          </button>
                          <button type="button" onClick={handleCancelEdit}>
                            取消
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            onClick={() => handleStartEdit(sa)}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(sa.id, sa.username)}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
