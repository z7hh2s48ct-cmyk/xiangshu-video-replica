import { teardown } from "./setup-backend.mjs";

/** Playwright globalTeardown: stop the API and client dev server. */
export default function globalTeardown() {
  teardown();
}
