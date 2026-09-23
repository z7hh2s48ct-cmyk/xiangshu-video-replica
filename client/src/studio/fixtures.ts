import type { StudioAnalytics } from "../api";
import { createState } from "./state";
import type {
  StudioAsset,
  StudioData,
  StudioPage,
  StudioPerson,
  StudioTask,
  StudioVideo,
} from "./types";

// 数据看板（C6）审核样例：只演示平台侧真实统计的呈现形态（成片趋势 /
// 类型分布 / 最近成片），数值与指标卡示例同口径（今日 8、累计 156）；
// 播放/互动等外部平台数据不在看板范畴。最近成片统一指向 task-completed，
// 保证审核走查里"查看视频"总能落到真实存在的任务详情。
const reviewWorks = [
  {
    title: "张工 · 建房预算",
    kind: "replica",
    completedAt: "2026-09-06 10:24:00",
    costCredits: 12,
  },
  {
    title: "新中式庭院的3个细节",
    kind: "replica",
    completedAt: "2026-09-04 15:40:00",
    costCredits: 8,
  },
  {
    title: "农村自建房户型避坑",
    kind: "independent",
    completedAt: "2026-09-02 09:12:00",
    costCredits: null,
  },
  {
    title: "三代同堂的家这样设计",
    kind: "replacement",
    completedAt: "2026-08-24 18:05:00",
    costCredits: 6,
  },
];

function analyticsSample(days: 7 | 30): StudioAnalytics {
  const daily7 = [1, 2, 0, 3, 2, 4, 8];
  const daily30 = [
    0,
    1,
    2,
    0,
    1,
    0,
    2,
    1,
    0,
    1,
    2,
    0,
    1,
    0,
    0,
    2,
    1,
    0,
    1,
    0,
    2,
    0,
    1,
    ...daily7,
  ];
  const counts = days === 7 ? daily7 : daily30;
  const failedCounts =
    days === 7
      ? [0, 0, 1, 0, 0, 1, 0]
      : [
          0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0,
          0, 0, 1, 0, 0, 1, 0,
        ];
  const daily = counts.map((completed, index, all) => {
    // 固定锚定 2026-09-06 收口，避免样例随真实时钟漂移。
    const day = new Date(Date.UTC(2026, 8, 6 - (all.length - 1 - index)));
    return {
      day: day.toISOString().slice(0, 10),
      completed,
      failed: failedCounts[index] ?? 0,
    };
  });
  const kindBreakdown =
    days === 7
      ? [
          { kind: "replica", completed: 11 },
          { kind: "independent", completed: 6 },
          { kind: "replacement", completed: 3 },
        ]
      : [
          { kind: "replica", completed: 21 },
          { kind: "independent", completed: 11 },
          { kind: "replacement", completed: 6 },
        ];
  return {
    range_days: days,
    generated_at: "2026-09-06T12:00:00+00:00",
    today_completed: 8,
    range_completed: counts.reduce((sum, count) => sum + count, 0),
    total_completed: 156,
    today_generation_batches: 6,
    range_generation_batches: days === 7 ? 15 : 29,
    total_generation_batches: 120,
    range_generation_outputs: counts.reduce((sum, count) => sum + count, 0),
    range_oral_outputs: 0,
    daily,
    kind_breakdown: kindBreakdown,
    recent_works: reviewWorks
      .slice(0, days === 7 ? 3 : 4)
      .map((work, index) => ({
        task_id: "task-completed",
        task_kind: "generation" as const,
        batch_id: `batch-review-${index + 1}`,
        project_id: `project-review-${index + 1}`,
        title: work.title,
        creation_kind: work.kind,
        completed_at: work.completedAt,
        cost_credits: work.costCredits,
      })),
  };
}

// Only imported by the explicit development review entry. Never a network-error fallback.
const photo = (name: string) => `/studio/${name}.png`;
export const reviewUser = {
  id: "review-user",
  username: "review",
  display_name: "李总",
  role: "customer" as const,
};
export const sampleScript =
  "我是张工，做乡墅设计。农村建房，预算别只盯着主体。\n\n门窗、水电、防水和庭院，都要提前列进清单。先把主体、装修和庭院分开核对，给调整留出空间。\n\n准备建房的朋友，可以先列出你的需求，我们再一步步梳理。";

export function createReviewData(): StudioData {
  const assets: StudioAsset[] = [
    ...["studio", "courtyard"].map(
      (scene): StudioAsset => ({
        id: `oral-${scene}-demo`,
        name: "口播分身示例封面",
        kind: "image",
        url: `/studio/oral-${scene}-demo.png`,
        group: "口播分身",
        source: "审核示例",
        saved: true,
      }),
    ),
    {
      id: "zhang-studio",
      name: "设计室讲解",
      kind: "image",
      url: photo("zhang-studio"),
      group: "人物素材",
      personId: "zhang",
      source: "人物库场景造型",
      saved: true,
    },
    {
      id: "zhang-courtyard",
      name: "庭院讲解",
      kind: "image",
      url: photo("zhang-courtyard"),
      group: "人物素材",
      personId: "zhang",
      source: "人物库场景造型",
      saved: true,
    },
    {
      id: "construction",
      name: "张工工地巡检.jpg",
      kind: "image",
      url: photo("construction"),
      group: "人物素材",
      personId: "zhang",
      source: "上传",
      saved: true,
    },
    {
      id: "five-views",
      name: "基础五视图 · 1张合成图",
      kind: "image",
      url: photo("five-views"),
      group: "人物素材",
      personId: "zhang",
      source: "人物库场景造型",
      composite: true,
      saved: true,
    },
    {
      id: "li",
      name: "李总 · 项目负责人",
      kind: "image",
      url: photo("li"),
      group: "人物素材",
      personId: "li",
      source: "上传",
      saved: true,
    },
    {
      id: "wang",
      name: "王经理 · 客户经理",
      kind: "image",
      url: photo("wang"),
      group: "人物素材",
      personId: "wang",
      source: "上传",
      saved: true,
    },
    {
      id: "villa",
      name: "新中式乡墅.jpg",
      kind: "image",
      url: photo("villa"),
      group: "乡墅项目",
      source: "上传",
      saved: true,
    },
    {
      id: "result-1",
      name: "张工·建房预算-已确认版.mp4",
      kind: "video",
      poster: photo("zhang-studio"),
      group: "口播成片",
      personId: "zhang",
      duration: "00:42",
      source: "任务中心",
      saved: true,
    },
    {
      id: "courtyard-video",
      name: "庭院参考.mp4",
      kind: "video",
      poster: photo("villa"),
      group: "乡墅项目",
      duration: "00:12",
      source: "上传",
      saved: true,
    },
    {
      id: "speech",
      name: "张工·建房预算-录音.wav",
      kind: "audio",
      group: "口播音频",
      personId: "zhang",
      duration: "00:42",
      source: "口播录音",
      saved: true,
    },
    {
      id: "facade-video",
      name: "外立面镜头.mp4",
      kind: "video",
      poster: photo("villa"),
      group: "乡墅项目",
      duration: "00:18",
      source: "上传",
      saved: true,
    },
  ];
  const people: StudioPerson[] = [
    {
      id: "zhang",
      name: "张工",
      role: "乡墅设计师",
      portrait: photo("zhang-courtyard"),
      version: 2,
      scope: "自建房方案 / 户型优化 / 预算梳理",
      audience: "准备建房的农村家庭",
      expression: "专业、通俗、不过度承诺",
      sheetId: "five-views",
      sceneLookCount: 3,
      photoIds: ["zhang-courtyard", "construction", "zhang-studio"],
      avatars: [
        {
          id: "avatar-studio",
          name: "张工 · 设计室讲解",
          imageId: "oral-studio-demo",
          ready: true,
          origin: "视频制作",
          duration: "00:31",
        },
        {
          id: "avatar-courtyard",
          name: "张工 · 庭院讲解",
          imageId: "oral-courtyard-demo",
          ready: true,
          origin: "视频制作",
          duration: "00:30",
        },
      ],
      voices: [
        {
          id: "voice-1",
          name: "张工本人音色 V1",
          confirmed: true,
        },
        {
          id: "voice-2",
          name: "张工本人音色 V2",
          confirmed: false,
        },
      ],
    },
    {
      id: "li",
      name: "李总",
      role: "项目负责人",
      portrait: photo("li"),
      version: 1,
      scope: "建房管理 / 施工统筹",
      audience: "计划建房的业主",
      expression: "务实、可信",
      sceneLookCount: 1,
      photoIds: ["li"],
      avatars: [
        {
          id: "avatar-li",
          name: "李总 · 项目讲解",
          imageId: "li",
          ready: true,
          origin: "视频制作",
          duration: "00:30",
        },
      ],
      voices: [
        {
          id: "voice-li",
          name: "李总本人音色 V1",
          confirmed: true,
        },
      ],
    },
    {
      id: "wang",
      name: "王经理",
      role: "客户经理",
      portrait: photo("wang"),
      version: 1,
      scope: "建房需求 / 客户沟通",
      audience: "准备建房的农村家庭",
      expression: "亲切、清楚",
      sceneLookCount: 1,
      photoIds: ["wang"],
      avatars: [],
      voices: [],
    },
  ];
  const titles = [
    "农村建房，预算别只盯着主体",
    "三代同堂的家，这样设计最舒适",
    "宅基地12米面宽，自建房这样布局",
    "新中式庭院，回家就是度假",
    "建房6个施工坑，千万别踩",
    "平层别墅也能这么美，关键在细节",
  ];
  const authors = [
    "乡墅建房笔记",
    "墅语空间",
    "张工聊建房",
    "庭院设计老周",
    "王经理说施工",
    "乡墅设计研究所",
  ];
  // C4 审核样例：抖音约 20 条、视频号 30 条，对齐真实数据源口径。
  const tagPool = [
    "农村自建房",
    "别墅设计",
    "建房预算",
    "施工避坑",
    "庭院案例",
    "乡墅",
    "宅基地建房",
  ];
  const videos: StudioVideo[] = (
    [
      ["抖音", 20],
      ["视频号", 30],
    ] as const
  ).flatMap(([platform, count]) =>
    Array.from({ length: count }, (_, i) => {
      const isWechat = platform === "视频号";
      return {
        id: `${platform}-${i + 1}`,
        title: titles[i % 6] + (i >= 6 ? ` · 案例${i + 1}` : ""),
        author: authors[i % 6],
        platform,
        category: [
          "建房预算",
          "户型设计",
          "户型设计",
          "庭院案例",
          "施工避坑",
          "庭院案例",
        ][i % 6],
        poster: photo(
          [
            "villa",
            "villa-modern",
            "villa-white",
            "villa-courtyard",
            "construction",
            "villa-bungalow",
          ][i % 6],
        ),
        duration: ["01:28", "01:15", "01:06", "01:12", "00:58", "01:22"][i % 6],
        likes: 18000 - i * 380,
        collections: 842,
        shares: 326,
        description: "主体之外，门窗、水电、防水和庭院，也要列进预算清单。",
        platformKey: isWechat ? "wechat_channels" : "douyin",
        nativeId: `${platform}-native-${i + 1}`,
        authorAvatar: null,
        verified: !isWechat && i % 3 === 0,
        comments: isWechat ? null : 210 + i * 7,
        publishedAt: 1788602461 - i * 86400,
        publishedDisplay: isWechat ? `${i + 1}天前` : null,
        likeDisplay: isWechat ? (i % 4 === 0 ? "10万+" : "1.2万") : null,
        tags: [tagPool[i % 7], tagPool[(i + 2) % 7], tagPool[(i + 4) % 7]],
        hasPlayableAudio: !isWechat,
      } satisfies StudioVideo;
    }),
  );
  const tasks: StudioTask[] = [
    {
      id: "task-running",
      title: "张工 · 建房预算",
      type: "数字人口播",
      status: "running",
      progress: 68,
      submitted: "今天 09:30",
      poster: photo("zhang-studio"),
      driverMode: "text",
      ipId: "zhang",
      avatarId: "avatar-studio",
      voiceId: "voice-1",
      scriptVersion: 3,
    },
    {
      id: "task-queued",
      title: "三层新中式乡墅",
      type: "视频复刻",
      status: "queued",
      submitted: "今天 09:32",
      poster: photo("villa"),
    },
    {
      id: "task-failed",
      title: "张工 · 庭院讲解首帧",
      type: "人物置换",
      status: "failed",
      submitted: "今天 09:28",
      poster: photo("zhang-courtyard"),
    },
    {
      id: "task-completed",
      title: "张工 · 建房预算-已确认版",
      type: "数字人口播",
      status: "completed",
      submitted: "今天 09:25",
      poster: photo("zhang-studio"),
      resultId: "result-1",
      driverMode: "text",
      ipId: "zhang",
      avatarId: "avatar-studio",
      voiceId: "voice-1",
      scriptVersion: 3,
    },
  ];
  tasks
    .filter((task) => task.type === "数字人口播")
    .forEach((task) => {
      task.draftSnapshot = createReviewState("oral").draft;
    });
  return {
    people,
    assets,
    materials: [],
    videos,
    tasks,
    projects: [],
    errors: [],
    loading: false,
    // 审核示例的指标卡数值（与示例任务状态一致：1 运行 + 1 排队 + 1 待处理）。
    stats: {
      today_completed: 8,
      running: 1,
      queued: 1,
      needs_attention: 1,
      total_completed: 156,
    },
    analytics7: analyticsSample(7),
    analytics30: analyticsSample(30),
  };
}

export function createReviewState(page: StudioPage) {
  const state = createState(page);
  state.draft = {
    ...state.draft,
    ipId: "zhang",
    sourceId: "抖音-1",
    originalImageId: "li",
    imageId: "zhang-courtyard",
    firstFrameId: ["replica", "replacement"].includes(page)
      ? "zhang-courtyard"
      : "villa",
    avatarId: "avatar-studio",
    voiceId: "voice-1",
    audioId: "speech",
    prompt:
      "镜头从庭院入口缓缓推进，展示新中式乡墅外立面，清晨自然光，画面平稳。",
    referenceIds: ["villa"],
    script: {
      id: "script-review",
      title: "张工 · 建房预算",
      original:
        "农村建房预算，别只算主体。门窗、水电、防水和庭院，也要提前列进清单。\n\n先把主体、装修和庭院分开核对，给调整留出空间。\n\n准备建房的朋友，可以先列出你的需求，我们再一步步梳理。",
      text: sampleScript,
      version: 3,
      confirmed: true,
    },
  };
  state.selectedPersonId = "zhang";
  state.draft.frameConfirmed = true;
  state.selectedVideoId = "抖音-1";
  state.selectedTaskId = "task-completed";
  state.selectedAssetId = page === "publishing" ? "result-1" : "speech";
  return state;
}
