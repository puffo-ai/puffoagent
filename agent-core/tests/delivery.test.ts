import assert from "node:assert/strict";
import test from "node:test";
import { normalizeReply } from "../src/messaging/delivery.js";
import { requiresVisibleReply } from "../src/messaging/reply.js";

test("normalizeReply returns a visible fallback for empty must-respond replies", () => {
  const body = normalizeReply({ kind: "reply", body: "   " }, true);

  assert.match(body ?? "", /did not produce a response/);
  assert.equal(requiresVisibleReply({ kind: "reply", body: "   " }, true), true);
});

test("normalizeReply preserves silent output only when a visible reply is not required", () => {
  assert.equal(normalizeReply({ kind: "silent" }, false), undefined);
  assert.equal(requiresVisibleReply({ kind: "silent" }, false), false);

  const body = normalizeReply({ kind: "silent" }, true);
  assert.match(body ?? "", /did not produce a response/);
  assert.equal(requiresVisibleReply({ kind: "silent" }, true), true);
});

test("normalizeReply redacts provider errors before visible delivery", () => {
  const body = normalizeReply({ kind: "error", error: "failed token=secret-value" }, true);

  assert.equal(body, "Agent error: failed token=[redacted]");
  assert.equal(requiresVisibleReply({ kind: "error", error: "failed token=secret-value" }, false), true);
});
