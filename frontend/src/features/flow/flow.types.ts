// Types dùng chung cho toàn bộ Flow feature

export type FlowTab = "create" | "queue" | "history" | "logs";
export type FlowRoutePanel = "image" | "video" | "series" | "queue" | "history" | "logs" | "accounts" | "help";
export type RailItem = "createImage" | "createVideo" | "queue" | "history" | "accounts" | "series" | "logs" | "help";
export type JobStatus = "processing" | "queued" | "done" | "failed" | "cancelled";
export type CreateKind = "video" | "image";
export type ImageMode = "text" | "edit" | "reference";
export type PromptInputType = "prompt" | "txt" | "csv" | "json";

export type FlowJob = {
  id: string;
  index: number;
  kind: CreateKind;
  prompt: string;
  inputType: PromptInputType;
  createdAt: number;
  status: JobStatus;
  progress: number;
  account: string;
  output?: string;
  outputFolder?: string;
  displayOutputFolder?: string;
  outputs?: string[];
  accountId?: string;
  error?: string | null;
  seriesContext?: { seriesTitle?: string; episodeIndex?: number; sceneIndex?: number; artifact?: string };
  settings: {
    model: string;
    ratio: string;
    duration: string;
    resolution: string;
    outputDir: string;
  };
};

export type FlowAccount = {
  id: string;
  label: string;
  plan: "Ultra" | "Pro" | "Free";
  email: string;
  status: "online" | "reconnect" | "connecting";
  credits: number | null;
  used: number;
  creditsSyncedAt?: number | null;
  isDefault?: boolean;
  projectId?: string;
  error?: string;
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
  outputDir: string;
  quality: string;
  resolution: string;
  concurrency: string;
  format: string;
  filePrefix: string;
  referenceStrength: number;
  autoDownload: boolean;
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
