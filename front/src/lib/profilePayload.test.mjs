import assert from "node:assert/strict";

import { profileWritePayload } from "./profilePayload.ts";

const fullProfile = {
  id: 42,
  name: "Fuclaude",
  site_key: "fuclaude",
  promo_code: "PROMO",
  invitation_code: "INVITE",
  aff_code: "AFF",
  enabled: true,
  register_url: "https://example.test/register",
  register_origin: "https://example.test",
  whitelist: ["example.com"],
  account_count: 3,
  key_count: 4,
  active_key_count: 2,
  in_use: true,
  checkin_supported: false,
  created_at: "2026-09-02T00:00:00Z",
  updated_at: "2026-09-02T00:00:00Z",
};

assert.deepEqual(profileWritePayload(fullProfile), {
  name: "Fuclaude",
  site_key: "fuclaude",
  promo_code: "PROMO",
  invitation_code: "INVITE",
  aff_code: "AFF",
  enabled: true,
});
