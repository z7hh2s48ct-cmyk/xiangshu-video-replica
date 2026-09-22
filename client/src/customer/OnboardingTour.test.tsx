import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import {
  markOnboardingSeen,
  ONBOARDING_STEPS,
  OnboardingTour,
  shouldShowOnboarding,
} from "./OnboardingTour";

afterEach(() => {
  window.localStorage.clear();
});

test("首次应显示、看过之后不再显示", () => {
  expect(shouldShowOnboarding("user-1")).toBe(true);
  markOnboardingSeen("user-1");
  expect(shouldShowOnboarding("user-1")).toBe(false);
  // 换账号要重新引导
  expect(shouldShowOnboarding("user-2")).toBe(true);
});

test("三步内容依次出现，最后一步是「开始使用」", () => {
  const { rerender } = render(
    <OnboardingTour onNext={() => {}} onSkip={() => {}} step={0} />,
  );
  expect(screen.getByRole("dialog", { name: "新手引导" })).toBeVisible();
  expect(screen.getByText(ONBOARDING_STEPS[0].title)).toBeVisible();
  expect(screen.getByText("第 1 / 3 步")).toBeVisible();
  expect(screen.getByRole("button", { name: "下一步" })).toBeVisible();

  rerender(<OnboardingTour onNext={() => {}} onSkip={() => {}} step={2} />);
  expect(screen.getByText(ONBOARDING_STEPS[2].title)).toBeVisible();
  expect(screen.getByRole("button", { name: "开始使用" })).toBeVisible();
});

test("跳过与下一步各自回调一次", () => {
  let nexts = 0;
  let skips = 0;
  render(
    <OnboardingTour
      onNext={() => {
        nexts += 1;
      }}
      onSkip={() => {
        skips += 1;
      }}
      step={0}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "下一步" }));
  fireEvent.click(screen.getByRole("button", { name: "跳过" }));
  expect(nexts).toBe(1);
  expect(skips).toBe(1);
});
