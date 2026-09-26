// Types và constants cho SrtImagePage — tách ra để file page gọn hơn

export type Job = {
  id: string
  name: string
  status: 'queued' | 'processing' | 'paused' | 'done' | 'error' | 'cancelled'
  progress: number
  error?: string
  logs?: string[]
}

export type MissingCue = {
  index: number
  timecode: string
  duration?: number
  label?: string
  expected_name?: string
}

export type MissingMediaInfo = {
  required: number
  available: number
  missing_count?: number
  available_files?: string[]
  missing_cues?: MissingCue[]
  preview: boolean
}

export const SETTINGS_KEY = 'zm_ai_tool.srt-image.settings.v1'
export const JOB_KEY = 'zm_ai_tool.srt-image.job-id.v1'

export const HELP = {
  media: ['Thư mục ảnh / video', 'Chọn một thư mục chứa toàn bộ ảnh hoặc clip dùng để dựng video. APP đọc trực tiếp trong thư mục và tự sắp xếp theo tên, không upload/copy từng video.', 'Dùng JPG, JPEG, JFIF, PNG, WEBP, BMP, MP4, MOV, MKV, WEBM, AVI hoặc M4V. Nên đặt tên 001, 002, 003… tương ứng từng dòng timeline.'],
  audio: ['File audio', 'Âm thanh narration chính của video. Audio có sẵn trong các clip đầu vào sẽ bị bỏ để tránh chồng tiếng.', 'Dùng MP3, WAV, M4A hoặc định dạng audio FFmpeg đọc được. Có thể bỏ qua nếu muốn video không có tiếng.'],
  timeline: ['File timeline', 'Quyết định file ảnh/clip nào xuất hiện và xuất hiện trong bao lâu theo mốc timecode. Mỗi dòng timecode tương ứng một file theo thứ tự tên (001, 002, 003…).', 'Hỗ trợ TXT, SRT, VTT, ASS/SSA, CSV, TSV, JSON và LRC. Ví dụ TXT: 001_[00:00:00.00-00:00:08.50], 001_[00.00-00.05], 001_[00:00-00:05]…'],
  output: ['File xuất', 'Chọn thư mục và tên video MP4 sẽ được lưu sau khi render. Nếu không chọn, APP lưu trong thư mục xuất mặc định.', 'Bấm Chọn để mở hộp thoại Windows. Ví dụ: D:\\\\Video\\\\lich-su-loai-nguoi.mp4.'],
  subtitles: ['File phụ đề', 'Chèn chữ phụ đề trực tiếp vào hình ảnh video.', 'Dùng file .SRT có timecode hợp lệ. Đây là file bắt buộc ở chế độ Ghép ảnh/video SRT.'],
  subtitleFontFamily: ['Phông chữ', 'Kiểu chữ (font) dùng cho phụ đề.', 'Nên chọn các font không chân (sans-serif) nét rõ như Noto Sans, Inter, Roboto để dễ đọc trên mọi thiết bị.'],
  subtitleSize: ['Cỡ chữ', 'Điều chỉnh kích thước chữ phụ đề khi chèn vào video.', 'Giá trị mặc định là 8. Tăng nếu chữ quá nhỏ, giảm nếu chữ chiếm nhiều khung hình.'],
  subtitleOffset: ['Lệch phụ đề', 'Dịch toàn bộ phụ đề sớm hoặc muộn hơn so với audio.', 'Số dương làm phụ đề xuất hiện muộn hơn; số âm làm phụ đề xuất hiện sớm hơn. Đơn vị là giây.'],
  subtitleMargin: ['Lề dưới', 'Điều chỉnh khoảng cách từ phụ đề đến mép dưới video.', 'Giá trị càng lớn thì phụ đề càng được đẩy lên cao. Mặc định là 34.'],
  subtitleBackground: ['Nền chữ', 'Kiểu nền phía sau chữ phụ đề, giống tùy chọn nền bản dịch bên Clone Video.', 'Tắt: chỉ viền · Đặc: nền đen mờ · Hộp: nền bo góc viền trắng.'],
  subtitleColor: ['Màu chữ', 'Màu sắc chính của chữ phụ đề.', 'Thường nên để màu trắng (#FFFFFF) hoặc vàng nhạt để tương phản tốt với các nền tối.'],
  effect: ['Hiệu ứng', 'Chọn cách chuyển từ cảnh hiện tại sang cảnh kế tiếp. Tắt sẽ giữ chuyển cảnh trực tiếp và render nhanh nhất.', 'Mặc định nên để Tắt. Chỉ bật khi muốn video có chuyển cảnh mềm hơn.'],
  transition: ['Thời lượng hiệu ứng', 'Số giây dành cho một lần chuyển cảnh. Giá trị lớn làm hai cảnh hòa vào nhau lâu hơn.', 'Khoảng 0,2–0,5 giây thường tự nhiên; mặc định 0,28 giây.'],
  resolution: ['Chất lượng xuất', 'Kích thước khung hình video xuất. Auto mặc định giữ tỷ lệ ảnh và xuất với cạnh ngắn 1080px.', 'Auto cho ảnh ngang là 1920×1080, ảnh dọc là 1080×1920; chọn preset khi cần khung hình cố định.'],
  fps: ['FPS', 'Số khung hình mỗi giây. FPS cao mượt hơn nhưng render chậm và file lớn hơn.', '30 FPS phù hợp hầu hết video; 60 FPS chỉ dùng khi nguồn có chuyển động nhanh.'],
  zoom: ['Zoom', 'Tạo chuyển động phóng nhẹ cho ảnh tĩnh để cảnh bớt đứng yên.', 'Chỉ có ý nghĩa rõ với ảnh; video đầu vào đã có chuyển động nên thường để Tắt.'],
  speed: ['Speed', 'Thay đổi tốc độ cả hình và narration để chúng vẫn khớp nhau.', '100% là tốc độ gốc; 110% nhanh hơn 10%; 80% chậm hơn 20%.'],
  quality: ['Chất lượng nén', 'Điều khiển mức nén video. Chất lượng cao cho hình đẹp hơn nhưng render lâu và file lớn.', 'Cân bằng phù hợp mặc định; chọn Nhanh khi cần thử hoặc Preview.'],
  volume: ['Âm lượng', 'Điều chỉnh âm lượng file narration chính trong video xuất.', '100% giữ nguyên; 80% giảm nhẹ; trên 100% có thể gây vỡ tiếng.'],
  encoder: ['Encoder', 'Chọn phần cứng dùng để mã hóa video. Tự động ưu tiên GPU khi máy hỗ trợ và chuyển sang CPU khi cần.', 'Nên để Tự động. Chọn CPU khi driver GPU gặp lỗi; chọn GPU để tăng tốc trên máy tương thích.'],
  preview: ['Preview', 'Giới hạn số giây render khi bấm Preview để kiểm tra nhanh trước khi xuất toàn bộ video.', '15 giây thường đủ để kiểm tra tỷ lệ, phụ đề, logo và âm lượng.'],
  metadata: ['Xóa metadata', 'Loại bỏ thông tin phụ như tên encoder và metadata khỏi file MP4 đầu ra.', 'Không ảnh hưởng hình hoặc tiếng. Bật nếu muốn file xuất sạch thông tin kỹ thuật.'],
  delogo: ['Xóa logo gốc', 'Xóa watermark AI (Veo 3, Grok…) bằng bộ lọc delogo của FFmpeg trên frame gốc trước khi scale.', 'Vùng xóa mặc định ở góc dưới phải. Tắt Tự định vị để kéo tay chọn vùng trên Preview.'],
  delogoAuto: ['Tự định vị', 'Tự động đặt vùng xóa logo ở góc dưới phải — vị trí mặc định của Veo 3, Grok.', 'Tắt để kéo chuột vẽ vùng xóa tùy ý trên Preview trực tiếp.'],
} as const

export type HelpKey = keyof typeof HELP

export function cachedSettings(): Record<string, unknown> {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')
  } catch {
    return {}
  }
}
