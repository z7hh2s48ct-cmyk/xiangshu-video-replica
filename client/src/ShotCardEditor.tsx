import type { ShotCard, ShotMotion } from "./api";

const SUBJECT_MOTION_STATE_OPTIONS: Array<{
  value: ShotMotion["subject_motion_state"];
  label: string;
}> = [
  { value: "STATIC", label: "静止" },
  { value: "WALKING", label: "行走" },
  { value: "RUNNING", label: "跑动" },
  { value: "TURNING", label: "转身" },
  { value: "GESTURING_ONLY", label: "仅手势" },
  { value: "OBJECT_MOTION", label: "物体运动" },
  { value: "NO_PERSON", label: "无人物" },
];

const SUBJECT_DIRECTION_OPTIONS: Array<{
  value: ShotMotion["subject_direction"];
  label: string;
}> = [
  { value: "toward_camera", label: "向镜头" },
  { value: "away_from_camera", label: "背离镜头" },
  { value: "left", label: "向画面左" },
  { value: "right", label: "向画面右" },
  { value: "lateral", label: "横向移动" },
  { value: "in_place", label: "原地" },
  { value: "none", label: "无" },
];

const MOTION_CAMERA_OPTIONS: Array<{
  value: ShotMotion["camera_motion"];
  label: string;
}> = [
  { value: "STATIC", label: "固定机位" },
  { value: "PUSH_IN", label: "推近" },
  { value: "PULL_BACK", label: "拉远" },
  { value: "HANDHELD_TRACKING", label: "手持跟拍" },
  { value: "PAN", label: "横摇" },
  { value: "TILT", label: "纵摇" },
  { value: "FOLLOW", label: "跟随主体" },
];

const DEFAULT_SHOT_MOTION: ShotMotion = {
  subject_motion_state: "STATIC",
  subject_direction: "none",
  subject_displacement: "无位移",
  hand_action: "未描述",
  camera_motion: "STATIC",
  relative_motion: "未描述",
};

const SHOT_TEXT_FIELDS = [
  { key: "shot_id", label: "镜头编号" },
  { key: "shot_type", label: "景别" },
  { key: "composition", label: "构图" },
  { key: "camera_motion", label: "运镜" },
  { key: "subject", label: "主体" },
  { key: "action", label: "动作" },
  { key: "scene", label: "场景" },
  { key: "spoken_text", label: "原口播" },
  { key: "transition", label: "转场" },
] as const;

const CLIPBOARD_HEADERS = [
  "镜头编号",
  "开始(秒)",
  "结束(秒)",
  "景别",
  "构图",
  "运镜",
  "主体",
  "动作",
  "人物与镜头运动",
  "场景",
  "原口播",
  "转场",
];

function clipboardCell(value: unknown): string {
  return String(value ?? "")
    .replace(/[\t\r\n]+/g, " ")
    .trim();
}

function optionLabel<T extends string>(
  options: Array<{ value: T; label: string }>,
  value: T,
): string {
  return options.find((option) => option.value === value)?.label ?? value;
}

function motionClipboardText(motion: ShotMotion | null | undefined): string {
  if (!motion) return "";
  return [
    `人物：${optionLabel(SUBJECT_MOTION_STATE_OPTIONS, motion.subject_motion_state)}`,
    `方向：${optionLabel(SUBJECT_DIRECTION_OPTIONS, motion.subject_direction)}`,
    `位移：${motion.subject_displacement}`,
    `手部：${motion.hand_action}`,
    `镜头：${optionLabel(MOTION_CAMERA_OPTIONS, motion.camera_motion)}`,
    `相对运动：${motion.relative_motion}`,
  ].join("；");
}

/** 制表符格式可直接粘贴到 Excel、飞书表格和常见文档表格。 */
export function formatShotCardsForClipboard(shots: ShotCard[]): string {
  const rows = shots.map((shot) => [
    shot.shot_id,
    shot.start_time,
    shot.end_time,
    shot.shot_type,
    shot.composition,
    shot.camera_motion,
    shot.subject,
    shot.action,
    motionClipboardText(shot.motion),
    shot.scene,
    shot.spoken_text,
    shot.transition,
  ]);
  return [CLIPBOARD_HEADERS, ...rows]
    .map((row) => row.map(clipboardCell).join("\t"))
    .join("\n");
}

export type ShotTableImportResult =
  | { ok: true; promptText: string }
  | { ok: false; error: string };

export function promptTextFromShotTableClipboard(
  clipboardText: string,
): ShotTableImportResult {
  const normalized = clipboardText.replace(/^\uFEFF/, "").trim();
  if (!normalized) return { ok: false, error: "剪贴板中没有可导入的分镜表。" };
  const lines = normalized.split(/\r?\n/);
  const firstLine = lines[0] ?? "";
  const headers = firstLine.split("\t").map((value) => value.trim());
  const required = ["镜头编号", "开始(秒)", "结束(秒)"];
  if (!required.every((header) => headers.includes(header))) {
    return {
      ok: false,
      error: "未识别到分镜表表头，请先使用拆解页面的“复制分镜表”按钮。",
    };
  }
  if (!lines.slice(1).some((line) => line.trim())) {
    return { ok: false, error: "分镜表中没有可导入的镜头数据。" };
  }
  const promptText = [
    "以下内容是拆解后的分镜表。请保留镜头顺序、时间、主体动作、原口播和转场，并转换为当前视频模式要求的 H3 提示词格式：",
    normalized,
  ].join("\n\n");
  if (Array.from(promptText).length > 7000) {
    return {
      ok: false,
      error: "分镜表超过 7000 字，请删减镜头描述后再导入。",
    };
  }
  return { ok: true, promptText };
}

export function ShotCardEditor({
  shots,
  readOnly = false,
  onChange,
}: {
  shots: ShotCard[];
  readOnly?: boolean;
  onChange: (index: number, shot: ShotCard) => void;
}) {
  const update = (
    index: number,
    key: Exclude<keyof ShotCard, "motion">,
    value: string,
  ) => {
    const shot = shots[index];
    if (!shot || readOnly) return;
    const nextValue =
      key === "start_time" || key === "end_time"
        ? toNonNegativeTime(value)
        : value;
    onChange(index, { ...shot, [key]: nextValue } as ShotCard);
  };

  const updateMotion = (index: number, patch: Partial<ShotMotion>) => {
    const shot = shots[index];
    if (!shot || readOnly) return;
    if (shot.motion == null && patch.subject_motion_state === undefined) return;
    onChange(index, {
      ...shot,
      motion: { ...(shot.motion ?? DEFAULT_SHOT_MOTION), ...patch },
    });
  };

  return (
    <div className="shot-table-wrap">
      <table className="shot-table">
        <thead>
          <tr>
            <th scope="col">时间段</th>
            <th scope="col">开始(秒)</th>
            <th scope="col">结束(秒)</th>
            {SHOT_TEXT_FIELDS.filter(({ key }) => key !== "shot_id").flatMap(
              ({ key, label }) => [
                <th key={key} scope="col">
                  {label}
                </th>,
                ...(key === "action"
                  ? [
                      <th key="motion" scope="col">
                        运动
                      </th>,
                    ]
                  : []),
              ],
            )}
          </tr>
        </thead>
        <tbody>
          {shots.map((shot, index) => (
            <tr key={shot.shot_id}>
              <td className="shot-table__id">
                <ShotCellInput
                  ariaLabel={`${shot.shot_id} 镜头编号`}
                  onChange={(value) => update(index, "shot_id", value)}
                  readOnly={readOnly}
                  value={shot.shot_id}
                />
                {shot.segment_kind ? (
                  <span
                    className="shot-segment-kind"
                    title={shot.boundary_reason ?? undefined}
                  >
                    {shot.segment_kind === "ACTION_BEAT" ? "动作段" : "切镜"}
                  </span>
                ) : null}
              </td>
              <td className="shot-table__time">
                <ShotCellInput
                  ariaLabel={`${shot.shot_id} 开始时间`}
                  onChange={(value) => update(index, "start_time", value)}
                  readOnly={readOnly}
                  type="number"
                  value={String(shot.start_time)}
                />
              </td>
              <td className="shot-table__time">
                <ShotCellInput
                  ariaLabel={`${shot.shot_id} 结束时间`}
                  onChange={(value) => update(index, "end_time", value)}
                  readOnly={readOnly}
                  type="number"
                  value={String(shot.end_time)}
                />
              </td>
              {SHOT_TEXT_FIELDS.filter(({ key }) => key !== "shot_id").flatMap(
                ({ key, label }) => [
                  <td
                    key={key}
                    className={
                      key === "spoken_text" ? "shot-table__spoken" : undefined
                    }
                  >
                    <ShotCellInput
                      ariaLabel={`${shot.shot_id} ${label}`}
                      onChange={(value) => update(index, key, value)}
                      readOnly={readOnly}
                      value={shot[key]}
                    />
                  </td>,
                  ...(key === "action"
                    ? [
                        <td key="motion" className="shot-table__motion">
                          <ShotMotionCell
                            ariaPrefix={shot.shot_id}
                            motion={shot.motion ?? null}
                            onChange={(patch) => updateMotion(index, patch)}
                            readOnly={readOnly}
                          />
                        </td>,
                      ]
                    : []),
                ],
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function toNonNegativeTime(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function ShotCellInput({
  ariaLabel,
  onChange,
  readOnly = false,
  type = "text",
  value,
}: {
  ariaLabel: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  type?: "number" | "text";
  value: string;
}) {
  return (
    <input
      aria-label={ariaLabel}
      className="shot-table-input"
      disabled={readOnly}
      min={type === "number" ? 0 : undefined}
      onChange={(event) => onChange(event.target.value)}
      step={type === "number" ? "0.1" : undefined}
      type={type}
      value={value}
    />
  );
}

function ShotMotionCell({
  ariaPrefix,
  motion,
  onChange,
  readOnly = false,
}: {
  ariaPrefix: string;
  motion: ShotMotion | null;
  onChange: (patch: Partial<ShotMotion>) => void;
  readOnly?: boolean;
}) {
  if (motion == null) {
    return (
      <select
        aria-label={`${ariaPrefix} 人物运动`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) => {
          const next = event.target.value as ShotMotion["subject_motion_state"];
          if (next) onChange({ subject_motion_state: next });
        }}
        value=""
      >
        <option value="">未标注</option>
        {SUBJECT_MOTION_STATE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }
  return (
    <div className="shot-motion-cell">
      <select
        aria-label={`${ariaPrefix} 人物运动`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) =>
          onChange({
            subject_motion_state: event.target
              .value as ShotMotion["subject_motion_state"],
          })
        }
        value={motion.subject_motion_state}
      >
        {SUBJECT_MOTION_STATE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <select
        aria-label={`${ariaPrefix} 位移方向`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) =>
          onChange({
            subject_direction: event.target
              .value as ShotMotion["subject_direction"],
          })
        }
        value={motion.subject_direction}
      >
        {SUBJECT_DIRECTION_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <select
        aria-label={`${ariaPrefix} 运镜方式`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) =>
          onChange({
            camera_motion: event.target.value as ShotMotion["camera_motion"],
          })
        }
        value={motion.camera_motion}
      >
        {MOTION_CAMERA_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 位移幅度`}
        onChange={(value) => onChange({ subject_displacement: value })}
        readOnly={readOnly}
        value={motion.subject_displacement}
      />
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 手部动作`}
        onChange={(value) => onChange({ hand_action: value })}
        readOnly={readOnly}
        value={motion.hand_action}
      />
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 相对运动`}
        onChange={(value) => onChange({ relative_motion: value })}
        readOnly={readOnly}
        value={motion.relative_motion}
      />
    </div>
  );
}
