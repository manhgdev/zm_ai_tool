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
  model_fallback: ["Đang chuyển sang Nano Banana 2", "Switching to Nano Banana 2"],
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
  // Strip the leading CODE: prefix to get the human-readable detail.
  const detail = raw.replace(/^FLOW_[A-Z0-9_]+:\s*/i, "").trim() || raw;

  /** Helper: build result with raw detail as title, no summary, custom action. */
  const make = (
    resolvedCode: string,
    actionVi: string,
    actionEn: string,
  ): FlowExplain => ({
    code: resolvedCode,
    titleVi: ({ FLOW_QUOTA_EXHAUSTED: "Đã đạt hạn mức sử dụng", FLOW_AUTOMATION_BLOCKED: "Flow phát hiện hoạt động bất thường", FLOW_CREDITS_EMPTY: "Tài khoản hết tín dụng" } as Record<string, string>)[resolvedCode] || detail,
    titleEn: ({ FLOW_QUOTA_EXHAUSTED: "Usage quota reached", FLOW_AUTOMATION_BLOCKED: "Flow detected unusual activity", FLOW_CREDITS_EMPTY: "Account has no credits" } as Record<string, string>)[resolvedCode] || detail,
    summaryVi: "",
    summaryEn: "",
    actionVi,
    actionEn,
  });

  if (code === "FLOW_RESULT_NOT_FOUND") {
    return make(
      code,
      "Flow sẽ tự gửi một yêu cầu mới. Nếu vẫn lặp lại sau các lần tự thử: Đồng bộ tài khoản rồi thử lại.",
      "Flow will automatically submit a new request. If it repeats after the automatic attempts: sync the account and retry.",
    );
  }
  if (code === "FLOW_IMAGE_RPC_TIMEOUT") {
    return make(
      code,
      "Chạy lại job. Kiểm tra Chrome Flow còn đăng nhập, credits còn, và model ảnh còn dùng được trên gói tài khoản.",
      "Retry the job. Confirm Chrome Flow is signed in, credits remain, and the image model is allowed on this plan.",
    );
  }
  if (code === "FLOW_VIDEO_RPC_TIMEOUT") {
    return make(
      code,
      "Chạy lại. Nếu lặp: đồng bộ tài khoản / mở lại session Flow trong Chrome.",
      "Retry. If it repeats: sync the account / reopen the Flow session in Chrome.",
    );
  }
  if (code === "FLOW_DURATION_CONTROL_TIMEOUT" || /invalid duration|duration .* was not/i.test(raw)) {
    const isOmni = /omni(?:\s+1\.1)?\s+flash/i.test(raw);
    return make(
      code === "FLOW_ERROR" ? "FLOW_DURATION_MISMATCH" : code,
      isOmni
        ? "Cập nhật app, đồng bộ tài khoản Flow rồi chạy lại; không tự đổi sang 8s."
        : "Cập nhật app / chạy lại với model đúng. Veo: để Flow dùng thời lượng mặc định; Omni Flash mới có chọn giây.",
      isOmni
        ? "Update the app, sync the Flow account, then retry; it will not silently switch to 8s."
        : "Update the app / retry with the right model. Veo uses Flow's default length; only Omni Flash has duration radios.",
    );
  }
  if (code === "FLOW_SETTING_MISMATCH" || /FLOW_SETTING_MISMATCH/i.test(raw)) {
    return make(
      "FLOW_SETTING_MISMATCH",
      "Đồng bộ catalog tài khoản (Sync), chọn lại model/tỷ lệ theo gói Free·Pro·Ultra, rồi chạy lại.",
      "Sync the account catalog, pick model/ratio allowed for Free·Pro·Ultra, then retry.",
    );
  }
  if (code === "FLOW_EMPTY_OUTPUT" || code === "FLOW_OUTPUT_MISSING" || code === "FLOW_OUTPUT_EMPTY") {
    return make(
      code,
      "Chạy lại job. Kiểm tra thư mục output còn ghi được.",
      "Retry the job. Check the output folder is writable.",
    );
  }
  if (code === "FLOW_GENERATION_REJECTED") {
    if (/abnormal activity|hoạt động bất thường|vui lòng chờ vài giây|please wait a few seconds/i.test(raw)) {
      return make(
        code,
        "Chờ vài giây rồi chạy lại; nếu còn lặp, giảm số job đồng thời hoặc mở Trung tâm trợ giúp Flow.",
        "Wait a few seconds and retry; if it repeats, reduce concurrent jobs or open the Flow Help Center.",
      );
    }
    return make(
      code,
      "Sửa prompt / bỏ ảnh tham chiếu nhạy cảm, rồi chạy lại.",
      "Edit the prompt / remove sensitive references, then retry.",
    );
  }
  if (code === "FLOW_AUTOMATION_BLOCKED" || /FLOW_AUTOMATION_BLOCKED/i.test(raw)) {
    return make(
      "FLOW_AUTOMATION_BLOCKED",
      "Không tự chạy lại liên tục. Chờ vài giây, giảm số job đồng thời và chạy lại thủ công; nếu còn lặp, dùng trực tiếp trên Flow.",
      "Do not retry repeatedly. Wait a few seconds, reduce concurrent jobs, and retry manually; if it persists, use Flow directly.",
    );
  }
  if (code === "FLOW_GENERATION_TIMEOUT") {
    return make(
      code,
      "Chạy lại (recovery có thể lấy media nếu Flow đã tạo xong sau đó).",
      "Retry (recovery may pick up media if Flow finished later).",
    );
  }
  if (code === "FLOW_CREDITS_INSUFFICIENT" || /FLOW_CREDITS_INSUFFICIENT|insufficient credits|not enough credits/i.test(raw)) {
    return make(
      "FLOW_CREDITS_INSUFFICIENT",
      "Đồng bộ credits, chọn model rẻ hơn hoặc chờ credits được nạp lại rồi tạo lại.",
      "Sync credits, choose a cheaper model, or wait for credits to return before creating again.",
    );
  }
  if (code === "FLOW_CREDITS_EMPTY" || /FLOW_CREDITS_EMPTY|hết tín dụng|out of flow credits/i.test(raw)) {
    return make(
      code === "FLOW_ERROR" ? "FLOW_CREDITS_EMPTY" : code,
      "Đồng bộ credits, đợi reset hàng ngày/tháng, hoặc dùng tài khoản còn dư tín dụng.",
      "Sync credits, wait for the daily/monthly reset, or use an account that still has credits.",
    );
  }
  if (
    code === "FLOW_QUOTA_EXHAUSTED"
    || /FLOW_QUOTA_EXHAUSTED|hết lượt|hết hạn mức|usage limit|generation limit|daily limit|monthly limit|rate.?limit|too many requests/i.test(raw)
  ) {
    return make(
      code === "FLOW_ERROR" ? "FLOW_QUOTA_EXHAUSTED" : code,
      "Đợi reset lượt, đổi tài khoản còn dư, hoặc giảm số lượng tạo đồng thời.",
      "Wait for the quota reset, switch to an account with remaining allowance, or lower concurrency.",
    );
  }
  if (code === "FLOW_SESSION_EXPIRED" || code === "FLOW_LOGIN_REQUIRED") {
    return make(
      code,
      "Vào Tài khoản → Kết nối lại, đăng nhập Google, rồi chạy lại job.",
      "Open Accounts → Reconnect, sign in to Google, then retry the job.",
    );
  }
  if (code === "FLOW_MODEL_UNAVAILABLE") {
    return make(
      code,
      "Đồng bộ tài khoản và chọn model còn hiện trên gói Free/Plus/Pro/Ultra.",
      "Sync the account and pick a model still shown for Free/Plus/Pro/Ultra.",
    );
  }
  if (code === "FLOW_UI_TIMEOUT") {
    return make(
      code,
      "Chạy lại. Nếu lặp: Đồng bộ tài khoản để cập nhật catalog control.",
      "Retry. If it repeats: Sync the account to refresh the control catalog.",
    );
  }
  if (/UnicodeDecodeError|invalid start byte/i.test(raw)) {
    return make(
      "BUILD_ENCODING",
      "Cần bản build mới đã sửa encoding.",
      "Need a new build with the encoding fix.",
    );
  }

  return make(
    code || "FLOW_ERROR",
    "Chạy lại. Nếu vẫn lỗi, sao chép log gửi để kiểm tra.",
    "Retry. If it persists, copy the log for debugging.",
  );
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

/** True when Flow blocked create due to credits or creation quota/rate limit. */
export function isFlowLimitError(raw: string | null | undefined): boolean {
  const text = String(raw || "");
  if (!text.trim()) return false;
  const explained = explainFlowError(text);
  if (explained.code === "FLOW_CREDITS_EMPTY" || explained.code === "FLOW_CREDITS_INSUFFICIENT" || explained.code === "FLOW_QUOTA_EXHAUSTED") {
    return true;
  }
  return /insufficient credits|not enough credits|out of credits|hết tín dụng|hết lượt|hết hạn mức|usage limit|generation limit|daily limit|monthly limit|quota|rate.?limit|too many requests/i.test(text);
}

/** Title + body for the Flow limit alert popup (VI/EN via ``t``). */
export function flowLimitPopupCopy(
  raw: string | null | undefined,
  t: LocaleFn,
): { title: string; message: string } {
  const explained = explainFlowError(String(raw || "FLOW_CREDITS_EMPTY"));
  const formatted = formatFlowExplain(
    explained.code === "FLOW_QUOTA_EXHAUSTED" || explained.code === "FLOW_CREDITS_EMPTY" || explained.code === "FLOW_CREDITS_INSUFFICIENT"
      ? explained
      : explainFlowError("FLOW_CREDITS_EMPTY"),
    t,
  );
  return {
    title: formatted.title,
    message: `${formatted.summary}\n\n${formatted.action}`,
  };
}
