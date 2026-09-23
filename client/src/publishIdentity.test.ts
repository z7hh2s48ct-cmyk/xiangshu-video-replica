import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

import script from "../src-tauri/src/publish_identity.js?raw";

const privateFixture = ["synthetic", "do-not-forward"].join("-");

function environment(
  origin: string,
  path: string,
  body: unknown,
  options: { timers?: boolean } = {},
) {
  const location = new URL(origin);
  const clone = vi.fn(() => ({ json: async () => body }));
  const requested: string[] = [];
  const pageFetch = vi.fn(async (input?: unknown) => {
    if (typeof input === "string") requested.push(input);
    return { url: origin + path, clone };
  });
  const win: Record<string, unknown> = { fetch: pageFetch };
  win.top = win;
  // The probe timer only exists when the harness opts in, so the passive-only
  // cases keep asserting that nothing is requested on the page's behalf.
  let scheduled: (() => void) | undefined;
  if (options.timers) {
    win.setInterval = (handler: () => void) => {
      scheduled = handler;
      return 1;
    };
    win.clearInterval = () => {
      scheduled = undefined;
    };
  }
  class XHR {
    open() {}
    addEventListener() {}
  }
  runInNewContext(script, { window: win, location, URL, XMLHttpRequest: XHR });
  const settle = async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  };
  return {
    win,
    clone,
    requested,
    pageFetch,
    tick: async () => {
      scheduled?.();
      await settle();
      return win.__xiangshuPublishIdentity;
    },
    scheduled: () => scheduled,
    read: async () => {
      await (win.fetch as () => Promise<unknown>)();
      await settle();
      return win.__xiangshuPublishIdentity;
    },
  };
}

it.each([
  [
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    {
      status_code: 0,
      user: {
        uid: "u1",
        nickname: "抖音用户",
        cookie: privateFixture,
      },
    },
    "抖音用户",
  ],
  [
    "https://channels.weixin.qq.com",
    "/cgi-bin/mmfinderassistant-bin/auth/auth_data",
    {
      errCode: 0,
      data: {
        finderUser: { finderUsername: "u1", nickname: "视频号用户" },
        authKey: privateFixture,
      },
    },
    "视频号用户",
  ],
  [
    "https://creator.xiaohongshu.com",
    "/api/galaxy/user/info",
    {
      success: true,
      data: {
        userId: "u1",
        userName: "小红书用户",
        token: privateFixture,
      },
    },
    "小红书用户",
  ],
])(
  "官方账号响应只提取 UID 和用户名：%s",
  async (origin, path, body, username) => {
    const result = await environment(origin, path, body).read();
    expect(result).toEqual({ platform_user_id: "u1", username });
    expect(JSON.stringify(result)).not.toContain(privateFixture);
  },
);

it("不读取其他用户资料接口或失败登录响应", async () => {
  const other = environment("https://creator.douyin.com", "/api/other-user", {
    status_code: 0,
    user: { uid: "u1", nickname: "其他人" },
  });
  expect(await other.read()).toBeUndefined();
  expect(other.clone).not.toHaveBeenCalled();
  expect(
    await environment(
      "https://creator.douyin.com",
      "/web/api/media/user/info/",
      { status_code: 8, user: { uid: "u1", nickname: "未登录" } },
    ).read(),
  ).toBeUndefined();
});

it("抖音在页面自身不发身份请求时主动复核并保存账号", async () => {
  const douyin = environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户" } },
    { timers: true },
  );
  // 扫码后的跳转使被动拦截落空：页面没有发起过任何请求。
  expect(douyin.win.__xiangshuPublishIdentity).toBeUndefined();
  expect(await douyin.tick()).toEqual({
    platform_user_id: "u1",
    username: "抖音用户",
  });
  expect(douyin.requested).toEqual(["/web/api/media/user/info/?aid=1128"]);
});

it("抖音识别成功后停止主动复核", async () => {
  const douyin = environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户" } },
    { timers: true },
  );
  await douyin.tick();
  await douyin.tick();
  expect(douyin.scheduled()).toBeUndefined();
  expect(douyin.requested).toHaveLength(1);
});

it("只有抖音启动主动复核", async () => {
  for (const [origin, path] of [
    ["https://channels.weixin.qq.com", "/auth/auth_data"],
    ["https://creator.xiaohongshu.com", "/api/galaxy/user/info"],
  ]) {
    const other = environment(origin, path, {}, { timers: true });
    expect(other.scheduled()).toBeUndefined();
    expect(other.requested).toEqual([]);
  }
});

it.each([
  [
    { url_list: ["https://p26.douyinpic.com/large.jpeg"] },
    undefined,
    undefined,
    "https://p26.douyinpic.com/large.jpeg",
  ],
  [
    undefined,
    { url_list: ["https://p26.douyinpic.com/thumb.jpeg"] },
    undefined,
    "https://p26.douyinpic.com/thumb.jpeg",
  ],
  [
    undefined,
    undefined,
    "https://p26.douyinpic.com/flat.jpeg",
    "https://p26.douyinpic.com/flat.jpeg",
  ],
])(
  "抖音从身份响应中提取头像，按 larger → thumb → url 回退",
  async (avatar_larger, avatar_thumb, avatar_url, expected) => {
    const result = await environment(
      "https://creator.douyin.com",
      "/web/api/media/user/info/",
      {
        status_code: 0,
        user: {
          uid: "u1",
          nickname: "抖音用户",
          avatar_larger,
          avatar_thumb,
          avatar_url,
        },
      },
    ).read();
    expect(result).toEqual({
      platform_user_id: "u1",
      username: "抖音用户",
      avatar_url: expected,
    });
  },
);

it("视频号从身份响应中提取头像", async () => {
  const result = await environment(
    "https://channels.weixin.qq.com",
    "/auth/auth_data",
    {
      errCode: 0,
      data: {
        finderUser: {
          finderUsername: "u1",
          nickname: "视频号用户",
          headImgUrl: "https://wx.qlogo.cn/finderhead/abc/0",
        },
      },
    },
  ).read();
  expect(result).toEqual({
    platform_user_id: "u1",
    username: "视频号用户",
    avatar_url: "https://wx.qlogo.cn/finderhead/abc/0",
  });
});

it.each([
  ["http://p26.douyinpic.com/insecure.jpeg"],
  ["data:image/png;base64,iVBORw0KGgo="],
  ["javascript:alert(1)"],
  [`https://p26.douyinpic.com/${"a".repeat(1200)}.jpeg`],
])("拒绝非 https 或超长的头像地址：%s", async (avatar_url) => {
  const result = await environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户", avatar_url } },
  ).read();
  expect(result).toEqual({ platform_user_id: "u1", username: "抖音用户" });
});

it("不在相似域名注入或读取账号", async () => {
  const other = environment(
    "https://creator.douyin.com.evil.test",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "假响应" } },
  );
  expect(await other.read()).toBeUndefined();
  expect(other.clone).not.toHaveBeenCalled();
});
