"""Run the MPF test and auto-click the first form field when the anchor wait begins."""
import subprocess
import sys
import time
import ctypes


def click(x, y):
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.3)
    ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.1)
    ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.2)


# App No center position
X, Y = 1100, 338

cmd = [sys.executable] + sys.argv[1:]
print(f"Running: {cmd}")
proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

clicked = False
lines = []
start = time.time()
while True:
    line = proc.stdout.readline()
    if line:
        lines.append(line.rstrip())
        print(line.rstrip(), flush=True)
        # When we see the anchor wait message, click the field
        if not clicked and (
            "waiting for you to click" in line
            or "click a text/date/dropdown" in line
        ):
            time.sleep(1.0)
            print(f"\n>>> Clicking anchor at ({X}, {Y})...\n", flush=True)
            click(X, Y)
            clicked = True
    else:
        # Check if process ended
        if proc.poll() is not None:
            break
        # If we've been waiting a while and the anchor was never clicked, try clicking anyway
        if not clicked and time.time() - start > 45:
            print(f"\n>>> Force-clicking anchor at ({X}, {Y})...\n", flush=True)
            click(X, Y)
            clicked = True
        time.sleep(0.2)

# Drain remaining output
for line in proc.stdout:
    lines.append(line.rstrip())
    print(line.rstrip(), flush=True)

print(f"\nExit code: {proc.returncode}")
sys.exit(proc.returncode)