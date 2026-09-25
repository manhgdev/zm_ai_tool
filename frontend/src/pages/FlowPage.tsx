import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { localize, useLocale } from "@/app/i18n";
import {
  IconArrowRight,
  IconBatch,
  IconBook,
  IconChevronDown,
  IconClock,
  IconDownload,
  IconGear,
  IconPlay,
  IconRefresh,
  IconVideo,
} from "@/shared/components/Icons";
import { BackTitle } from "@/shared/components/BackTitle";
import { OutputFolderField } from "@/shared/components/OutputFolderField";
import { copyText } from "@/shared/lib/clipboard";
import FlowSeriesPanel, { type FlowSeriesSceneContext } from "./FlowSeriesPanel";
import { FlowTemplatesPanel } from "@/features/flow/FlowTemplatesPanel";
import {
  type FlowTab, type FlowRoutePanel, type RailItem, type JobStatus,
  type CreateKind, type ImageMode, type PromptInputType,
  type FlowJob, type FlowAccount, type FlowLog, type FlowSettings, type FlowModelCapability,
  type BrowserDirectoryHandle, type BrowserDirectoryWindow,
} from "@/features/flow/flow.types";
import {
  DRAFT_VIDEO_KEY, DRAFT_IMAGE_KEY, DRAFT_LEGACY_KEY, SETTINGS_KEY,
  WEB_AUTO_DOWNLOAD_DEFAULT_KEY, WEB_OUTPUT_ROOT_KEY, TAB_KEY, RAIL_KEY,
  ACCOUNTS_KEY, CREATE_KIND_KEY, ACTIVE_PANEL_KEY, IMAGE_MODE_KEY, COLLAPSED_FOLDERS_KEY,
  FLOW_VIDEO_MODELS, FLOW_IMAGE_MODELS, FLOW_OMNI_FLASH_DURATIONS,
  settingsForCreateKind, settingsWithSelectedModel,
  defaultFlowOutputFolder as buildDefaultFlowOutputFolder,
  flowConfiguredOutputFolder as buildFlowConfiguredOutputFolder,
  normalizeLegacyFlowOutputDir as normalizeFlowOutputDir,
  flowOutputParentPath,
  flowOutputMediaKind as detectFlowOutputMediaKind,
  flowOutputFolderName as sanitizeFlowOutputFolderName,
  flowOutputFolderParts as splitFlowOutputFolderParts,
  flowGroupProgress, readText, readSettings, readAccounts,
  flowRoutePanel, writeFlowRoutePanel,
  normalizeFlowJobs as normalizeFlowJobRows,
  normalizeFlowAccounts,
  selectedFlowAccount as resolveSelectedFlowAccount,
  flowRequest, loadFlowSnapshot,
  saveWebOutputRoot, loadWebOutputRoot,
  writeFlowOutputToDirectory, downloadFlowOutput, deleteFlowOutputFromDirectory,
} from "@/features/flow/flow.helpers";
import "./FlowPage.css";

// Keep page-level names stable for callers and static regression checks while
// the implementation remains in the shared Flow helpers.
function defaultFlowOutputFolder(now?: Date) {
  return buildDefaultFlowOutputFolder(now);
}

function normalizeLegacyFlowOutputDir(value: string) {
  return normalizeFlowOutputDir(value);
}

function flowConfiguredOutputFolder(value: string, kind: CreateKind) {
  const outputDir = normalizeLegacyFlowOutputDir(value);
  if (/^(?:[A-Za-z]:[\\/]|[\\/])/.test(outputDir)) return `${outputDir}/${kind}`;
  return buildFlowConfiguredOutputFolder(outputDir, kind);
}

function flowOutputMediaKind(output: string, fallback: CreateKind | "file") {
  return detectFlowOutputMediaKind(output, fallback as CreateKind);
}

function flowOutputFolderName(value: string) {
  return sanitizeFlowOutputFolderName(value);
}

function flowOutputFolderParts(value: string) {
  return splitFlowOutputFolderParts(value).map((part) => flowOutputFolderName(part));
}

function normalizeFlowJobs(rows: Array<Record<string, unknown>>, accounts: FlowAccount[]) {
  // raw.settings is normalized by the shared helper before queue rendering.
  return normalizeFlowJobRows(rows, accounts);
}

function selectedFlowAccount(accounts: FlowAccount[], accountLabel: string) {
  return accounts.find((account) => account.label === accountLabel)
    || accounts.find((account) => account.isDefault)
    || accounts.find((account) => account.status === "online")
    || resolveSelectedFlowAccount(accounts, accountLabel);
}

function accountCapabilityModels(account: FlowAccount | undefined, kind: CreateKind): FlowModelCapability[] {
  if (account?.capabilityStatus !== "verified") return [];
  return account.capabilityCatalog?.[kind]?.models || [];
}

function applyAccountCapabilities(
  account: FlowAccount | undefined,
  current: FlowSettings,
  kind: CreateKind,
  requestedModel: string,
): FlowSettings {
  const section = account?.capabilityStatus === "verified" ? account.capabilityCatalog?.[kind] : undefined;
  const models = section?.models || [];
  if (!models.length) return settingsWithSelectedModel(current, kind, requestedModel);
  const selected = models.find((item) => item.name === requestedModel)
    || models.find((item) => item.name === section?.defaultModel)
    || models[0];
  const ratioKey = kind === "image" ? "imageRatio" : "ratio";
  const ratio = selected.ratios.includes(current[ratioKey])
    ? current[ratioKey]
    : selected.defaultRatio || selected.ratios[0] || current[ratioKey];
  const duration = kind === "video" && selected.durations.length && !selected.durations.includes(current.duration)
    ? selected.defaultDuration || selected.durations[0]
    : current.duration;
  const resolution = selected.resolutions.length && !selected.resolutions.includes(current.resolution.toLowerCase())
    ? selected.defaultResolution || selected.resolutions[0]
    : current.resolution;
  const next = {
    ...settingsWithSelectedModel(current, kind, selected.name),
    [ratioKey]: ratio,
    duration,
    resolution,
  };
  return next.model === current.model
    && next.videoModel === current.videoModel
    && next.imageModel === current.imageModel
    && next.ratio === current.ratio
    && next.imageRatio === current.imageRatio
    && next.duration === current.duration
    && next.resolution === current.resolution ? current : next;
}

function flowRouteQueryPanel() {
  return new URLSearchParams(window.location.search).get("p") || "";
}

// Flow model labels are kept in the shared arrays and mirrored here as the
// supported authenticated Flow surface for source-level compatibility.
// Omni Flash; Veo 3.1 - Lite; Veo 3.1 - Lite [Lower Priority];
// Veo 3.1 - Fast; Veo 3.1 - Quality; Nano Banana Pro; Nano Banana 2;
// Nano Banana 2 Lite.

function IconImage({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="m4 17 5-5 4 4 2-2 5 5" />
    </svg>
  );
}
function IconLog({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M5 4h14v16H5z" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </svg>
  );
}


export default function FlowPage({ onBack, onOpenSrtImage }: { onBack: () => void; onOpenSrtImage: (mediaFolder: string) => void }) {
  const { locale } = useLocale();
  const t = (vi: string, en: string) => localize(locale, vi, en);
  const fileRef = useRef<HTMLInputElement>(null);
  const sourceRef = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<FlowTab>(() => {
    const routePanel = flowRouteQueryPanel() || flowRoutePanel() || (readText(ACTIVE_PANEL_KEY, "video") as FlowRoutePanel);
    if (routePanel === "queue" || routePanel === "history" || routePanel === "logs") return routePanel;
    const saved = readText(TAB_KEY, "create");
    return saved === "queue" || saved === "history" || saved === "logs"
      ? saved
      : "create";
  });
  const [railOpen, setRailOpen] = useState(
    () => readText(RAIL_KEY, "1") === "1",
  );
  const [videoPrompt, setVideoPrompt] = useState(() =>
    readText(
      DRAFT_VIDEO_KEY,
      readText(
        DRAFT_LEGACY_KEY,
        "Tokyo về đêm, phố Shibuya ướt sau cơn mưa, ánh đèn neon phản chiếu trên mặt đường.\n\nBuổi sáng yên bình bên hồ trong rừng thông, sương mù nhẹ trên mặt nước.\n\nThành phố tương lai lúc hoàng hôn, xe bay lướt qua các tòa nhà chọc trời.",
      ),
    ),
  );
  const [imagePrompt, setImagePrompt] = useState(() =>
    readText(
      DRAFT_IMAGE_KEY,
      readText(
        DRAFT_LEGACY_KEY,
        "Tokyo về đêm, phố Shibuya ướt sau cơn mưa, ánh đèn neon phản chiếu trên mặt đường.\n\nBuổi sáng yên bình bên hồ trong rừng thông, sương mù nhẹ trên mặt nước.\n\nThành phố tương lai lúc hoàng hôn, xe bay lướt qua các tòa nhà chọc trời.",
      ),
    ),
  );
  // readSettings keeps the stable default ``concurrency: "8"`` for Flow.
  const [settings, setSettings] = useState<FlowSettings>(readSettings);
  const [importName, setImportName] = useState("");
  const [promptInputType, setPromptInputType] = useState<PromptInputType>("prompt");
  const [jobs, setJobs] = useState<FlowJob[]>([]);
  const [logs, setLogs] = useState<FlowLog[]>([]);
  const [createKind, setCreateKind] = useState<CreateKind>(() =>
    flowRoutePanel() === "image" || (!flowRoutePanel() && readText(CREATE_KIND_KEY, "video") === "image") ? "image" : "video",
  );
  const prompt = createKind === "video" ? videoPrompt : imagePrompt;
  const setPrompt = (val: string | ((prev: string) => string)) => {
    if (createKind === "video") {
      setVideoPrompt(val);
    } else {
      setImagePrompt(val);
    }
  };
  const [imageMode, setImageMode] = useState<ImageMode>(() => {
    const saved = readText(IMAGE_MODE_KEY, "text");
    return saved === "edit" || saved === "reference" ? saved : "text";
  });
  const [sourceFiles, setSourceFiles] = useState<File[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(true);
  const [utilityView, setUtilityView] = useState<"accounts" | "help" | "series" | null>(() => {
    const routePanel = flowRoutePanel() || (readText(ACTIVE_PANEL_KEY, "video") as FlowRoutePanel);
    return routePanel === "accounts" || routePanel === "help" || routePanel === "series" ? routePanel : null;
  });
  const [seriesDraft, setSeriesDraft] = useState<FlowSeriesSceneContext | null>(null);
  const [accounts, setAccounts] = useState<FlowAccount[]>(readAccounts);
  const [syncingAccountIds, setSyncingAccountIds] = useState<Set<string>>(new Set());
  const [isSyncingAll, setIsSyncingAll] = useState(false);
  const [editingAccount, setEditingAccount] = useState<string | "new" | null>(
    null,
  );
  const [accountDraft, setAccountDraft] = useState<{
    label: string;
    email: string;
    plan: "Ultra" | "Pro" | "Free";
  }>({
    label: "",
    email: "",
    plan: "Free",
  });
  const [collapsedFolders, setCollapsedFolders] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(sessionStorage.getItem(COLLAPSED_FOLDERS_KEY) || "{}"); } catch { return {}; }
  });
  const toggleFolderCollapsed = (groupKey: string) => {
    setCollapsedFolders((prev) => {
      const next = { ...prev, [groupKey]: !prev[groupKey] };
      try { sessionStorage.setItem(COLLAPSED_FOLDERS_KEY, JSON.stringify(next)); } catch { /* ponytail: quota */ }
      return next;
    });
  };
  const [apiError, setApiError] = useState("");
  const [logsCopied, setLogsCopied] = useState(false);
  const [backendReady, setBackendReady] = useState(false);
  const [isDesktopApp, setIsDesktopApp] = useState(false);
  const [runtimeKnown, setRuntimeKnown] = useState(false);
  const [webOutputRootReady, setWebOutputRootReady] = useState(false);
  const webOutputRootRef = useRef<BrowserDirectoryHandle | null>(null);
  const completedOutputsRef = useRef<Set<string> | null>(null);
  const [preview, setPreview] = useState<{
    job: FlowJob;
    outputIndex: number;
  } | null>(null);
  const [retryTarget, setRetryTarget] = useState<{
    job: FlowJob;
    jobs: FlowJob[];
    model: string;
    ratio: string;
    concurrency: string;
    accountId: string;
  } | null>(null);
  const [confirmAction, setConfirmAction] = useState<{
    message: string;
    confirmLabel: string;
    run: () => void | Promise<unknown>;
  } | null>(null);
  const [queueKind, setQueueKind] = useState<CreateKind | "all">("all");
  const selectCreateKind = (kind: CreateKind) => {
    setCreateKind(kind);
    setSettings((current) => settingsForCreateKind(current, kind));
  };
  useEffect(() => {
    const applyRoute = () => {
      const panel = flowRoutePanel() || (readText(ACTIVE_PANEL_KEY, "video") as FlowRoutePanel);
      if (panel === "series" || panel === "accounts" || panel === "help") {
        setUtilityView(panel);
        return;
      }
      setUtilityView(null);
      if (panel === "image" || panel === "video") {
        selectCreateKind(panel);
        setTab("create");
      } else {
        setTab(panel as FlowTab);
      }
    };
    applyRoute();
    window.addEventListener("popstate", applyRoute);
    return () => window.removeEventListener("popstate", applyRoute);
  }, []);
  const queueGroups = useMemo(() => {
    const grouped = new Map<string, { kind: CreateKind; outputDir: string; outputFolder: string; displayOutputFolder: string; jobs: FlowJob[]; maxCreatedAt: number }>();
    jobs.forEach((job) => {
      const outputDir = job.settings.outputDir || "";
      const outputFolder = job.outputFolder || (isDesktopApp ? flowOutputParentPath(job.outputs?.[0]) : "");
      const displayOutputFolder = job.displayOutputFolder || outputFolder;
      const key = `${job.kind}\u0000${outputDir}`;
      const group = grouped.get(key);
      if (group) {
        group.jobs.push(job);
        if (job.createdAt > group.maxCreatedAt) group.maxCreatedAt = job.createdAt;
        if (!group.outputFolder && outputFolder) group.outputFolder = outputFolder;
        if (!group.displayOutputFolder && displayOutputFolder) group.displayOutputFolder = displayOutputFolder;
      } else {
        grouped.set(key, {
          kind: job.kind,
          outputDir,
          outputFolder,
          displayOutputFolder,
          jobs: [job],
          maxCreatedAt: job.createdAt,
        });
      }
    });
    return [...grouped.values()]
      .sort((left, right) => right.maxCreatedAt - left.maxCreatedAt)
      .map((group) => ({
        ...group,
        jobs: [...group.jobs].sort((a, b) => a.index - b.index || a.createdAt - b.createdAt),
      }));
  }, [isDesktopApp, jobs]);
  const queueKindGroups = useMemo(() => (
    (["video", "image"] as const)
      .map((kind) => ({ kind, folders: queueGroups.filter((group) => group.kind === kind) }))
  ), [queueGroups]);
  const activeQueueGroups = useMemo(
    () => queueKind === "all" ? queueGroups : queueGroups.filter((group) => group.kind === queueKind),
    [queueGroups, queueKind],
  );

  useEffect(() => {
    if (queueKind !== "all" && !activeQueueGroups.length && queueGroups.some((group) => group.kind !== queueKind)) {
      setQueueKind(queueKind === "video" ? "image" : "video");
    }
  }, [activeQueueGroups.length, queueGroups, queueKind]);

  useEffect(() => {
    try {
      localStorage.setItem(DRAFT_VIDEO_KEY, videoPrompt);
    } catch { }
  }, [videoPrompt]);
  useEffect(() => {
    try {
      localStorage.setItem(DRAFT_IMAGE_KEY, imagePrompt);
    } catch { }
  }, [imagePrompt]);
  useEffect(() => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch { }
  }, [settings]);
  useEffect(() => {
    try {
      localStorage.setItem(TAB_KEY, tab);
    } catch { }
  }, [tab]);
  useEffect(() => {
    try {
      localStorage.setItem(RAIL_KEY, railOpen ? "1" : "0");
    } catch { }
  }, [railOpen]);
  useEffect(() => {
    try {
      localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
    } catch { }
  }, [accounts]);
  useEffect(() => {
    try {
      localStorage.setItem(CREATE_KIND_KEY, createKind);
    } catch { }
  }, [createKind]);
  useEffect(() => {
    try {
      localStorage.setItem(IMAGE_MODE_KEY, imageMode);
    } catch { }
  }, [imageMode]);
  useEffect(() => {
    if (!accounts.length || accounts.some((account) => account.label === settings.account)) return;
    const fallback = selectedFlowAccount(accounts, settings.account);
    if (fallback) {
      setSettings((current) => ({ ...current, account: fallback.label }));
    }
  }, [accounts, settings.account]);
  useEffect(() => {
    const account = selectedFlowAccount(accounts, settings.account);
    if (!account?.capabilityCatalog || account.capabilityStatus !== "verified") return;
    setSettings((current) => applyAccountCapabilities(
      account,
      current,
      createKind,
      createKind === "image" ? current.imageModel : current.videoModel,
    ));
  }, [accounts, settings.account, createKind]);
  useEffect(() => {
    let active = true;
    const loadInitialSnapshot = async () => {
      try {
        const { accountData, jobData } = await loadFlowSnapshot();
        if (!active) return;
        const loadedAccounts = normalizeFlowAccounts(accountData.accounts);
        setAccounts(loadedAccounts);
        setJobs(normalizeFlowJobs(jobData.jobs, loadedAccounts));
        setBackendReady(true);
        setApiError("");
      } catch (error) {
        if (active) {
          setBackendReady(false);
          setApiError(error instanceof Error ? error.message : String(error));
        }
      }
    };
    void loadInitialSnapshot();
    return () => {
      active = false;
    };
  }, []);
  const hasActiveFlowJobs = jobs.some(
    (job) => job.status === "processing" || job.status === "queued",
  );
  const hasConnectingAccounts = accounts.some((account) => account.status === "connecting");
  // Track trạng thái trước để detect khi connecting → online/reconnect
  const prevAccountStatusRef = useRef<Record<string, string>>({});
  useEffect(() => {
    accounts.forEach((account) => {
      const prev = prevAccountStatusRef.current[account.id];
      if (prev === "connecting" && account.status === "online") {
        toast.success(t("Đã kết nối tài khoản thành công!", "Account connected successfully!"));
      } else if (prev === "connecting" && account.status === "reconnect") {
        toast.info(t("Đang mở Chrome — vui lòng đăng nhập Google.", "Chrome opened — please sign in to Google."));
      }
      prevAccountStatusRef.current[account.id] = account.status;
    });
  }, [accounts]);
  useEffect(() => {
    if (!backendReady || !hasActiveFlowJobs) return;
    let active = true;
    const refreshJobs = () =>
      void flowRequest<{ jobs: Array<Record<string, unknown>>; accounts?: FlowAccount[] }>("/api/flow/jobs")
        .then((data) => {
          if (!active) return;
          const refreshedAccounts = data.accounts
            ? normalizeFlowAccounts(data.accounts)
            : accounts;
          if (data.accounts) setAccounts(refreshedAccounts);
          setJobs((current) => {
            // Preserve optimistic cancelled/deleted state against the poll
            const cancelled = cancelledIdsRef.current;
            const deleted = deletedIdsRef.current;
            // Drop _opt_ stubs; merge backend state with local overrides
            const merged = normalizeFlowJobs(data.jobs, refreshedAccounts)
              .filter((j) => !deleted.has(j.id))
              .map((j) =>
                (cancelled.has(j.id) && j.status !== "cancelled")
                  ? { ...j, status: "cancelled" as const, stage: "cancelled", progress: 0 }
                  : j,
              );
            // Only keep _opt_ stubs while the submit POST is still in flight;
            // once POST completes (or backend has returned real data), drop them
            // to avoid showing each prompt twice.
            const stubs = postInFlightRef.current
              ? current.filter((j) => j.id.startsWith("_opt_"))
              : [];
            return [...merged, ...stubs];
          });
        })
        .catch((error) => {
          if (active) setApiError(error instanceof Error ? error.message : String(error));
        });
    const timer = window.setInterval(refreshJobs, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [accounts, backendReady, hasActiveFlowJobs]);
  useEffect(() => {
    if (tab !== "logs") return;
    let active = true;
    const refreshLogs = () =>
      void flowRequest<{ logs: FlowLog[] }>("/api/flow/logs")
        .then((data) => { if (active) setLogs(data.logs); })
        .catch(() => undefined);
    refreshLogs();
    if (!hasActiveFlowJobs) return () => { active = false; };
    const timer = window.setInterval(refreshLogs, 10000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [hasActiveFlowJobs, tab]);
  useEffect(() => {
    if (!hasConnectingAccounts) return;
    let active = true;
    const refreshAccounts = () =>
      void flowRequest<{ accounts: FlowAccount[] }>("/api/flow/accounts")
        .then((data) => { if (active) setAccounts(normalizeFlowAccounts(data.accounts)); })
        .catch(() => undefined);
    // Call immediately so the UI updates as soon as Chrome closes (no 10-s dead wait).
    refreshAccounts();
    const timer = window.setInterval(refreshAccounts, 10000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [hasConnectingAccounts]);
  useEffect(() => {
    void flowRequest<{ desktop?: boolean }>("/api/config")
      .then((data) => setIsDesktopApp(Boolean(data.desktop)))
      .catch(() => setIsDesktopApp(false))
      .finally(() => setRuntimeKnown(true));
  }, []);
  useEffect(() => {
    if (!runtimeKnown || isDesktopApp) return;
    let active = true;
    void loadWebOutputRoot(WEB_OUTPUT_ROOT_KEY)
      .then(async (handle) => {
        if (!handle || !active) return;
        const permission = await handle.queryPermission?.({ mode: "readwrite" });
        if (active && permission === "granted") {
          webOutputRootRef.current = handle;
          setWebOutputRootReady(true);
        }
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, [isDesktopApp, runtimeKnown]);
  useEffect(() => {
    if (!runtimeKnown || isDesktopApp) return;
    try {
      if (localStorage.getItem(WEB_AUTO_DOWNLOAD_DEFAULT_KEY)) return;
      localStorage.setItem(WEB_AUTO_DOWNLOAD_DEFAULT_KEY, "1");
      setSettings((current) => ({ ...current, autoDownload: true }));
    } catch {
      setSettings((current) => ({ ...current, autoDownload: true }));
    }
  }, [isDesktopApp, runtimeKnown]);
  useEffect(() => {
    if (!backendReady || !runtimeKnown || isDesktopApp || !settings.autoDownload) return;
    const completed = jobs.flatMap((job) =>
      job.status === "done"
        ? (job.outputs || []).map((_output, outputIndex) => ({
          key: `${job.id}:${outputIndex}`,
          job,
          outputIndex,
        }))
        : [],
    );
    if (!completedOutputsRef.current) completedOutputsRef.current = new Set();
    const root = webOutputRootRef.current;
    for (const item of completed) {
      if (completedOutputsRef.current.has(item.key)) continue;
      completedOutputsRef.current.add(item.key);
      if (!root) {
        downloadFlowOutput(item.job, item.outputIndex);
        continue;
      }
      void writeFlowOutputToDirectory(item.job, item.outputIndex, root, item.job.settings.outputDir)
        .then(() => toast.success(t(
          "Đã lưu output Flow vào thư mục đã chọn.",
          "Flow output saved to the selected folder.",
        )))
        .catch(() => {
          completedOutputsRef.current?.delete(item.key);
          setApiError(t(
            "Không thể lưu output Flow vào thư mục đã chọn.",
            "Could not save the Flow output to the selected folder.",
          ));
        });
    }
  }, [backendReady, runtimeKnown, isDesktopApp, jobs, settings.autoDownload, settings.outputDir, locale, webOutputRootReady]);

  const allCompletedOutputs = useMemo(() => {
    const list: Array<{ job: FlowJob; outputIndex: number }> = [];
    for (const job of jobs) {
      if (job.status === "done" && Array.isArray(job.outputs) && job.outputs.length > 0) {
        for (let i = 0; i < job.outputs.length; i++) {
          list.push({ job, outputIndex: i });
        }
      }
    }
    return list;
  }, [jobs]);

  const currentPreviewIndex = useMemo(() => {
    if (!preview) return -1;
    return allCompletedOutputs.findIndex(
      (item) => item.job.id === preview.job.id && item.outputIndex === preview.outputIndex,
    );
  }, [preview, allCompletedOutputs]);

  const movePreview = (delta: number) => {
    setPreview((current) => {
      if (!current || allCompletedOutputs.length < 2) return current;
      const currentIndex = allCompletedOutputs.findIndex(
        (item) => item.job.id === current.job.id && item.outputIndex === current.outputIndex,
      );
      if (currentIndex === -1) {
        const total = current.job.outputs?.length || 0;
        if (total < 2) return current;
        return { ...current, outputIndex: (current.outputIndex + delta + total) % total };
      }
      const total = allCompletedOutputs.length;
      const nextIndex = (currentIndex + delta + total) % total;
      return allCompletedOutputs[nextIndex];
    });
  };

  useEffect(() => {
    if (!preview) return;
    const navigate = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreview(null);
      if (event.key === "ArrowLeft") { event.preventDefault(); movePreview(-1); }
      if (event.key === "ArrowRight") { event.preventDefault(); movePreview(1); }
    };
    window.addEventListener("keydown", navigate);
    return () => window.removeEventListener("keydown", navigate);
  }, [preview, allCompletedOutputs]);

  const promptCount = useMemo(
    () =>
      prompt
        .split(/\n\s*\n/)
        .map((item) => item.trim())
        .filter(Boolean).length,
    [prompt],
  );
  const latestCompletedVideo = jobs.find(
    (job) => job.kind === "video" && job.status === "done" && job.outputs?.length,
  );
  const displayedAccount = selectedFlowAccount(accounts, settings.account);
  const accountOptionLabels = Object.fromEntries(
    accounts.map((account) => [
      account.label,
      `${account.label} · ${t(`Gói ${account.plan}`, `${account.plan} plan`)}`,
    ]),
  );
  const capabilityModels = accountCapabilityModels(displayedAccount, createKind);
  const capabilityModel = capabilityModels.find((item) => item.name === settings.model);
  const modelOptions = capabilityModels.length
    ? capabilityModels.map((item) => item.name)
    : createKind === "video" ? [...FLOW_VIDEO_MODELS] : [...FLOW_IMAGE_MODELS];
  const ratioOptions = capabilityModel?.ratios.length
    ? capabilityModel.ratios
    : createKind === "video" ? ["16:9", "9:16"] : ["1:1", "16:9", "9:16", "4:3", "3:4"];
  const isOmniFlash = createKind === "video" && /omni.*flash/i.test(settings.model);
  const durationOptions = capabilityModel?.durations.length
    ? capabilityModel.durations
    : isOmniFlash
      ? [...FLOW_OMNI_FLASH_DURATIONS]
      : [settings.duration || "8"];
  // Resolution: chỉ Omni Flash video mới có catalog resolutions.
  // Veo 3.1 không có resolution control trên Flow UI → ẩn.
  // Image: theo plan tier.
  const accountPlan = displayedAccount?.plan ?? "Free";
  const imageResolutionOptions =
    accountPlan === "Ultra" ? ["1K", "2K", "4K"]
    : accountPlan === "Pro"  ? ["1K", "2K"]
    : ["1K"];
  const resolutionOptions = capabilityModel?.resolutions.length
    ? capabilityModel.resolutions
    : createKind === "image" ? imageResolutionOptions : [];
  const retryAccount = accounts.find((account) => account.id === retryTarget?.accountId);
  const retryCapabilities = retryTarget ? accountCapabilityModels(retryAccount, retryTarget.job.kind) : [];
  const retryModelCapability = retryCapabilities.find((item) => item.name === retryTarget?.model);
  const retryModelOptions = retryCapabilities.length
    ? retryCapabilities.map((item) => item.name)
    : retryTarget?.job.kind === "video" ? [...FLOW_VIDEO_MODELS] : [...FLOW_IMAGE_MODELS];
  const retryRatioOptions = retryModelCapability?.ratios.length
    ? retryModelCapability.ratios
    : retryTarget?.job.kind === "video" ? ["16:9", "9:16"] : ["1:1", "16:9", "9:16", "4:3", "3:4"];
  const previewOutput = preview?.job.outputs?.[preview.outputIndex] || "";
  const previewMediaKind = preview ? flowOutputMediaKind(previewOutput, preview.job.kind) : "file";
  const previewSrc = preview ? `/api/flow/jobs/${preview.job.id}/outputs/${preview.outputIndex}` : "";
  const statusText = (status: JobStatus) =>
    status === "processing"
      ? t("Đang xử lý", "Processing")
      : status === "queued"
        ? t("Đang chờ", "Queued")
        : status === "done"
          ? t("Hoàn thành", "Completed")
          : status === "cancelled"
            ? t("Đã hủy", "Cancelled")
            : t("Lỗi", "Failed");
  const jobStatusText = (job: FlowJob) => {
    if (job.status !== "processing") return statusText(job.status);
    if (job.stage === "retrying") return t("Đang thử tạo lại", "Retrying generation");
    if (job.stage === "recovering") return t("Đang tìm kết quả đã gửi", "Recovering submitted result");
    if (job.stage === "resubmitting" || job.stage === "submitting") return t("Đang gửi yêu cầu", "Submitting request");
    if (job.stage === "downloading") return t("Đang tải kết quả", "Downloading result");
    if (job.stage === "generating") return t("Flow đang tạo", "Generating in Flow");
    if (job.stage === "preparing" || job.stage === "starting") return t("Đang chuẩn bị", "Preparing");
    return statusText(job.status);
  };
  const jobErrorText = (error: string) =>
    error.startsWith("FLOW_EMPTY_OUTPUT")
      ? t(
        "Flow không trả về file video/ảnh. Job chưa thành công.",
        "Flow returned no video/image file. The job did not succeed.",
      )
      : error.startsWith("FLOW_RESULT_NOT_FOUND")
        ? t(
          "Flow không có kết quả đang chờ hoặc đã hoàn thành cho lần gửi này. Hãy chạy lại để gửi yêu cầu mới.",
          "Flow has no pending or completed result for this submission. Retry to send a new request.",
        )
        : error.startsWith("FLOW_GENERATION_REJECTED")
          ? t(
            "Flow báo không tạo được nội dung này và không tính phí. Hãy điều chỉnh prompt hoặc cài đặt rồi chạy lại.",
            "Flow could not generate this content and did not charge for it. Adjust the prompt or settings, then retry.",
          )
          : error;
  const showCreate = tab === "create";
  const activateRail = (item: RailItem) => {
    const panelName = item === "createImage" ? "image" : (item === "createVideo" ? "video" : item);
    try { localStorage.setItem(ACTIVE_PANEL_KEY, panelName); } catch { }
    if (item === "createImage" || item === "createVideo") {
      setUtilityView(null);
      selectCreateKind(item === "createImage" ? "image" : "video");
      if (item === "createVideo") setAdvancedOpen(true);
      setTab("create");
      writeFlowRoutePanel(item === "createImage" ? "image" : "video");
    } else if (item === "queue" || item === "history" || item === "logs") {
      setUtilityView(null);
      setTab(item);
      writeFlowRoutePanel(item);
    } else {
      setUtilityView(item);
      writeFlowRoutePanel(item);
    }
  };
  const importPrompts = (file?: File) => {
    if (!file) return;
    setImportName(file.name);
    const extension = file.name.split(".").pop()?.toLowerCase();
    setPromptInputType(
      extension === "csv" || extension === "json" ? extension : "txt",
    );
    const reader = new FileReader();
    reader.onload = () => {
      setPrompt(String(reader.result || "").trim());
      if (fileRef.current) fileRef.current.value = "";
    };
    reader.readAsText(file);
  };
  const pastePrompt = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text.trim()) {
        const msg = t("Clipboard đang trống.", "Clipboard is empty.");
        setApiError(msg);
        toast.info(msg);
        return;
      }
      setPrompt(text);
      setPromptInputType("prompt");
      setImportName("");
      setApiError("");
      toast.success(t("Đã dán nội dung từ clipboard.", "Pasted content from clipboard."));
    } catch {
      const msg = t(
        "Không đọc được clipboard. Hãy cấp quyền dán hoặc dùng Cmd/Ctrl+V.",
        "Could not read the clipboard. Allow paste access or use Cmd/Ctrl+V.",
      );
      setApiError(msg);
      toast.error(msg);
    }
  };
  const actionLock = useRef(false);
  // Track jobs cancelled/deleted while the submit POST is still in flight,
  // so the POST response can't resurrect them.
  const cancelledIdsRef = useRef<Set<string>>(new Set());
  const deletedIdsRef = useRef<Set<string>>(new Set());
  // True while a batch POST /api/flow/jobs is in flight so the poll keeps stubs
  const postInFlightRef = useRef(false);
  const [actionBusy, setActionBusy] = useState(false);
  const runAction = async (action: () => void | Promise<unknown>) => {
    if (actionLock.current) return;
    actionLock.current = true;
    setActionBusy(true);
    try { await action(); }
    catch (error) { toast.error(error instanceof Error ? error.message : String(error)); }
    finally { actionLock.current = false; setActionBusy(false); }
  };
  const cancelCreateAction = async () => {
    try {
      // Optimistic: mark all active jobs as cancelled immediately;
      // remove _opt_ stubs (no backend state to cancel) and record real IDs.
      setJobs((current) => {
        const next: FlowJob[] = [];
        for (const job of current) {
          if (job.id.startsWith("_opt_")) continue; // not in backend, just drop
          if (job.status === "queued" || job.status === "processing") {
            cancelledIdsRef.current.add(job.id);
            next.push({ ...job, status: "cancelled" as const, stage: "cancelled", progress: 0 });
          } else {
            next.push(job);
          }
        }
        return next;
      });
      actionLock.current = false;
      setActionBusy(false);
      // Fire cancel-all in background; do NOT re-fetch jobs (poll will sync later)
      flowRequest<{ ok: boolean }>("/api/flow/jobs/cancel-all", { method: "POST" }).catch(() => {});
      toast.success(t("Đã hủy các job đang chờ/chạy.", "Queued and running jobs cancelled."));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };
  const createFlowJobs = async () => {
    const prompts = prompt
      .split(/\n\s*\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    const account = selectedFlowAccount(accounts, settings.account);
    if (!prompts.length || !account) {
      if (!account) {
        // Chưa có tài khoản → tự nhảy qua tab Tài khoản và mở form thêm
        setUtilityView("accounts");
        addAccount();
        toast.info(t("Vui lòng thêm tài khoản Flow trước khi tạo.", "Please add a Flow account first."));
      } else {
        setApiError(t("Cần nhập prompt.", "A prompt is required."));
      }
      return;
    }
    // Show the queue immediately; enqueue/upload can involve Chrome or file IO.
    // Keeping the user on the queue makes per-job cancel/delete controls
    // available while the request is still being accepted.
    setTab("queue");
    writeFlowRoutePanel("queue");
    if (account.status !== "online") {
      // Thử tự reconnect headless trước
      toast.info(t("Đang thử kết nối lại tự động...", "Attempting auto-reconnect..."));
      try {
        const refreshed = await flowRequest<FlowAccount>(
          `/api/flow/accounts/${account.id}/sync`,
          { method: "POST" },
        );
        if (refreshed.status === "online") {
          // Headless thành công → cập nhật state và chạy tiếp (fall-through)
          setAccounts((current) =>
            current.map((item) => (item.id === refreshed.id ? refreshed : item)),
          );
        } else {
          throw new Error("not-online");
        }
      } catch {
        // Headless thất bại → tự mở Chrome để user đăng nhập lại
        setUtilityView("accounts");
        toast.info(
          t(
            "Cần đăng nhập lại — đang mở Chrome...",
            "Re-login required — opening Chrome...",
          ),
        );
        connectAccount(account);
        return;
      }
    }
    if (createKind === "video" && account.planStatus === "verified" && account.plan === "Free") {
      const message = t(
        "Tài khoản gói thường chỉ hỗ trợ tạo ảnh. Vui lòng chuyển sang loại 'Ảnh' hoặc chọn tài khoản Pro/Ultra.",
        "Free accounts only support image generation. Please switch to 'Image' or select a Pro/Ultra account.",
      );
      setApiError(message);
      toast.warning(message);
      return;
    }
    try {
      if (!isDesktopApp && settings.autoDownload && !webOutputRootRef.current) {
        try {
          const cachedHandle = await loadWebOutputRoot(WEB_OUTPUT_ROOT_KEY);
          if (cachedHandle) {
            const perm = await cachedHandle.queryPermission?.({ mode: "readwrite" });
            if (perm === "granted") {
              webOutputRootRef.current = cachedHandle;
              setWebOutputRootReady(true);
            } else if (cachedHandle.requestPermission) {
              const req = await cachedHandle.requestPermission({ mode: "readwrite" });
              if (req === "granted") {
                webOutputRootRef.current = cachedHandle;
                setWebOutputRootReady(true);
              }
            }
          }
        } catch {
          // Fallback to standard download if permission is denied
        }
      }
      let effectiveSettings = settings.outputDir.trim()
        ? settings
        : { ...settings, outputDir: defaultFlowOutputFolder() };
      effectiveSettings = {
        ...effectiveSettings,
        ratio: createKind === "image" ? effectiveSettings.imageRatio : effectiveSettings.ratio,
        // Normalize resolution: if current value not valid for this kind/plan, use first valid option
        resolution: resolutionOptions.length && !resolutionOptions.includes(effectiveSettings.resolution)
          ? resolutionOptions[0]
          : effectiveSettings.resolution,
      };

      if (createKind === "image" && account.planStatus === "verified" && account.plan === "Free" && effectiveSettings.model === "Nano Banana Pro") {
        effectiveSettings = { ...effectiveSettings, model: "Nano Banana 2", imageModel: "Nano Banana 2" };
      }

      if (effectiveSettings !== settings) {
        setSettings(effectiveSettings);
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(effectiveSettings));
      }
      if (seriesDraft) {
        const created = await flowRequest<{ jobs: Array<Record<string, unknown>> }>(
          `/api/flow/series/${seriesDraft.seriesId}/episodes/${seriesDraft.episodeId}/scenes/${seriesDraft.sceneId}/generate`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              artifact: seriesDraft.artifact,
              accountId: account.id,
              settings: { ...effectiveSettings, count: 1 },
              promptOverride: prompt.trim() === seriesDraft.scenePrompt.trim() ? "" : prompt.trim(),
            }),
          },
        );
        setJobs((current) => [
          ...normalizeFlowJobs(created.jobs, accounts),
          ...current.filter((item) => !created.jobs.some((row) => String(row.id) === item.id)),
        ]);
        setApiError("");
        setTab("queue");
        writeFlowRoutePanel("queue");
        return;
      }
      let uploaded: string[] = [];
      if (sourceFiles.length) {
        const form = new FormData();
        sourceFiles.forEach((file) => form.append("files", file));
        const data = await flowRequest<{ files: Array<{ path: string }> }>(
          "/api/flow/assets",
          { method: "POST", body: form },
        );
        uploaded = data.files.map((item) => item.path);
      }
      // Optimistic queue: show all jobs immediately without waiting for server IDs.
      const nowSec = Date.now() / 1000;
      const optimisticJobs: FlowJob[] = prompts.map((p, i) => ({
        id: `_opt_${nowSec}_${i}`,
        index: i + 1,
        kind: createKind,
        prompt: p,
        inputType: "prompt" as const,
        createdAt: nowSec,
        status: "queued" as const,
        stage: "",
        progress: 0,
        accountId: account.id,
        account: account.label,
        outputs: [],
        output: "",
        outputFolder: effectiveSettings.outputDir || "",
        displayOutputFolder: effectiveSettings.outputDir || "",
        error: null,
        settings: {
          model: effectiveSettings.model || (createKind === "image" ? "Nano Banana 2" : "Veo 3.1 - Fast"),
          ratio: effectiveSettings.ratio || "16:9",
          duration: String(effectiveSettings.duration || "8"),
          resolution: effectiveSettings.resolution || "1K",
          outputDir: effectiveSettings.outputDir || "flow",
        },
      }));
      setJobs((current) => [
        ...optimisticJobs,
        ...current.filter((j) => !j.id.startsWith("_opt_")),
      ]);
      setApiError("");
      setTab("queue");
      writeFlowRoutePanel("queue");
      // Release busy so the queue is interactive while POST is in flight
      actionLock.current = false;
      setActionBusy(false);
      // Mark POST as in-flight so the poll keeps stubs (prevents showing jobs twice)
      postInFlightRef.current = true;
      const created = await flowRequest<{ jobs: Array<Record<string, unknown>> }>("/api/flow/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompts,
          kind: createKind,
          mode:
            createKind === "image"
              ? imageMode
              : sourceFiles.length
                ? "frame"
                : "text",
          accountId: account.id,
          inputType: promptInputType,
          sourceFiles: uploaded,
          settings: effectiveSettings,
        }),
      });
      // Replace optimistic stubs with real server jobs,
      // but preserve cancelled/deleted state that happened while POST was in flight.
      setJobs((current) => {
        const cancelled = cancelledIdsRef.current;
        const deleted = deletedIdsRef.current;
        const realJobs = normalizeFlowJobs(created.jobs, accounts)
          .filter((j) => !deleted.has(j.id))
          .map((j) =>
            cancelled.has(j.id)
              ? { ...j, status: "cancelled" as const, stage: "cancelled", progress: 0 }
              : j,
          );
        return [
          ...realJobs,
          ...current.filter(
            (j) => !j.id.startsWith("_opt_") && !created.jobs.some((row) => String(row.id) === j.id),
          ),
        ];
      });
      cancelledIdsRef.current = new Set();
      deletedIdsRef.current = new Set();
      postInFlightRef.current = false;
    } catch (error) {
      postInFlightRef.current = false;
      setApiError(error instanceof Error ? error.message : String(error));
    }
  };
  const addAccount = () => {
    setAccountDraft({ label: "", email: "", plan: "Free" });
    setEditingAccount("new");
  };
  const editAccount = (account: FlowAccount) => {
    setAccountDraft({
      label: account.label,
      email: account.email,
      plan: account.plan || "Free",
    });
    setEditingAccount(account.id);
  };
  const saveAccount = async () => {
    if (!accountDraft.label.trim() || !accountDraft.email.trim()) return;
    try {
      const editing = accounts.find((item) => item.id === editingAccount);
      const url =
        editingAccount === "new"
          ? "/api/flow/accounts"
          : `/api/flow/accounts/${editingAccount}`;
      const savedAccount = await flowRequest<FlowAccount>(url, {
        method: editingAccount === "new" ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...accountDraft,
          projectId: editing?.projectId || "",
          isDefault: editing?.isDefault || false,
        }),
      });
      setAccounts((current) => [savedAccount, ...current.filter((item) => item.id !== savedAccount.id)]);
      setEditingAccount(null);
      setApiError("");
    } catch (error) {
      setApiError(error instanceof Error ? error.message : String(error));
    }
  };
  const deleteAccount = (account: FlowAccount) => {
    setConfirmAction({
      message: t(`Xóa tài khoản ${account.label}?`, `Delete account ${account.label}?`),
      confirmLabel: t("Xóa tài khoản", "Delete account"),
      run: () => flowRequest(`/api/flow/accounts/${account.id}`, { method: "DELETE" })
        .then(() => setAccounts((current) => current.filter((item) => item.id !== account.id)))
        .catch((error) => setApiError(error instanceof Error ? error.message : String(error))),
    });
  };
  const updateJob = (raw: Record<string, unknown>) => {
    const next = normalizeFlowJobs([raw], accounts)[0];
    setJobs((current) => current.map((item) => item.id === next.id ? next : item));
  };
  const cancelJob = (id: string) =>
    void flowRequest<Record<string, unknown>>(`/api/flow/jobs/${id}/cancel`, { method: "POST" })
      .then((raw) => {
        updateJob(raw);
        toast.success(t("Đã gửi yêu cầu hủy job.", "Job cancellation requested."));
      })
      .catch((error) => {
        const msg = error instanceof Error ? error.message : String(error);
        setApiError(msg);
        toast.error(msg);
      });
  const openRetrySettings = (retryJobs: FlowJob[]) => {
    const job = retryJobs[0];
    if (!job) return;
    setRetryTarget({
      job,
      jobs: retryJobs,
      model: job.settings.model,
      ratio: job.settings.ratio,
      concurrency: String(job.settings.concurrency || settings.concurrency),
      accountId: job.accountId || accounts.find((account) => account.label === job.account)?.id || "",
    });
  };
  const retryJob = (id: string) => {
    const job = jobs.find((item) => item.id === id);
    if (job) openRetrySettings([job]);
  };
  const confirmRetryJob = () => {
    if (!retryTarget) return;
    const { jobs: retryJobs, model, ratio, concurrency, accountId } = retryTarget;
    setRetryTarget(null);
    void Promise.all(retryJobs.map((job) => flowRequest(`/api/flow/jobs/${job.id}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId, settings: { model, ratio, concurrency } }),
    })))
      .then(async () => {
        const data = await flowRequest<{ jobs: Array<Record<string, unknown>> }>("/api/flow/jobs");
        setJobs(normalizeFlowJobs(data.jobs, accounts));
        toast.success(t(`Đã đưa ${retryJobs.length} job vào hàng đợi chạy lại.`, `Queued ${retryJobs.length} jobs for retry.`));
      })
      .catch((error) => {
        const msg = error instanceof Error ? error.message : String(error);
        setApiError(msg);
        toast.error(msg);
      });
  };
  const deleteWebFlowOutputs = async (job: FlowJob) => {
    const root = webOutputRootRef.current;
    if (isDesktopApp || !root) return;
    for (let outputIndex = 0; outputIndex < (job.outputs?.length || 0); outputIndex += 1) {
      await deleteFlowOutputFromDirectory(job, outputIndex, root);
    }
  };
  const deleteJob = (id: string) => {
    const job = jobs.find((item) => item.id === id);
    setConfirmAction({
      message: t("Xóa job và file đầu ra trên đĩa? Không thể hoàn tác.", "Delete this job and its output files from disk? This cannot be undone."),
      confirmLabel: t("Xóa job", "Delete job"),
      run: () => (async () => {
        await flowRequest(`/api/flow/jobs/${id}`, { method: "DELETE" });
        if (job) await deleteWebFlowOutputs(job);
        setJobs((current) => current.filter((item) => item.id !== id));
        toast.success(t("Đã xóa job thành công.", "Job deleted successfully."));
      })().catch(() => {
        const msg = t(
          "Không thể xóa đầy đủ job và file output của nó.",
          "Could not fully delete the job and its output files.",
        );
        setApiError(msg);
        toast.error(msg);
      }),
    });
  };
  const cancelAllJobs = useCallback(() => {
    const activeCount = jobs.filter((job) => job.status === "queued" || job.status === "processing").length;
    if (!activeCount) return;
    setConfirmAction({
      message: t(`Hủy ${activeCount} job đang chờ/chạy?`, `Cancel ${activeCount} queued/running jobs?`),
      confirmLabel: t("Hủy tất cả", "Cancel all"),
      run: () => {
        // Optimistic: mark active jobs cancelled immediately;
        // remove _opt_ stubs (no backend equivalent) and record real IDs.
        setJobs((current) => {
          const next: FlowJob[] = [];
          for (const job of current) {
            if (job.id.startsWith("_opt_")) continue; // drop stubs
            if (job.status === "queued" || job.status === "processing") {
              cancelledIdsRef.current.add(job.id);
              next.push({ ...job, status: "cancelled" as const, stage: "cancelled", progress: 0 });
            } else {
              next.push(job);
            }
          }
          return next;
        });
        setApiError("");
        toast.success(t("Đã hủy tất cả job.", "All jobs cancelled."));
        return flowRequest<{ ok: boolean }>("/api/flow/jobs/cancel-all", { method: "POST" })
          .catch((error) => {
            const msg = error instanceof Error ? error.message : String(error);
            setApiError(msg);
            toast.error(msg);
          });
      },
    });
  }, [jobs, t]);
  const retryAllJobs = useCallback(async () => {
    const retryable = jobs.filter((job) => job.status === "failed" || job.status === "cancelled");
    if (!retryable.length) return;
    openRetrySettings(retryable);
  }, [jobs, t, accounts]);
  const retryFolderJobs = (outputDir: string, kind: CreateKind, folderJobs: FlowJob[]) => {
    const retryable = folderJobs.filter((job) =>
      job.kind === kind
      && String(job.settings.outputDir || "").trim() === String(outputDir || "").trim()
      && (job.status === "failed" || job.status === "cancelled"),
    );
    if (!retryable.length) return;
    openRetrySettings(retryable);
  };
  const deleteAllJobs = () => {
    if (!jobs.length) return;
    setConfirmAction({
      message: t(`Xóa ${jobs.length} job cùng file đầu ra trên đĩa? Không thể hoàn tác.`, `Delete all ${jobs.length} jobs and their output files from disk? This cannot be undone.`),
      confirmLabel: t("Xóa tất cả", "Delete all"),
      run: () => (async () => {
        // Await backend DELETE first — backend cancels threads + clears DB instantly,
        // then cleans up files in background. F5 after this will always see empty queue.
        await flowRequest<{ ok: boolean }>("/api/flow/jobs", { method: "DELETE" });
        await Promise.all(jobs.map((job) => deleteWebFlowOutputs(job)));
        deletedIdsRef.current = new Set();
        cancelledIdsRef.current = new Set();
        setJobs([]);
        setApiError("");
        toast.success(t("Đã xóa tất cả job.", "All jobs deleted."));
      })().catch(() => {
        const msg = t(
          "Không thể xóa đầy đủ hàng đợi và file output.",
          "Could not fully delete the queue and its output files.",
        );
        setApiError(msg);
        toast.error(msg);
      }),
    });
  };
  const cancelFolderJobs = (outputDir: string, folderJobs: FlowJob[]) => {
    const activeCount = folderJobs.filter((job) => job.status === "queued" || job.status === "processing").length;
    if (!activeCount) return;
    setConfirmAction({
      message: t(
        `Hủy ${activeCount} job đang chờ/chạy trong thư mục này?`,
        `Cancel ${activeCount} queued/running jobs in this folder?`,
      ),
      confirmLabel: t("Hủy", "Cancel"),
      run: () => {
        // Optimistic cancel for folder jobs
        setJobs((current) => {
          const next: FlowJob[] = [];
          for (const job of current) {
            if (job.id.startsWith("_opt_")) continue;
            // Match by settings.outputDir (relative key used as group key),
            // NOT by job.outputFolder (absolute path on disk).
            const inFolder = String(job.settings.outputDir || "").trim() === String(outputDir || "").trim();
            if (inFolder && (job.status === "queued" || job.status === "processing")) {
              cancelledIdsRef.current.add(job.id);
              next.push({ ...job, status: "cancelled" as const, stage: "cancelled", progress: 0 });
            } else {
              next.push(job);
            }
          }
          return next;
        });
        return flowRequest<{ jobs: Array<Record<string, unknown>> }>(
          "/api/flow/jobs/cancel-folder",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ outputDir, kind: folderJobs[0]?.kind || "" }),
          },
        ).catch(() => {});
      },
    });
  };
  const deleteFolderJobs = (outputDir: string, folderJobs: FlowJob[]) => {
    if (!folderJobs.length) return;
    setConfirmAction({
      message: t(
        `Xóa toàn bộ ${folderJobs.length} job và file trong thư mục này?`,
        `Delete all ${folderJobs.length} jobs and files in this folder?`,
      ),
      confirmLabel: t("Xóa thư mục", "Delete folder"),
      run: () => (async () => {
        // Optimistic: remove this folder's jobs immediately using the same key
        // the group uses (settings.outputDir, NOT job.outputFolder which is absolute)
        setJobs((current) => {
          const next: FlowJob[] = [];
          for (const job of current) {
            if (String(job.settings.outputDir || "").trim() === String(outputDir || "").trim()) {
              deletedIdsRef.current.add(job.id);
            } else {
              next.push(job);
            }
          }
          return next;
        });
        setApiError("");
        toast.success(t("Đã xóa thư mục và các job thành công.", "Folder and jobs deleted successfully."));
        // Fire file + backend delete in background
        await Promise.all(folderJobs.map((job) => deleteWebFlowOutputs(job)));
        flowRequest<{ ok: boolean }>(
          "/api/flow/jobs/delete-folder",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ outputDir, kind: folderJobs[0]?.kind || "" }),
          },
        ).catch(() => {});
      })().catch(() => {
        const msg = t(
          "Không thể xóa đầy đủ thư mục và file output.",
          "Could not fully delete the folder and its output files.",
        );
        setApiError(msg);
        toast.error(msg);
      }),
    });
  };
  const setDefaultAccount = async (id: string) => {
    const selected = accounts.find((account) => account.id === id);
    if (!selected) return;
    try {
      const saved = await flowRequest<FlowAccount>(`/api/flow/accounts/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: selected.label,
          email: selected.email,
          plan: selected.plan,
          projectId: selected.projectId || "",
          isDefault: true,
        }),
      });
      setAccounts((current) => current.map((account) =>
        account.id === saved.id
          ? { ...saved, isDefault: true }
          : { ...account, isDefault: false },
      ));
      setSettings((current) => ({ ...current, account: saved.label }));
      setApiError("");
      toast.success(t("Đã đặt tài khoản mặc định thành công.", "Default account updated successfully."));
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setApiError(msg);
      toast.error(msg);
    }
  };
  const connectAccount = (account: FlowAccount) => {
    const isReconnect = !!account.projectId;
    // Chỉ 1 toast ngay khi nhấn — kết quả sẽ được toast bởi useEffect watch status
    toast.info(
      isReconnect
        ? t("Đang thử kết nối lại...", "Attempting to reconnect...")
        : t("Đang mở Chrome để đăng nhập...", "Opening Chrome for login...")
    );
    void flowRequest<FlowAccount>(`/api/flow/accounts/${account.id}/connect`, {
      method: "POST",
    }).then((connected) => {
      setAccounts((current) => current.map((item) => item.id === connected.id ? normalizeFlowAccounts([connected])[0] : item));
    }).catch((error) => {
      const msg = error instanceof Error ? error.message : String(error);
      toast.error(t("Kết nối thất bại", "Connection failed") + ": " + msg);
    });
  };
  const syncAccount = (account: FlowAccount) => {
    if (syncingAccountIds.has(account.id)) return;
    setSyncingAccountIds((s) => new Set(s).add(account.id));
    toast.info(t("Đang đồng bộ tài khoản và cấu hình Flow...", "Syncing account and Flow capabilities..."));
    void flowRequest<FlowAccount>(`/api/flow/accounts/${account.id}/sync`, { method: "POST" })
      .then((updated) => {
        setAccounts((current) => current.map((item) => item.id === updated.id ? updated : item));
        toast.success(updated.capabilityStatus === "verified"
          ? t("Đã đồng bộ credits, model, tỷ lệ và thời lượng", "Credits, models, ratios, and durations synced")
          : t("Đã đồng bộ credits; cấu hình Flow đang dùng bản lưu gần nhất", "Credits synced; Flow capabilities are using the latest saved snapshot"));
      })
      .catch((error) => {
        const msg = error instanceof Error ? error.message : String(error);
        // Refresh accounts so the UI picks up any status change (e.g. reconnect)
        // that the backend wrote to JSON before raising the error.
        void flowRequest<{ accounts: FlowAccount[] }>("/api/flow/accounts")
          .then((data) => { if (data.accounts) setAccounts(normalizeFlowAccounts(data.accounts)); });
        if (msg.includes("SESSION_EXPIRED") || msg.includes("401")) {
          toast.error(t("Phiên đã hết hạn, vui lòng kết nối lại tài khoản", "Session expired — please reconnect the account"));
        } else {
          toast.error(t("Đồng bộ thất bại", "Sync failed") + ": " + msg);
        }
      })
      .finally(() => setSyncingAccountIds((s) => { const next = new Set(s); next.delete(account.id); return next; }));
  };
  const syncAllAccounts = async () => {
    const online = accounts.filter((a) => a.status === "online" && a.projectId);
    if (!online.length || isSyncingAll) return;
    setIsSyncingAll(true);
    setSyncingAccountIds(new Set(online.map((a) => a.id)));
    toast.info(t("Đang đồng bộ credits tất cả tài khoản...", "Syncing credits for all accounts..."));
    try {
      const results = await Promise.allSettled(
        online.map(async (account) => {
          try {
            const updated = await flowRequest<FlowAccount>(`/api/flow/accounts/${account.id}/sync`, { method: "POST" });
            setAccounts((current) => current.map((item) => item.id === updated.id ? updated : item));
            return updated;
          } finally {
            setSyncingAccountIds((s) => { const next = new Set(s); next.delete(account.id); return next; });
          }
        })
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      if (succeeded > 0) {
        toast.success(t(`Đã đồng bộ xong ${succeeded}/${online.length} tài khoản`, `Successfully synced ${succeeded}/${online.length} accounts`));
      } else {
        toast.error(t("Đồng bộ thất bại, vui lòng kiểm tra kết nối", "Sync failed, please check connection"));
      }
    } catch (error) {
      toast.error(t("Đồng bộ thất bại", "Sync failed") + ": " + (error instanceof Error ? error.message : String(error)));
    } finally {
      setIsSyncingAll(false);
      setSyncingAccountIds(new Set());
    }
  };
  const revealOutput = (jobId: string, outputIndex: number) =>
    void flowRequest(`/api/flow/jobs/${jobId}/outputs/${outputIndex}/reveal`, {
      method: "POST",
    }).catch((error) =>
      setApiError(error instanceof Error ? error.message : String(error)),
    );

  const pickOutputFolder = async (): Promise<string | undefined> => {
    try {
      const result = await flowRequest<{ path?: string }>(
        "/api/system/pick-folder",
        { method: "POST" },
      );
      return result.path || undefined;
    } catch (error) {
      if (
        error &&
        typeof error === "object" &&
        "name" in error &&
        error.name === "AbortError"
      ) {
        setApiError("");
        return undefined;
      }
      setApiError(error instanceof Error ? error.message : String(error));
      return undefined;
    }
  };
  const queueFolderLabel = (kind: CreateKind, outputDir: string, outputFolder = "", displayOutputFolder = "") => {
    const normalizedOutputFolder = outputFolder.replace(/\\/g, "/");
    // Never expose the web backend's temporary public directory as the output
    // folder. It is not a real user destination and cannot be merged later.
    if (isDesktopApp && outputFolder && !normalizedOutputFolder.includes("/backend/public/")) return outputFolder;
    if (displayOutputFolder) return displayOutputFolder;
    const configured = flowConfiguredOutputFolder(outputDir, kind);
    // Older jobs may only have `test` or `test/video`. The configured value
    // is canonical and always includes the complete Flow output location.
    return configured || flowOutputFolderParts(outputDir).join("/");
  };
  const openSrtImageWithFlowFolder = (outputFolder: string) => {
    // Pass the resolved absolute folder, not the editable suffix. The merge
    // page can then render from exactly this Flow output directory.
    onOpenSrtImage(outputFolder);
  };
  const pickWebOutputFolder = async () => {
    const picker = (window as BrowserDirectoryWindow).showDirectoryPicker;
    if (!picker) {
      setApiError(t(
        "Chrome hiện tại không hỗ trợ chọn thư mục tải xuống.",
        "This Chrome version does not support choosing a download folder.",
      ));
      return;
    }
    try {
      const handle = await picker({ mode: "readwrite" });
      await saveWebOutputRoot(WEB_OUTPUT_ROOT_KEY, handle);
      webOutputRootRef.current = handle;
      setWebOutputRootReady(true);
      setApiError("");
      toast.success(t(
        `Đã cấp quyền lưu tự động vào thư mục máy tính: ${handle.name}`,
        `Auto-save authorized for computer folder: ${handle.name}`,
      ));
      return `/${handle.name}/ZM_AI_TOOL/flow/${createKind}/`;
    } catch (error) {
      if (error && typeof error === "object" && "name" in error && error.name === "AbortError") return;
      setApiError(error instanceof Error ? error.message : String(error));
    }
  };



  const clearLogs = () => {
    setConfirmAction({
      message: t("Xóa toàn bộ log Flow?", "Clear all Flow logs?"),
      confirmLabel: t("Xóa log", "Clear logs"),
      run: () => flowRequest("/api/flow/logs", { method: "DELETE" })
        .then(() => setLogs([]))
        .catch((error) => setApiError(error instanceof Error ? error.message : String(error))),
    });
  };
  const copyLogs = async () => {
    const text = logs
      .map((entry) => {
        const context = [
          entry.jobId ? `job=${entry.jobId}` : "",
          entry.accountId ? `account=${entry.accountId}` : "",
        ]
          .filter(Boolean)
          .join(" ");
        const details = Object.keys(entry.details || {}).length
          ? ` details=${JSON.stringify(entry.details)}`
          : "";
        return `[${new Date(entry.createdAt * 1000).toISOString()}] [${entry.level.toUpperCase()}] ${entry.event}${context ? ` ${context}` : ""}${entry.message ? ` - ${entry.message}` : ""}${details}`;
      })
      .join("\n");
    try {
      await copyText(text, t('Đã sao chép log.', 'Logs copied.'));
      setLogsCopied(true);
      window.setTimeout(() => setLogsCopied(false), 1800);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : String(error));
    }
  };
  const logEventText = (event: string) => {
    const labels: Record<string, string> = {
      account_connecting: t("Đang kết nối tài khoản", "Connecting account"),
      account_connected: t("Đã kết nối tài khoản", "Account connected"),
      account_connect_failed: t(
        "Kết nối tài khoản thất bại",
        "Account connection failed",
      ),
      job_queued: t("Đã thêm vào hàng đợi", "Added to queue"),
      job_started: t("Bắt đầu xử lý", "Processing started"),
      browser_ready: t("Session trình duyệt sẵn sàng", "Browser session ready"),
      generation_submitted: t("Đã gửi yêu cầu tạo", "Generation submitted"),
      output_downloaded: t("Đã tải output", "Output downloaded"),
      job_completed: t("Job hoàn thành", "Job completed"),
      job_cancel_requested: t("Đã yêu cầu hủy", "Cancellation requested"),
      job_cancelled: t("Job đã hủy", "Job cancelled"),
      job_retry: t("Đang chạy lại job", "Retrying job"),
      job_failed: t("Job gặp lỗi", "Job failed"),
    };
    return labels[event] || event;
  };

  return (
    <main className="flow-page">
      <aside
        className={`flow-rail ${railOpen ? "is-open" : ""}`}
        aria-label={t("Điều hướng Flow", "Flow navigation")}
      >
        <button
          className="flow-rail-toggle"
          type="button"
          onClick={() => setRailOpen((open) => !open)}
          aria-label={
            railOpen
              ? t("Thu gọn menu", "Collapse menu")
              : t("Mở rộng menu", "Expand menu")
          }
          aria-expanded={railOpen}
        >
          <span />
          <span />
          <span />
        </button>
        <nav>
          {(
            [
              ["createImage", IconImage, t("Tạo ảnh", "Create image")],
              ["createVideo", IconVideo, t("Tạo video", "Create video")],
              ["series", IconBook, t("Series", "Series")],
              ["queue", IconBatch, t("Hàng đợi", "Queue")],
              ["history", IconClock, t("Lịch sử", "History")],
              ["accounts", IconGear, t("Tài khoản", "Accounts")],
              ["logs", IconLog, t("Log", "Logs")],
              ["help", IconBook, t("Trợ giúp", "Help")],
            ] as const
          ).map(([id, Icon, label]) => (
            <button
              key={id}
              type="button"
              className={
                ((!utilityView && id === "createImage" &&
                  tab === "create" &&
                  createKind === "image") ||
                  (!utilityView && id === "createVideo" &&
                    tab === "create" &&
                    createKind === "video") ||
                  (!utilityView && id === tab) ||
                  id === utilityView
                  ? "is-active "
                  : "") + (id === "accounts" || id === "help" ? "is-muted" : "")
              }
              onClick={() => activateRail(id)}
              title={label}
              aria-label={label}
            >
              <Icon size={18} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="flow-rail-credit">
          <strong>
            {displayedAccount?.credits != null
              ? displayedAccount.credits.toLocaleString()
              : "—"}
          </strong>
          <span>
            {displayedAccount?.credits != null
              ? t("credits còn lại", "credits left")
              : t("Chưa đồng bộ", "Not synced")}
          </span>
        </div>
      </aside>
      <section className="flow-workspace">
        <div className="flow-heading">
          <BackTitle onBack={onBack}>
            <span className="flow-page-title">
              Flow (Veo 3)
              <small>
                {t(
                  "Tạo ảnh và video bằng tài khoản Google Pro/Ultra.",
                  "Create images and videos with Google Pro/Ultra accounts.",
                )}
              </small>
            </span>
          </BackTitle>
          <div
            className={`flow-api-state ${backendReady ? "is-online" : "is-offline"}`}
            role="status"
          >
            <span />
            {backendReady
              ? t("Backend Flow đã kết nối", "Flow backend connected")
              : t("Backend Flow chưa sẵn sàng", "Flow backend unavailable")}
            {apiError && <small>{apiError}</small>}
          </div>
        </div>
        {utilityView === "accounts" && (
          <section className="flow-accounts">
            <header>
              <div>
                <h2>{t("Tài khoản Google Flow", "Google Flow accounts")}</h2>
                <p>
                  {t(
                    "Quản lý Chrome profile và phiên đăng nhập riêng cho từng tài khoản Pro/Ultra.",
                    "Manage a separate Chrome profile and login session for each Pro/Ultra account.",
                  )}
                </p>
              </div>
              <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                <button
                  type="button"
                  className="flow-account-add is-ghost"
                  title={t("Đồng bộ credits tất cả tài khoản", "Sync credits for all accounts")}
                  onClick={() => void syncAllAccounts()}
                  disabled={isSyncingAll || !accounts.some((a) => a.status === "online" && a.projectId)}
                  style={{ display: "flex", alignItems: "center", gap: "6px" }}
                >
                  <IconRefresh
                    size={14}
                    style={{
                      animation: isSyncingAll ? "spin 0.8s linear infinite" : "none",
                    }}
                  />
                  {isSyncingAll
                    ? t("Đang đồng bộ...", "Syncing...")
                    : t("Đồng bộ tất cả", "Sync all")}
                </button>
                <button
                  type="button"
                  className="flow-account-add"
                  onClick={addAccount}
                >
                  + {t("Thêm tài khoản", "Add account")}
                </button>
              </div>
            </header>
            {editingAccount && (
              <form
                className="flow-account-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  saveAccount();
                }}
              >
                <div>
                  <h3>
                    {editingAccount === "new"
                      ? t("Thêm tài khoản", "Add account")
                      : t("Sửa tài khoản", "Edit account")}
                  </h3>
                  <p>
                    {t(
                      "Thông tin dùng để liên kết Chrome profile và session Flow.",
                      "Details used to bind the Chrome profile and Flow session.",
                    )}
                  </p>
                </div>
                <label>
                  <span>{t("Tên hiển thị", "Display name")}</span>
                  <input
                    autoFocus
                    value={accountDraft.label}
                    onChange={(event) =>
                      setAccountDraft((current) => ({
                        ...current,
                        label: event.target.value,
                      }))
                    }
                    placeholder="Ultra 01"
                  />
                </label>
                <label>
                  <span>Email Google</span>
                  <input
                    type="email"
                    value={accountDraft.email}
                    onChange={(event) =>
                      setAccountDraft((current) => ({
                        ...current,
                        email: event.target.value,
                      }))
                    }
                    placeholder="name@gmail.com"
                  />
                </label>

                <footer>
                  <button type="button" onClick={() => setEditingAccount(null)}>
                    {t("Hủy", "Cancel")}
                  </button>
                  <button
                    type="submit"
                    disabled={
                      !accountDraft.label.trim() || !accountDraft.email.trim()
                    }
                  >
                    {t("Lưu", "Save")}
                  </button>
                </footer>
              </form>
            )}
            <div className="flow-account-grid">
              {accounts.map((account) => (
                <article
                  key={account.id}
                  className={account.isDefault ? "is-default" : ""}
                >
                  <div className="flow-account-head">
                    <span>{account.plan === "Free" || account.plan === "Pro" || account.plan === "Ultra"
                      ? account.plan
                      : t("Chưa xác minh", "Unverified")}</span>
                    <mark className={account.status}>
                      {account.status === "online"
                        ? t("Online", "Online")
                        : account.status === "connecting"
                          ? t("Đang kết nối", "Connecting")
                          : t("Cần kết nối lại", "Reconnect needed")}
                    </mark>
                  </div>
                  <div className="flow-account-name-row">
                    <h3>
                      {account.label}
                      {account.isDefault && (
                        <small>{t("Mặc định", "Default")}</small>
                      )}
                    </h3>
                    {account.status === "online" && account.projectId && (
                      <button
                        type="button"
                        className="flow-account-sync-status-btn"
                        title={t("Đồng bộ credits", "Sync credits")}
                        disabled={syncingAccountIds.has(account.id)}
                        onClick={() => syncAccount(account)}
                      >
                        <IconRefresh
                          size={12}
                          style={{
                            animation: syncingAccountIds.has(account.id) ? "spin 1s linear infinite" : "none",
                          }}
                        />
                      </button>
                    )}
                  </div>
                  <p>{account.email}</p>
                  <small className="flow-account-plan-source">
                    {account.planStatus === "verified"
                      ? t("Đã xác minh từ Flow", "Verified from Flow")
                      : t("Cần đồng bộ gói trước khi tạo", "Sync plan before generation")}
                  </small>

                  <div className="flow-account-credits">
                    <div className="flow-account-credits-head">
                      <strong>
                        {account.credits != null
                          ? account.credits.toLocaleString()
                          : "—"}
                      </strong>
                      <span>
                        {account.credits != null
                          ? t("credits còn lại", "credits left")
                          : t("Chưa đồng bộ credits", "Credits not synced")}
                      </span>
                    </div>
                    {account.credits != null && account.used > 0 && (
                      <>
                        <i>
                          <em
                            style={{
                              width: `${Math.max(8, 100 - account.used)}%`,
                            }}
                          />
                        </i>
                        <small>
                          {account.used}% {t("đã dùng", "used")}
                        </small>
                      </>
                    )}
                  </div>
                  <footer>
                    {account.isDefault ? (
                      <span>{t("Đang dùng mặc định", "Current default")}</span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => void setDefaultAccount(account.id)}
                      >
                        {t("Đặt mặc định", "Set default")}
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={account.status === "connecting"}
                      onClick={() => connectAccount(account)}
                    >
                      {account.status === "connecting"
                        ? t("Đang mở Chrome…", "Opening Chrome…")
                        : account.status === "online"
                          ? t("Đăng nhập lại", "Reconnect")
                          : t("Kết nối", "Connect")}
                    </button>
                    <button type="button" onClick={() => editAccount(account)}>
                      {t("Sửa", "Edit")}
                    </button>
                    <button
                      type="button"
                      className="is-danger"
                      disabled={account.isDefault}
                      onClick={() => deleteAccount(account)}
                    >
                      {t("Xóa", "Delete")}
                    </button>
                  </footer>
                </article>
              ))}
            </div>
            <aside className="flow-account-note">
              <IconGear size={18} />
              {t(
                "Mỗi tài khoản dùng Chrome profile và hàng đợi riêng. Bấm Kết nối rồi đăng nhập Google trong cửa sổ Chrome.",
                "Each account uses its own Chrome profile and queue. Click Connect, then sign in to Google in the Chrome window.",
              )}
            </aside>
          </section>
        )}
        {utilityView === "help" && (
          <FlowTemplatesPanel />
        )}
        {utilityView === "series" && (
          <FlowSeriesPanel
            accounts={accounts.map((acc) => ({
              id: acc.id, label: acc.label, status: acc.status, plan: acc.plan,
              capabilityCatalog: acc.capabilityCatalog, capabilityStatus: acc.capabilityStatus,
            }))}
            onOpenScene={(context) => {
              setSeriesDraft(context);
              setUtilityView(null);
              selectCreateKind(context.artifact === "keyframe" ? "image" : "video");
              setImageMode("reference");
              setPrompt(context.scenePrompt);
              setPromptInputType("prompt");
              setSourceFiles([]);
              setTab("create");
              writeFlowRoutePanel(context.artifact === "keyframe" ? "image" : "video");
            }}
            onGenerateAnchor={async (seriesId, anchorPrompt) => {
              const account = selectedFlowAccount(accounts, settings.account);
              if (!account) throw new Error(t("Cần tài khoản Flow đã kết nối để tạo ảnh neo.", "A connected Flow account is required to generate an anchor image."));
              const accountSettings = applyAccountCapabilities(account, settings, "image", settings.imageModel);
              const imageSettings = { ...accountSettings, model: accountSettings.imageModel, ratio: accountSettings.imageRatio, count: 1 };
              const created = await flowRequest<{ jobs: Array<Record<string, unknown>> }>(`/api/flow/series/${seriesId}/anchors/generate`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ prompt: anchorPrompt, accountId: account.id, settings: imageSettings }),
              });
              const added = normalizeFlowJobs(created.jobs, accounts);
              setJobs((current) => [...added, ...current.filter((item) => !added.some((job) => job.id === item.id))]);
              return added[0]?.id || "";
            }}
          />
        )}
        {!utilityView && (
          <div className="flow-tabs" role="tablist">
            {(
              [
                ["create", t("Tạo nội dung", "Create")],
                ["queue", t("Hàng đợi", "Queue")],
                ["history", t("Lịch sử", "History")],
                ["logs", t("Log", "Logs")],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={tab === id ? "is-active" : ""}
                onClick={() => {
                  setTab(id);
                  writeFlowRoutePanel(id === "create" ? (createKind === "image" ? "image" : "video") : id);
                }}
              >
                {id === "create" ? (
                  <IconVideo size={16} />
                ) : id === "queue" ? (
                  <IconBatch size={16} />
                ) : id === "history" ? (
                  <IconClock size={16} />
                ) : (
                  <IconBook size={16} />
                )}
                {label}
              </button>
            ))}
          </div>
        )}
        {!utilityView && showCreate && (
          <div className="flow-create-grid">
            {seriesDraft && (
              <div className="flow-series-breadcrumb">
                <span>{t("Series", "Series")} › {seriesDraft.seriesTitle} › {seriesDraft.episodeTitle} › {seriesDraft.sceneTitle}</span>
                <small>{t("Bible và ảnh continuity sẽ được áp dụng khi gửi cảnh này.", "The Bible and continuity images are applied when this scene is submitted.")}</small>
                <button type="button" onClick={() => { setUtilityView("series"); writeFlowRoutePanel("series"); }}>{t("Quay về Series", "Back to Series")}</button>
              </div>
            )}
            <section className="flow-card flow-prompt-card">
              {createKind === "image" && (
                <div
                  className="flow-image-modes"
                  role="tablist"
                  aria-label={t("Chế độ tạo ảnh", "Image generation mode")}
                >
                  {(
                    [
                      ["text", t("Text → Ảnh", "Text → Image")],
                      ["edit", t("Ảnh → Ảnh", "Image → Image")],
                      ["reference", t("Tham chiếu → Ảnh", "Reference → Image")],
                    ] as const
                  ).map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      role="tab"
                      aria-selected={imageMode === id}
                      className={imageMode === id ? "is-active" : ""}
                      onClick={() => {
                        setImageMode(id);
                        setSourceFiles([]);
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
              {createKind === "image" && imageMode !== "text" && (
                <div className="flow-source-row">
                  <div>
                    <IconImage size={20} />
                    <span>
                      <b>
                        {imageMode === "edit"
                          ? t("Ảnh nguồn", "Source image")
                          : t("Ảnh tham chiếu", "Reference images")}
                      </b>
                      <small>
                        {imageMode === "edit"
                          ? t(
                            "Một ảnh để chỉnh sửa hoặc biến thể",
                            "One image to edit or create variants",
                          )
                          : t(
                            "Tối đa 3 ảnh giữ nhân vật/phong cách",
                            "Up to 3 images for subject/style consistency",
                          )}
                      </small>
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => sourceRef.current?.click()}
                  >
                    {t("Chọn ảnh", "Choose images")}
                  </button>
                  <input
                    ref={sourceRef}
                    type="file"
                    hidden
                    accept="image/png,image/jpeg,image/webp"
                    multiple={imageMode === "reference"}
                    onChange={(event) =>
                      setSourceFiles(
                        Array.from(event.target.files || []).slice(
                          0,
                          imageMode === "reference" ? 3 : 1,
                        ),
                      )
                    }
                  />
                  {sourceFiles.length > 0 && (
                    <p>
                      {sourceFiles.map((file) => (
                        <mark key={`${file.name}-${file.lastModified}`}>
                          {file.name}
                          <button
                            type="button"
                            onClick={() =>
                              setSourceFiles((current) =>
                                current.filter((item) => item !== file),
                              )
                            }
                            aria-label={t(
                              `Bỏ ${file.name}`,
                              `Remove ${file.name}`,
                            )}
                          >
                            ×
                          </button>
                        </mark>
                      ))}
                    </p>
                  )}
                </div>
              )}
              <div className="flow-card-title">
                <b>
                  {t(
                    `1. Prompt ${createKind === "video" ? "video" : "ảnh"}`,
                    `1. ${createKind === "video" ? "Video" : "Image"} prompt`,
                  )}
                </b>
                <div className="flow-prompt-actions">
                  <span>
                    {promptCount} {t("prompt", "prompts")}
                  </span>
                  <button type="button" onClick={() => void pastePrompt()}>
                    {t("Dán", "Paste")}
                  </button>
                  <button
                    type="button"
                    className="is-danger"
                    disabled={!prompt}
                    onClick={() => {
                      setPrompt("");
                      setPromptInputType("prompt");
                      setImportName("");
                    }}
                  >
                    {t("Xóa", "Clear")}
                  </button>
                </div>
              </div>
              <textarea
                value={prompt}
                onChange={(event) => {
                  setPrompt(event.target.value);
                  setPromptInputType("prompt");
                  setImportName("");
                }}
                placeholder={
                  createKind === "video"
                    ? t(
                      "Mô tả cảnh, chuyển động camera và âm thanh mong muốn.",
                      "Describe the scene, camera movement, and desired audio.",
                    )
                    : t(
                      "Mô tả chủ thể, bối cảnh, ánh sáng và phong cách ảnh.",
                      "Describe the subject, setting, lighting, and image style.",
                    )
                }
              />
              <div className="flow-prompt-foot">
                <span>
                  {t(
                    "Mỗi đoạn cách nhau một dòng trống.",
                    "One blank line separates each prompt.",
                  )}
                </span>
                <span>{prompt.length.toLocaleString()} {t("ký tự", "characters")}</span>
              </div>
              <div className="flow-import">
                <div className="flow-import-row">
                  <button type="button" onClick={() => fileRef.current?.click()}>
                    <span className="flow-paperclip">⌕</span>
                    {t("Nhập TXT / CSV / JSON", "Import TXT / CSV / JSON")}
                  </button>
                  <span>
                    {t(
                      "TXT: mỗi đoạn một prompt · CSV/JSON: lấy cột prompt.",
                      "TXT: one prompt per block · CSV/JSON: reads the prompt field.",
                    )}
                  </span>
                </div>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".txt,.csv,.json,text/plain,application/json"
                  onChange={(event) => importPrompts(event.target.files?.[0])}
                />
                {importName && (
                  <small>
                    {t("Đã thêm", "Added")}: {importName}
                  </small>
                )}
              </div>
            </section>
            <section className="flow-card flow-settings-card">
              <div className="flow-card-title">
                <b>{t("2. Cài đặt nhanh", "2. Quick settings")}</b>
                <button
                  className="flow-text-button"
                  type="button"
                  onClick={() => setAdvancedOpen((open) => !open)}
                  aria-expanded={advancedOpen}
                >
                  <IconGear size={15} />
                  {advancedOpen
                    ? t("Thu gọn", "Collapse")
                    : t("Nâng cao", "Advanced")}
                </button>
              </div>
              {createKind === "video" && selectedFlowAccount(accounts, settings.account)?.plan === "Free" && (
                <div style={{ padding: "8px 12px", background: "rgba(234, 179, 8, 0.12)", borderRadius: "6px", color: "var(--color-warning, #eab308)", fontSize: "12px", marginBottom: "8px" }}>
                  ⚠️ {t("Tài khoản gói thường chỉ hỗ trợ tạo ảnh (Nano Banana 2). Để tạo video cần gói Pro hoặc Ultra.", "Free accounts only support image generation (Nano Banana 2). Video requires a Pro or Ultra plan.")}
                </div>
              )}
              <div className="flow-settings-grid">
                <FlowSelect
                  label={t("Model", "Model")}
                  value={settings.model}
                  onChange={(model) =>
                    setSettings((current) => applyAccountCapabilities(displayedAccount, current, createKind, model))
                  }
                  options={modelOptions}
                />
                <FlowSelect
                  label={t("Tỷ lệ", "Ratio")}
                  value={createKind === "image" ? settings.imageRatio : settings.ratio}
                  onChange={(value) =>
                    setSettings((current) =>
                      createKind === "image"
                        ? { ...current, imageRatio: value }
                        : { ...current, ratio: value }
                    )
                  }
                  options={ratioOptions}
                />
                {createKind === "video" ? (
                  <>
                    <FlowSelect
                      label={t("Thời lượng", "Duration")}
                      value={durationOptions.includes(settings.duration) ? settings.duration : durationOptions[0]}
                      onChange={(duration) =>
                        setSettings((current) => ({ ...current, duration }))
                      }
                      options={durationOptions}
                      disabled={durationOptions.length < 2}
                      suffix={t(" giây", " sec")}
                    />
                    <FlowSelect
                      label={t("Độ phân giải", "Resolution")}
                      value={resolutionOptions.includes(settings.resolution) ? settings.resolution : resolutionOptions[0]}
                      onChange={(resolution) =>
                        setSettings((current) => ({ ...current, resolution }))
                      }
                      options={resolutionOptions}
                    />
                  </>
                ) : (
                  <>
                    <FlowSelect
                      label={t("Độ phân giải", "Resolution")}
                      value={resolutionOptions.includes(settings.resolution) ? settings.resolution : resolutionOptions[0]}
                      onChange={(resolution) =>
                        setSettings((current) => ({ ...current, resolution }))
                      }
                      options={resolutionOptions}
                    />
                  </>
                )}
                <label>
                  <span>
                    {createKind === "video"
                      ? t("Số video", "Videos")
                      : t("Số ảnh", "Images")}
                  </span>
                  <div className="flow-counter">
                    <button
                      type="button"
                      onClick={() =>
                        setSettings((current) => ({
                          ...current,
                          ...(createKind === "image"
                            ? { imageCount: Math.max(1, (current.imageCount ?? 1) - 1) }
                            : { count: Math.max(1, current.count - 1) }),
                        }))
                      }
                      aria-label={t("Giảm số lượng", "Decrease quantity")}
                    >
                      −
                    </button>
                    <strong>{createKind === "image" ? (settings.imageCount ?? 1) : settings.count}</strong>
                    <button
                      type="button"
                      onClick={() =>
                        setSettings((current) => ({
                          ...current,
                          ...(createKind === "image"
                            ? { imageCount: Math.min(4, (current.imageCount ?? 1) + 1) }
                            : { count: Math.min(4, current.count + 1) }),
                        }))
                      }
                      aria-label={t("Tăng số lượng", "Increase quantity")}
                    >
                      +
                    </button>
                  </div>
                </label>
                <FlowSelect
                  label={t("Tài khoản", "Account")}
                  value={settings.account}
                  onChange={(account) =>
                    setSettings((current) => ({ ...current, account }))
                  }
                  options={accounts.map((account) => account.label)}
                  optionLabels={accountOptionLabels}
                  online
                  className="flow-select-account"
                />
              </div>
              {advancedOpen && (
                <div className="flow-advanced">
                  <FlowSelect
                    label={t("Chất lượng", "Quality")}
                    value={settings.quality}
                    onChange={(quality) =>
                      setSettings((current) => ({ ...current, quality }))
                    }
                    options={["Standard", "High"]}
                  />
                  <FlowSelect
                    label={t("Luồng chạy", "Concurrent jobs")}
                    value={settings.concurrency}
                    onChange={(concurrency) =>
                      setSettings((current) => ({ ...current, concurrency }))
                    }
                    options={Array.from({ length: 50 }, (_, index) => String(index + 1))}
                  />
                  <FlowSelect
                    label={t("Định dạng lưu", "Output format")}
                    value={settings.format}
                    onChange={(format) =>
                      setSettings((current) => ({ ...current, format }))
                    }
                    options={
                      createKind === "image" ? ["PNG", "JPG", "WebP"] : ["MP4"]
                    }
                  />
                  <label>
                    <span>{t("Tiền tố tên file", "Filename prefix")}</span>
                    <input
                      value={settings.filePrefix}
                      onChange={(event) =>
                        setSettings((current) => ({
                          ...current,
                          filePrefix: event.target.value,
                        }))
                      }
                    />
                  </label>
                  {createKind === "image" && imageMode !== "text" && (
                    <label className="flow-range">
                      <span>
                        {t("Mức bám ảnh tham chiếu", "Reference strength")} ·{" "}
                        {settings.referenceStrength}%
                      </span>
                      <input
                        type="range"
                        min="0"
                        max="100"
                        value={settings.referenceStrength}
                        onChange={(event) =>
                          setSettings((current) => ({
                            ...current,
                            referenceStrength: Number(event.target.value),
                          }))
                        }
                      />
                    </label>
                  )}
                </div>
              )}
              <div className="flow-output-row">
                <OutputFolderField
                  isDesktopApp={isDesktopApp}
                  value={settings.outputDir}
                  onChange={(outputDir) =>
                    setSettings((current) => ({ ...current, outputDir }))
                  }
                  onSave={() =>
                    setSettings((current) => {
                      localStorage.setItem(SETTINGS_KEY, JSON.stringify(current));
                      return current;
                    })
                  }
                  onChoose={isDesktopApp ? pickOutputFolder : pickWebOutputFolder}
                  defaultPath={t('Ví dụ: du-an-01 hoặc video-01.mp4', 'Example: project-01 or video-01.mp4')}
                  appFolder={`flow/${createKind}`}
                  label={t("3. Thư mục kết quả", "3. Output folder")}
                />



                {!isDesktopApp && (
                  <label className="flow-check">
                    <input
                      type="checkbox"
                      checked={settings.autoDownload}
                      onChange={(event) =>
                        setSettings((current) => ({
                          ...current,
                          autoDownload: event.target.checked,
                        }))
                      }
                    />
                    {t("Tự động tải về khi hoàn thành", "Auto-download when completed")}
                  </label>
                )}
              </div>
              <div className={`flow-create-actions ${createKind === "video" ? "has-preview" : ""} ${actionBusy ? "has-cancel" : ""}`}>
                {createKind === "video" && (
                  <button
                    type="button"
                    className="flow-preview-latest"
                    disabled={!latestCompletedVideo}
                    onClick={() =>
                      latestCompletedVideo &&
                      setPreview({ job: latestCompletedVideo, outputIndex: 0 })
                    }
                  >
                    <IconPlay size={16} />
                    {latestCompletedVideo
                      ? t("Xem trước video", "Preview video")
                      : t("Chưa có video", "No video yet")}
                  </button>
                )}
                <button
                  type="button"
                  className="flow-generate"
                  disabled={actionBusy}
                  onClick={() => void runAction(createFlowJobs)}
                >
                  <IconPlay size={17} />
                  {actionBusy && <span role="status">{t("Đang xử lý…", "Processing…")}</span>}
                  {createKind === "video"
                    ? seriesDraft?.artifact === "video" ? t("TẠO VIDEO CẢNH", "CREATE SCENE VIDEO") : t("TẠO VIDEO", "CREATE VIDEO")
                    : seriesDraft?.artifact === "keyframe" ? t("TẠO KEYFRAME", "CREATE KEYFRAME") : t("TẠO ẢNH", "CREATE IMAGES")}
                  <small>
                    {t(
                      "Gửi qua Chrome profile của tài khoản đã chọn",
                      "Sent through the selected account Chrome profile",
                    )}
                  </small>
                </button>
                {actionBusy && (
                  <button type="button" className="flow-text-button is-warning" onClick={() => void cancelCreateAction()}>
                    {t("Hủy", "Cancel")}
                  </button>
                )}
              </div>
            </section>
          </div>
        )}
        {!utilityView && (tab === "create" || tab === "queue") && (
          <section className="flow-card flow-queue-card">
            <div className="flow-card-title">
              <b>{t(`Hàng đợi (${jobs.length})`, `Queue (${jobs.length})`)}</b>
              <div className="flow-queue-tools">
                <button className="flow-text-button" type="button" disabled={actionBusy || !jobs.some((job) => job.status === "failed" || job.status === "cancelled")} onClick={retryAllJobs}>{t("Chạy lại tất cả", "Retry all")}</button>
                <button className="flow-text-button" type="button" disabled={!jobs.some((job) => job.status === "queued" || job.status === "processing")} onClick={cancelAllJobs}>{t("Hủy tất cả", "Cancel all")}</button>
                <button className="flow-text-button is-danger" type="button" disabled={!jobs.length} onClick={deleteAllJobs}>{t("Xóa tất cả", "Delete all")}</button>
              </div>
            </div>
            <div className="flow-queue-kind-tabs" role="tablist" aria-label={t("Loại hàng đợi", "Queue type")}>
              {[
                { kind: "all" as const, count: jobs.length },
                ...queueKindGroups.map(({ kind, folders }) => ({
                  kind,
                  count: folders.reduce((total, folder) => total + folder.jobs.length, 0),
                })),
              ].map(({ kind, count }) => (
                <button
                  key={kind}
                  type="button"
                  role="tab"
                  aria-selected={queueKind === kind}
                  className={queueKind === kind ? "is-active" : ""}
                  onClick={() => setQueueKind(kind)}
                >
                  {kind === "all" ? t("Tất cả", "All") : kind === "video" ? t("Video", "Videos") : t("Ảnh", "Images")} ({count})
                </button>
              ))}
            </div>
            <div className="flow-queue-list">
              {activeQueueGroups.map((group) => {
                const groupKey = `${group.kind}-${group.outputDir}`;
                const isCollapsed = !!collapsedFolders[groupKey];
                const summary = flowGroupProgress(group.jobs);
                const folderPathText = queueFolderLabel(group.kind, group.outputDir, group.outputFolder, group.displayOutputFolder);
                return (
                  <div key={groupKey} className="flow-queue-group-item">
                    <header className={`flow-queue-kind-header${isCollapsed ? " is-collapsed" : ""}`}>
                      <div className="flow-queue-header-row">
                        <button
                          type="button"
                          className="flow-queue-folder-toggle"
                          onClick={() => toggleFolderCollapsed(groupKey)}
                          aria-expanded={!isCollapsed}
                          aria-label={isCollapsed ? t("Mở rộng danh sách", "Expand list") : t("Thu nhỏ danh sách", "Collapse list")}
                        >
                          <IconChevronDown
                            size={14}
                            style={{
                              transform: isCollapsed ? "rotate(-90deg)" : "rotate(0deg)",
                              transition: "transform 180ms cubic-bezier(0.4, 0, 0.2, 1)",
                            }}
                          />
                        </button>
                        <div
                          className="flow-queue-folder-path"
                          onClick={() => toggleFolderCollapsed(groupKey)}
                          title={folderPathText}
                        >
                          <small>{folderPathText}</small>
                        </div>
                        <div className="flow-queue-folder-actions">
                          <button className="flow-text-button" type="button" onClick={() => openSrtImageWithFlowFolder(queueFolderLabel(group.kind, group.outputDir, group.outputFolder, group.displayOutputFolder))}>{t("Ghép", "Merge")}</button>
                          {group.jobs.some((job) => job.status === "failed" || job.status === "cancelled") && (
                            <button className="flow-text-button is-retry" type="button" disabled={actionBusy} onClick={() => retryFolderJobs(group.outputDir, group.kind, group.jobs)}>
                              {t("Chạy lại", "Retry")}
                            </button>
                          )}
                          <button className="flow-text-button is-warning" type="button" disabled={actionBusy || !group.jobs.some((job) => job.status === "queued" || job.status === "processing")} onClick={() => cancelFolderJobs(group.outputDir, group.jobs)}>{t("Hủy", "Cancel")}</button>
                          <button className="flow-text-button is-danger" type="button" onClick={() => deleteFolderJobs(group.outputDir, group.jobs)}>{t("Xóa", "Delete")}</button>
                        </div>
                      </div>
                      <div className="flow-queue-folder-summary">
                        <span>{t("Tiến độ tổng", "Overall progress")}</span>
                        <div
                          className="flow-queue-folder-progress"
                          role="progressbar"
                          aria-label={t("Tiến độ tổng của thư mục", "Overall folder progress")}
                          aria-valuemin={0}
                          aria-valuemax={100}
                          aria-valuenow={summary.progress}
                        >
                          <i style={{ width: `${summary.progress}%` }} />
                        </div>
                        <strong>{summary.progress}%</strong>
                        <small>{t(`${summary.completed}/${summary.total} hoàn thành`, `${summary.completed}/${summary.total} completed`)}</small>
                      </div>
                    </header>
                    {!isCollapsed && group.jobs.map((job) => (
                      <article
                        key={job.id}
                        className={`flow-queue-job flow-queue-job--${job.status}`}
                      >
                        <button
                          className="flow-job-thumb"
                          type="button"
                          disabled={!job.outputs?.length}
                          onClick={() =>
                            job.outputs?.length && setPreview({ job, outputIndex: 0 })
                          }
                          aria-label={
                            job.outputs?.length
                              ? t("Xem trước output", "Preview output")
                              : t("Output chưa sẵn sàng", "Output not ready")
                          }
                        >
                          {job.outputs?.length ? (
                            job.kind === "video" ? (
                              <video
                                src={`/api/flow/jobs/${job.id}/outputs/0`}
                                muted
                                playsInline
                                preload="metadata"
                              />
                            ) : (
                              <img
                                src={`/api/flow/jobs/${job.id}/outputs/0`}
                                alt=""
                                loading="lazy"
                              />
                            )
                          ) : job.kind === "video" ? (
                            <IconPlay size={15} />
                          ) : (
                            <IconImage size={17} />
                          )}
                        </button>
                        <div>
                          <strong>
                            {String(job.index).padStart(3, "0")} · {job.prompt}
                          </strong>
                          <span>
                            {job.kind === "video"
                              ? `${job.settings.model} · ${job.settings.ratio} · ${job.settings.duration}s`
                              : `${job.settings.model} · ${job.settings.ratio} · ${job.settings.resolution}`}{" "}
                            · {job.account}
                          </span>
                          <div className="flow-job-progress">
                            <i>
                              <em style={{ width: `${job.progress}%` }} />
                            </i>
                            <small>{job.progress}%</small>
                          </div>
                          {job.error && (
                            <small className="flow-job-error">{jobErrorText(job.error)}</small>
                          )}
                        </div>
                        <aside>
                          <mark>{jobStatusText(job)}</mark>
                          <div className="flow-job-actions">
                            {(job.status === "queued" ||
                              job.status === "processing") && (
                                <button type="button" onClick={() => cancelJob(job.id)}>
                                  {t("Hủy", "Cancel")}
                                </button>
                              )}
                            {(job.status === "failed" ||
                              job.status === "cancelled") && (
                                <button type="button" onClick={() => retryJob(job.id)}>
                                  {t("Chạy lại", "Retry")}
                                </button>
                              )}
                            <button
                              type="button"
                              className="is-danger"
                              onClick={() => deleteJob(job.id)}
                            >
                              {t("Xóa", "Delete")}
                            </button>
                          </div>
                          {job.outputs?.length ? (
                            <div className="flow-queue-outputs">
                              {job.outputs.map((_output, outputIndex) => (
                                <span key={outputIndex}>
                                  <button
                                    type="button"
                                    onClick={() => setPreview({ job, outputIndex })}
                                  >
                                    {t("Xem trước", "Preview")}{" "}
                                    {job.outputs!.length > 1 ? outputIndex + 1 : ""}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() =>
                                      revealOutput(job.id, outputIndex)
                                    }
                                  >
                                    {t("Mở thư mục", "Open folder")}
                                  </button>
                                  {!isDesktopApp && (
                                    <a
                                      href={`/api/flow/jobs/${job.id}/outputs/${outputIndex}?download=1`}
                                      download
                                    >
                                      {t("Tải về", "Download")}
                                    </a>
                                  )}
                                </span>

                              ))}
                            </div>
                          ) : null}
                        </aside>
                      </article>
                    ))}
                  </div>
                );
              })}
            </div>
          </section>
        )}
        {!utilityView && tab === "create" && (
          <section className="flow-card flow-results">
            <div className="flow-card-title">
              <b>{t("Kết quả gần đây", "Recent results")}</b>
              <button
                className="flow-text-button"
                type="button"
                onClick={() => setTab("history")}
              >
                {t("Xem tất cả", "View all")}
              </button>
            </div>
            <div className="flow-result-grid">
              {jobs
                .filter(
                  (job) => job.kind === createKind && job.status === "done",
                )
                .slice(0, 4)
                .map((job) => (
                  <article key={job.id}>
                    <button
                      className="flow-result-thumb"
                      type="button"
                      onClick={() => setPreview({ job, outputIndex: 0 })}
                      aria-label={t("Xem trước output", "Preview output")}
                    >
                      {job.kind === "video" ? (
                        <video
                          src={`/api/flow/jobs/${job.id}/outputs/0`}
                          muted
                          playsInline
                          preload="metadata"
                        />
                      ) : (
                        <img
                          src={`/api/flow/jobs/${job.id}/outputs/0`}
                          alt={job.prompt}
                          loading="lazy"
                        />
                      )}
                      <mark>{t("Hoàn thành", "Completed")}</mark>
                    </button>
                    <strong>{job.prompt}</strong>
                    <span>{job.id}</span>
                    <p>
                      {job.outputs?.length || 0}{" "}
                      {job.kind === "video"
                        ? t("video", "videos")
                        : t("ảnh", "images")}
                    </p>
                    <footer>
                      {job.outputs?.map((_output, outputIndex) => (
                        <span className="flow-output-actions" key={outputIndex}>
                          <button
                            type="button"
                            onClick={() => setPreview({ job, outputIndex })}
                          >
                            {t("Xem trước", "Preview")}
                          </button>
                          <button
                            type="button"
                            onClick={() => revealOutput(job.id, outputIndex)}
                          >
                            {t("Mở thư mục", "Open folder")}
                          </button>
                          {!isDesktopApp && (
                            <a
                              href={`/api/flow/jobs/${job.id}/outputs/${outputIndex}?download=1`}
                              download
                            >
                              <IconDownload size={15} />
                              {t("Tải về", "Download")}
                            </a>
                          )}
                        </span>
                      ))}
                    </footer>
                  </article>
                ))}
              {!jobs.some(
                (job) => job.kind === createKind && job.status === "done",
              ) && (
                  <div className="flow-results-empty">
                    {t(
                      "Chưa có kết quả thật. Kết quả tải xong sẽ xuất hiện tại đây.",
                      "No real results yet. Completed downloads will appear here.",
                    )}
                  </div>
                )}
            </div>
          </section>
        )}
        {!utilityView && (tab === "history" || tab === "queue") && (
          <section className="flow-card flow-history">
            <div className="flow-card-title">
              <b>{t("Lịch sử nhiệm vụ", "Task history")}</b>
              <span>{t("Dữ liệu backend", "Backend data")}</span>
            </div>
            <div className="flow-history-scroll">
              <table>
                <thead>
                  <tr>
                    {[
                      t("Thời gian", "Time"),
                      t("Loại", "Type"),
                      t("Prompt", "Prompt"),
                      t("Model", "Model"),
                      t("Tài khoản", "Account"),
                      t("TT", "Status"),
                      t("Output", "Output"),
                      t("Tác vụ", "Actions"),
                    ].map((label) => (
                      <th key={label}>{label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...jobs].sort((left, right) => right.createdAt - left.createdAt).map((job, index) => (
                    <tr key={job.id}>
                      <td>24/08 · 14:{30 - index * 4}</td>
                      <td>
                        <mark>
                          {job.kind === "video"
                            ? t("Video", "Video")
                            : t("Ảnh", "Image")}
                        </mark>
                      </td>
                      <td title={job.prompt}>{job.prompt}</td>
                      <td title={job.settings.model}>
                        {job.settings.model}
                      </td>
                      <td title={job.account}>{job.account}</td>
                      <td>
                        <mark className={`flow-status-${job.status}`}>
                          {jobStatusText(job)}
                        </mark>
                      </td>
                      <td>
                        {job.outputs?.length
                          ? job.outputs.map((_output, outputIndex) => (
                            <span
                              className="flow-output-actions"
                              key={outputIndex}
                            >
                              <button
                                type="button"
                                onClick={() =>
                                  setPreview({ job, outputIndex })
                                }
                              >
                                {t("Xem", "View")}{" "}
                                {(job.outputs?.length || 0) > 1
                                  ? outputIndex + 1
                                  : ""}
                              </button>
                              {isDesktopApp ? (
                                <button
                                  type="button"
                                  onClick={() =>
                                    revealOutput(job.id, outputIndex)
                                  }
                                >
                                  {t("Mở", "Open")}
                                </button>
                              ) : (
                                <a
                                  href={`/api/flow/jobs/${job.id}/outputs/${outputIndex}?download=1`}
                                  download
                                >
                                  {t("Tải", "Save")}
                                </a>
                              )}
                            </span>
                          ))
                          : "—"}
                      </td>
                      <td>
                        <div className="flow-table-actions">
                          {job.status === "queued" ||
                            job.status === "processing" ? (
                            <button
                              type="button"
                              onClick={() => cancelJob(job.id)}
                            >
                              {t("Hủy", "Cancel")}
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => retryJob(job.id)}
                            >
                              {t("Chạy lại", "Retry")}
                            </button>
                          )}
                          <button
                            type="button"
                            className="is-danger"
                            onClick={() => deleteJob(job.id)}
                          >
                            {t("Xóa", "Delete")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
        {!utilityView && tab === "logs" && (
          <section className="flow-card flow-logs">
            <div className="flow-card-title">
              <div>
                <b>{t("Log hoạt động Flow", "Flow activity logs")}</b>
                <span>
                  {t(
                    "Theo dõi tạo nội dung, tải output và lỗi backend",
                    "Track generation, output downloads, and backend errors",
                  )}
                </span>
              </div>
              <div className="flow-log-actions">
                <button
                  type="button"
                  className="flow-log-copy"
                  onClick={() => void copyLogs()}
                  disabled={!logs.length}
                >
                  {logsCopied
                    ? t("Đã sao chép", "Copied")
                    : t("Sao chép log", "Copy logs")}
                </button>
                <button
                  type="button"
                  className="flow-log-clear"
                  onClick={clearLogs}
                  disabled={!logs.length}
                >
                  {t("Xóa log", "Clear logs")}
                </button>
              </div>
            </div>
            {logs.length ? (
              <div className="flow-log-list" role="log" aria-live="polite">
                {logs.map((entry) => {
                  const account = accounts.find(
                    (item) => item.id === entry.accountId,
                  );
                  const detailText = Object.keys(entry.details || {}).length
                    ? JSON.stringify(entry.details)
                    : "";
                  return (
                    <article
                      className={`flow-log-row is-${entry.level}`}
                      key={entry.id}
                    >
                      <time>
                        {new Date(entry.createdAt * 1000).toLocaleString(
                          locale === "vi" ? "vi-VN" : "en-US",
                        )}
                      </time>
                      <mark>{entry.level.toUpperCase()}</mark>
                      <div className="flow-log-message">
                        <strong>{logEventText(entry.event)}</strong>
                        {entry.message && <p>{entry.message}</p>}
                        {detailText && <code>{detailText}</code>}
                      </div>
                      <div className="flow-log-context">
                        {entry.jobId && <span>Job: {entry.jobId}</span>}
                        {entry.accountId && (
                          <span>
                            {t("Tài khoản", "Account")}:{" "}
                            {account?.label || entry.accountId}
                          </span>
                        )}
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="flow-logs-empty">
                <IconBook size={24} />
                <b>{t("Chưa có log Flow", "No Flow logs yet")}</b>
                <span>
                  {t(
                    "Log sẽ xuất hiện khi kết nối tài khoản hoặc chạy job.",
                    "Logs appear when an account connects or a job runs.",
                  )}
                </span>
              </div>
            )}
          </section>
        )}
        {actionBusy && <p role="status" aria-live="polite">{t("Đang xử lý yêu cầu, vui lòng chờ…", "Processing your request, please wait…")}</p>}
        {retryTarget && (
          <div className="flow-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setRetryTarget(null); }}>
            <section className="flow-confirm-dialog flow-retry-dialog" role="dialog" aria-modal="true" aria-labelledby="flow-retry-title">
              <header>
                <div><strong id="flow-retry-title">{t("Cài đặt chạy lại", "Retry settings")}</strong><small>{t(`${retryTarget.jobs.length} job lỗi hoặc đã hủy`, `${retryTarget.jobs.length} failed or cancelled jobs`)}</small></div>
                <button type="button" onClick={() => setRetryTarget(null)} aria-label={t("Đóng", "Close")}>×</button>
              </header>
              <div className="flow-retry-fields">
                <FlowSelect
                  label={t("Model", "Model")}
                  value={retryTarget.model}
                  onChange={(model) => setRetryTarget((current) => {
                    if (!current) return current;
                    const capability = retryCapabilities.find((item) => item.name === model);
                    const ratio = capability?.ratios.includes(current.ratio)
                      ? current.ratio
                      : capability?.defaultRatio || capability?.ratios[0] || current.ratio;
                    return { ...current, model, ratio };
                  })}
                  options={retryModelOptions}
                />
                <FlowSelect
                  label={t("Tỷ lệ", "Ratio")}
                  value={retryTarget.ratio}
                  onChange={(ratio) => setRetryTarget((current) => current ? { ...current, ratio } : current)}
                  options={retryRatioOptions}
                />
                <FlowSelect
                  label={t("Tài khoản", "Account")}
                  value={retryTarget.accountId}
                  onChange={(accountId) => setRetryTarget((current) => current ? { ...current, accountId } : current)}
                  options={accounts.filter((account) => account.status === "online").map((account) => account.id)}
                  optionLabels={Object.fromEntries(accounts.map((account) => [
                    account.id,
                    `${account.label} · ${t(`Gói ${account.plan}`, `${account.plan} plan`)}`,
                  ]))}
                />
                <FlowSelect
                  label={t("Luồng chạy", "Concurrent jobs")}
                  value={retryTarget.concurrency}
                  onChange={(concurrency) => setRetryTarget((current) => current ? { ...current, concurrency } : current)}
                  options={Array.from({ length: 50 }, (_, index) => String(index + 1))}
                />
              </div>
              <footer>
                <button type="button" onClick={() => setRetryTarget(null)}>{t("Quay lại", "Go back")}</button>
                <button type="button" className="is-primary" onClick={confirmRetryJob}>{t("Chạy lại", "Retry")}</button>
              </footer>
            </section>
          </div>
        )}
        {confirmAction && (
          <div
            className="flow-preview-backdrop"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setConfirmAction(null);
            }}
          >
            <section className="flow-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="flow-confirm-title">
              <header>
                <strong id="flow-confirm-title">{t("Xác nhận thao tác", "Confirm action")}</strong>
                <button type="button" onClick={() => setConfirmAction(null)} aria-label={t("Đóng", "Close")}>×</button>
              </header>
              <p>{confirmAction.message}</p>
              <footer>
                <button type="button" onClick={() => setConfirmAction(null)}>{t("Quay lại", "Go back")}</button>
                <button
                  type="button"
                  className="is-danger"
                  onClick={() => {
                    const run = confirmAction.run;
                    setConfirmAction(null);
                    void runAction(run);
                  }}
                >
                  {confirmAction.confirmLabel}
                </button>
              </footer>
            </section>
          </div>
        )}
        {preview && (
          <div
            className="flow-preview-backdrop"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setPreview(null);
            }}
          >
            <section
              className="flow-preview-dialog"
              role="dialog"
              aria-modal="true"
              aria-label={t("Xem trước kết quả", "Output preview")}
            >
              <header>
                <div>
                  <strong>
                    {allCompletedOutputs.length > 1 && currentPreviewIndex >= 0
                      ? `[${currentPreviewIndex + 1}/${allCompletedOutputs.length}] ${t("Xem trước kết quả", "Output preview")}`
                      : t("Xem trước kết quả", "Output preview")}
                  </strong>
                  <small>{preview.job.prompt}</small>
                </div>
                <button
                  type="button"
                  onClick={() => setPreview(null)}
                  aria-label={t("Đóng xem trước", "Close preview")}
                >
                  ×
                </button>
              </header>
              <div className="flow-preview-media">
                {previewMediaKind === "video" ? (
                  <video
                    key={previewSrc}
                    src={previewSrc}
                    controls
                    autoPlay
                  />
                ) : previewMediaKind === "audio" ? (
                  <audio key={previewSrc} src={previewSrc} controls autoPlay />
                ) : previewMediaKind === "image" ? (
                  <img
                    key={previewSrc}
                    src={previewSrc}
                    alt={preview.job.prompt}
                  />
                ) : (
                  <iframe
                    key={previewSrc}
                    src={previewSrc}
                    title={t("Xem trước tệp kết quả", "Output file preview")}
                  />
                )}
                {allCompletedOutputs.length > 1 && (
                  <>
                    <button
                      className="flow-preview-nav is-previous"
                      type="button"
                      onClick={() => movePreview(-1)}
                      aria-label={t("Kết quả trước", "Previous output")}
                      title={t("Kết quả trước (Phím ←)", "Previous output (Left Arrow)")}
                    >
                      <IconArrowRight size={22} />
                    </button>
                    <button
                      className="flow-preview-nav is-next"
                      type="button"
                      onClick={() => movePreview(1)}
                      aria-label={t("Kết quả tiếp theo", "Next output")}
                      title={t("Kết quả tiếp theo (Phím →)", "Next output (Right Arrow)")}
                    >
                      <IconArrowRight size={22} />
                    </button>
                    <span className="flow-preview-counter" aria-live="polite">
                      {currentPreviewIndex >= 0 ? currentPreviewIndex + 1 : preview.outputIndex + 1} / {allCompletedOutputs.length}
                    </span>
                  </>
                )}
              </div>
              <footer>
                <button
                  type="button"
                  onClick={() =>
                    revealOutput(preview.job.id, preview.outputIndex)
                  }
                >
                  {t("Mở thư mục", "Open folder")}
                </button>
                {!isDesktopApp && (
                  <a
                    href={`/api/flow/jobs/${preview.job.id}/outputs/${preview.outputIndex}?download=1`}
                    download
                  >
                    <IconDownload size={15} />
                    {t("Tải về", "Download")}
                  </a>
                )}
                <button type="button" onClick={() => setPreview(null)}>
                  {t("Đóng", "Close")}
                </button>
              </footer>
            </section>
          </div>
        )}
      </section>
    </main>
  );
}

function FlowSelect({
  label,
  value,
  onChange,
  options,
  suffix = "",
  online = false,
  disabled = false,
  optionLabels,
  className,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  suffix?: string;
  online?: boolean;
  disabled?: boolean;
  optionLabels?: Record<string, string>;
  className?: string;
}) {
  return (
    <label className={className}>
      <span>{label}</span>
      <div className="flow-select-wrap">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {optionLabels?.[option] || option}
              {suffix}
            </option>
          ))}
        </select>
        {online && <i>Online</i>}
      </div>
    </label>
  );
}
