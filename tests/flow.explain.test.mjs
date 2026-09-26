import test from "node:test";
import assert from "node:assert/strict";
import { explainFlowError, explainFlowEvent } from "../frontend/src/features/flow/flow.explain.ts";

test("explains FLOW_RESULT_NOT_FOUND with recovery action", () => {
  const explained = explainFlowError(
    "FLOW_RESULT_NOT_FOUND: Flow has no pending or completed result for this submission",
  );
  assert.equal(explained.code, "FLOW_RESULT_NOT_FOUND");
  assert.match(explained.actionVi, /Chạy lại/i);
});

test("explains empty batchGenerateImages timeout", () => {
  const explained = explainFlowError(
    "Timed out (120s) waiting for batchGenerateImages. Captured so far: []",
  );
  assert.equal(explained.code, "FLOW_IMAGE_RPC_TIMEOUT");
  assert.match(explained.summaryVi, /batchGenerateImages/i);
});

test("maps job_failed event title", () => {
  const event = explainFlowEvent("job_failed");
  assert.equal(event.titleEn, "Job failed");
});
