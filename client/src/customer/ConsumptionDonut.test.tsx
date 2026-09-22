import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ConsumptionDonut, consumptionSegments } from "./ConsumptionDonut";

test("按金额倒序切段，偏移量逐段累加", () => {
  const segments = consumptionSegments([
    { business: "asr", credits: 50 },
    { business: "video", credits: 100 },
    { business: "oral", credits: 50 },
  ]);
  expect(segments.map((segment) => segment.business)).toEqual([
    "video",
    "asr",
    "oral",
  ]);
  expect(segments[0].share).toBeCloseTo(50, 5);
  expect(segments[0].offset).toBe(0);
  expect(segments[1].offset).toBeCloseTo(50, 5);
  expect(segments[2].offset).toBeCloseTo(75, 5);
});

test("超过前 5 名并成「其他」，金额仍算进总额", () => {
  const items = [100, 90, 80, 70, 60, 5, 5].map((credits, index) => ({
    business: `biz-${index}`,
    credits,
  }));
  const segments = consumptionSegments(items);
  expect(segments).toHaveLength(6);
  expect(segments.at(-1)?.business).toBe("other");
  expect(segments.at(-1)?.label).toBe("其他");
  // 两笔 5 并成 10
  expect(segments.at(-1)?.credits).toBe(10);
});

test("后端自带的 other 桶与折叠尾部并成同一个「其他」，不产生重复段", () => {
  // 后端把没有计费科目的历史行归到 other；这里 other 恰好也在小额尾部里。
  const segments = consumptionSegments([
    { business: "video", credits: 100 },
    { business: "oral", credits: 80 },
    { business: "asr", credits: 60 },
    { business: "rewrite", credits: 40 },
    { business: "character", credits: 20 },
    { business: "other", credits: 10 },
    { business: "voice_clone", credits: 5 },
  ]);
  expect(
    segments.filter((segment) => segment.business === "other"),
  ).toHaveLength(1);
  expect(segments.filter((segment) => segment.label === "其他")).toHaveLength(
    1,
  );
  // 后端 other(10) + 折叠尾项 voice_clone(5)
  expect(segments.at(-1)?.credits).toBe(15);
  // 后者即便金额很大（足够进前 5）也不该另起一段
  const heavyOther = consumptionSegments([
    { business: "video", credits: 100 },
    { business: "other", credits: 90 },
    { business: "asr", credits: 1 },
  ]);
  expect(
    heavyOther.filter((segment) => segment.business === "other"),
  ).toHaveLength(1);
  expect(heavyOther.at(-1)?.business).toBe("other");
  expect(heavyOther.at(-1)?.credits).toBe(90);
});

test("0 与负数金额的项直接丢掉，空数组返回空", () => {
  expect(
    consumptionSegments([
      { business: "a", credits: 0 },
      { business: "b", credits: -5 },
    ]),
  ).toEqual([]);
});

test("渲染图例：Top3 高亮、占比与总额可见", () => {
  render(
    <ConsumptionDonut
      days={30}
      items={[
        { business: "video", credits: 300 },
        { business: "asr", credits: 100 },
      ]}
    />,
  );
  expect(
    screen.getByRole("img", { name: /最近 30 天消费构成，共 400 积分/ }),
  ).toBeVisible();
  expect(screen.getByText("视频生成")).toBeVisible();
  // 与子账号权限矩阵同一份文案（permissionViz.BUSINESS_FEATURES），不是「语音转写」。
  expect(screen.getByText("语音识别")).toBeVisible();
  expect(screen.getByText("75.0%")).toBeVisible();
  expect(screen.getByText("400")).toBeVisible();
  // Top 3 有高亮类
  const top = screen.getByText("视频生成").closest("li");
  expect(top?.className).toContain("uc-donut__legend-top");
});

test("没有消费时给出空态而不是空圆环", () => {
  render(<ConsumptionDonut days={30} items={[]} />);
  expect(screen.getByText("最近 30 天还没有消费记录。")).toBeVisible();
});
