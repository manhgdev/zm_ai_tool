import { localize, type AppLocale } from '@/app/i18n'

const labels: Record<string, [string, string, string, string]> = {
  ffmpeg: ['FFmpeg', 'FFmpeg', 'Xử lý âm thanh và xuất video.', 'Audio processing and video export.'],
  ffprobe: ['FFprobe', 'FFprobe', 'Đọc thông tin file media.', 'Read media file information.'],
  ai_runtime: ['Whisper (nhận dạng giọng nói)', 'Whisper (speech recognition)', 'Nhận dạng lời thoại bằng Faster-Whisper.', 'Transcribe speech with Faster-Whisper.'],
  ai_runtime_diarization: ['Sherpa-ONNX (tách người nói)', 'Sherpa-ONNX (speaker diarization)', 'Model được tải khi sử dụng tách người nói.', 'Models download when speaker diarization is used.'],
  ai_runtime_ocr: ['OCR (nhận dạng chữ)', 'OCR (text recognition)', 'Đọc chữ trong video bằng RapidOCR và OpenCV.', 'Read video text with RapidOCR and OpenCV.'],
  ai_runtime_vieneu: ['zmAI + VieNeu cục bộ', 'zmAI + VieNeu Local', 'Tổng hợp và nhân bản giọng nói.', 'Speech synthesis and voice cloning.'],
  ocr_cuda: ['Tăng tốc OCR bằng GPU', 'GPU-accelerated OCR', 'Dùng CUDA hoặc DirectML phù hợp với thiết bị.', 'Use CUDA or DirectML matching the hardware.'],
  demucs: ['Demucs (tách giọng hát)', 'Demucs (vocal separation)', 'Tách giọng và nhạc nền khi cần.', 'Separate vocals and background music when needed.'],
  say: ['Giọng nói hệ thống macOS', 'macOS system speech', 'Chuyển văn bản thành giọng nói hệ thống.', 'System text-to-speech.'],
  espeak: ['espeak-ng', 'espeak-ng', 'Giọng nói hệ thống Windows/Linux.', 'Windows/Linux system speech.'],
  ollama: ['Ollama', 'Ollama', 'Dịch cục bộ, không bắt buộc.', 'Optional local translation.'],
  node: ['Node.js (NVM)', 'Node.js (NVM)', 'Môi trường chạy JavaScript.', 'JavaScript runtime.'],
  data: ['Thư mục dữ liệu', 'Data folder', 'Lưu project, cache và file đầu ra.', 'Store projects, cache and output files.'],
}

export function setupLabel(locale: AppLocale, id: string, field: 'name' | 'hint'): string {
  const row = labels[id]
  if (!row) return field === 'name' ? id : localize(locale, 'Xem chi tiết kỹ thuật bên dưới.', 'See technical details below.')
  return field === 'name' ? localize(locale, row[0], row[1]) : localize(locale, row[2], row[3])
}
