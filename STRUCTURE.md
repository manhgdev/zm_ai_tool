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
└─ release.sh     Đồng bộ 3 chỗ rồi push: build_app/VERSION + package.json + git tag
                  Dùng: ./scripts/release.sh 4.2.0
                        ./scripts/release.sh patch|minor|major
                  CI sẽ tự tạo asset đúng tên theo tag: ZM_AIO_TOOL_v<version>-macos-arm64.pkg
```

> **Quy tắc version**: luôn dùng `release.sh` để bump. Không sửa `VERSION` / `package.json` tay rồi tạo tag riêng — sẽ gây lệch tên asset CI.

��ng lồ nếu có thể tách file.
- API + type theo domain: `project.api.ts`, `project.types.ts`, `cleaner.api.ts`, `studio.api.ts`, `license.api.ts`.
- Lớp cũ `components/`, `lib/`, `services/` ở root `src/`: không thêm code mới; khi chạm, chuyển dần sang `shared/` hoặc feature nếu diff nhỏ.
- Không tạo wrapper/placeholder “cho chuẩn cấu trúc”.
- Mọi UI mới phải hỗ trợ song ngữ (VI/EN) qua `localize(locale, vi, en)` hoặc `translate(locale, key)` theo quy tắc trong `AGENTS.md`.

---

## Backend

```text
backend/
├─ main.py                    Uvicorn entry → create_app()
├─ api/
│  ├─ app.py                  FastAPI app + CORS + static mounts (/media, /renders, /fonts)
│  ├─ deps.py                 Pydantic schemas + validators dùng chung
│  ├─ job_spawn.py            Thread spawn pipeline (bắt lỗi → set_status)
│  ├─ video_serve.py          Serve MP4 + Range request HTTP 206 an toàn
│  ├─ routes_all.py           Aggregator: include_router từng domain
│  └─ routes/                 HTTP endpoints theo domain
│     ├─ projects.py          Upload, video streaming, status, settings, rebake-speed
│     ├─ jobs.py              Run (ASR+dịch), dub, cancel, export, output
│     ├─ segments.py          Segments CRUD, compound time/text
│     ├─ overlays.py          Text overlays CRUD & positioning
│     ├─ audio.py             No-vocals Demucs stem, audio cache, download audio
│     ├─ system.py            Hardware probe (CPU/RAM/GPU), config, checks, install AI packages
│     ├─ license.py           Kích hoạt & kiểm tra bản quyền
│     ├─ queue.py             Hàng đợi xử lý tác vụ Batch
│     ├─ review.py            Kịch bản, phân cảnh, visual match & render Review Phim
│     ├─ cleaner.py           Tẩy watermark, inpainting / blur video
│     ├─ srt_image.py         Ghép video/ảnh + audio + SRT
│     ├─ srt_export.py        Trích xuất & định dạng phụ đề SRT
│     ├─ tts_studio.py        Studio synthesize / clone / voice patch
│     ├─ tts_voices.py        Danh sách giọng TTS (CapCut, VieNeu, zmAI, ElevenLabs)
│     ├─ tts_preview.py       Nghe thử mẫu giọng đọc
│     ├─ download.py          Tải video đa nền tảng (yt-dlp)
│     └─ rendered.py          Thư viện video đã render (danh sách, đổi tên, xóa, mở thư mục)
├─ pipeline/
│  ├─ run.py                  Facade: run_pipeline / run_dub / run_export
│  ├─ translate.py            Facade MT (dịch thuật)
│  ├─ subtitles.py            Xử lý phụ đề & timing
│  ├─ srt_image.py            Engine ghép ảnh/video + audio + SRT
│  ├─ srt_export.py           Engine xuất & chuẩn hóa SRT
│  ├─ orchestrate/            Job nhiều bước
│  │  ├─ asr_translate.py     Pipeline phối hợp ASR → Dịch
│  │  ├─ dub.py               Pipeline phối hợp Lồng tiếng
│  │  ├─ export_job.py        run_export (điều phối xuất video)
│  │  ├─ export_overlays.py   TextOverlay editor → cue burn
│  │  ├─ export_outputs.py    Đóng gói mp4/cover/audio/SRT/GIF
│  │  └─ tts_fit.py           Fit audio timing & co giãn tốc độ
│  ├─ asr/                    faster-whisper + diarization (sherpa-onnx)
│  ├─ ocr/                    RapidOCR + extract_parts (runtime/scan/merge/detect)
│  ├─ mt/                     Free (Google, TikTok, MyMemory) / Ollama / Cloud LLM (OpenAI, Gemini, Grok, DeepSeek, OpenRouter)
│  ├─ tts/                    Manager, studio, voice store, engines (vieneu, capcut, eleven, system)
│  ├─ cleaner/                Xử lý làm sạch video (cleaner_ffmpeg, cleaner_jobs)
│  ├─ review/                 Pipeline Review Phim (run, script, scenes, match, compose, vision, llm, story)
│  ├─ queue/                  Engine xử lý hàng đợi nền (engine, store, paths)
│  ├─ clone_run/              Headless execution & open-source clone runner
│  ├─ gpu/                    Quản lý tài nguyên GPU (manager)
│  ├─ export/
│  │  ├─ burn.py              Facade cover_and_burn
│  │  ├─ burn_parts/          ass, layout_geo/_text, draw_text (RGBA), ocr_boxes, pipeline, render
│  │  ├─ mux.py               Facade muxing
│  │  ├─ stem.py              Demucs / no_vocals (tách nhạc nền & giọng)
│  │  ├─ mux_audio.py         mux_dub / mix TTS + BGM
│  │  ├─ fonts.py             Resolve font đa hệ điều hành
│  │  ├─ cover_mask.py        Tạo mặt nạ che phụ đề
│  │  └─ srt.py               Xử lý SRT export
│  ├─ download/               yt-dlp wrapper & download jobs
│  └─ core/                   Hạ tầng dùng chung
│     ├─ project.py           Meta, layout, status
│     ├─ jobs.py              Cancel flag + kill process tree + run_cmd
│     ├─ media.py             FFmpeg helpers, detect_device
│     ├─ speed_timeline.py    Bake speed + timeline baseline 1× (remap, meta_baked_speed)
│     ├─ resources.py         adaptive_workers (giới hạn trần CPU ~85%)
│     ├─ accel.py             Ưu tiên thiết bị CUDA / MPS / CPU (VieNeu, Whisper, …)
│     ├─ system_check/        Probe phần cứng + install gói AI + checks
│     ├─ runtime_site.py      Frozen .venv-runtime
│     └─ …
└─ …

tests/
├─ backend/                   Mirror theo domain backend
│  ├─ api/  asr/  core/  download/  export/  mt/  ocr/  tts/  pipeline/
│  ├─ video/                  Fixture video nhỏ
│  └─ conftest.py             Thêm `backend/` vào Python path khi pytest
├─ i18n.catalog.test.mjs      Kiểm tra tính nhất quán và độ bao phủ của catalog đa ngôn ngữ
├─ fixtures/
│  ├─ fonts/                  Video fixture kiểm tra font
│  ├─ frames/                 Ảnh frame dọc/ngang dùng cho OCR và export
│  ├─ subtitles/              File SRT mẫu
│  └─ state/                  JSON state/DB mẫu
└─ manual/
   └─ font/                   Script chẩn đoán font/layout thủ công + ảnh mẫu
```

### Quy ước backend

| Lớp | Trách nhiệm |
|---|---|
| `api/routes/*` | Parse/validate HTTP, gọi pipeline, map lỗi → status code |
| `pipeline/<domain>/` | Nghiệp vụ; **không** import FastAPI |
| `orchestrate/` | Phối hợp ≥2 domain (ASR→dịch, dub, export) |
| `core/` | Helper hạ tầng đã dùng bởi nhiều domain |
| Facade (`run.py`, `burn.py`, `mux.py`, `translate.py`) | Re-export API ổn định sau khi tách file |

- Bug dùng chung: sửa **một lần** ở helper/pipeline gốc + một check nhỏ (`tests/backend/` hoặc assert) nếu logic không tầm thường.
- Job dài: `begin_job` / `arm_job` / `request_cancel` + `register_process` mọi subprocess để **Huỷ** kill được.
- GPU: `core/accel.py` và `pipeline/gpu/manager.py` là nguồn sự thật cho device preference; engine chỉ gọi helper, không hardcode path Windows.

---

## Desktop pack

```text
build_app/
├─ launcher.py       Cửa sổ ứng dụng + spawn backend API + thiết lập ZM_AI_TOOL_HOME
├─ build.mjs         Vite build + PyInstaller packaging
├─ check_build.mjs   Kiểm tra tính toàn vẹn bản build desktop
└─ release/          ZM_AIO_TOOL_v<version>/ (chạy cả thư mục)
```

AI nặng cài sau vào `%LOCALAPPDATA%\ZM_AI_TOOL\.venv-runtime` (và `.venv-ocr`), không nhét full vào EXE.

---

## Quy tắc phụ thuộc

```text
Frontend:  app/pages  →  features  →  shared
Backend:   api/routes  →  orchestrate | pipeline/<domain>  →  core
```

- `shared` / `core` **không** import ngược feature/domain cụ thể.
- Hai feature không import component nội bộ của nhau; phần dùng chung thật → `shared`.
- Route không gọi route khác; gọi chung hàm pipeline.
- `*.bk*`, `*_pre_v4`, backup xoay vòng: local only, không commit (xem `.gitignore`).

---

## Khi cập nhật tài liệu này

Chỉ sửa khi đổi:

1. Cây thư mục cấp domain, hoặc  
2. Trách nhiệm module (ai gọi ai), hoặc  
3. Quy tắc phụ thuộc / facade.

Không ghi changelog tính năng, hướng dẫn cài, hay danh sách endpoint đầy đủ — đặt ở **README.md**.
