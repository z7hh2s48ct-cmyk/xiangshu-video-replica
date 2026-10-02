/** Resolve persisted references without caching customer data across sessions. */
export async function attachCustomerTaskReferences(
  path: string,
  payload: unknown,
  readReference: (type: string, id: string) => Promise<{ short_ref: string }>,
  readableMessage: (message: unknown, fallback: string) => string,
): Promise<unknown> {
  const family =
    path.startsWith("/api/generation-batches") ||
    path.startsWith("/api/generation-tasks")
      ? "VIDEO"
      : path.includes("first-frame-tasks")
        ? "FIRST_FRAME_IMAGE"
        : path.includes("character-sheet-tasks") ||
            (path.startsWith("/api/simple-characters/") &&
              (path.includes("task-status") ||
                path.includes("/tasks") ||
                path.includes("/scene-looks")))
          ? "CHARACTER_SHEET_IMAGE"
          : path.includes("character-generation-tasks") ||
              (path.startsWith("/api/character-versions/") &&
                path.includes("/generation-tasks"))
            ? "CHARACTER_VIEW_IMAGE"
            : path.startsWith("/api/oral/tasks")
              ? "ORAL_VIDEO"
              : path.includes("analysis-tasks")
                ? "ANALYSIS"
                : null;
  if (!family) return payload;
  const pending = new Map<string, Promise<string>>();
  const reference = (id: string) => {
    let request = pending.get(id);
    if (!request) {
      request = readReference(family, id)
        .then((result) => {
          if (!/^[0-9A-F]{8}$/.test(result.short_ref))
            throw new Error("Invalid reference");
          return `错误编号：${result.short_ref}`;
        })
        .catch(() => `任务编号：${id}`);
      pending.set(id, request);
    }
    return request;
  };
  async function visit(value: unknown, depth = 0): Promise<unknown> {
    if (depth > 4 || !value || typeof value !== "object") return value;
    if (Array.isArray(value))
      return Promise.all(value.map((item) => visit(item, depth + 1)));
    const row = { ...value } as Record<string, unknown>;
    for (const key of ["items", "tasks", "batch"]) {
      if (row[key]) row[key] = await visit(row[key], depth + 1);
    }
    if (typeof row.id !== "string" || Array.isArray(row.tasks)) return row;
    const failed =
      ["FAILED", "SUBMISSION_UNCERTAIN", "UNKNOWN", "ARCHIVE_FAILED"].includes(
        String(row.status),
      ) || row.archive_status === "ARCHIVE_FAILED";
    if (!failed) return row;
    const field =
      "error_message_redacted" in row
        ? "error_message_redacted"
        : "error_message";
    const message = readableMessage(
      row[field],
      "本次任务未完成，请核对任务状态后重试。",
    );
    row[field] = `${message} ${await reference(row.id)}`;
    return row;
  }
  return visit(payload);
}
