# ZM AI TOOL — Video Clone & Film Review Studio

Ứng dụng desktop/web để dịch, lồng tiếng, biên tập timeline và tạo video review phim. ZM AI TOOL ưu tiên xử lý cục bộ; dịch vụ cloud chỉ được dùng khi bạn chủ động chọn và cấu hình chúng.

[![Version](https://img.shields.io/github/package-json/v/manhgdev/zm_ai_tool)](package.json)
[![Node](https://img.shields.io/badge/node-24-green.svg)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## Tính năng

| Khu vực | Dùng để làm gì |
|---|---|
| **Clone Video** | Nhận dạng lời thoại/SRT, dịch, TTS, che phụ đề cũ và xuất video. |
| **Live Preview** | Chỉnh caption, BBox che chữ, âm thanh, tốc độ, overlay, logo và timeline. |
| **Review Phim** | Phân cảnh, phân tích nội dung, tạo lời bình, ghép TTS và xuất recap. |
| **Hàng loạt** | Áp dụng cấu hình cho nhiều video trong hàng đợi. |
| **Flow (Veo 3)** | Tạo ảnh hoặc video bằng tài khoản Google Flow đã kết nối, có hàng đợi, lịch sử và preview kết quả. |
| **Text to Speech** | Tổng hợp TTS từ text/SRT; 413+ giọng từ zmAI, VieNeu, CapCut, ElevenLabs và hệ thống. |
| **Tải video & Tools** | Tải video, Cleaner, ghép video/ảnh + audio + SRT và tạo video vẽ tay. |

## Clone Video

1. Tạo project hoặc tải video vào.
2. Chọn nguồn nhận dạng: **Whisper**, **CapCut cloud**, **OCR chữ trên màn hình** hoặc **SRT**.
3. Chọn ngôn ngữ, công cụ dịch và tùy chọn tách người nói.
4. Chạy preview hoặc toàn video.
5. Chỉnh caption, BBox, TTS, âm gốc, logo/watermark và timeline trong Live Preview.
6. Xuất video, audio, SRT hoặc GIF.

![Clone Video](previews/clone-video.png)

### Nhận dạng và dịch

- **Whisper**: nhận dạng lời nói cục bộ.
- **CapCut cloud**: CapCut nhận dạng và dịch trong một tác vụ. Cloud chỉ trả trạng thái, không luôn trả phần trăm chính xác; UI hiển thị trạng thái và thời gian chờ thực tế.
- **OCR**: tạo track chữ trên màn hình riêng, không ghi đè caption thoại.
- **SRT**: import vào Media Library rồi áp dụng thành caption project.
- Dịch hỗ trợ Google, MyMemory, TikTok, Ollama và các provider cloud đã cấu hình.

### Live Preview Editor

- Timeline có video gốc, âm gốc, caption, lồng tiếng, watermark/overlay và media import.
- Caption nguồn và caption dịch là hai track dữ liệu độc lập; mặc định hiển thị track dịch.
- BBox che chữ tách biệt với watermark/logo, có thể kéo thả hoặc nhập tọa độ.
- Media Library hỗ trợ video, audio, ảnh, SRT và LUT; asset được lưu theo project.
- Preview nhanh dùng cùng pipeline render với export cho đoạn thời gian đã chọn.

![Live Preview Editor](previews/live-previews.png)

### Tạo kiểu caption

Font, màu, nền, viền và bố cục caption được chỉnh trong Inspector; preview và export sử dụng cùng dữ liệu bố cục.

![Caption Styling](previews/caption.png)

## Review Phim

Review Phim dùng pipeline riêng, không thay đổi project Clone Video:

1. Phát hiện cảnh và lấy transcript.
2. Phân tích hình/cốt truyện theo block cảnh.
3. Viết lời bình dựa trên bằng chứng của cảnh.
4. Tạo TTS, ghép cảnh và che phụ đề gốc theo lane đã xác định.
5. Xuất video hoàn chỉnh hoặc mở project trong editor.

Tiến trình hiển thị theo stage và số mục hoàn thành. Các tác vụ dài có thể hủy; backend nhận cờ hủy và dừng tiến trình con liên quan.

![Review Phim](previews/review-phim.png)

## Flow (Veo 3)

Flow tạo ảnh và video qua tài khoản Google **Pro** hoặc **Ultra** đã kết nối. Chọn model, tỷ lệ, độ phân giải, số lượng và tài khoản trước khi gửi prompt vào hàng đợi.

- Hỗ trợ **Text → Ảnh**, **Ảnh → Ảnh**, **Tham chiếu → Ảnh** và tạo video từ prompt/khung hình.
- Prompt có thể nhập tay, dán từ clipboard hoặc import TXT, CSV, JSON.
- Mỗi job lưu lại model, tỷ lệ, thời lượng, tài khoản, output và trạng thái để xem lại/chạy lại.
- WEB: chọn thư mục một lần để tự ghi output vào `ZM_AI_TOOL/flow/<tên-thư-mục>/` khi hoàn thành. APP lưu vào thư mục output đã chọn.
- Có preview video mới nhất, hàng đợi, lịch sử, log, hủy/xóa từng job hoặc toàn bộ hàng đợi.

### Model theo gói tài khoản

| Model | Pro | Ultra |
|---|:---:|:---:|
| Veo 3.1 - Lite | ✅ | ✅ |
| Veo 3.1 - Fast | ✅ | ✅ |
| Veo 3.1 - Quality | ✅ | ✅ |
| Omni Flash | ✅ | ✅ |
| Veo 3.1 - Lite [Lower Priority] | ❌ | ✅ |

![Flow (Veo 3)](previews/flow.png)

## Vẽ tay

Công cụ Vẽ tay biến ảnh thành video mô phỏng quá trình vẽ. Chọn ảnh nguồn, kiểu nét và preset xuất; sau đó kiểm tra preview trước khi tạo video.

- Hỗ trợ chế độ tay cầm bút vẽ nét, bút chì, marker và cọ.
- Tinh chỉnh thời lượng, độ chi tiết, độ dày nét, FPS, độ phân giải và thứ tự đường nét.
- Xem preview, bản đồ nét và đường nét trước khi render.
- Có thể đưa nhiều ảnh vào **Vẽ tay hàng loạt** để tạo job và chạy theo hàng đợi.

![Công cụ Vẽ tay](previews/draw.png)

## TTS và phần cứng

- **413+ giọng** từ zmAI (online catalog), VieNeu, CapCut, ElevenLabs và giọng hệ thống — hiển thị đầy đủ trên cả web lẫn app.
- Hồ sơ người nói gồm tên vai, màu caption và giọng; đổi giọng sẽ tạo lại TTS của đúng vai đó.
- NVIDIA dùng CUDA khi runtime tương thích; Apple Silicon dùng Metal/CoreML khi hỗ trợ; nếu không sẽ fallback CPU.
- FFmpeg dùng encoder phần cứng khi máy và bản FFmpeg hỗ trợ.

Vào **Cấu hình → Thiết lập/Tài nguyên** để xem trạng thái runtime và cài model theo nhu cầu.

![Text to Speech](previews/text-to-speed.png)

![Cấu hình cloud và phần cứng](previews/cloud-support.png)

## Tải video và công cụ media

Tải video từ URL, đưa thẳng video vào Clone/Review, hoặc dùng các công cụ độc lập để dọn watermark và ghép media với audio/SRT.

![Tải video](previews/dowwowwnload-video.png)

### Video Cleaner

Cleaner xử lý logo, watermark và vùng chữ theo thời gian; chọn blur, nền màu hoặc phương án inpaint khi runtime hỗ trợ.

![Video Cleaner](previews/clean-video.png)

### Ghép media, audio và SRT

Ghép ảnh/video với audio và phụ đề, phù hợp để tạo video theo kịch bản hoặc audio có sẵn.

![Ghép video/ảnh + audio + SRT](previews/meger-video-img-audio-srt.png)

![Thiết lập ghép media](previews/meger-video-img-audio-srt-setting.png)

## Chạy từ mã nguồn

### Yêu cầu

- Node.js 24 (khớp GitHub Actions release).
- Python 3.12. Nếu chưa có Python tương thích, setup tự chuẩn bị `uv` và tải Python 3.12.10 riêng cho tool; không cần gỡ Python khác đang có trên máy.
- FFmpeg và FFprobe trong `PATH`.
- macOS hoặc Windows được hỗ trợ chính thức bởi workflow build hiện tại.

### Cài đặt nhanh

```bash
git clone https://github.com/manhgdev/zm_ai_tool.git
cd zm_ai_tool
npm run setup
bun run dev
```

- Web UI: `http://localhost:5173` (Vite tự chọn cổng tiếp theo nếu bận)
- API: `http://127.0.0.1:8787`
- API docs: `http://127.0.0.1:8787/docs`

Setup tải và kiểm tra thực thi model LaMa dùng để xóa logo, giống model đi kèm bản desktop. Nếu cần bổ sung model cho server đã cài trước đó:

```bash
backend/.venv/bin/python scripts/setup_lama.py
backend/.venv/bin/python scripts/lama_smoke.py
```

Khởi động lại backend sau khi bổ sung model, rồi chạy lại bước xóa logo trên file gốc.

### Chỉ có hai bản: Web và APP

- **Web dev:** `npm run dev` chạy Vite + backend Web để phát triển/thử nghiệm.
  Mặc định mở `http://localhost:5173`, tự tăng lên 5174… nếu bận; API ở
  `127.0.0.1:8787`. `Ctrl+C` dừng cả hai.
- **Web production:** `npm run build` rồi `npm run start` chạy backend phục vụ
  giao diện tĩnh từ `frontend/dist` tại `127.0.0.1:8787`, không khởi động Vite.
  Cổng đang bận sẽ báo lỗi; `Ctrl+C` dừng backend và worker.
- **APP:** `bun run build:app` đóng gói ứng dụng desktop; launcher tự chọn backend
  APP. `bun run build` chỉ build giao diện dùng chung cho Web/APP.

Cài môi trường một lần bằng `bun run setup`. Để cấu hình Google, tạo `.env`
ở gốc repository theo `.env.example`:

```dotenv
ZM_AI_TOOL_GOOGLE_CLIENT_ID=your-google-web-client-id
ZM_AI_TOOL_GOOGLE_CLIENT_SECRET=your-google-web-client-secret
# Khi đưa lên server, thêm:
# ZM_AI_TOOL_WEB_ORIGIN=https://zmaitool.com
```

Google Cloud → Google Auth platform → Clients → tạo **Web application**.
Đăng ký callback `https://zmaitool.com/api/auth/google/callback`; nếu thử local,
đăng ký thêm `http://localhost:5173/api/auth/google/callback`.
`ZM_AI_TOOL_WEB_ORIGIN` trong `.env` dành cho **start/production**; dev bỏ qua
giá trị này và truyền origin theo cổng Vite thực tế cho backend. Vì vậy có thể
giữ `https://zmaitool.com` trong `.env` khi chạy dev. Nếu Vite nhảy sang 5174,
cần đăng ký thêm `http://localhost:5174/api/auth/google/callback` trong Google Cloud;
script không tự thay đổi cấu hình Google. `ZM_AI_TOOL_PORT` là cổng API dùng cho
cả dev/start, mặc định 8787; không phải cổng Vite. Client ID/Secret có thể để trống
để xem giao diện; API dữ liệu riêng vẫn yêu cầu đăng nhập.

Mỗi tài khoản Google có project, job, cấu hình, kết quả và cache UI riêng.
Dữ liệu lưu ở `backend/web-data`, tách khỏi APP. Có thể đặt
`ZM_AI_TOOL_WEB_DATA=/srv/zm-ai-data` để dùng thư mục bền vững trên server.
Web chỉ dùng quyền tài khoản; không nhập key. Tài khoản mới chờ quản trị viên cấp thời hạn.
Web dùng một tiến trình API riêng cho mỗi tài khoản để tái sử dụng store/queue
hiện có; tối đa `ZM_AI_TOOL_WEB_MAX_WORKERS` (mặc định 8) tài khoản có worker trong
một lần chạy. Hiện máy chủ Web hỗ trợ Linux/macOS; trình duyệt dùng được mọi OS.
Mở rộng nhiều host cần cơ chế lập lịch worker.

**Tài khoản, key và quản trị:**

- `.env` thêm `ZM_AI_TOOL_OWNER_EMAILS=nguyenmanhict@gmail.com,manhgdev@gmail.com`.
  Lần khởi tạo đầu tạo hai hồ sơ Chủ vĩnh viễn; chỉ liên kết sau khi Google xác minh
  đúng email. Sau đó database giữ vai trò, không tự nâng lại quyền khi restart.
- `/admin/accounts`: thêm theo email Google, cấp/gia hạn thời gian, phân quyền,
  giao đại lý, khóa, thu hồi phiên, xóa/khôi phục. Tài khoản đã liên kết giữ ID và workspace.
- `/admin/keys`: tạo key `ZMAI-…` mới, thời hạn, số thiết bị (mặc định 1), gia hạn,
  khóa, tạo lại, thu hồi thiết bị và xóa/khôi phục. Key đầy đủ hiển thị một lần;
  database chỉ lưu băm. Key cũ từ `api.zm.io.vn` không dùng trong APP cập nhật.
- Chủ quản lý tất cả. Admin chỉ sửa/cấp Đại lý và User. Đại lý chỉ quản lý User/key
  được giao, trong số chỗ và thời hạn cho phép; mặc định hạn mức bằng 0.
  Chỗ đã lên lịch và key chưa kích hoạt vẫn tính hạn mức. Đại lý hết hạn/khóa mất
  quyền quản lý; quyền riêng của khách đã cấp vẫn giữ nguyên.
- Thời hạn: giờ/ngày/tháng/năm, ngày giờ cụ thể hoặc vĩnh viễn tường minh. Key bắt
  đầu ngay, theo lịch hoặc lần kích hoạt đầu. Lưu UTC, quản trị hiển thị UTC+7.
  Gia hạn tính từ hạn cũ còn hiệu lực hoặc thời điểm hiện tại nếu đã hết hạn.
- SQLite `accounts.sqlite3` có phiên bản migration, bản sao trước migration
  `accounts.before-management.sqlite3`, lịch sử quản lý và xóa mềm. Sao lưu khi
  đã dừng gateway/worker để SQLite và workspace nhất quán; giữ cả WAL nếu copy nóng.
- APP đăng nhập Google qua trình duyệt hệ thống, callback loopback, state/PKCE/mã
  một lần theo [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252.html); Secret ở gateway.
  Mặc định APP gọi `https://zmaitool.com`. Để thử APP local, đặt biến môi trường
  `ZM_AI_TOOL_AUTH_SERVER_URL=http://localhost:18787` **khi mở APP**; APP không đọc `.env`
  Web. Không đổi cấu hình production trong mã nguồn để chạy thử.
- APP xác minh lúc mở và mỗi 5 phút; sau 15 phút kể từ lần xác minh server hợp lệ
  cuối cùng sẽ chặn job mới. Cache không gia hạn thời gian, không vượt ngày hết hạn.
  Khóa/thu hồi có hiệu lực ngay khi APP nhận phản hồi. Web đọc SQLite tại lúc nhận việc.
  Job đã chạy hoàn tất; job chờ chuyển sang tạm dừng/yêu cầu thao tác, cần tiếp tục
  thủ công sau khi gia hạn.
- Mỗi APP dùng một phương thức tại một thời điểm: tài khoản Google **hoặc** key,
  không cộng dồn thời hạn. Trong **Cấu hình → Quyền sử dụng**, xem phương thức,
  hạn dùng và số thiết bị/giới hạn theo server; dùng **Kiểm tra lại quyền** sau khi
  quản trị thay đổi cấu hình. Đăng xuất/hủy kích hoạt trước khi đổi tài khoản hoặc key.
- Dev dùng URL được in ở dòng `Web →`; callback Google phải đăng ký chính xác
  `<URL đó>/api/auth/google/callback`. Người dùng tự chọn/xác minh
  Google khi nghiệm thu; không dùng phiên giả để kết luận OAuth thật thành công.

**Triển khai Web trên server:** cài môi trường, build bằng `bun run build`, đặt
origin HTTPS và Google credentials trong `.env`, rồi chạy chính entry backend:

```sh
npm run start
```

Reverse proxy HTTPS của `zmaitool.com` về `127.0.0.1:8787`, giữ header `Origin`,
hỗ trợ upload/video Range, tắt buffering cho SSE và đặt read timeout dài.
Không log query callback OAuth; không public cổng worker riêng.
Chạy một gateway, không bật reload hoặc nhiều Uvicorn worker khi chạy job thật.
Biến môi trường của service/shell ưu tiên hơn `.env`; secret chỉ nằm ở backend.
Sao lưu toàn bộ `WEB_DATA` gồm SQLite và dữ liệu các tài khoản.

**Ubuntu với PM2:** chạy các lệnh sau từ thư mục gốc repository, bằng cùng user
đã cài môi trường. Chuẩn bị `.env` với `ZM_AI_TOOL_WEB_ORIGIN=https://zmaitool.com`
và Google Client ID/Secret; không đưa secret vào `pm2.start.json`.

```sh
npm run setup                 # Lần đầu hoặc khi dependency thay đổi
npm run build
# Chờ job đang chạy hoàn tất trước khi dừng/restart.
pm2 startOrRestart pm2.start.json --only zm_ai_tool --update-env
curl --fail http://127.0.0.1:8787/api/health
curl --fail --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8787/
pm2 logs zm_ai_tool --lines 50 --nostream
pm2 save
```

Health phải trả `"mode":"web"`; mở domain HTTPS để kiểm tra giao diện và đăng nhập.
PM2 gọi `npm run start`, backend phục vụ `frontend/dist`, không chạy Vite. `.env` được
đọc mỗi lần khởi động; cổng mặc định 8787, có thể đổi bằng `ZM_AI_TOOL_PORT`.
Nginx phải dùng cùng cổng. Lần đầu cấu hình tự chạy sau reboot bằng `pm2 startup`,
chạy đúng lệnh mà PM2 in ra rồi `pm2 save`. Các lần cập nhật sau dùng
`npm run build && pm2 restart pm2.start.json --only zm_ai_tool --update-env`.

### Các lệnh thường dùng

| Lệnh | Mục đích |
|---|---|
| `npm run setup` | Cài dependency và chuẩn bị môi trường. |
| `npm run dev` | Web development: Vite + backend xác thực Google. |
| `npm run start` | Web production: backend phục vụ giao diện đã build, không chạy Vite. |
| `npm run build` | Type-check TypeScript và build frontend. |
| `npm run test:i18n` | Kiểm tra catalog Việt/Anh. |
| `npm run build:app` | Đóng gói desktop; macOS cài đè `~/Applications`. Tăng patch: `BUMP_VERSION=1`. |
| `npm run build:release:macos` | macOS giống CI: `CLEAN=1` → `.pkg`. |
| `npm run build:release:windows` | Windows local giống `build:app` (Setup.exe; cần Inno Setup). |
| `npm run build:installer` | Chỉ chạy lại Inno Setup (Setup.exe) khi đã có thư mục ứng dụng đã build. |
| `npm run check:build` | Kiểm tra artifact desktop. |
| `npm run release -- patch` | Bump patch version, tạo tag và push lên GitHub (trigger CI build). |

## Cấu hình cloud tùy chọn

Nhập key ở **Cấu hình** trong Web hoặc APP. Web lưu API key riêng theo tài khoản;
worker không kế thừa API key cloud từ môi trường chung của server.
File `.env` dùng cấu hình đăng nhập/địa chỉ Web; không commit file này hoặc API key vào Git.

## Đóng gói và phát hành

```bash
# Phát hành patch (ví dụ 3.7.3 → 3.7.4) — bump version + tag + push trong một lệnh
npm run release -- patch

# Hoặc chỉ định thẳng version
npm run release -- 3.8.0
```

Artifact local được tạo trong `build_app/release/`. GitHub Actions build macOS và Windows khi push tag theo dạng `v*` và đính kèm các gói phát hành vào GitHub Release.

`npm run build:app` trên từng OS tạo đủ bản phát hành (giống CI):

- **Version:** mặc định giữ nguyên. Tăng số thứ 3 (0–9): `BUMP_VERSION=1` → sau `.9` nhảy minor (`8.0.9` → `8.1.0`). Version chỉ lấy từ `package.json`; CI kiểm tra tag phải khớp version này.
- **macOS:** `ZM AI TOOL.app` + `.pkg`; sau build cài đè `~/Applications`. `/Applications` (nếu root) dùng `sudo installer -pkg …`.
- **Windows:** chỉ tạo `…-windows-x64-Setup.exe` (cần Inno Setup 6 / `ISCC` trên máy); không nén hoặc phát hành Portable ZIP.

- **Windows (chi tiết):**
  - 📦 **Bản Cài đặt (Setup / Installer):** `ZM_AI_TOOL_v{version}-windows-x64-Setup.exe` — Bộ cài Wizard tiêu chuẩn (Inno Setup), tự động tạo shortcut Desktop/Start Menu, hỗ trợ gỡ cài đặt và lưu runtime/dữ liệu tại `%LOCALAPPDATA%\ZM_AI_TOOL`.
- **macOS:** `ZM_AI_TOOL_v{version}-macos-arm64.pkg` / `ZM_AI_TOOL_v{version}-macos-x86_64.pkg` → cài thành `ZM AI TOOL.app`.

Trước khi release:

```bash
npm run test:i18n
npm run build
PYTHONPATH=backend backend/.venv/bin/python -m pytest tests/backend
```

## Dữ liệu

- Web: project/job/media nằm trong `backend/web-data/users/<account-id>/`; cache trình duyệt có namespace theo tài khoản.
- Desktop app dùng thư mục dữ liệu ứng dụng của hệ điều hành.
- Xóa project hoặc asset từ UI phải xóa dữ liệu backend tương ứng; asset đang được timeline sử dụng sẽ không được xóa.
- Video gốc, export và voice clone có thể chiếm nhiều dung lượng; dọn chúng trong quản lý project/cache.

## Cấu trúc mã nguồn

```text
frontend/src/       React + TypeScript + Vite
backend/api/        FastAPI routes và schema
backend/pipeline/   ASR, OCR, dịch, TTS, Review, export và media
build_app/          Desktop launcher/build
scripts/            setup và dev runner
tests/              pytest và i18n tests
previews/           Ảnh minh họa giao diện
```

Xem thêm [STRUCTURE.md](STRUCTURE.md) để biết quy ước module.

## License

Phân phối theo [Apache License 2.0](LICENSE).
