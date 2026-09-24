import { setup } from "./setup-backend.mjs";

/** Playwright globalSetup: provision the dedicated PG database, migrate it,
 * seed activation codes, then start the API and the client dev server. */
export default async function globalSetup() {
  await setup();
}
