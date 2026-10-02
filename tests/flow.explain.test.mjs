import test from "node:test";
import assert from "node:assert/strict";
import { explainFlowError, explainFlowEvent, technicalFlowDetail } from "../frontend/src/features/flow/flow.explain.ts";

test("technical account diagnostics remove nested error-code and UI-status duplicates", () => {
  const detail = technicalFlowDetail(
    "FLOW_AUTOMATION_BLOCKED: FLOW_AUTOMATION_BLOCKED: warning Không thành công We noticed some unusual activity. Please visit the Help Center.",
  );
  assert.equal(detail, "FLOW_AUTOMATION_BLOCKED: We noticed some unusual activity. Please visit the Help Center.");
  assert.equal((detail.match(/FLOW_AUTOMATION_BLOCKED/g) || []).length, 1);
});

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

 test("technical detail preserves distinct nested codes and provider diagnostics", () => {
  const raw = "FLOW_WORKER_FAILED: FLOW_PLAN_SYNC_FAILED: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://flow.google.com/project/example";
  assert.equal(technicalFlowDetail(raw), raw);
  assert.equal(technicalFlowDetail("FLOW_QUOTA_EXHAUSTED: quota exceeded"), "FLOW_QUOTA_EXHAUSTED: quota exceeded");
  assert.equal(technicalFlowDetail(""), "");
  const repeated = "FLOW_AUTOMATION_BLOCKED: FLOW_AUTOMATION_BLOCKED: warning Không thành công We noticed some unusual activity.";
  assert.equal(technicalFlowDetail(technicalFlowDetail(repeated)), technicalFlowDetail(repeated));
});

test("download failures explain existing media without automatic generation retry", () => {
  const result = explainFlowError("FLOW_DOWNLOAD_FAILED: Locator.wait_for timeout");
  assert.equal(result.titleVi, "Đã tạo video, tải file thất bại");
  assert.equal(result.titleEn, "Video generated, download failed");
  assert.match(result.actionVi, /Kết quả trên Flow được giữ lại/);
  assert.match(result.actionEn, /result is preserved on Flow/);
});
