import test from "node:test";
import assert from "node:assert/strict";
import { explainFlowError, explainFlowEvent } from "../frontend/src/features/flow/flow.explain.ts";

test("explains FLOW_RESULT_NOT_FOUND — raw detail as title, no summary, keeps action", () => {
  const explained = explainFlowError(
    "FLOW_RESULT_NOT_FOUND: Flow has no pending or completed result for this submission",
  );
  assert.equal(explained.code, "FLOW_RESULT_NOT_FOUND");
  // Raw technical detail surfaces as title, not a friendly label
  assert.match(explained.titleVi, /Flow has no pending/i);
  // No explanation paragraph
  assert.equal(explained.summaryVi, "");
  // Action is kept
  assert.match(explained.actionVi, /tự gửi/i);
});

test("explains empty batchGenerateImages timeout — raw detail as title, action kept", () => {
  const explained = explainFlowError(
    "Timed out (120s) waiting for batchGenerateImages. Captured so far: []",
  );
  assert.equal(explained.code, "FLOW_IMAGE_RPC_TIMEOUT");
  // Raw message surfaces as title
  assert.match(explained.titleVi, /batchGenerateImages/i);
  // No summary
  assert.equal(explained.summaryVi, "");
  // Action still present
  assert.match(explained.actionVi, /Chạy lại/i);
});

test("maps job_failed event title", () => {
  const event = explainFlowEvent("job_failed");
  assert.equal(event.titleEn, "Job failed");
});

test("distinguishes Flow unusual-activity blocks from content rejection — raw detail as title", () => {
  const explained = explainFlowError(
    "FLOW_GENERATION_REJECTED: Chúng tôi nhận thấy có hoạt động bất thường nào đó. Vui lòng chờ vài giây rồi thử lại.",
  );
  assert.equal(explained.code, "FLOW_GENERATION_REJECTED");
  // Raw detail in title (not friendly label)
  assert.match(explained.titleVi, /hoạt động bất thường/i);
  // No summary
  assert.equal(explained.summaryVi, "");
  // Action for unusual-activity path
  assert.match(explained.actionVi, /Chờ vài giây/i);
});
