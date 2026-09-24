import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type UseMediaRowFitOptions, useMediaRowFit } from "./useMediaRowFit";

/** 用桩替掉 ResizeObserver：记录实例与回调，供测试手工触发。 */
class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  readonly observed: Element[] = [];
  disconnected = false;
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    MockResizeObserver.instances.push(this);
  }

  observe(target: Element) {
    this.observed.push(target);
  }

  unobserve() {}

  disconnect() {
    this.disconnected = true;
  }

  fire() {
    this.callback([], this as unknown as ResizeObserver);
  }
}

const ROW_WIDTH = 1000;

let rowWidth = ROW_WIDTH;
let frameHeightOf: (row: HTMLElement) => number = () => 0;
let frameMeasurements = 0;

function rect(width: number, height: number): DOMRect {
  return {
    width,
    height,
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    toJSON: () => ({}),
  } as DOMRect;
}

function Harness({ options }: { options: UseMediaRowFitOptions }) {
  const { rowRef, rowStyle } = useMediaRowFit(options);
  return (
    <div className="media-row" data-testid="row" ref={rowRef} style={rowStyle}>
      <div className="panel">
        <div className="media-frame" data-testid="frame" />
      </div>
      <div className="panel" data-testid="right" />
    </div>
  );
}

/** 读行元素上媒体框当前实际生效的列宽（钩子可能直接写在 DOM 上）。 */
function columnWidth(): number {
  return Number.parseFloat(
    screen.getByTestId("row").style.getPropertyValue("--media-col"),
  );
}

/** 读当前生效的 --media-col 反推出本行媒体高度，模拟「改列宽 → 右栏回流 → 媒体变高」。 */
function heightFromColumnWidth(row: HTMLElement): number {
  const width = Number.parseFloat(row.style.getPropertyValue("--media-col"));
  // 未设置时 CSS 兜底 240px（见 .media-row 的 grid-template-columns 默认值）。
  return 300 + (Number.isNaN(width) ? 240 : width) * 0.1;
}

beforeEach(() => {
  rowWidth = ROW_WIDTH;
  frameHeightOf = () => 0;
  frameMeasurements = 0;
  MockResizeObserver.instances = [];
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  // jsdom 不做布局，clientWidth 恒为 0，钩子会退到 getBoundingClientRect().width。
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      if (this.classList.contains("media-frame")) {
        frameMeasurements += 1;
        const row = this.closest<HTMLElement>(".media-row");
        return rect(0, row ? frameHeightOf(row) : 0);
      }
      return rect(this.classList.contains("media-row") ? rowWidth : 0, 0);
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useMediaRowFit", () => {
  it("按媒体高度 × 宽高比 + 内边距补偿计算列宽", () => {
    frameHeightOf = () => 500;
    render(<Harness options={{ ratio: 9 / 16, minHeight: 300 }} />);

    // 500 × 0.5625 + 42 = 323.25
    expect(columnWidth()).toBeCloseTo(323.25, 5);
  });

  it("内边距补偿可配：去掉 42px 补偿后列宽正好少 42px", () => {
    frameHeightOf = () => 500;
    render(
      <Harness
        options={{ ratio: 9 / 16, minHeight: 300, padCompensation: 0 }}
      />,
    );

    expect(columnWidth()).toBeCloseTo(500 * (9 / 16), 5);
  });

  it("迭代求值：列宽与媒体高度互为因果，收敛后稳定", () => {
    // 媒体高度随列宽变化，模拟「改列宽 → 右栏回流 → 行高变化」的循环依赖。
    frameHeightOf = heightFromColumnWidth;
    render(<Harness options={{ ratio: 9 / 16, minHeight: 300 }} />);

    const settled = columnWidth();
    expect(settled).toBeGreaterThan(0);
    // 单次求值有残差，必须再测量：> 1 才说明真的在迭代，且上限 3 次。
    expect(frameMeasurements).toBeGreaterThan(1);
    expect(frameMeasurements).toBeLessThanOrEqual(3);

    // 再触发一次观察器，结果应停在同一点（相邻两次差 < 0.5px）。
    act(() => {
      MockResizeObserver.instances[0].fire();
    });
    expect(Math.abs(columnWidth() - settled)).toBeLessThan(0.5);
  });

  it("列宽不超过 capPercent 对应的像素上限", () => {
    frameHeightOf = () => 500;
    // 16:9 且右栏很高：不设上限时列宽会是 500 × 1.778 + 42 = 931px。
    render(<Harness options={{ ratio: 16 / 9, minHeight: 300 }} />);

    expect(columnWidth()).toBeCloseTo(ROW_WIDTH * 0.4, 5);
  });

  it("capPercent 可配", () => {
    frameHeightOf = () => 500;
    render(
      <Harness options={{ ratio: 16 / 9, minHeight: 300, capPercent: 25 }} />,
    );

    expect(columnWidth()).toBeCloseTo(ROW_WIDTH * 0.25, 5);
  });

  it("省略 minHeight 时在 JS 里复刻 clamp(240px, 34dvh, 360px)", () => {
    const original = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: 1000,
    });
    try {
      // 媒体框尚未布局（高度 0）时，走基准高度：0.34 × 1000 = 340。
      render(<Harness options={{ ratio: 9 / 16 }} />);

      expect(
        screen.getByTestId("row").style.getPropertyValue("--media-min-h"),
      ).toBe("340px");
      expect(columnWidth()).toBeCloseTo(340 * (9 / 16) + 42, 5);
    } finally {
      if (original) {
        Object.defineProperty(window, "innerHeight", original);
      }
    }
  });

  it("行尚未布局（量不到宽度）时退化为基准比例，不做上限截断", () => {
    rowWidth = 0;
    frameHeightOf = () => 0;
    render(<Harness options={{ ratio: 9 / 16, minHeight: 300 }} />);

    expect(columnWidth()).toBeCloseTo(300 * (9 / 16) + 42, 5);
  });

  it("卸载时 disconnect 观察器", () => {
    const { unmount } = render(
      <Harness options={{ ratio: 9 / 16, minHeight: 300 }} />,
    );

    const [observer] = MockResizeObserver.instances;
    expect(observer.observed).toEqual([screen.getByTestId("row")]);
    expect(observer.disconnected).toBe(false);

    unmount();
    expect(observer.disconnected).toBe(true);
  });
});
