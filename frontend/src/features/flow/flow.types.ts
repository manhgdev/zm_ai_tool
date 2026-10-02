// Types dùng chung cho toàn bộ Flow feature

export type FlowTab = "create" | "queue" | "history" | "logs";
export type FlowRoutePanel = "image" | "video" | "series" | "queue" | "history" | "logs" | "accounts" | "help";
export type RailItem = "createImage" | "createVideo" | "queue" | "history" | "accounts" | "series" | "logs" | "help";
export type JobStatus = "processing" | "queued" | "done" | "failed" | "cancelled";
export type CreateKind = "video" | "image";
export type ImageMode = "text" | "edit" | "reference";
export type VideoMode = "text" | "frame";
export type PromptInputType = "prompt" | "txt" | "csv" | "json";

export type FlowModelFallback = { id: string; jobId: string; accountId: string; fromModel: string; toModel: string; rawError: string; createdAt: number };

export type FlowJob = {
  id: string;
  index: number;
  kind: CreateKind;
  mode: ImageMode | VideoMode;
  prompt: string;
  inputType: PromptInputType;
  createdAt: number;
  status: JobStatus;
  stage?: string;
  progress: number;
  account: string;
  output?: string;
  outputFolder?: string;
  displayOutputFolder?: string;
  outputs?: string[];
  accountId?: string;
  randomAccount?: boolean;
  error?: string | null;
  quotaModelFallbackApplied?: boolean;
  modelFallback?: FlowModelFallback;
  seriesContext?: { seriesTitle?: string; episodeIndex?: number; sceneIndex?: number; artifact?: string };
  settings: {
    model: string;
    ratio: string;
    duration: string;
    resolution: string;
    /** Video download menu quality (360p/720p/1080p/4K). */
    quality?: string;
    outputDir: string;
    concurrency?: string;
  };
};

export type FlowModelCapability = {
  name: string;
  /** Credits charged for one generation of this video model, when Flow reports it. */
  creditCost?: number;
  ratios: string[];
  durations: string[];
  resolutions: string[];
  defaultRatio?: string;
  defaultDuration?: string;
  defaultResolution?: string;
};

export type FlowCapabilityCatalog = {
  version: number;
  source: string;
  syncedAt: number;
  /** Plan used when this account-specific pricing snapshot was synced. */
  pricingPlan?: string;
  image: { defaultModel?: string; models: FlowModelCapability[] };
  video: { defaultModel?: string; models: FlowModelCapability[] };
};

export type FlowAccount = {
  id: string;
  label: string;
  plan: "Ultra" | "Pro" | "Plus" | "Free";
  email: string;
  status: "online" | "reconnect" | "connecting";
  credits: number | null;
  used: number;
  creditsSyncedAt?: number | null;
  isDefault?: boolean;
  projectId?: string;
  error?: string;
  planStatus?: "verified" | "unknown";
  planSource?: string;
  planSyncedAt?: number | null;
  flowTier?: string;
  flowSku?: string;
  flowServiceTier?: string;
  capabilityCatalog?: FlowCapabilityCatalog | null;
  capabilityStatus?: "verified" | "stale" | "unknown";
  capabilitySyncedAt?: number | null;
  preferredImageModel?: string;
  lastModelFallback?: FlowModelFallback;
  capabilityError?: string;
  suspendedUntil?: number | null;
  suspendReason?: string | null;
};

export type FlowLog = {
  id: string;
  level: "info" | "success" | "warning" | "error";
  event: string;
  jobId: string;
  accountId: string;
  message: string;
  details: Record<string, unknown>;
  createdAt: number;
};

export type FlowSettings = {
  model: string;
  videoModel: string;
  imageModel: string;
  ratio: string;        // video ratio
  imageRatio: string;   // image ratio (separate from video)
  duration: string;
  count: number;        // video count
  imageCount: number;   // image count (separate from video)
  account: string;
  videoAccount?: string;
  imageAccount?: string;
  outputDir: string;
  quality: string;
  resolution: string;
  concurrency: string;
  format: string;
  filePrefix: string;
  referenceStrength: number;
  autoDownload: boolean;
  /** Keep Flow's Chrome window hidden unless explicitly requested. */
  headless: boolean;
};

export type FlowSnapshot = {
  accountData: { accounts: FlowAccount[] };
  jobData: { jobs: Array<Record<string, unknown>> };
};

// Browser File System API types (web output)
export type BrowserFileHandle = {
  getFile: () => Promise<File>;
  createWritable: () => Promise<{ write: (data: Blob) => Promise<void>; close: () => Promise<void> }>;
};
export type BrowserDirectoryHandle = {
  name: string;
  getDirectoryHandle: (name: string, options?: { create?: boolean }) => Promise<BrowserDirectoryHandle>;
  getFileHandle: (name: string, options?: { create?: boolean }) => Promise<BrowserFileHandle>;
  removeEntry: (name: string) => Promise<void>;
  queryPermission?: (descriptor?: { mode: "readwrite" }) => Promise<"granted" | "denied" | "prompt">;
  requestPermission?: (descriptor?: { mode: "readwrite" }) => Promise<"granted" | "denied" | "prompt">;
};
export type BrowserDirectoryWindow = Window & {
  showDirectoryPicker?: (options?: { mode?: "readwrite" }) => Promise<BrowserDirectoryHandle>;
};
