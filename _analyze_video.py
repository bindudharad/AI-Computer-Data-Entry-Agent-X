import sys
import cv2
from atlas.vision.ocr import create_ocr_reader

SRC = r"C:\Users\Bindudhara D\Videos\Screen Recordings\Screen Recording 2026-08-12 094505.mp4"
STEP_S = 1.5

cap = cv2.VideoCapture(SRC)
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
dur = total / fps
print(f"fps={fps:.1f} frames={total} duration={dur:.1f}s", file=sys.stderr)

reader = create_ocr_reader()
last_frame = -1
step = int(round(fps * STEP_S))
idx = 0
while True:
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, frame = cap.read()
    if not ok:
        break
    t = idx / fps
    res = reader.read_image(frame)
    texts = []
    for r in res:
        txt = getattr(r, "text", None) or r[1] if not isinstance(r, str) else r
        texts.append(txt)
    joined = " | ".join(str(x) for x in texts if str(x).strip())
    print(f"[{t:6.1f}s] {joined}")
    idx += step
    if idx >= total:
        break

cap.release()
