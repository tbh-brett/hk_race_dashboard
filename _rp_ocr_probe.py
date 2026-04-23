"""Probe RapidOCR output on a running position photo."""
import sys
from rapidocr_onnxruntime import RapidOCR

img_path = sys.argv[1] if len(sys.argv) > 1 else r"running_position_photos\20260412\R1.jpg"
ocr = RapidOCR()
result, _ = ocr(img_path)
print(f"OCR: {len(result) if result else 0} text boxes")
if not result:
    sys.exit(0)

for box, text, conf in result:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    cx = int(sum(xs) / 4)
    cy = int(sum(ys) / 4)
    try:
        conf_f = float(conf)
    except Exception:
        conf_f = -1.0
    print(f"  ({cx:3d},{cy:3d}) conf={conf_f:.2f}  {text!r}")
