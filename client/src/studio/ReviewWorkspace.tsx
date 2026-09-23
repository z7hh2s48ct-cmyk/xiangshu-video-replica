import { createReviewData, createReviewState, reviewUser } from "./fixtures";
import { StudioWorkspace } from "./StudioWorkspace";
import { routeFromHash } from "./state";

export default function ReviewWorkspace() {
  if (!import.meta.env.DEV) return null;
  const data = createReviewData();
  const state = createReviewState(routeFromHash(window.location.hash));
  if (["replica", "replacement"].includes(state.page)) {
    data.projects = [
      {
        id: "review-replica-project",
        owner_user_id: reviewUser.id,
        name: "张工 · 设计室讲解",
        status: "ACTIVE",
        reference_asset_id: "zhang-studio",
        reference_upload_status: "READY",
        analysis_status: "READY",
      },
    ];
    state.draft.projectId = "review-replica-project";
    state.draft.sourceId = "zhang-studio";
    state.draft.sourceAssetId = "zhang-studio";
    state.draft.imageId = "zhang-courtyard";
    state.draft.firstFrameId = "zhang-courtyard";
    state.draft.duration = 15;
    state.draft.script = {
      ...state.draft.script,
      title: "张工 · 庭院采光讲解",
      original:
        "农村建房看采光，别只看窗户大小。庭院、客厅和楼梯间要一起规划。",
      text: "农村建房看采光，庭院、客厅和楼梯间要一起规划，住起来才更舒服。",
      confirmed: true,
    };
  }
  return (
    <StudioWorkspace
      currentUser={reviewUser}
      reviewData={data}
      initialState={state}
    />
  );
}
