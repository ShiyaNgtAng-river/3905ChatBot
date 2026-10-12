"""Create synthetic fixtures on macOS; no real messages, microphone or accounts."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import wave

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.home / "fixtures"
    root.mkdir(parents=True, exist_ok=True)
    lines = [
        "虚构会议通知（测试样本）",
        "项目：ORCHID",
        "时间：2026年10月8日 15:30",
        "地点：B302",
        "状态：仅为建议，尚未确认",
    ]
    text = "这是测试录音。会议建议在十月八日下午三点半举行，地点是三零二会议室，还没有确认。"
    image = Image.new("RGB", (1400, 620), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 50)
    for n, line in enumerate(lines):
        draw.text((60, 50 + n * 105), line, font=font, fill="black")
    image.save(root / "notice.png")
    subprocess.run(
        ["say", "-v", "Tingting", "-r", "155", "-o", str(root / "voice.aiff"), text],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(root / "voice.aiff"),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(root / "voice.wav"),
        ],
        check=True,
    )
    with wave.open(str(root / "silence.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000 * 5)
    (root / "truth.json").write_text(
        json.dumps(
            {"image_lines": lines, "audio_text": text}, ensure_ascii=False, indent=2
        )
        + "\n"
    )
    print(root)


if __name__ == "__main__":
    main()
