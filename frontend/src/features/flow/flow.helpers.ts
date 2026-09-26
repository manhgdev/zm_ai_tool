import type {
  FlowSettings, FlowJob, FlowAccount, FlowSnapshot,
  FlowRoutePanel, CreateKind,
  BrowserDirectoryHandle,
} from "./flow.types";

// ── LocalStorage keys ─────────────────────────────────────────────────────────
export const DRAFT_VIDEO_KEY = "zm-flow-veo:draft-video:v1";
export const DRAFT_IMAGE_KEY = "zm-flow-veo:draft-image:v1";
export const DRAFT_LEGACY_KEY = "zm-flow-veo:draft:v1";
export const SETTINGS_KEY = "zm-flow-veo:settings:v1";
export const WEB_AUTO_DOWNLOAD_DEFAULT_KEY = "zm-flow-veo:web-auto-download:v1";
export const WEB_OUTPUT_ROOT_KEY = "zm-flow-veo:web-output-root:v1";
export const TAB_KEY = "zm-flow-veo:tab:v1";
export const RAIL_KEY = "zm-flow-veo:rail:v1";
export const ACCOUNTS_KEY = "zm-flow-veo:accounts:v1";
export const CREATE_KIND_KEY = "zm-flow-veo:create-kind:v1";
export const ACTIVE_PANEL_KEY = "zm-flow-veo:active-panel:v1";
export const IMAGE_MODE_KEY = "zm-flow-veo:image-mode:v1";
export const VIDEO_MODE_KEY = "zm-flow-veo:video-mode:v1";
export const COLLAPSED_FOLDERS_KEY = "zm-flow-veo:collapsed-folders:v1";

// ── Model lists ───────────────────────────────────────────────────────────────
export const FLOW_VIDEO_MODELS = [
  "Veo 3.1 - Lite",
  "Veo 3.1 - Fast",
  "Veo 3.1 - Quality",
  "Omni 1.1 Flash",
] as const;

export const FLOW_OMNI_FLASH_DURATIONS = ["4", "6", "8", "10"] as const;

export const FLOW_RESOLUTIONS = ["360p", "480p", "720p", "1080p"] as const;
export const FLOW_VIDEO_DOWNLOAD_QUALITIES = ["360p", "720p", "1080p", "4K"] as const;

/** Download menu options available for a Flow account plan (video only). */
export function flowVideoDownloadQualities(plan: string | undefined | null): string[] {
  const normalized = String(plan || "Free").trim();
  if (normalized === "Ultra") return ["360p", "720p", "1080p", "4K"];
  if (normalized === "Pro" || /^plus$/i.test(normalized)) return ["360p", "720p", "1080p"];
  // Free: 360p is the fastest/lowest Flow download tier; 720p still available.
  return ["360p", "720p"];
}

export const FLOW_IMAGE_MODELS = [
  "Nano Banana Pro",
  "Nano Banana 2",
  "Nano Banana 2 Lite",
] as const;

export const isVideoModel = (model: string) =>
  FLOW_VIDEO_MODELS.includes(model as (typeof FLOW_VIDEO_MODELS)[number]);

export const isImageModel = (model: string) =>
  FLOW_IMAGE_MODELS.includes(model as (typeof FLOW_IMAGE_MODELS)[number]);

// ── Settings helpers ──────────────────────────────────────────────────────────
export function settingsForCreateKind(settings: FlowSettings, kind: CreateKind): FlowSettings {
  const model = kind === "image" ? settings.imageModel : settings.videoModel;
  return {
    ...settings,
    model: model || (kind === "image" ? "Nano Banana 2" : "Veo 3.1 - Fast"),
  };
}

export function settingsWithSelectedModel(settings: FlowSettings, kind: CreateKind, model: string): FlowSettings {
  return {
    ...settings,
    model,
    ...(kind === "image" ? { imageModel: model } : { videoModel: model }),
  };
}

export function defaultFlowOutputFolder(now = new Date()) {
  const pad = (v: number) => String(v).padStart(2, "0");
  return `flow_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

export function normalizeLegacyFlowOutputDir(value: string) {
  const trimmed = value.trim().replace(/[\/]+$/, "");
  return trimmed.replace(/^(.+)[\/](?:video|image)$/i, "$1").replace(/^(.+)-(?:video|image)$/i, "$1") || trimmed;
}

export function flowConfiguredOutputFolder(value: string, kind: CreateKind) {
  const outputDir = normalizeLegacyFlowOutputDir(value);
  if (!outputDir) return "";
  if (/^(?:[A-Za-z]:[\/]|[\/])/.test(outputDir)) return `${outputDir}/${kind}`;
  return `ZM_AI_TOOL/flow/${kind}/${outputDir.replace(/^[\/]+/, "")}`;
}

export function flowOutputFolderName(value: string) {
  return value.trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "").slice(0, 96) || "results";
}

export function flowOutputFolderParts(value: string) {
  const parts = value.trim().split(/[\/]+/).filter(Boolean).map(flowOutputFolderName);
  return parts.length ? parts : ["results"];
}

export function flowOutputParentPath(output?: string) {
  const value = String(output || "").trim();
  if (!value || /^https?:\/\//i.test(value)) return "";
  const slashIndex = Math.max(value.lastIndexOf("/"), value.lastIndexOf("\\"));
  return slashIndex > 0 ? value.slice(0, slashIndex) : "";
}

export function flowOutputMediaKind(output: string, fallback: CreateKind) {
  const ext = output.split(/[?#]/, 1)[0].match(/\.([^.\/]+)$/)?.[1]?.toLowerCase() || "";
  if (/^(?:avif|bmp|gif|heic|jpe?g|png|svg|webp)$/.test(ext)) return "image";
  if (/^(?:aac|flac|m4a|mp3|oga|ogg|opus|wav)$/.test(ext)) return "audio";
  if (/^(?:m4v|mkv|mov|mp4|ogv|webm)$/.test(ext)) return "video";
  return ext ? "file" : fallback;
}

export function flowGroupProgress(jobs: FlowJob[]) {
  const total = jobs.length;
  const completed = jobs.filter((j) => j.status === "done").length;
  const progress = total
    ? Math.round(jobs.reduce((sum, j) => sum + Math.max(0, Math.min(100, Number(j.progress) || 0)), 0) / total)
    : 0;
  return { completed, progress, total };
}

// ── LocalStorage readers ──────────────────────────────────────────────────────
export function readText(key: string, fallback: string) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}

export function readSettings(): FlowSettings {
  const fallback: FlowSettings = {
    model: "Veo 3.1 - Fast", videoModel: "Veo 3.1 - Fast", imageModel: "Nano Banana 2",
    ratio: "16:9", imageRatio: "16:9", duration: "8", count: 1, imageCount: 1,
    account: "Ultra 01",
    outputDir: defaultFlowOutputFolder(), quality: "720p", resolution: "1K",
    concurrency: "8", format: "PNG", filePrefix: "flow", referenceStrength: 70, autoDownload: true,
  };
  try {
    const { enhancePrompt: _ep, seed: _s, ...saved } = JSON.parse(
      localStorage.getItem(SETTINGS_KEY) || "{}",
    ) as Partial<FlowSettings> & { enhancePrompt?: boolean; seed?: string };
    const merged = { ...fallback, ...saved };
    if (merged.model === "Veo 3.1 Fast") merged.model = "Veo 3.1 - Fast";
    if (merged.model === "Veo 3.1 Quality") merged.model = "Veo 3.1 - Quality";
    if (merged.model === "Omni Flash") merged.model = "Omni 1.1 Flash";
    if (merged.videoModel === "Omni Flash") merged.videoModel = "Omni 1.1 Flash";
    if (merged.model === "Veo 3.1 - Lite [Lower Priority]") merged.model = fallback.videoModel;
    if (/^Imagen 3/i.test(merged.model)) merged.model = "Nano Banana 2";
    if (!String(merged.videoModel || "").trim()) merged.videoModel = fallback.videoModel;
    if (!String(merged.imageModel || "").trim()) merged.imageModel = fallback.imageModel;
    if (!["16:9", "9:16", "1:1", "4:3", "3:4"].includes(merged.imageRatio)) merged.imageRatio = fallback.imageRatio;
    // Legacy Quality labels → download resolution (720p faster, 1080p heavier).
    if (/^standard$/i.test(String(merged.quality || ""))) merged.quality = "720p";
    if (/^high$/i.test(String(merged.quality || ""))) merged.quality = "1080p";
    if (!FLOW_VIDEO_DOWNLOAD_QUALITIES.includes(merged.quality as (typeof FLOW_VIDEO_DOWNLOAD_QUALITIES)[number])) {
      merged.quality = fallback.quality;
    }
    if (![1, 2, 3, 4].includes(Number(merged.imageCount))) merged.imageCount = fallback.imageCount;
    if (!["16:9", "9:16", "1:1", "4:3", "3:4"].includes(merged.ratio)) merged.ratio = fallback.ratio;
    const concurrency = Number(merged.concurrency);
    if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 18) merged.concurrency = fallback.concurrency;
    if (!String(merged.outputDir || "").trim() || merged.outputDir === "flow_20250824_143022") {
      merged.outputDir = defaultFlowOutputFolder();
    } else {
      merged.outputDir = normalizeLegacyFlowOutputDir(String(merged.outputDir));
    }
    return merged;
  } catch { return fallback; }
}


export function readAccounts(): FlowAccount[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "");
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}

// ── Routing helpers ───────────────────────────────────────────────────────────
export function flowRoutePanel(): FlowRoutePanel | null {
  if (typeof window === "undefined") return null;
  const panel = new URLSearchParams(window.location.search).get("p") || "";
  return ["image", "video", "series", "queue", "history", "logs", "accounts", "help"].includes(panel)
    ? panel as FlowRoutePanel : null;
}

export function writeFlowRoutePanel(panel: FlowRoutePanel) {
  const url = new URL(window.location.href);
  url.searchParams.set("p", panel);
  const dest = `${url.pathname}${url.search}${url.hash}`;
  if (dest !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
    window.history.pushState({ flowPanel: panel }, "", dest);
  }
}

// ── Normalization ─────────────────────────────────────────────────────────────
export function normalizeFlowJobs(rows: Array<Record<string, unknown>>, accounts: FlowAccount[]): FlowJob[] {
  return rows.map((raw) => {
    const s = raw.settings && typeof raw.settings === "object" ? raw.settings as Record<string, unknown> : {};
    const savedModel = String(s.model || (raw.kind === "image" ? "Nano Banana 2" : "Veo 3.1 - Fast"));
    return {
      id: String(raw.id),
      index: Number(raw.inputIndex || 0),
      kind: raw.kind === "image" ? "image" : "video",
      prompt: String(raw.prompt || ""),
      inputType: ["txt", "csv", "json"].includes(raw.inputType as string) ? raw.inputType as "txt" | "csv" | "json" : "prompt",
      createdAt: Number(raw.createdAt || Date.now() / 1000),
      status: ["processing", "queued", "done", "cancelled"].includes(raw.status as string) ? raw.status as FlowJob["status"] : "failed",
      stage: String(raw.stage || ""),
      progress: Number(raw.progress || 0),
      accountId: String(raw.accountId || ""),
      account: accounts.find((a) => a.id === raw.accountId)?.label || String(raw.accountId || ""),
      outputs: Array.isArray(raw.outputs) ? raw.outputs.map(String) : [],
      output: Array.isArray(raw.outputs) ? String(raw.outputs[0] || "") : "",
      outputFolder: raw.outputFolder ? String(raw.outputFolder) : "",
      displayOutputFolder: raw.displayOutputFolder ? String(raw.displayOutputFolder) : "",
      seriesContext: raw.seriesContext && typeof raw.seriesContext === "object" ? raw.seriesContext as FlowJob["seriesContext"] : undefined,
      error: raw.error ? String(raw.error) : null,
      settings: {
        model: savedModel === "Veo 3.1 - Lite [Lower Priority]" ? "Veo 3.1 - Fast" : savedModel,
        ratio: String(s.ratio || "16:9"),
        duration: String(s.duration || "8"),
        resolution: String(s.resolution || (raw.kind === "image" ? "1K" : "")),
        quality: String(s.quality || ""),
        outputDir: String(s.outputDir || "flow"),
        concurrency: s.concurrency != null ? String(s.concurrency) : undefined,
      },
    };
  });
}

/** Compact job meta line: model · ratio · duration · res · download quality. */
export function formatFlowJobSettingsMeta(
  kind: CreateKind,
  settings: Pick<FlowJob["settings"], "model" | "ratio" | "duration" | "resolution" | "quality">,
  opts?: { downloadLabel?: string },
): string {
  const parts: string[] = [];
  const model = String(settings.model || "").trim();
  const ratio = String(settings.ratio || "").trim();
  if (model) parts.push(model);
  if (ratio) parts.push(ratio);
  if (kind === "video") {
    const duration = String(settings.duration || "").trim();
    if (duration) parts.push(/s$/i.test(duration) ? duration : `${duration}s`);
    const resolution = String(settings.resolution || "").trim();
    const quality = String(settings.quality || "").trim();
    if (resolution) parts.push(resolution);
    if (quality) {
      const sameAsRes = resolution && quality.toLowerCase() === resolution.toLowerCase();
      if (!sameAsRes) {
        const label = String(opts?.downloadLabel || "").trim();
        parts.push(label && resolution ? `${label} ${quality}` : quality);
      }
    }
  } else {
    const resolution = String(settings.resolution || "").trim();
    if (resolution) parts.push(resolution);
  }
  return parts.join(" · ");
}

export function normalizeFlowAccounts(rows: FlowAccount[]): FlowAccount[] {
  return rows.map((a) => ({
    ...a,
    used: Number(a.used || 0),
    credits: a.creditsSyncedAt || (a.status === "online" && a.projectId && a.credits != null)
      ? Number(a.credits) : null,
  }));
}

export function selectedFlowAccount(accounts: FlowAccount[], accountLabel: string) {
  return accounts.find((a) => a.label === accountLabel)
    || accounts.find((a) => a.isDefault)
    || accounts.find((a) => a.status === "online")
    || accounts[0];
}

// ── API ───────────────────────────────────────────────────────────────────────
export async function flowRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

let recentFlowSnapshot: { at: number; promise: Promise<FlowSnapshot> } | null = null;

export function loadFlowSnapshot(): Promise<FlowSnapshot> {
  const now = Date.now();
  if (recentFlowSnapshot && now - recentFlowSnapshot.at < 5000) return recentFlowSnapshot.promise;
  const promise = Promise.all([
    flowRequest<{ accounts: FlowAccount[] }>("/api/flow/accounts"),
    flowRequest<{ jobs: Array<Record<string, unknown>> }>("/api/flow/jobs"),
  ]).then(([accountData, jobData]) => ({ accountData, jobData }));
  recentFlowSnapshot = { at: now, promise };
  promise.catch(() => { if (recentFlowSnapshot?.promise === promise) recentFlowSnapshot = null; });
  return promise;
}

// ── Web File System API helpers ───────────────────────────────────────────────
function openWebOutputDatabase() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open("zm-ai-web-output", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("directories");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function saveWebOutputRoot(key: string, handle: BrowserDirectoryHandle) {
  const db = await openWebOutputDatabase();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction("directories", "readwrite");
    tx.objectStore("directories").put(handle, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

export async function loadWebOutputRoot(key: string): Promise<BrowserDirectoryHandle | null> {
  const db = await openWebOutputDatabase();
  const handle = await new Promise<BrowserDirectoryHandle | null>((resolve, reject) => {
    const req = db.transaction("directories", "readonly").objectStore("directories").get(key);
    req.onsuccess = () => resolve((req.result as BrowserDirectoryHandle | undefined) || null);
    req.onerror = () => reject(req.error);
  });
  db.close();
  return handle;
}

export async function flowOutputDirectory(root: BrowserDirectoryHandle, kind: CreateKind, outputFolder: string, create: boolean) {
  if (root.name === kind) {
    return flowOutputFolderParts(outputFolder).reduce(
      (f, part) => f.then((cur) => cur.getDirectoryHandle(part, { create })),
      Promise.resolve(root),
    );
  }
  const appRoot = root.name === "ZM_AI_TOOL" ? root : await root.getDirectoryHandle("ZM_AI_TOOL", { create });
  const flowRoot = root.name === "flow" ? root : await appRoot.getDirectoryHandle("flow", { create });
  const kindRoot = await flowRoot.getDirectoryHandle(kind, { create });
  return flowOutputFolderParts(outputFolder).reduce(
    (f, part) => f.then((cur) => cur.getDirectoryHandle(part, { create })),
    Promise.resolve(kindRoot),
  );
}

export async function writeFlowOutputToDirectory(job: FlowJob, outputIndex: number, root: BrowserDirectoryHandle, outputFolder: string) {
  const res = await fetch(`/api/flow/jobs/${job.id}/outputs/${outputIndex}?download=1`);
  if (!res.ok) throw new Error(String(res.status));
  const sourceName = String(job.outputs?.[outputIndex] || "").split(/[\/]/).pop();
  const ext = job.kind === "video" ? "mp4" : "png";
  const filename = sourceName || `flow_${job.id}_${outputIndex + 1}.${ext}`;
  const target = await flowOutputDirectory(root, job.kind, outputFolder, true);
  const file = await target.getFileHandle(filename, { create: true });
  const writable = await file.createWritable();
  await writable.write(await res.blob());
  await writable.close();
}

export function downloadFlowOutput(job: FlowJob, outputIndex: number) {
  const sourceName = String(job.outputs?.[outputIndex] || "").split(/[\/]/).pop();
  const link = Object.assign(document.createElement("a"), {
    href: `/api/flow/jobs/${job.id}/outputs/${outputIndex}?download=1`,
    download: sourceName || `flow_${job.id}_${outputIndex + 1}`,
    hidden: true,
  });
  document.body.append(link);
  link.click();
  link.remove();
}

export async function deleteFlowOutputFromDirectory(job: FlowJob, outputIndex: number, root: BrowserDirectoryHandle) {
  const sourceName = String(job.outputs?.[outputIndex] || "").split(/[\/]/).pop();
  if (!sourceName) return false;
  try {
    const target = await flowOutputDirectory(root, job.kind, job.settings.outputDir, false);
    const file = await target.getFileHandle(sourceName);
    await file.getFile();
    await target.removeEntry(sourceName);
    return true;
  } catch (error) {
    if (error && typeof error === "object" && "name" in error && (error as { name: string }).name === "NotFoundError") return false;
    throw error;
  }
}
