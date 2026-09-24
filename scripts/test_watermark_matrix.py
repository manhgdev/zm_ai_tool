"""Run the watermark detector/inpaint pass over a local Flow image matrix.

Flow exports are intentionally not committed. Put them in --input-dir, one
file per aspect ratio, and inspect the copied originals, cleaned outputs and
mask overlays in --output-dir.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    import cv2
    from pipeline.export.cover_mask import _inpaint_region
    from pipeline.ocr.logo import detect_logo_bbox_inprocess

    args.output_dir.mkdir(parents=True, exist_ok=True)
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    images = sorted(p for p in args.input_dir.iterdir() if p.suffix.casefold() in extensions)
    if not images:
        parser.error(f"No supported images found in {args.input_dir}")

    report: list[dict[str, object]] = []
    for source in images:
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image is None:
            report.append({"file": source.name, "status": "decode_failed"})
            continue
        detection = detect_logo_bbox_inprocess(source)
        cleaned = image.copy()
        if detection and isinstance(detection.get("bbox"), dict):
            bbox = detection["bbox"]
            h, w = cleaned.shape[:2]
            box = (
                round(float(bbox.get("x", 0)) * w),
                round(float(bbox.get("y", 0)) * h),
                round((float(bbox.get("x", 0)) + float(bbox.get("w", 0))) * w),
                round((float(bbox.get("y", 0)) + float(bbox.get("h", 0))) * h),
            )
            _inpaint_region(cleaned, box, polygon=detection.get("polygon"))
        target = args.output_dir / f"{source.stem}.cleaned{source.suffix}"
        cv2.imwrite(str(target), cleaned)

        overlay = image.copy()
        if detection and isinstance(detection.get("bbox"), dict):
            h, w = overlay.shape[:2]
            b = detection["bbox"]
            x0, y0 = round(float(b.get("x", 0)) * w), round(float(b.get("y", 0)) * h)
            x1, y1 = round((float(b.get("x", 0)) + float(b.get("w", 0))) * w), round((float(b.get("y", 0)) + float(b.get("h", 0))) * h)
            cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 0, 255), max(1, round(min(w, h) / 500)))
        cv2.imwrite(str(args.output_dir / f"{source.stem}.mask.png"), overlay)
        report.append({"file": source.name, "status": "ok", "detection": detection, "output": target.name})

    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
