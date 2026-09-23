# Changelog

## v8.4.6 — 2026-09-23

- Tạm ẩn Veo 3.1 Lite Lower Priority trên Flow và Flow Series; backend báo model không khả dụng.
- Nâng giới hạn luồng Flow lên 16 cho Flow, Series và Automation.
- Tự động hoá mở Ghép ảnh/video SRT với đường dẫn media, audio, image_prompts.txt, phụ đề và cài đặt job.
- Vẽ tay dùng bút máy mặc định, cho chọn sprite bằng icon; giữ ảnh gốc sắc nét ở cuối video.
- Tải cập nhật dùng bốn kết nối cho gói lớn nếu máy chủ hỗ trợ HTTP Range.

## v8.4.5 — 2026-09-23

- Drawing Windows không còn crash vì log Unicode trên code page `cp1252`.
- Renderer ép UTF-8 cho stdout/stderr và worker Windows.
- Thêm test hồi quy cho lỗi encoding trước khi tạo MP4.

## v8.4.4 — 2026-09-23

- Giới hạn CSS trang giới thiệu để tiêu đề Tự động hoá không bị phóng lớn.
- Ghép ảnh/video SRT: inpaint logo cho batch ảnh tĩnh trước Drawing, tránh delogo lần hai.
- Làm sạch video: thêm inpainting từng khung hình cho vùng logo được nhận diện; giữ âm thanh nguồn.
- Chất lượng xóa logo trên nền phức tạp và bản Windows cần kiểm chứng thực tế.

## v8.4.3 — 2026-09-23

- Thêm nút X và Hủy cập nhật trong tiến trình tải.
- Hủy thật sự dừng tải/retry, giữ file `.part` để lần sau tiếp tục và không tự cài.
- Bổ sung trạng thái `cancelling`/`cancelled` và khóa race trước khi bàn giao updater.
- Giữ nguyên quy trình updater đóng app, thay file và tự mở lại khi người dùng không hủy.

## v8.4.2 — 2026-09-23

- Drawing dùng đúng `ffprobe.exe` trong bundle Windows thay vì phụ thuộc PATH.
- Sửa lỗi MP4 hợp lệ bị loại nhầm với thông báo `Streaming renderer finished without an MP4 output`.

## v8.4.1 — 2026-09-23

- Drawing loại bỏ MP4 trung gian không có video stream trước khi ghép.
- Kiểm tra `ffprobe` cả file trung gian và file đầu ra cuối.
- Ẩn CMD worker/FFmpeg trên Windows nhưng vẫn giữ khả năng Hủy tiến trình.
- Retry riêng ảnh lỗi codec thay vì hủy toàn bộ batch.

## v8.4.0 — 2026-09-23

- Drawing kiểm tra codec và MP4 trung gian trước khi chuyển mã.
- Tự retry riêng ảnh bị lỗi codec, không hủy toàn bộ batch khi một worker thất bại tạm thời.
- Sửa lỗi `Output file does not contain any stream` khi render nhiều ảnh song song trên Windows.

## v8.3.9 — 2026-09-23

- Xác minh gói Flow mới nhất trước mỗi lần tạo ảnh/video; không dùng plan cũ để cấp quyền.
- Hiển thị rõ trạng thái Free/Pro/Ultra hoặc chưa xác minh tài khoản Flow.
- Tiến độ Flow cập nhật liên tục sau khi submit, không còn đứng ở 5% rồi nhảy thẳng 100% khi chạy hàng loạt.

## v8.3.8 — 2026-09-23

- Phóng to ảnh/video trong hộp thoại xem trước theo vùng preview, vẫn giữ đúng tỷ lệ.
- Flow bulk dispatch theo đúng thứ tự queue khi chạy nhiều luồng.
- Không retry generation sau khi Flow đã nhận job, tránh tạo media trùng và trừ credits hai lần.
- Chỉ đánh dấu hoàn tất sau khi file ảnh/video đã tải xuống và kiểm tra tồn tại, dung lượng hợp lệ.
- Tăng thời gian recovery video lên 900 giây và hiển thị progress liên tục trong lúc render.
- Chuẩn hóa workflow CI dùng Node.js 24 và action Python tương thích.

## v6.0.0 — 2026-09-13

- Hỗ trợ Google Flow với tài khoản Gói thường (Free) tạo ảnh với Nano Banana 2/Lite (16:9, x1).
- Tự động fallback và ngăn chặn cấu hình không tương thích cho gói thường.
- Cải thiện độ tin cậy đồng bộ credits và phiên Google Flow qua `flow.google.com` (batchexecute RPC).
- Khắc phục lỗi giao diện chữ credits bị đè trên macOS WebKit.
- Thêm hiệu ứng xoay (spin animation) và trạng thái đang đồng bộ cho nút "Đồng bộ tất cả".

## v5.0.1 — 2026-09-05

- Ổn định runtime APP trên Windows/macOS: chuẩn hóa `PATH`, môi trường worker và thư mục tạm.
- Tách tác vụ AI native khỏi process UI/API để lỗi CUDA, Torch, OpenCV và ONNX không làm APP tự thoát.
- Flow profile bỏ qua cache/lock tạm, xử lý race khi profile bị xóa và báo yêu cầu đăng nhập lại rõ ràng.
- Updater desktop thay bundle có staging/rollback, đóng APP cũ, mở bản mới và hỗ trợ macOS arm64/x64.
- Bổ sung kiểm thử hồi quy cho runtime, Flow profile, worker và gói phát hành.
