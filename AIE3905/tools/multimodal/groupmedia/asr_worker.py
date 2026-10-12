"""Offline worker. A separate process provides a real cancellation/timeout boundary."""

import json
import sys

from faster_whisper import WhisperModel


def main():
    model_path, audio_path, language = sys.argv[1:]
    model = WhisperModel(
        model_path,
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        local_files_only=True,
    )
    segments, info = model.transcribe(
        audio_path,
        language=language or None,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    rows = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    print(
        json.dumps(
            {
                "text": "".join(s["text"] for s in rows),
                "segments": rows,
                "language": info.language,
                "duration_seconds": info.duration,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
