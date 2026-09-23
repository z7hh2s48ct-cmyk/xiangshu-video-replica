import { useStudio } from "./context";
import type { StudioPage } from "./types";
import { Tabs } from "./ui";

export function CreationNavigation() {
  const { state, navigate } = useStudio();
  const page =
    state.page === "reference"
      ? "video"
      : state.page === "replacement"
        ? "replica"
        : state.page === "oral-audio"
          ? "oral"
          : state.page;
  return (
    <div className="studio-creation-nav">
      <Tabs<StudioPage>
        value={page}
        onChange={navigate}
        items={[
          { id: "replica", label: "视频复刻" },
          { id: "video", label: "AI 视频" },
          { id: "oral", label: "数字人口播" },
        ]}
      />
    </div>
  );
}
