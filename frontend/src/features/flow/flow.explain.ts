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

/** Keep developer detail useful: remove Flow DOM dumps while retaining code and cause. */
export function technicalFlowDetail(message: string | null | undefined): string {
  const raw = String(message || "").trim();
  if (!raw) return "";
  const code = codeOf(raw);
  // Only remove repetitions of the outer code; keep distinct nested causes.
  // codeOf returns a fixed identifier containing letters, digits and underscores.
  const repeatedCode = new RegExp(`^(?:${code}:\\s*)+`, "i");
  const detail = raw
    .replace(repeatedCode, "")
    .replace(/^(?:warning|error|info)\s+(?:không thành công|failed|error|success)\s+/i, "")
    .replace(/\s+/g, " ").trim();
  // Keep the original cause for copy/paste; only collapse obvious UI dumps and cap size.
  const compact = detail.length > 1200 ? `${detail.slice(0, 1200)} …[truncated]` : detail;
  return `${code}: ${compact}`;
}

type LocaleFn = (vi: string, en: string) => string;

const STAGE_LABELS: Record<string, [string, string]> = {
  queued: ["Đang chờ", "Queued"],
  starting: ["Đang khởi động", "Starting"],
  preparing: ["Đang chuẩn bị UI Flow", "Preparing Flow UI"],
  submitting: ["Đang gửi yêu cầu", "Submitting"],
  resubmitting: ["Đang gửi lại", "Resubmitting"],
  generating: ["Flow đang tạo", "Generating"],
  recovering: ["Đang tìm kết quả đã gửi", "Recovering submitted result"],
  recovering_session: ["Đang khôi phục phiên", "Recovering session"],
  waiting_session_reset: ["Đang chờ khôi phục phiên", "Waiting for session recovery"],
  reconciling: ["Đang đối soát kết quả", "Reconciling result"],
  downloading: ["Đang tải file", "Downloading"],
  removing_logo: ["Đang xoá logo", "Removing logo"],
  retrying: ["Đang thử lại", "Retrying"],
  waiting_network: ["Đang chờ mạng", "Waiting for network"],
  model_fallback: ["Đang chuyển sang Nano Banana 2.1", "Switching to Nano Banana 2.1"],
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
  // Prefer the leaf login code when nested FLOW_LOGIN_REQUIRED: … wraps it.
  if (/FLOW_LOGIN_VERIFICATION_REQUIRED/i.test(text)) return "FLOW_LOGIN_VERIFICATION_REQUIRED";
  if (/FLOW_LOGIN_PASSWORD_REQUIRED/i.test(text)) return "FLOW_LOGIN_PASSWORD_REQUIRED";
  if (/FLOW_COOKIE_EXPIRED/i.test(text)) return "FLOW_COOKIE_EXPIRED";
  const flow = text.match(/^(FLOW_[A-Z0-9_]+)/);
  if (flow) return flow[1];
  if (/batchGenerateImages/i.test(text)) return "FLOW_IMAGE_RPC_TIMEOUT";
  if (/batchAsyncGenerateVideo/i.test(text)) return "FLOW_VIDEO_RPC_TIMEOUT";
  if (/Timeout \d+ms exceeded/i.test(text) && /8\s*(?:s|sec|giây)/i.test(text)) {
    return "FLOW_DURATION_CONTROL_TIMEOUT";
  }
  if (/Timeout \d+ms exceeded/i.test(text)) return "FLOW_UI_TIMEOUT";
  if (/LOGIN_REQUIRED|SESSION_EXPIRED|recaptcha|not signed in|flow account was not found|account not found/i.test(text)) {
    return "FLOW_SESSION_EXPIRED";
  }
  return text ? "FLOW_ERROR" : "";
}

/** Map raw backend/job errors to a short title, cause, and next step. */
export function explainFlowError(message: string): FlowExplain {
  const raw = String(message || "").trim();
  const code = codeOf(raw);
  // Strip repeated CODE: prefixes (nested FLOW_LOGIN_REQUIRED: …) for display.
  const detail = raw.replace(/^(?:FLOW_[A-Z0-9_]+:\s*)+/i, "").trim() || raw;

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

  if (code === "FLOW_COOKIE_EXPIRED") {
    return {
      code,
      titleVi: "Chrome đang yêu cầu đăng nhập Google",
      titleEn: "Chrome requires Google sign-in",
      summaryVi: "Phiên Chrome này chưa vào được Flow. Tài khoản và cookie đã lưu vẫn được giữ.",
      summaryEn: "This Chrome session could not access Flow. The account and saved cookies are kept.",
      actionVi: "Trong Tài khoản, bấm Kết nối hoặc Mở phiên để kiểm tra và hoàn tất đăng nhập Google nếu được yêu cầu.",
      actionEn: "In Accounts, select Connect or Open session to check and complete Google sign-in if requested.",
    };
  }

  if (code === "FLOW_SESSION_EXPIRED") {
    return make(
      code,
      "Phiên Flow đã đăng xuất; hệ thống đang thử đăng nhập lại bằng cookie đã lưu.",
      "The Flow session signed out; the app is signing in again with the saved cookie.",
    );
  }

  if (code === "FLOW_ACCOUNT_NOT_FOUND") {
    return make(
      code,
      "Tài khoản Flow đã bị xóa khỏi danh sách; chọn lại tài khoản còn tồn tại.",
      "The Flow account row was deleted; choose an account that still exists.",
    );
  }

  if (code === "FLOW_LOGO_REMOVE_FAILED") {
    return {
      code,
      titleVi: "Xoá logo thất bại",
      titleEn: "Logo removal failed",
      summaryVi: detail,
      summaryEn: detail,
      actionVi: "Ảnh đã tải xong. Bấm Chạy lại chỉ để xoá logo, không tạo ảnh mới.",
      actionEn: "The image is already downloaded. Retry only redoes logo removal and does not generate a new image.",
    };
  }

  if (code === "FLOW_COOKIE_SESSION_UNVERIFIED") {
    return {
      code,
      titleVi: "Chưa xác minh được phiên Cookie",
      titleEn: "Cookie session could not be verified",
      summaryVi: "Không kết luận Cookie hết hạn khi phiên Chrome vẫn có thể đang hoạt động.",
      summaryEn: "Cookie expiry is not confirmed while the Chrome session may still be active.",
      actionVi: "Giữ nguyên Cookie, đóng phiên trùng nếu có rồi mở lại phiên account để kiểm tra.",
      actionEn: "Keep the Cookie, close any duplicate session, then reopen the account session to check it.",
    };
  }

  if (code === "FLOW_LOGIN_PASSWORD_REQUIRED" || code === "FLOW_LOGIN_VERIFICATION_REQUIRED") {
    return {
      code,
      titleVi: code === "FLOW_LOGIN_PASSWORD_REQUIRED" ? "Cần mật khẩu Google" : "Chưa hoàn tất đăng nhập Google",
      titleEn: code === "FLOW_LOGIN_PASSWORD_REQUIRED" ? "Google password required" : "Google sign-in is incomplete",
      summaryVi: "", summaryEn: "",
      actionVi: code === "FLOW_LOGIN_PASSWORD_REQUIRED"
        ? "Sửa tài khoản, nhập lại Password rồi bấm Lưu và kết nối. Mật khẩu không được lưu sau khi khởi động lại."
        : "Trong tab Accounts, bấm Kết nối để mở Chrome trên máy backend (VPS cần desktop/VNC) và hoàn tất đăng nhập Google bằng Chrome; chỉ nhập mật khẩu nếu Google yêu cầu.",
      actionEn: code === "FLOW_LOGIN_PASSWORD_REQUIRED"
        ? "Edit the account, enter the password, then select Save and connect. Passwords do not survive a restart."
        : "In Accounts, select Connect to open Chrome on the backend host (a VPS needs desktop/VNC) and complete Google sign-in; enter a password only if Google requests it.",
    };
  }

  if (/ERR_INTERNET_DISCONNECTED|ERR_CONNECTION_(?:RESET|TIMED_OUT)|ERR_NETWORK_CHANGED|FLOW_NETWORK_UNAVAILABLE|Page\.goto|Navigation timeout|net::ERR_/i.test(raw)
    && !/flow-content\.google|fifeUrl|FLOW_DOWNLOAD/i.test(raw)) {
    return make(
      "FLOW_NETWORK_DISCONNECTED",
      "Mạng/Chrome chưa vào được Flow. App đang chờ mạng rồi tự chạy tiếp (có thể Huỷ). Kiểm tra Wi‑Fi/VPN nếu chờ lâu.",
      "Network/Chrome could not reach Flow. The app waits for the network and resumes automatically (you can Cancel). Check Wi‑Fi/VPN if it waits too long.",
    );
  }

  // CDN/TLS blips while fetching generated media (ECONNRESET on flow-content.google).
  if (
    code === "FLOW_DOWNLOAD_FAILED"
    || code === "FLOW_DOWNLOAD_URL_MISSING"
    || /ECONNRESET|secure TLS connection|socket disconnected before secure TLS|flow-content\.google/i.test(raw)
  ) {
    return {
      code: code === "FLOW_DOWNLOAD_URL_MISSING" ? code : "FLOW_DOWNLOAD_FAILED",
      titleVi: "Đã tạo media, tải file thất bại",
      titleEn: "Media generated, download failed",
      summaryVi: "", summaryEn: "",
      actionVi: "Kết quả trên Flow được giữ lại. Chạy lại sẽ thử tải file lần nữa, không tạo media mới.",
      actionEn: "The result is preserved on Flow. Retry will download the file again without generating new media.",
    };
  }
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
      "Flow báo lỗi tạo media. Xem chi tiết lỗi rồi thử lại.",
      "Flow reported a media generation error. Check the error details and retry.",
    );
  }
  if (/already reported for this account/i.test(raw)) {
    return make(
      "FLOW_AUTOMATION_BLOCKED",
      "Job chưa được gửi lên Flow. Đây là lần chặn trước của tài khoản đó, không phải lần tạo mới. Chọn tài khoản khác rồi chạy lại.",
      "This job never reached Flow. The message is that account's previous block, not a new generation. Pick another account and retry.",
    );
  }
  if (code === "FLOW_AUTOMATION_BLOCKED" || /FLOW_AUTOMATION_BLOCKED/i.test(raw)) {
    return make(
      "FLOW_AUTOMATION_BLOCKED",
      "App reset phiên, kết nối lại, rồi chạy lại job sau 3 giây. Nếu vẫn lỗi: tự chuyển sang tài khoản khác.",
      "The app resets the session, reconnects, then retries the job after 3 seconds. If it still fails: it switches to another account.",
    );
  }
  if (code === "FLOW_GENERATION_TIMEOUT") {
    return make(
      code,
      "Flow chưa trả media sau khi gửi. App sẽ khôi phục kết quả đã gửi trước (không tạo mới ngay). Chạy lại cũng recover trước.",
      "Flow has not returned media after submit yet. The app recovers the existing submission first (no immediate regenerate). Retry also recovers first.",
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
    download_retry: ["Đang thử tải lại kết quả đã tạo", "Retrying download of generated media"],
    output_downloaded: ["Đã tải file output", "Output downloaded"],
    job_completed: ["Job hoàn thành", "Job completed"],
    job_cancel_requested: ["Đã yêu cầu hủy job", "Cancellation requested"],
    job_cancelled: ["Job đã hủy", "Job cancelled"],
    account_fallback: ["Đã chuyển sang tài khoản khác", "Switched to another account"],
    account_suspended: ["Tài khoản đã bị tạm cách ly", "Account temporarily quarantined"],
    account_unsuspended: ["Đã gỡ cách ly tài khoản", "Account suspension cleared"],
    job_retry: ["Đưa job vào chạy lại", "Job queued for retry"],
    job_auto_retry: ["Tự chạy lại job bằng Chrome mới", "Auto-retrying job in a fresh Chrome"],
    unusual_activity_session_reset: ["Reset phiên Flow vì hoạt động bất thường", "Resetting Flow session after unusual activity"],
    unusual_activity_reconnected: ["Đã kết nối lại sau reset phiên", "Reconnected after session reset"],
    unusual_activity_retry: ["Chạy lại job sau reset phiên", "Retrying job after session reset"],
    unusual_activity_session_reset_failed: ["Reset phiên thất bại", "Session reset failed"],
    session_cookie_check: ["Đang kiểm tra đăng nhập Flow", "Checking Flow sign-in"],
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

/**
 * Pro image quota is an automatic model fallback. Keep this separate from
 * the generic limit classifier so image fallback errors stay inline.
 */
export function isProImageQuotaError(
  kind: string | null | undefined,
  model: string | null | undefined,
  raw: string | null | undefined,
): boolean {
  return String(kind || "").trim().toLowerCase() === "image"
    && String(model || "").trim().toLowerCase() === "nano banana pro"
    && isAutomaticImageQuotaError(kind, raw);
}

/** Image quota is handled by model/account fallback; do not interrupt with a popup. */
export function isAutomaticImageQuotaError(
  kind: string | null | undefined,
  raw: string | null | undefined,
): boolean {
  return String(kind || "").trim().toLowerCase() === "image"
    && explainFlowError(String(raw || "")).code === "FLOW_QUOTA_EXHAUSTED";
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
