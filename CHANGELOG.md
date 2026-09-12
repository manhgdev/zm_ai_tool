# Changelog

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

