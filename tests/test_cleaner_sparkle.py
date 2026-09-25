"""Deterministic local image tests; no network, models, or generated media assets."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import cv2
import numpy as np

from pipeline.cleaner.sparkle import find_sparkle, remove_sparkle
from pipeline.cleaner import cleaner_ffmpeg as cleaner
from pipeline.cleaner.cleaner_jobs import create_job, get_job, _JOBS, _LOCK


def fixture(width=768, height=432, opacity=.30):
    yy, xx = np.mgrid[:height, :width]
    noise = np.random.default_rng(13).normal(0, 4, (height, width))
    stripes = 62 + 30 * np.sin(yy * .21 + xx * .025) + noise
    background = np.stack([stripes + 14, stripes + 6, stripes], axis=2).clip(0, 255).astype(np.uint8)
    # Independent supersampled analytic fixture with a known clean background.
    side = round(min(width, height) * .070)
    cx, cy = round(width * .93), round(height * .87)
    px = (np.arange(width * 4, dtype=np.float32) / 4 - cx) / (side / 2)
    py = (np.arange(height * 4, dtype=np.float32) / 4 - cy) / (side / 2)
    alpha = (abs(px)[None, :] ** .65 + abs(py)[:, None] ** .65 <= 1).astype(np.float32)
    alpha = cv2.GaussianBlur(cv2.resize(alpha, (width, height), interpolation=cv2.INTER_AREA), (0, 0), .5)
    marked = np.rint(background * (1 - opacity * alpha[..., None]) + 255 * opacity * alpha[..., None]).astype(np.uint8)
    return background, marked, alpha


class LocalSparkleTests(unittest.TestCase):
    def test_matches_ratios_and_recovers_texture_not_flat_inpaint(self):
        for width, height in [(768, 432), (432, 768), (512, 512), (640, 480), (840, 360)]:
            with self.subTest(ratio=(width, height)):
                background, marked, alpha = fixture(width, height)
                model = find_sparkle(marked)
                self.assertIsNotNone(model)
                self.assertAlmostEqual(model.opacity, .30, delta=.055)
                output = remove_sparkle(marked.copy(), model)
                active = alpha > .95
                before = np.abs(marked.astype(float) - background)[active].mean()
                after = np.abs(output.astype(float) - background)[active].mean()
                self.assertLess(after, before * .22, (before, after))
                x0, y0, x1, y1 = model.box
                outside = np.ones(alpha.shape, bool)
                outside[y0:y1, x0:x1] = False
                np.testing.assert_array_equal(output[outside], marked[outside])

    def test_non_watermarks_do_not_trigger_a_fixed_corner_mask(self):
        base, _, _ = fixture(1376, 768)
        for kind in ['flat', 'noise', 'stripes', 'text', 'circle', 'cross', 'diamond']:
            with self.subTest(kind=kind):
                image = base.copy()
                if kind == 'flat': image[:] = 100
                if kind == 'noise': image = np.random.default_rng(7).integers(0, 255, image.shape, np.uint8)
                if kind == 'text': cv2.putText(image, '2026', (1200, 690), cv2.FONT_HERSHEY_SIMPLEX, 1, (225, 225, 225), 2)
                if kind == 'circle': cv2.circle(image, (1278, 671), 28, (230, 230, 230), -1)
                if kind == 'cross':
                    cv2.line(image, (1251, 671), (1305, 671), (220, 220, 220), 8)
                    cv2.line(image, (1278, 644), (1278, 698), (220, 220, 220), 8)
                if kind == 'diamond': cv2.fillPoly(image, [np.array([(1278, 641), (1308, 671), (1278, 701), (1248, 671)])], (230, 230, 230))
                before = image.copy()
                self.assertIsNone(find_sparkle(image))
                np.testing.assert_array_equal(image, before)

    def test_image_job_uses_real_local_path_not_video_ocr_or_old_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            background, marked, _ = fixture()
            source = Path(directory) / 'ảnh gốc.png'
            ok, buffer = cv2.imencode('.png', marked)
            self.assertTrue(ok)
            buffer.tofile(source)
            job = create_job(source.name, 'logo', {}, str(source), directory)
            try:
                with patch.object(cleaner, '_detect_logo_with_retry', side_effect=AssertionError('OCR not needed for matched sparkle')), \
                     patch.object(cleaner, '_inpaint_video', side_effect=AssertionError('PNG is not video')), \
                     patch.object(cleaner._CACHE, 'restore', side_effect=AssertionError('Do not restore blurred logo cache')):
                    cleaner.run_cleaner_job_sync(job['id'])
                done = get_job(job['id'])
                self.assertEqual(done['status'], 'done', done['error'])
                output = cv2.imdecode(np.fromfile(done['output_path'], np.uint8), cv2.IMREAD_COLOR)
                self.assertEqual(output.shape, marked.shape)
                self.assertTrue(any('Local alpha recovery' in line for line in done['logs']))
                np.testing.assert_array_equal(cv2.imdecode(np.fromfile(source, np.uint8), cv2.IMREAD_COLOR), marked)
                self.assertLess(np.mean(abs(output.astype(float) - background)), np.mean(abs(marked.astype(float) - background)))
            finally:
                with _LOCK:
                    _JOBS.pop(job['id'], None)

    def test_missing_detection_does_not_write_a_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'blank.png', Path(directory) / 'out.png'
            cv2.imwrite(str(source), np.full((432, 768, 3), 90, np.uint8))
            with patch.object(cleaner, '_detect_logo_with_retry', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'CLEANER_LOGO_NOT_DETECTED'):
                    cleaner._inpaint_image(str(source), str(output), 'test')
            self.assertFalse(output.exists())

    def test_transparency_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            _, marked, _ = fixture()
            image = cv2.cvtColor(marked, cv2.COLOR_BGR2BGRA)
            image[:, :, 3] = np.arange(image.shape[1], dtype=np.uint16)[None, :] % 256
            source, output = Path(directory) / 'alpha.png', Path(directory) / 'out.png'
            cv2.imwrite(str(source), image)
            cleaner._inpaint_image(str(source), str(output), 'test')
            result = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
            np.testing.assert_array_equal(result[:, :, 3], image[:, :, 3])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg integration needs local tools')
    def test_static_video_retains_size_fps_frames_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            _, marked, _ = fixture()
            png = Path(directory) / 'frame.png'
            source = Path(directory) / 'source.mp4'
            output = Path(directory) / 'out.mp4'
            cv2.imwrite(str(png), marked)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-loop', '1', '-framerate', '12', '-i', str(png),
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                            '-c:v', 'libx264', '-crf', '10', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
            with patch.object(cleaner, '_detect_logo_with_retry', side_effect=AssertionError('static sparkle should match')), \
                 patch.object(cleaner, 'h264_encoder_args', return_value=['-c:v', 'libx264', '-crf', '18']):
                cleaner._inpaint_video(str(source), str(output), 'test')
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)]))
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            self.assertEqual((video['width'], video['height']), (768, 432))
            self.assertEqual(video['r_frame_rate'], '12/1')
            self.assertEqual(int(video['nb_frames']), 24)
            self.assertTrue(any(s['codec_type'] == 'audio' for s in info['streams']))


if __name__ == '__main__':
    unittest.main()
