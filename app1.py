# app_whisper_chunks.py
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import streamlit as st
import whisper

SUPPORTED_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}


@dataclass
class ChunkResult:
    filename: str
    seconds: float
    text: str


def have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def run_ffmpeg(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{p.stderr.strip()}")


def split_audio_into_chunks(*, in_path: Path, out_dir: Path, chunk_seconds: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = in_path.stem
    ext = in_path.suffix.lower()

    # 1) Fast split (copy codec) -> same extension
    fast_pattern = str(out_dir / f"{stem}_%05d{ext}")
    try:
        run_ffmpeg([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(in_path),
            "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
            "-c", "copy",
            fast_pattern,
        ])
        chunks = sorted(out_dir.glob(f"{stem}_*{ext}"))
        if chunks:
            return chunks
    except Exception:
        pass

    # 2) Fallback: re-encode to mono 16k WAV (ASR-friendly)
    wav_pattern = str(out_dir / f"{stem}_%05d.wav")
    run_ffmpeg([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(in_path),
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        wav_pattern,
    ])
    chunks = sorted(out_dir.glob(f"{stem}_*.wav"))
    if not chunks:
        raise RuntimeError("No chunks produced by ffmpeg.")
    return chunks


@st.cache_resource
def load_whisper_medium():
    return whisper.load_model("medium")


def transcribe_chunks_whisper(
    *,
    model,
    chunk_paths: list[Path],
    language: str,
    progress_cb,
) -> tuple[str, list[ChunkResult], list[float]]:
    stitched_parts: list[str] = []
    results: list[ChunkResult] = []
    secs: list[float] = []

    for i, p in enumerate(chunk_paths, start=1):
        t0 = time.time()
        r = model.transcribe(str(p), language=language)
        dt = time.time() - t0

        text = (r.get("text") or "").strip()
        stitched_parts.append(text)
        results.append(ChunkResult(filename=p.name, seconds=dt, text=text))
        secs.append(dt)

        progress_cb(i, len(chunk_paths), p.name, dt)

    stitched_text = "\n".join(stitched_parts).strip()
    return stitched_text, results, secs


def main() -> None:
    st.set_page_config(page_title="Whisper (medium) — Streamlit", layout="wide")
    st.title("Whisper (medium) — chunk → transcribe → stitch")

    if not have_ffmpeg():
        st.error("ffmpeg not found. Install it with:  brew install ffmpeg")
        st.stop()

    with st.sidebar:
        st.header("Settings")
        chunk_seconds = st.slider("Chunk length (seconds)", min_value=10, max_value=180, value=60, step=5)
        language = st.text_input("Language", value="fr").strip() or "fr"
        st.caption("Model is fixed: whisper `medium`.")

    files = st.file_uploader(
        "Upload audio files",
        type=[e.lstrip(".") for e in sorted(SUPPORTED_EXTS)],
        accept_multiple_files=True,
    )

    colA, colB = st.columns([1, 1], gap="large")
    with colA:
        st.subheader("Run")
        run = st.button("Transcribe", type="primary", disabled=(not files))

    if not run:
        with colB:
            st.subheader("Outputs")
            st.info("Upload one or more audio files, then click **Transcribe**.")
        return

    st.write("Loading Whisper model: `medium` …")
    model = load_whisper_medium()

    out_bundle = []

    with st.status("Processing…", expanded=True) as status:
        for up in files:
            suffix = Path(up.name).suffix.lower()
            if suffix not in SUPPORTED_EXTS:
                st.warning(f"Skipping unsupported file: {up.name}")
                continue

            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                in_path = tmpdir / up.name
                in_path.write_bytes(up.read())

                file_tag = in_path.stem
                chunks_dir = tmpdir / "chunks" / file_tag
                outputs_dir = tmpdir / "out"
                outputs_dir.mkdir(parents=True, exist_ok=True)

                st.write(f"**{up.name}** → splitting into {chunk_seconds}s chunks…")
                chunk_paths = split_audio_into_chunks(
                    in_path=in_path,
                    out_dir=chunks_dir,
                    chunk_seconds=chunk_seconds,
                )
                st.write(f"Chunks: `{len(chunk_paths)}`")

                prog = st.progress(0)
                log = st.empty()

                def progress_cb(i, total, name, dt):
                    prog.progress(int((i / total) * 100))
                    log.write(f"[{i}/{total}] {name} — {dt:.2f}s")

                st.write("Transcribing with Whisper (medium)…")
                stitched_text, per_chunk, per_chunk_secs = transcribe_chunks_whisper(
                    model=model,
                    chunk_paths=chunk_paths,
                    language=language,
                    progress_cb=progress_cb,
                )

                out_txt = outputs_dir / f"{in_path.stem}.txt"
                out_json = outputs_dir / f"{in_path.stem}.json"

                out_txt.write_text(stitched_text + "\n", encoding="utf-8")

                payload = {
                    "file": up.name,
                    "model": "medium",
                    "language": language,
                    "chunk_seconds": chunk_seconds,
                    "num_chunks": len(chunk_paths),
                    "seconds_per_chunk": per_chunk_secs,
                    "chunks": [asdict(x) for x in per_chunk],
                    "text": stitched_text,
                }
                out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                out_bundle.append({
                    "name": up.name,
                    "txt_name": out_txt.name,
                    "txt_bytes": out_txt.read_bytes(),
                    "json_name": out_json.name,
                    "json_bytes": out_json.read_bytes(),
                })

                st.success(f"Done: {up.name}")

        status.update(label="All files processed.", state="complete", expanded=False)

    st.divider()
    st.subheader("Outputs")

    for item in out_bundle:
        with st.expander(item["name"], expanded=True):
            st.download_button(
                label=f"Download TXT ({item['txt_name']})",
                data=item["txt_bytes"],
                file_name=item["txt_name"],
                mime="text/plain",
            )
            st.download_button(
                label=f"Download JSON ({item['json_name']})",
                data=item["json_bytes"],
                file_name=item["json_name"],
                mime="application/json",
            )
            st.text_area(
                "Transcript (stitched)",
                item["txt_bytes"].decode("utf-8", errors="replace"),
                height=220,
            )


if __name__ == "__main__":
    main()