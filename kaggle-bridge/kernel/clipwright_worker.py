"""ClipWright Kaggle bridge — kernel entry point (Phase 1 prototype).

Run inside a Kaggle kernel:

    python /kaggle/working/clipwright_worker.py --job /kaggle/input/<dataset>/job.json

The script walks Gates A→G from the specification and writes everything under
``/kaggle/working/clipwright-output``:

    environment.json      Gate A hardware/software report
    transcript.json       ClipWright v2 transcript cache (milliseconds)
    ai-selection.json     Gate C validated selection + raw model reply
    clips/*.mp4           Gate D/G rendered clips (plus an NVENC benchmark twin)
    bench/*.mp4           encoder comparison renders (deleted unless kept)
    result.json           Gate F result manifest with SHA-256 checksums
    benchmark.json        machine-readable timings
    REPORT.md             human-readable run report
    job.json              the validated job that ran (credentials stripped by design)

Exit code is 0 only when every Gate G check passed. A failed run still writes
its reports, because the failure reason is the deliverable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

OUTPUT_DIR = Path("/kaggle/working/clipwright-output")
INPUT_ROOT = Path("/kaggle/input")
MAX_SAVED_OUTPUT_BYTES = 19 * 1024 * 1024 * 1024  # Kaggle autosaves ~20 GB


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #


def bootstrap_bridge_path() -> Path:
    """Locate the ``processor`` package.

    The bridge ships as a private dataset, so the package is normally at
    ``/kaggle/input/<dataset>/kaggle-bridge``. ``BRIDGE_PATH`` overrides that
    for local testing.
    """
    candidates: List[Path] = []
    override = os.environ.get("BRIDGE_PATH")
    if override:
        candidates.append(Path(override))
    candidates.extend([Path.cwd(), Path.cwd().parent, Path("/kaggle/working/kaggle-bridge")])
    if INPUT_ROOT.exists():
        for child in sorted(INPUT_ROOT.iterdir()):
            candidates.extend([child, child / "kaggle-bridge"])
            if child.is_dir():
                for grandchild in sorted(child.iterdir()):
                    candidates.append(grandchild / "kaggle-bridge")
                    candidates.append(grandchild)

    for candidate in candidates:
        if (candidate / "processor" / "__init__.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    raise SystemExit(
        "Could not find the 'processor' package. Upload this repository as a private "
        "Kaggle dataset (see README, 'Push the bridge as a dataset') and make sure the "
        "dataset root contains 'kaggle-bridge/processor/', or set BRIDGE_PATH."
    )


BRIDGE_ROOT = bootstrap_bridge_path()

from processor import (  # noqa: E402  (import after sys.path bootstrap)
    ai_inference,
    downloader,
    environment,
    manifest,
    rendering,
    reporting,
    transcription,
    validation,
)


# --------------------------------------------------------------------------- #
# job + input resolution
# --------------------------------------------------------------------------- #


def resolve_job_path(explicit: Optional[str]) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"job manifest not found: {path}")
        return path

    for candidate in (
        Path("/kaggle/working/job.json"),
        Path.cwd() / "job.json",
    ):
        if candidate.is_file():
            return candidate

    if INPUT_ROOT.exists():
        matches = sorted(INPUT_ROOT.rglob("*.json"))
        preferred = [path for path in matches if path.name in ("job.json", "sample-job.json")]
        for path in preferred or matches:
            try:
                payload = manifest.load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping) and payload.get("schema_version") == manifest.JOB_SCHEMA_VERSION:
                return path

    raise SystemExit(
        "No job manifest found. Attach a private dataset containing job.json, or copy "
        "config/sample-job.json to /kaggle/working/job.json and edit the paths."
    )


def resolve_local_input(job: Mapping[str, Any]) -> Path:
    """Find the named input video under /kaggle/input (or the job's own folder)."""
    source = job.get("source") or {}
    filename = str(source.get("filename") or "")
    if not filename:
        raise SystemExit("job.source.filename is required for local_file sources")

    direct = Path(source.get("path") or "")
    if direct.is_file():
        return direct

    if INPUT_ROOT.exists():
        exact = sorted(INPUT_ROOT.rglob(filename))
        if exact:
            return exact[0]
        # Fall back to any video: a single-file dataset often gets renamed by Kaggle.
        videos = sorted(
            path
            for path in INPUT_ROOT.rglob("*")
            if path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
        )
        if videos:
            return videos[0]

    raise SystemExit(
        f"input video {filename!r} was not found under {INPUT_ROOT}. Attach the private "
        "dataset that contains it."
    )


# --------------------------------------------------------------------------- #
# captions
# --------------------------------------------------------------------------- #


def write_ass_captions(
    transcript: Mapping[str, Any],
    path: Path,
    *,
    start_ms: int,
    end_ms: int,
    width: int,
    height: int,
) -> Path:
    """Write a minimal ASS file for the clip window.

    NOTE: this is a deliberately plain 2-word-per-cue renderer. ClipWright's own
    caption templates (``backend/src/caption_templates.py``) are not ported in
    Phase 1; this file proves that subtitle burn-in works on Kaggle's ffmpeg and
    that the ASS path is correct. Styling parity is a Phase 2 item.
    """
    words = [
        word
        for word in (transcript.get("words") or [])
        if isinstance(word.get("start"), int) and start_ms <= word["start"] < end_ms
    ]
    cues: List[Tuple[int, int, str]] = []
    chunk: List[Mapping[str, Any]] = []
    for word in words:
        chunk.append(word)
        if len(chunk) >= 2:
            cues.append((chunk[0]["start"], chunk[-1]["end"], " ".join(str(w.get("text", "")).strip() for w in chunk)))
            chunk = []
    if chunk:
        cues.append((chunk[0]["start"], chunk[-1]["end"], " ".join(str(w.get("text", "")).strip() for w in chunk)))

    font_size = max(24, int(height * 0.042))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: ClipWright,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,4,1,2,60,60,120,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue_start, cue_end, text in cues:
        relative_start = max(0, cue_start - start_ms)
        relative_end = max(relative_start + 200, cue_end - start_ms)
        lines.append(
            f"Dialogue: 0,{_ass_time(relative_start)},{_ass_time(relative_end)},ClipWright,,0,0,0,,"
            f"{text.replace('{', '(').replace('}', ')')}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ass_time(milliseconds: int) -> str:
    hours, remainder = divmod(max(0, int(milliseconds)), 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1000)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{ms // 10:02d}"


# --------------------------------------------------------------------------- #
# main pipeline
# --------------------------------------------------------------------------- #


def run(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ClipWright Kaggle bridge (Phase 1)")
    parser.add_argument("--job", help="path to a job manifest; auto-detected when omitted")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="output directory")
    parser.add_argument("--keep-benchmark-clips", action="store_true", help="keep encoder comparison renders")
    args = parser.parse_args(argv)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = output_dir / "clips"
    benches_dir = output_dir / "bench"
    clips_dir.mkdir(exist_ok=True)

    run_notes: List[str] = []
    collector = reporting.BenchmarkCollector()

    # ---- Gate A -----------------------------------------------------------
    with collector.timer("gate_a_environment") as gate_a:
        env_report = environment.collect(probe_ffmpeg=True)
        environment.write_report(output_dir / "environment.json", env_report)
        gate_a.details = {
            "cuda_available": env_report.cuda_available,
            "gpu": (env_report.payload.get("cuda", {}).get("devices") or [{}])[0].get("name"),
            "problems": env_report.problems,
        }
    print(environment.to_markdown(env_report))

    job_path = resolve_job_path(args.job)
    job = manifest.load_and_validate_job(job_path)
    manifest.dump_json(output_dir / "job.json", job)
    processing = job.get("processing") or {}
    mode = str(processing.get("mode") or "gpu")
    require_gpu = bool(processing.get("require_gpu", True))

    if mode == "gpu" and require_gpu:
        try:
            environment.require_gpu(env_report, mode=mode)
        except environment.GpuUnavailableError as error:
            _write_failure(output_dir, job, env_report, collector, str(error))
            print(f"FAILED (Gate A): {error}", file=sys.stderr)
            return 2

    # ---- input resolution / Gate E ---------------------------------------
    source_spec = job.get("source") or {}
    source_type = str(source_spec.get("type") or "local_file")
    warnings: List[str] = []

    if source_type == "youtube":
        with collector.timer("gate_e_download") as download_phase:
            try:
                result = downloader.download(
                    str(source_spec.get("url")),
                    output_dir / "inputs",
                    max_bytes=int(source_spec.get("max_bytes") or downloader.MAX_DOWNLOAD_BYTES),
                    max_duration_seconds=int(source_spec.get("max_duration_seconds") or downloader.MAX_DURATION_SECONDS),
                )
            except downloader.DownloadError as error:
                _write_failure(output_dir, job, env_report, collector, str(error))
                print(f"FAILED (Gate E): {error}", file=sys.stderr)
                return 3
            source_path = Path(result.path)
            download_phase.details = result.as_dict()
    else:
        try:
            source_path = resolve_local_input(job)
        except SystemExit as error:
            _write_failure(output_dir, job, env_report, collector, str(error))
            print(f"FAILED (input): {error}", file=sys.stderr)
            return 5

    source_checks = validation.run_source_checks(source_path)
    if not source_checks.ok:
        message = "; ".join(f"{check.name}: {check.detail}" for check in source_checks.failures)
        _write_failure(output_dir, job, env_report, collector, f"source validation failed — {message}")
        print(f"FAILED (source): {message}", file=sys.stderr)
        return 4

    source_probe = validation.stream_summary(validation.probe_media(source_path))
    source_duration = float(source_probe.get("container_duration") or 0.0)
    source_sha = manifest.sha256_file(source_path)

    expected_sha = (source_spec.get("sha256") or "").lower()
    if expected_sha and expected_sha != source_sha:
        _write_failure(output_dir, job, env_report, collector, "source checksum does not match the manifest")
        print("FAILED: source checksum mismatch", file=sys.stderr)
        return 5

    # ---- Gate B -----------------------------------------------------------
    transcription_cfg = job.get("transcription") or {}
    transcript_payload: Dict[str, Any] = {}
    transcript_path: Optional[Path] = None
    transcription_meta: Dict[str, Any] = {}
    cache: Dict[str, Any] = {}

    if transcription_cfg.get("enabled", True):
        with collector.timer("gate_b_transcription") as transcription_phase:
            try:
                result = transcription.transcribe(
                    source_path,
                    model_size=str(transcription_cfg.get("model") or "base"),
                    device=str(transcription_cfg.get("device") or "auto"),
                    language=transcription_cfg.get("language"),
                    download_root=transcription_cfg.get("download_root"),
                    allow_download=bool(transcription_cfg.get("allow_download", True)),
                    word_timestamps=bool(transcription_cfg.get("word_timestamps", True)),
                )
            except transcription.TranscriptionError as error:
                _write_failure(output_dir, job, env_report, collector, f"transcription failed: {error}")
                print(f"FAILED (Gate B): {error}", file=sys.stderr)
                return 6
            cache = result.transcript
            transcript_path = transcription.write_transcript(output_dir / "transcript.json", cache)
            transcription_meta = result.as_dict()
            warnings.extend(result.warnings)
            transcription_phase.details = transcription_meta
        collector.sample_gpu_memory()

    # ---- Gate C -----------------------------------------------------------
    ai_cfg = job.get("ai") or {}
    ai_payload: Dict[str, Any] = {}
    ai_meta: Dict[str, Any] = {}

    if ai_cfg.get("enabled", True) and cache:
        with collector.timer("gate_c_ai_selection") as ai_phase:
            try:
                backend, backend_notes = ai_inference.build_backend(
                    backend=str(ai_cfg.get("backend") or "auto"),
                    model=str(ai_cfg.get("model") or ai_inference.DEFAULT_MODEL),
                    base_url=str(ai_cfg.get("base_url") or "http://127.0.0.1:11434"),
                    transformers_model=str(ai_cfg.get("transformers_model") or ai_inference.DEFAULT_TRANSFORMERS_MODEL),
                    device=str(ai_cfg.get("device") or ("cuda" if env_report.cuda_available else "cpu")),
                    num_ctx=int(ai_cfg.get("num_ctx") or 8192),
                    temperature=ai_cfg.get("temperature"),
                )
                ai_payload = ai_inference.run_analysis(
                    cache,
                    backend=backend,
                    source_duration_seconds=source_duration,
                    max_segments=ai_cfg.get("max_segments"),
                )
                ai_payload["backend_notes"] = backend_notes
            except ai_inference.AnalysisError as error:
                run_notes.append(f"AI selection failed: {error}")
                warnings.append(f"AI selection failed: {error}")
                ai_phase.details = {"error": str(error)}
        collector.sample_gpu_memory()

        if ai_payload:
            segments = ai_payload.get("segments") or []
            if not segments:
                warnings.append("the model returned no usable segments; nothing will be rendered")
            manifest.dump_json(output_dir / "ai-selection.json", ai_payload)
            ai_meta = {
                "backend": ai_payload.get("backend"),
                "model": ai_payload.get("model"),
                "attempts": ai_payload.get("attempts") or [],
                "segments": len(segments),
                "rejected_segments": len(ai_payload.get("rejected_segments") or []),
                "prompt_chars": ai_payload.get("prompt_chars"),
                "transcript_lines": ai_payload.get("transcript_lines"),
            }

    # ---- Gate D/G: rendering ---------------------------------------------
    render_cfg = job.get("rendering") or {}
    target = render_cfg.get("target") or {}
    preset = rendering.RenderPreset(
        width=int(target.get("width") or 1080),
        height=int(target.get("height") or 1920),
        fps=int(target.get("fps") or 30),
        crf=int(render_cfg.get("crf") or rendering.PRODUCTION_CRF),
        preset=str(render_cfg.get("preset") or rendering.PRODUCTION_PRESET),
        codec=str(render_cfg.get("codec") or "libx264"),
        mode=str(render_cfg.get("mode") or "pad"),
    )
    captions_enabled = bool(render_cfg.get("captions"))
    max_clips = int((job.get("outputs") or {}).get("max_clips") or 3)
    selected = sorted(
        (ai_payload.get("segments") or []),
        key=lambda segment: segment.get("virality", {}).get("total_score", 0),
        reverse=True,
    )[:max_clips]

    render_results: List[Dict[str, Any]] = []
    clip_reports: List[validation.ValidationReport] = []
    clip_paths: List[Path] = []

    if selected:
        benches_dir.mkdir(exist_ok=True)
        for index, segment in enumerate(selected, start=1):
            start_seconds = int(segment["start_seconds"])
            end_seconds = int(segment["end_seconds"])
            clip_path = clips_dir / f"clip-{index:02d}-{start_seconds:04d}-{end_seconds:04d}.mp4"

            captions_path = None
            if captions_enabled and cache:
                captions_path = write_ass_captions(
                    cache,
                    output_dir / "captions" / f"clip-{index:02d}.ass",
                    start_ms=start_seconds * 1000,
                    end_ms=end_seconds * 1000,
                    width=preset.width,
                    height=preset.height,
                )

            with collector.timer(f"render_clip_{index}") as render_phase:
                try:
                    result = rendering.render_clip(
                        source_path, start_seconds, end_seconds, clip_path, preset,
                        captions_path=captions_path,
                    )
                except rendering.RenderingError as error:
                    run_notes.append(f"clip {index} failed to render: {error}")
                    render_phase.details = {"error": str(error)}
                    continue
                render_phase.details = result.as_dict()
                render_results.append(result.as_dict())
                clip_paths.append(clip_path)
                warnings.extend(result.warnings)

            report = validation.run_output_checks(
                clip_path,
                expected_duration=float(end_seconds - start_seconds),
                expected_size=(preset.width, preset.height),
                expected_fps=float(preset.fps),
                expect_audio=True,
            )
            clip_reports.append(report)
            if captions_path is not None:
                report.add(
                    validation.Check(
                        "captions_rendered",
                        captions_path.is_file(),
                        f"burned in from {captions_path.name}",
                    )
                )

            # One encoder comparison per run: same source, same range, other codec.
            if index == 1 and render_cfg.get("benchmark_nvenc", True):
                other = "h264_nvenc" if preset.codec == "libx264" else "libx264"
                bench_preset = rendering.RenderPreset(
                    width=preset.width, height=preset.height, fps=preset.fps,
                    crf=preset.crf, preset=preset.preset, codec=other, mode=preset.mode,
                )
                bench_path = benches_dir / f"bench-{other}-{index:02d}.mp4"
                with collector.timer(f"render_benchmark_{other}") as bench_phase:
                    try:
                        bench_result = rendering.render_clip(
                            source_path, start_seconds, end_seconds, bench_path, bench_preset
                        )
                        bench_phase.details = bench_result.as_dict()
                        render_results.append(bench_result.as_dict())
                    except rendering.RenderingError as error:
                        bench_phase.details = {"error": str(error)}
                        warnings.append(f"benchmark encode with {other} failed: {error}")
                if not args.keep_benchmark_clips:
                    bench_path.unlink(missing_ok=True)

    # ---- manifests + validation ------------------------------------------
    validation_summary = validation.summarise(clip_reports) if clip_reports else {"ok": False, "checks": [], "failed": [], "warnings": [], "checked": 0}
    if not clip_reports:
        validation_summary.setdefault("notes", []).append("no clips were rendered, so no output validation ran")

    total_output_bytes = sum(item.size_bytes for item in output_files)
    if total_output_bytes > MAX_SAVED_OUTPUT_BYTES:
        warnings.append(
            f"outputs total {total_output_bytes} bytes, above Kaggle's ~20 GB autosave limit; "
            "delivery will be truncated"
        )

    benchmark = reporting.build_benchmark(
        environment=env_report.as_dict(),
        job=job,
        collector=collector,
        transcription=transcription_meta,
        ai=ai_meta,
        renders=render_results,
        source={
            "filename": source_path.name,
            "size_bytes": source_path.stat().st_size,
            "sha256": source_sha,
            "duration_seconds": round(source_duration, 3),
            "streams": source_probe,
        },
        baseline=job.get("baseline_local"),
    )
    reporting.write_reports(
        output_dir,
        benchmark,
        reporting.to_markdown(benchmark, validation=validation_summary),
        validation=validation_summary,
    )

    # Collect artefacts only after every report exists, so result.json covers them all.
    output_files: List[manifest.OutputFile] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "result.json":
            continue
        if benches_dir in path.parents and not args.keep_benchmark_clips:
            continue
        output_files.append(
            manifest.OutputFile.from_path(path, name=str(path.relative_to(output_dir)))
        )

    status = "completed" if (clip_paths and validation_summary.get("ok")) else "partial"
    if not clip_paths:
        status = "failed"

    result = manifest.build_result(
        job=job,
        status=status,
        outputs=output_files,
        transcript_path=str(transcript_path) if transcript_path else None,
        ai_selection={
            "backend": ai_payload.get("backend"),
            "model": ai_payload.get("model"),
            "segments": [
                {
                    "start_time": segment.get("start_time"),
                    "end_time": segment.get("end_time"),
                    "hook_title": segment.get("hook_title"),
                    "total_score": (segment.get("virality") or {}).get("total_score"),
                }
                for segment in selected
            ],
        },
        durations_seconds={phase.name: round(phase.seconds, 3) for phase in collector.phases},
        validation_report=validation_summary,
        error=None if status == "completed" else {"message": "one or more gates did not pass"},
        environment={"gpu": (env_report.payload.get("cuda", {}).get("devices") or [{}])[0].get("name"),
                     "cuda_available": env_report.cuda_available},
        notes=run_notes + warnings,
    )
    manifest.dump_json(output_dir / "result.json", result)

    self_check = manifest.verify_outputs(result)
    if not self_check.ok:
        print("SELF-CHECK FAILED:", *self_check.errors, sep="\n  ", file=sys.stderr)
        return 8

    print(reporting.to_markdown(benchmark, validation=validation_summary))
    print(f"Outputs written to {output_dir}")
    print(f"Status: {status}")
    for warning in warnings:
        print(f"warning: {warning}")
    return 0 if status == "completed" else 7


def _write_failure(
    output_dir: Path,
    job: Mapping[str, Any],
    env_report: Optional[environment.EnvironmentReport],
    collector: reporting.BenchmarkCollector,
    message: str,
) -> None:
    """Produce the reports a failed run still owes the operator."""
    result = manifest.build_result(
        job=job,
        status="failed",
        outputs=[],
        durations_seconds={phase.name: round(phase.seconds, 3) for phase in collector.phases},
        error={"message": message},
        environment=(env_report.as_dict() if env_report else None),
    )
    manifest.dump_json(output_dir / "result.json", result)
    try:
        (output_dir / "REPORT.md").write_text(
            "# ClipWright Kaggle bridge — run report\n\n"
            f"**Failed:** {message}\n\n"
            + (environment.to_markdown(env_report) if env_report else ""),
            encoding="utf-8",
        )
    except OSError:
        pass


def main() -> None:
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as error:  # pragma: no cover - last-resort diagnostics
        traceback.print_exc()
        print(f"UNHANDLED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
