"""Render demo.gif: a terminal screencast of loopguard catching a looping agent."""
from PIL import Image, ImageDraw, ImageFont

W, H = 880, 300
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 15)
LINE_H = 22
BG, FG, DIM, WARN = "#1e1e2e", "#cdd6f4", "#6c7086", "#f38ba8"
OK = "#a6e3a1"

# The cast: (text, color) lines revealed one per frame
cast = [
    ("$ loopguard watch ~/.claude/projects/myapp -v", DIM),
    ("[loopguard] watching session.jsonl (poll 2s)", DIM),
    ("", FG),
    ("  agent: Bash ls -la", FG),
    ("  agent: Bash ls -la", FG),
    ("  agent: Read src/app.py", FG),
    ("  agent: Bash ls -la", FG),
    ("  agent: Bash ls -la", FG),
    ("  agent: Bash ls -la", FG),
    ("", FG),
    ("[loopguard] 14:02:11Z  LOOP: tool 'bash' repeated 3+ times in last 20 events", WARN),
    ("[loopguard] Telegram alert sent to @you", OK),
    ("[loopguard] incident open — new unique tool call will reset", DIM),
]


def frame(shown, alert):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 34], fill="#11111b")
    d.text((16, 8), "loopguard — live demo", font=FONT, fill="#89b4fa")
    for i, (text, color) in enumerate(shown):
        d.text((16, 48 + i * LINE_H), text, font=FONT, fill=color)
    if alert:
        d.rectangle([0, H - 30, W, H], fill="#313244")
        d.text((16, H - 24), "exit=1  incident detected", font=FONT, fill=WARN)
    return img


frames, durations = [], []
for n in range(1, len(cast) + 1):
    alert = n >= 11  # from the LOOP line on, show status bar
    frames.append(frame(cast[:n], alert))
    durations.append(350 if alert else 550)

frames[0].save("demo.gif", save_all=True, append_images=frames[1:],
               duration=durations, loop=0, optimize=True)
print("demo.gif written,", len(frames), "frames")
