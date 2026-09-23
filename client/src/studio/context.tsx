import { createContext, useContext } from "react";
import type { StudioContextValue } from "./types";

export const StudioContext = createContext<StudioContextValue | null>(null);
export function useStudio(): StudioContextValue {
  const value = useContext(StudioContext);
  if (!value) throw new Error("Studio 页面必须位于 StudioContext 内");
  return value;
}
