# Cấu trúc source

Tài liệu này mô tả **thư mục, trách nhiệm module và quy tắc phụ thuộc**. Không liệt kê mọi file con; không thay README (cài đặt, API đầy đủ, license).

---

## Frontend

```text
frontend/src/
├─ app/
│  ├─ App.tsx                 Shell chính, mode routing, upload/switch project, ProgressPopup, modals
│  ├─ appMode.ts              Định nghĩa & parse chế độ: clone, live-preview, film, batch, renders, tts, cleaner, srt-image, srt-export, download, license
│  ├─ appSettings.ts          Load/persist settings + setup gate + idleStatus
│  ├─ useProjectSession.ts    Facade appSettings + useSessionRestore (F5 mở lại project)
│  └─ i18n.tsx / ui.en.json   Hệ thống đa ngôn ngữ (VI/EN) chuẩn hóa qua translate/localize
├─ pages/                     Trang cấp cao theo từng chế độ làm việc
│  ├─ FlowPage.tsx            Tạo video/ảnh AI (Flow) — state, routing, handlers
│  ├─ FlowSeriesPanel.tsx     Quản lý loạt tập Flow (series/episode/scene)
│  ├─ SrtImagePage.tsx        Ghép ảnh / video với âm thanh và file SRT
│  ├─ srtImage.types.ts       Types + hằng số HELP + cachedSettings cho SrtImagePage
│  ├─ ClonePage.tsx           Upload / khởi tạo Clone Video
│  ├─ EditorPage.tsx          Container cho LivePreviewEditor
│  ├─ FilmPage.tsx            Review Phim / Tóm tắt kịch bản & ghép cảnh
│  ├─ BatchPage.tsx           Xử lý video hàng loạt theo hàng đợi
│  ├─ RendersPage.tsx         Thư viện quản lý các video đã render
│  ├─ TtsPage.tsx             Container cho Text to Speech Studio
│  ├─ VideoCleanerPage.tsx    Làm sạch video (xóa watermark, logo, phụ đề cứng)
│  ├─ SrtExportPage.tsx       Trích xuất, tinh chỉnh và xuất phụ đề SRT
│  └─ DownloadPage.tsx        Tải video từ URL mạng xã hội đa nền tảng
├─ features/                  Modules nghiệp vụ theo tính năng
│  ├─ configuration/
│  │  ├─ ConfigModal.tsx         Cấu hình engine / API keys / setup phần cứng & runtime
│  │  └─ configModal.helpers.ts  Types + constants (PROVIDERS, emptyCloud…) tách ra khỏi modal
│  ├─ cleaner/                cleaner.api.ts — API tác vụ tẩy watermark/logo
│  ├─ download/               Form tải URL + quản lý hàng đợi download
│  ├─ editor/
│  │  ├─ LivePreviewEditor.tsx   Timeline + preview (orchestration UI)
│  │  └─ lib/                    Helper thuần (captionMeasure, coverBox, coverLayout, editorMath, timeline, waveform)
│  ├─ flow/
│  │  ├─ flow.types.ts           Tất cả TypeScript types của Flow (FlowJob, FlowAccount, FlowSettings…)
│  │  ├─ flow.helpers.ts         Constants, helpers thuần & API calls (flowRequest, loadFlowSnapshot…)
│  │  ├─ flowSeries.helpers.ts   Types + helpers cho FlowSeriesPanel (Series, Episode, Scene, request…)
│  │  └─ FlowTemplatesPanel.tsx  Panel chọn template prompt
│  ├─ license/                LicensePage.tsx + license.api.ts — Quản lý kích hoạt bản quyền
│  ├─ project/                API project, sidebar, types, compound
│  │  └─ use*.ts                 Hook luồng dài của App: useSegmentEditing, useProjectMedia (bake/rebake),
│  │                             useDubControl, useExportFlow, useJobPolling, useProjectCompound, useProjectState
│  ├─ studio/                 Cài đặt nâng cao: ReviewSettingsPanel, CloneBatchSettingsPanel, studio.api.ts
│  └─ tts/                    TTS Studio UI + CSS
│     ├─ TtsStudio.tsx           Orchestration + state
│     ├─ Tts*Panel / TtsIcons    Panel con (input, history, voice) + icon nội bộ
│     └─ lib/
│        ├─ ttsStudioHelpers.tsx  SliderNumber, WAVE_BARS, SECTION_LABELS, STORAGE_KEYS…
│        └─ voiceDisplay, srt, download, format  (logic thuần)
└─ shared/                    Thành phần dùng chung toàn ứng dụng
   ├─ api/                    HTTP helper (httpClient.ts)
   ├─ components/             Header, ProgressPopup, Icons, ErrorBoundary, …
   ├─ lib/                    cn, util
   ├─ types/                  Types dùng chung
   └─ ui/                     resizable, scroll-area, sonner toast, dialog, …
```

### Quy ước frontend

- `App.tsx`: state/liên kết cấp app (project, status, dub/export/cancel). Luồng dài thuộc feature → hook hoặc file feature.
- **File page lớn**: logic thuần (types, constants, helpers, API) **phải đặt trong file `*.helpers.ts` hoặc `*.types.ts` cùng thư mục**, không nhét vào đầu component. Ví dụ: `flow.helpers.ts`, `configModal.helpers.ts`, `srtImage.types.ts`.
- `LivePreviewEditor` / `TtsStudio` / `FilmPage`: panel lớn; logic thuần đặt `features/*/lib/`.
- API + type theo domain: `project.api.ts`, `project.types.ts`, `cleaner.api.ts`, `studio.api.ts`, `license.api.ts`.
- Lớp cũ `components/`, `lib/`, `services/` ở root `src/`: không thêm code mới; khi chạm, chuyển dần sang `shared/` hoặc feature nếu diff nhỏ.
- Không tạo wrapper/placeholder "cho chuẩn cấu trúc".
- Mọi UI mới phải hỗ trợ song ngữ (VI/EN) qua `localize(locale, vi, en)` hoặc `translate(locale, key)` theo quy tắc trong `AGENTS.md`.

---

## Scripts

```text
scripts/
└─ release.sh     Bump package.json rồi tạo git tag và push
                  Dùng: ./scripts/release.sh 4.2.0
                        ./scripts/release.sh patch|minor|major
                  CI sẽ tự tạo asset đúng tên theo tag: ZM_AI_TOOL_v<version>-macos-arm64.pkg
```

> **Quy tắc version**: luôn dùng `release.sh` để bump. `package.json` là nguồn version duy nhất; tag phải khớp version này.
