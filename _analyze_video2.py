import sys
import cv2
from atlas.vision.ocr import create_ocr_reader

SRC = r"C:\Users\Bindudhara D\Videos\Screen Recordings\Screen Recording 2026-08-12 094505.mp4"
TIMES = [90, 100, 110, 113, 114.5, 116, 118, 120, 122]

cap = cv2.VideoCapture(SRC)
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
reader = create_ocr_reader()
for t in TIMES:
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    if not ok:
        print(f"[{t}s] no frame")
        continue
    res = reader.read_image(frame)
    texts = []
    for r in res:
        txt = getattr(r, "text", None) or r[1] if not isinstance(r, str) else r
        texts.append(txt)
    joined = " | ".join(str(x) for x in texts if str(x).strip())
    print(f"[{t:6.1f}s] {joined}")
    print("=" * 80)
cap.release()
