/** Human-readable Flow log/error explanations (VI + EN) for the Logs tab and queue. */

export type FlowExplain = {
  titleVi: string;
  titleEn: string;
  summaryVi: string;
  summaryEn: string;
  actionVi: string;
  actionEn: string;
  code: string;
};

type LocaleFn = (vi: string, en: string) => string;

const STAGE_LABELS: Record<string, [string, string]> = {
  queued: ["Đang chờ", "Queued"],
  starting: ["Đang khởi động", "Starting"],
  preparing: ["Đang chuẩn bị UI Flow", "Preparing Flow UI"],
  submitting: ["Đang gửi yêu cầu", "Submitting"],
  resubmitting: ["Đang gửi lại", "Resubmitting"],
  generating: ["Flow đang tạo", "Generating"],
  recovering: ["Đang tìm kết quả đã gửi", "Recovering submitted result"],
  downloading: ["Đang tải file", "Downloading"],
  retrying: ["Đang thử lại", "Retrying"],
  failed: ["Thất bại", "Failed"],
  cancelled: ["Đã hủy", "Cancelled"],
  action_required: ["Cần thao tác", "Action required"],
  done: ["Hoàn thành", "Done"],
  profile: ["Đang mở Chrome profile", "Opening Chrome profile"],
};

export function flowStageLabel(stage: string, t: LocaleFn): string {
  const key = String(stage || "").trim();
  const pair = STAGE_LABELS[key];
  return pair ? t(pair[0], pair[1]) : key || t("không rõ", "unknown");
}

function codeOf(message: string): string {
  const text = String(message || "").trim();
  const flow = text.match(/^(FLOW_[A-Z0-9_]+)/);
  if (flow) return flow[1];
  if (/batchGenerateImages/i.test(text)) return "FLOW_IMAGE_RPC_TIMEOUT";
  if (/batchAsyncGenerateVideo/i.test(text)) return "FLOW_VIDEO_RPC_TIMEOUT";
  if (/Timeout \d+ms exceeded/i.test(text) && /8\s*(?:s|sec|giây)/i.test(text)) {
    return "FLOW_DURATION_CONTROL_TIMEOUT";
  }
  if (/Timeout \d+ms exceeded/i.test(text)) return "FLOW_UI_TIMEOUT";
  if (/LOGIN_REQUIRED|SESSION_EXPIRED|recaptcha|not signed in/i.test(text)) {
    return "FLOW_SESSION_EXPIRED";
  }
  return text ? "FLOW_ERROR" : "";
}

/** Map raw backend/job errors to a short title, cause, and next step. */
export function explainFlowError(message: string): FlowExplain {
  const raw = String(message || "").trim();
  const code = codeOf(raw);

  if (code === "FLOW_RESULT_NOT_FOUND") {
    return {
      code,
      titleVi: "Không tìm thấy kết quả đã gửi",
      titleEn: "Submitted result not found",
      summaryVi:
        "Job vào bước phục hồi nhưng Flow không còn media đang tạo hay đã xong cho lần gửi này (có thể bị reject, đổi project, hoặc mất tile).",
      summaryEn:
        "Recovery ran, but Flow has no pending or finished media for this submission (reject, project switch, or missing tile).",
      actionVi: "Bấm Chạy lại để gửi yêu cầu mới. Nếu lặp lại: Đồng bộ tài khoản rồi thử lại.",
      actionEn: "Click Retry to submit a new request. If it repeats: Sync the account, then retry.",
    };
  }
  if (code === "FLOW_IMAGE_RPC_TIMEOUT") {
    return {
      code,
      titleVi: "Timeout khi chờ Flow tạo ảnh",
      titleEn: "Timed out waiting for image generation",
      summaryVi:
        "Đã bấm tạo ảnh nhưng trong thời gian chờ không bắt được phản hồi API batchGenerateImages (Captured so far rỗng = không thấy RPC).",
      summaryEn:
        "Image create was clicked, but batchGenerateImages was not captured in time (empty Captured so far means no RPC seen).",
      actionVi:
        "Chạy lại job. Kiểm tra Chrome Flow còn đăng nhập, credits còn, và model ảnh còn dùng được trên gói tài khoản.",
      actionEn:
        "Retry the job. Confirm Chrome Flow is signed in, credits remain, and the image model is allowed on this plan.",
    };
  }
  if (code === "FLOW_VIDEO_RPC_TIMEOUT") {
    return {
      code,
      titleVi: "Timeout khi chờ Flow tạo video",
      titleEn: "Timed out waiting for video generation",
      summaryVi:
        "Đã gửi tạo video nhưng không bắt được phản hồi API video trong thời gian chờ.",
      summaryEn:
        "Video generation was submitted, but the video RPC was not captured before the timeout.",
      actionVi: "Chạy lại. Nếu lặp: đồng bộ tài khoản / mở lại session Flow trong Chrome.",
      actionEn: "Retry. If it repeats: sync the account / reopen the Flow session in Chrome.",
    };
  }
  if (code === "FLOW_DURATION_CONTROL_TIMEOUT" || /invalid duration|duration .* was not/i.test(raw)) {
    return {
      code: code === "FLOW_ERROR" ? "FLOW_DURATION_MISMATCH" : code,
      titleVi: "Lỗi chọn thời lượng trên Flow",
      titleEn: "Flow duration control error",
      summaryVi:
        "Worker cố chọn thời lượng (ví dụ 8s) nhưng model/gói này không có control đó trên UI Flow (Veo thường không có tab 4/6/8/10s).",
      summaryEn:
        "The worker tried to pick a duration (e.g. 8s) that this model/plan does not expose in Flow UI (Veo often has no 4/6/8/10s tabs).",
      actionVi: "Cập nhật app / chạy lại với model đúng. Veo: để Flow dùng thời lượng mặc định; Omni Flash mới có chọn giây.",
      actionEn: "Update the app / retry with the right model. Veo uses Flow’s default length; only Omni Flash has duration radios.",
    };
  }
  if (code === "FLOW_SETTING_MISMATCH" || /FLOW_SETTING_MISMATCH/i.test(raw)) {
    return {
      code: "FLOW_SETTING_MISMATCH",
      titleVi: "Cài đặt không khớp UI Flow",
      titleEn: "Settings do not match Flow UI",
      summaryVi: raw.replace(/^FLOW_SETTING_MISMATCH:\s*/i, "") || "Tỷ lệ / thời lượng / độ phân giải không chọn được trên Flow.",
      summaryEn: raw.replace(/^FLOW_SETTING_MISMATCH:\s*/i, "") || "Ratio / duration / resolution could not be selected in Flow.",
      actionVi: "Đồng bộ catalog tài khoản (Sync), chọn lại model/tỷ lệ theo gói Free·Pro·Ultra, rồi chạy lại.",
      actionEn: "Sync the account catalog, pick model/ratio allowed for Free·Pro·Ultra, then retry.",
    };
  }
  if (code === "FLOW_EMPTY_OUTPUT" || code === "FLOW_OUTPUT_MISSING" || code === "FLOW_OUTPUT_EMPTY") {
    return {
      code,
      titleVi: "Không có file output",
      titleEn: "No output file",
      summaryVi: "Flow báo xong nhưng không tải được file ảnh/video về máy.",
      summaryEn: "Flow finished, but no image/video file was downloaded.",
      actionVi: "Chạy lại job. Kiểm tra thư mục output còn ghi được.",
      actionEn: "Retry the job. Check the output folder is writable.",
    };
  }
  if (code === "FLOW_GENERATION_REJECTED") {
    return {
      code,
      titleVi: "Flow từ chối nội dung",
      titleEn: "Flow rejected the content",
      summaryVi: "Flow không tạo được nội dung này và thường không trừ credits.",
      summaryEn: "Flow could not generate this content and usually does not charge credits.",
      actionVi: "Sửa prompt / bỏ ảnh tham chiếu nhạy cảm, rồi chạy lại.",
      actionEn: "Edit the prompt / remove sensitive references, then retry.",
    };
  }
  if (code === "FLOW_GENERATION_TIMEOUT") {
    return {
      code,
      titleVi: "Timeout chờ kết quả Flow",
      titleEn: "Timed out waiting for Flow result",
      summaryVi: "Đã gửi nhưng kết quả chưa sẵn sàng trong thời gian chờ.",
      summaryEn: "Submitted, but the result was not ready before the timeout.",
      actionVi: "Chạy lại (recovery có thể lấy media nếu Flow đã tạo xong sau đó).",
      actionEn: "Retry (recovery may pick up media if Flow finished later).",
    };
  }
  if (code === "FLOW_SESSION_EXPIRED" || code === "FLOW_LOGIN_REQUIRED") {
    return {
      code,
      titleVi: "Phiên Flow hết hạn",
      titleEn: "Flow session expired",
      summaryVi: "Chrome profile mất đăng nhập Google Flow.",
      summaryEn: "The Chrome profile lost the Google Flow sign-in.",
      actionVi: "Vào Tài khoản → Kết nối lại, đăng nhập Google, rồi chạy lại job.",
      actionEn: "Open Accounts → Reconnect, sign in to Google, then retry the job.",
    };
  }
  if (code === "FLOW_MODEL_UNAVAILABLE") {
    return {
      code,
      titleVi: "Model không có trên tài khoản",
      titleEn: "Model unavailable on this account",
      summaryVi: raw.replace(/^FLOW_MODEL_UNAVAILABLE:\s*/i, "") || "Model không nằm trong danh sách UI Flow của gói này.",
      summaryEn: raw.replace(/^FLOW_MODEL_UNAVAILABLE:\s*/i, "") || "The model is not in this plan’s Flow UI list.",
      actionVi: "Đồng bộ tài khoản và chọn model còn hiện trên gói Free/Pro/Ultra.",
      actionEn: "Sync the account and pick a model still shown for Free/Pro/Ultra.",
    };
  }
  if (code === "FLOW_UI_TIMEOUT") {
    return {
      code,
      titleVi: "Timeout thao tác UI Flow",
      titleEn: "Flow UI action timed out",
      summaryVi: "Playwright chờ một nút/tab trên trang Flow quá lâu (UI đổi hoặc panel chưa mở).",
      summaryEn: "Playwright waited too long for a Flow control (UI changed or settings panel closed).",
      actionVi: "Chạy lại. Nếu lặp: Đồng bộ tài khoản để cập nhật catalog control.",
      actionEn: "Retry. If it repeats: Sync the account to refresh the control catalog.",
    };
  }
  if (/UnicodeDecodeError|invalid start byte/i.test(raw)) {
    return {
      code: "BUILD_ENCODING",
      titleVi: "Lỗi encoding khi đóng gói",
      titleEn: "Packaging encoding error",
      summaryVi: "File nguồn không phải UTF-8 (thường launcher.py bị corrupt).",
      summaryEn: "A source file is not valid UTF-8 (often a corrupted launcher.py).",
      actionVi: "Cần bản build mới đã sửa encoding.",
      actionEn: "Need a new build with the encoding fix.",
    };
  }

  return {
    code: code || "FLOW_ERROR",
    titleVi: "Job gặp lỗi",
    titleEn: "Job failed",
    summaryVi: raw || "Không có chi tiết lỗi.",
    summaryEn: raw || "No error detail.",
    actionVi: "Chạy lại. Nếu vẫn lỗi, sao chép log gửi để kiểm tra.",
    actionEn: "Retry. If it persists, copy the log for debugging.",
  };
}

export function explainFlowEvent(event: string): { titleVi: string; titleEn: string } {
  const map: Record<string, [string, string]> = {
    account_connecting: ["Đang kết nối tài khoản", "Connecting account"],
    account_connected: ["Đã kết nối tài khoản", "Account connected"],
    account_connect_failed: ["Kết nối tài khoản thất bại", "Account connection failed"],
    job_queued: ["Đã thêm vào hàng đợi", "Added to queue"],
    job_started: ["Bắt đầu xử lý job", "Job processing started"],
    browser_ready: ["Chrome session sẵn sàng", "Browser session ready"],
    generation_submitted: ["Đã gửi yêu cầu tạo lên Flow", "Generation submitted to Flow"],
    api_generation_submitted: ["Đã bắt được RPC tạo media", "Generation RPC captured"],
    api_generation_complete: ["RPC báo tạo xong", "Generation RPC completed"],
    poll_generation_complete: ["Poll project thấy media xong", "Project poll found finished media"],
    ui_generation_fallback: ["Fallback chờ UI (không bắt được RPC)", "UI wait fallback (RPC not captured)"],
    output_downloaded: ["Đã tải file output", "Output downloaded"],
    job_completed: ["Job hoàn thành", "Job completed"],
    job_cancel_requested: ["Đã yêu cầu hủy job", "Cancellation requested"],
    job_cancelled: ["Job đã hủy", "Job cancelled"],
    job_retry: ["Đưa job vào chạy lại", "Job queued for retry"],
    job_failed: ["Job thất bại", "Job failed"],
    capability_settings_migrated: ["Đã chỉnh settings theo catalog tài khoản", "Settings migrated to account catalog"],
    late_worker_error_ignored: ["Bỏ qua lỗi worker muộn (job đã xong)", "Ignored late worker error (job already done)"],
  };
  const pair = map[event];
  return pair
    ? { titleVi: pair[0], titleEn: pair[1] }
    : { titleVi: event, titleEn: event };
}

export function formatFlowExplain(
  explain: FlowExplain,
  t: LocaleFn,
): { title: string; summary: string; action: string } {
  return {
    title: t(explain.titleVi, explain.titleEn),
    summary: t(explain.summaryVi, explain.summaryEn),
    action: t(explain.actionVi, explain.actionEn),
  };
}
