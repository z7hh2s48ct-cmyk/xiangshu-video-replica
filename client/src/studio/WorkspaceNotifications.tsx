import { useEffect, useRef, useState } from "react";
import {
  getWorkspaceNotifications,
  markWorkspaceNotificationsRead,
  type WorkspaceNotifications as NotificationData,
} from "../api";
import { useStudio } from "./context";
import { StudioDialog } from "./StudioWorkspace";
import { Button, Empty, Icon } from "./ui";

const statuses: Record<string, string> = {
  SUCCEEDED: "已完成",
  FAILED: "生成失败",
  SUBMISSION_UNCERTAIN: "提交状态待确认",
  ARCHIVE_FAILED: "归档失败",
};

export function WorkspaceNotifications() {
  const { review, navigate, user } = useStudio();
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<NotificationData | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    void revision;
    void user.id;
    const current = ++generation.current;
    setResult(null);
    setError("");
    setBusy(false);
    if (review) return;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      // MATERIAL-PERF-D（P1-5）：页面隐藏时跳过本轮请求（下一个 30s tick 续上）。
      if (!document.hidden) {
        try {
          const next = await getWorkspaceNotifications();
          if (current !== generation.current) return;
          setResult(next);
          setError("");
        } catch (cause) {
          if (current !== generation.current) return;
          setError(cause instanceof Error ? cause.message : "读取通知失败");
        }
      }
      if (current === generation.current)
        timer = setTimeout(() => void poll(), 30_000);
    }
    void poll();
    return () => {
      generation.current += 1;
      clearTimeout(timer);
    };
  }, [review, revision, user.id]);

  async function markAll() {
    if (busy) return;
    const current = generation.current;
    setBusy(true);
    try {
      await markWorkspaceNotificationsRead();
      if (generation.current === current) setRevision((value) => value + 1);
    } catch (cause) {
      if (generation.current === current)
        setError(cause instanceof Error ? cause.message : "标记已读失败");
    } finally {
      if (generation.current === current) setBusy(false);
    }
  }
  return (
    <>
      <button
        type="button"
        className="studio-notifications"
        aria-label="查看任务动态"
        onClick={() => setOpen(true)}
      >
        <Icon name="bell" size={28} />
        {!!result?.unread_count && (
          <small>
            {result.unread_count > 99 ? "99+" : result.unread_count}
          </small>
        )}
      </button>
      {open && (
        <StudioDialog title="任务通知" onClose={() => setOpen(false)}>
          {review ? (
            <p>审核模式不读取真实任务通知。</p>
          ) : (
            <>
              {error && (
                <p role="alert">
                  {error}{" "}
                  <Button onClick={() => setRevision((value) => value + 1)}>
                    重新读取
                  </Button>
                </p>
              )}
              {!error && !result && <p role="status">正在读取通知…</p>}
              {result?.enabled === false && (
                <p>任务通知已关闭，可在个人中心的账号设置中开启。</p>
              )}
              {result?.enabled && (
                <>
                  <p>最近 50 条任务状态；未读 {result.unread_count} 条。</p>
                  <Button
                    disabled={
                      busy ||
                      result.unread_count === 0 ||
                      user.role === "auditor"
                    }
                    onClick={() => void markAll()}
                  >
                    全部标为已读
                  </Button>
                  <div className="studio-search-results">
                    {result.items.map((item) => (
                      <Button
                        key={item.id}
                        onClick={() => {
                          navigate("task-detail", {
                            selectedTaskId:
                              item.task_kind === "oral_task"
                                ? `oral-${item.task_id}`
                                : item.task_id,
                            selectedTaskKind: item.task_kind,
                            selectedTaskBackendId: item.task_id,
                            returnTo: "tasks",
                          });
                          setOpen(false);
                        }}
                      >
                        {item.unread ? "● " : ""}
                        {item.title} · {statuses[item.status] ?? "待处理"}
                        <small>
                          {new Date(item.occurred_at).toLocaleString("zh-CN")}
                        </small>
                      </Button>
                    ))}
                  </div>
                  {result.items.length === 0 && (
                    <Empty
                      title="暂无任务通知"
                      description="任务完成或需要处理时，会在这里显示。"
                    />
                  )}
                </>
              )}
            </>
          )}
        </StudioDialog>
      )}
    </>
  );
}
