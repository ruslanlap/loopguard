"""Render demo.gif from REAL loopguard replay output (honest demo)."""
import subprocess
from PIL import Image, ImageDraw, ImageFont

W, H = 760, 260
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
LINE_H = 20
BG, FG, DIM, WARN = "#1e1e2e", "#cdd6f4", "#6c7086", "#f38ba8"
OKC = "#a6e3a1"

# capture real command + real output
cmd_line = "$ loopguard replay session.jsonl"
proc = subprocess.run(["./loopguard.py", "replay", "fixtures/loop_tool.jsonl"],
                      capture_output=True, text=True)
lines = [l.rstrip() for l in proc.stdout.splitlines() if l.strip()]
assert any("LOOP" in l for l in lines), "expected LOOP in replay output"

cast = [(cmd_line, DIM)]
for l in lines:
    color = WARN if "⚠" in l else OKC
    cast.append((l.replace("⚠  ", "⚠ "), color))

def frame(shown):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill="#11111b")
    d.text((14, 7), "loopguard — real `replay` output", font=FONT, fill="#89b4fa")
    for i, (text, color) in enumerate(shown):
        d.text((14, 42 + i * LINE_H), text, font=FONT, fill=color)
    return img

frames, durations = [], []
for n in range(1, len(cast) + 1):
    frames.append(frame(cast[:n]))
    durations.append(450 if n == len(cast) else 320)

frames[0].save("demo.gif", save_all=True, append_images=frames[1:],
               duration=durations, loop=0, optimize=True)
print("demo.gif:", len(frames), "frames,", len(cast) - 1, "real output lines")
