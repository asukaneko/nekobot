"""让缓存音频的文件扩展名与厂商实际返回的格式保持一致。"""

import os


def normalize_generated_audio_path(path: str) -> str:
    with open(path, "rb") as handle:
        header = handle.read(64)
    stem, extension = os.path.splitext(path)
    actual_extension = extension
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        actual_extension = ".wav"
    elif header.startswith(b"OggS"):
        actual_extension = ".opus" if b"OpusHead" in header else ".ogg"
    elif header.startswith(b"fLaC"):
        actual_extension = ".flac"
    elif header[4:8] == b"ftyp":
        actual_extension = ".m4a"
    elif header.startswith(b"#!AMR"):
        actual_extension = ".amr"
    elif header.startswith(b"ID3"):
        actual_extension = ".mp3"
    if actual_extension != extension:
        corrected_path = stem + actual_extension
        os.replace(path, corrected_path)
        return corrected_path
    return path
