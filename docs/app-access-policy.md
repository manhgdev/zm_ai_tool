# Quy tắc quyền sử dụng APP — 12 mục

Tài liệu đối chiếu khi sửa quyền sử dụng trong APP desktop. Đây là yêu cầu hành vi;
không tự động coi mọi mục đã được kiểm thử chỉ vì có trong tài liệu.

## Nguyên tắc chung

- Khi chưa đăng nhập/chưa kích hoạt hoặc không còn quyền sử dụng, chỉ chặn đúng
  thao tác chính trong bảng bên dưới. Không khóa cả trang, tab hay mọi tương tác.
- Các bước chuẩn bị, nhập dữ liệu, xem trước, chỉnh sửa và quản lý vẫn mở, trừ
  đúng thao tác được ghi là cần quyền sử dụng.
- Khi nhấn thao tác bị chặn, mở popup **Cấu hình → Quyền sử dụng** và giải thích
  rõ cần đăng nhập Google có quyền sử dụng hoặc kích hoạt bằng mã ứng dụng.
  Nội dung popup phải có cả tiếng Việt và tiếng Anh theo ngôn ngữ đang chọn.
- Cho phép đóng popup để tiếp tục dùng các phần không bị chặn.
- **Mỗi lần mở Quyền sử dụng phải kết nối server để kiểm tra lại**, không chỉ
  dựa vào trạng thái lưu từ lần mở trước. Nếu không kiểm tra được, hiển thị lỗi
  rõ ràng, không báo xác minh thành công.
- Khi bị từ chối quyền, giữ nguyên bản nháp, timeline, job và kết quả đã có;
  không xóa dữ liệu hoặc kết quả cũ trước khi backend chấp nhận thao tác.
- Backend cũng phải kiểm tra đúng thao tác cần quyền; không chỉ chặn bằng nút
  trên giao diện. API dùng chung phải phân biệt mục đích chuẩn bị/xem trước với
  tạo mới/chạy/xuất bản.
- Chính sách này không làm yếu xác thực, phân quyền tài khoản của bản Web.
- “Mở” nghĩa là không chặn vì quyền sử dụng APP; vẫn giữ kiểm tra dữ liệu đầu vào,
  file, API key, model, trạng thái đang chạy và quyền truy cập tài nguyên.

## Danh sách đầy đủ 12 mục

| STT | Tính năng / đường dẫn | Chỉ chặn thao tác này khi chưa có quyền | Những phần vẫn mở |
| --- | --- | --- | --- |
| 1 | Text to Speech — `/text-to-speech` | **Tạo giọng nói** | Mở trang, nhập/sửa nội dung, chọn giọng, nghe thử, quản lý giọng/tài khoản và các thao tác còn lại. |
| 2 | Ghép media — `/media-compose` | **Render** | Thêm media, chỉnh nội dung/bố cục, cấu hình và xem trước. |
| 3 | Làm sạch video / ảnh — `/video-cleaner` | **Bắt đầu xử lý** | Chọn video/ảnh, chọn vùng cần làm sạch, chỉnh thông số và chuẩn bị dữ liệu. |
| 4 | Tải video — `/download-video` | **Không chặn** | Mở đầy đủ tính năng tải video; không yêu cầu kích hoạt APP. |
| 5 | Xuất phụ đề — `/subtitle-export` | **Không chặn** | Mở đầy đủ tính năng xuất phụ đề; không yêu cầu kích hoạt APP. |
| 6 | Video vẽ tay — `/drawing` | **Tạo video vẽ tay** | Chuẩn bị nội dung, chỉnh sửa, xem trước và các thao tác khác. |
| 7 | **Cấu hình** | **Không chặn — mở full** | Tất cả mục cấu hình, gồm thư mục đầu ra, lưu/đặt lại cấu hình, dọn dẹp, gói cài đặt, tài khoản, Cloud AI và Quyền sử dụng. |
| 8 | Chạy hàng loạt — `/batch` | **Chạy X job** | Thêm/xếp job, chỉnh sửa cấu hình và quản lý các job đã có. |
| 9 | Tự động hóa — `/automation` | **▶ Chạy job** | Chuẩn bị/sửa job, gợi ý, phân tích bản nháp YouTube, cấu hình và thao tác tiếp tục/thử lại với job đã có. |
| 10 | Clone video — `/video-clone` | **Dịch cả video** | Chọn nguồn, cấu hình, xem trước, chỉnh sửa và các thao tác còn lại. |
| 11 | Film — `/film` | **+ Tạo & Chạy** | Chuẩn bị, lưu/sửa job, quản lý và chạy lại kết quả/job đã có. |
| 12 | Trình biên tập — `/live-preview` | **Xuất bản** | Chuẩn bị dữ liệu cho trình biên tập, chỉnh timeline/phụ đề/media, xem trước và các thao tác còn lại. |

“X” trong **Chạy X job** là số lượng job thực tế, không phải tên nút cố định.

## Checklist khi sửa hoặc bổ sung sau này

- [ ] Sửa đúng hàng trong bảng: ghi rõ thao tác nào cần quyền và phần nào vẫn mở.
- [ ] Không thêm lớp khóa toàn trang/tab hoặc chặn chung mọi click, form, kéo thả.
- [ ] Rà cả frontend, backend và mọi nơi gọi API/helper dùng chung; phân biệt
  nghe thử với tạo giọng, xếp job với chạy job, chuẩn bị editor với dịch cả video.
- [ ] Kiểm tra khi chưa có quyền: phần vẫn mở dùng được; đúng nút bị chặn mở
  popup rõ ràng, không tạo tác vụ ngoài ý muốn và không mất dữ liệu cũ.
- [ ] Kiểm tra popup bằng tiếng Việt và tiếng Anh; đóng được, mở lại phải kiểm
  tra server lại. Kiểm tra cả trường hợp không kết nối được server.
- [ ] Kiểm tra khi đã có quyền: thao tác chính chạy bình thường.
- [ ] Giữ nguyên kiểm tra xác thực/phân quyền của bản Web và các lỗi đầu vào.
- [ ] Cập nhật test hồi quy tương ứng; nếu có đổi UI, cập nhật bản dịch và chạy
  `npm run test:i18n` cùng `npm run build`.

## Điểm tra cứu trong mã nguồn

- `frontend/src/app/appMode.ts`: đường dẫn chính thức của các trang.
- `backend/api/app.py`, `backend/api/deps.py`: kiểm tra quyền cho thao tác API;
  xem thêm handler tương ứng trong `backend/api/routes/`.
- `frontend/src/features/license/taskAccess.ts`: nhận phản hồi yêu cầu quyền và
  yêu cầu mở popup.
- `frontend/src/features/license/LicensePage.tsx`: thông báo quyền sử dụng và
  kiểm tra server khi mở.
- `frontend/src/features/configuration/ConfigModal.tsx`: cấu hình và thư mục đầu ra.
- `tests/test_action_access.py`, `tests/test_render_access.py`,
  `tests/task-access.test.mjs`: test hồi quy liên quan.

Lệnh kiểm tra tập trung sau khi sửa logic quyền:

```sh
PYTHONPATH=backend backend/.venv/bin/python -m pytest tests/test_action_access.py tests/test_render_access.py -q
node --test tests/task-access.test.mjs
```
