"""Assemble a vertical Short: per-beat clips, burned-in captions, narration, music, and sound effects."""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from . import bed, captions, depth, drone, ff, plates, sfx, tts, visuals
from .spec import Beat, ShortSpec, Visual

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
MAX_SHORT_SECONDS = 180
MAX_KBPS = 12_000
# Buffer fetches the video when the post goes out: it got no answer fetching a 63 MB Short, and 46 MB ones went up.
MAX_UPLOAD_MB = 40
PIPELINE_ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = PIPELINE_ROOT / "assets" / "fonts"
SFX_CACHE = PIPELINE_ROOT / ".cache" / "sfx"
BT709 = ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
TO_BT709 = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
INTERMEDIATE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(FPS), *BT709]
# AAC encoding overshoots the PCM true peak by up to about 1 dB, and the check allows -1 dBFS in the MP4.
LOUDNORM = "loudnorm=I=-14:TP=-2:LRA=11"
SFX_PLACEMENT = {"whoosh": (-0.27, -10.0), "pop": (0.0, -9.0), "riser": (-1.2, -13.0)}
# One look over pictures from every archive (plates.grade warms and mutes them first): a little contrast, a
# soft vignette, and fine moving grain, all in YUV so nothing converts colors twice. Applied before the
# captions, so they stay crisp.
GRADE = "eq=contrast=1.05,vignette=angle=PI/5,noise=c0s=2:c0f=t+u"
# The bold look (ShortSpec.look) over pictures plates.vivid has already brightened: a lighter vignette.
BOLD_GRADE = "eq=contrast=1.04:saturation=1.05,vignette=angle=PI/7,noise=c0s=2:c0f=t+u"
# The camera moves inside a plate 1.12 times the frame (plates.OVERSCAN): zoom 1 shows the whole plate, 1.12 a
# frame-sized part of it. (start zoom, end zoom, start x, end x, start y, end y); x and y run 0 to 1 across the
# room the zoom leaves.
MOVES = {
    "zoom_in": (1.0, 1.08, 0.5, 0.5, 0.5, 0.5), "zoom_out": (1.08, 1.0, 0.5, 0.5, 0.5, 0.5),
    "pan_left": (1.1, 1.1, 0.85, 0.15, 0.5, 0.5), "pan_right": (1.1, 1.1, 0.15, 0.85, 0.5, 0.5),
    "pan_up": (1.1, 1.1, 0.5, 0.5, 0.85, 0.15), "pan_down": (1.1, 1.1, 0.5, 0.5, 0.15, 0.85),
    "none": (1.04, 1.04, 0.5, 0.5, 0.5, 0.5),
}
# A beat at least this long, whose picture has a box around what the line names, cuts to a close-up of it.
DETAIL_SECONDS = 2.8
# A beat at least this long whose wide picture is shown as a card cuts to a closer framing of it.
REFRAME_SECONDS = 4.2


def _cover(width: int, height: int, focus_x: float = 0.5, focus_y: float = 0.5) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-ow)*{focus_x}:(ih-oh)*{focus_y},setsar=1"
    )


def _render_clip(asset: visuals.Asset, visual: Visual, frames: int, out: Path, rng: random.Random) -> None:
    """A stock video beat, cropped to the frame, with a slow zoom if the beat asks for one."""
    seconds = frames / FPS
    motion = visual.motion
    source_seconds = ff.duration(asset.path)
    loop = source_seconds < seconds + 0.2
    start = 0.0 if loop else rng.uniform(0.0, max(0.0, source_seconds - seconds - 0.2))
    vf = f"fps={FPS},{_cover(W, H, visual.focus_x, visual.focus_y)}"
    if motion in ("zoom_in", "zoom_out"):
        rate = 0.10 / max(frames, 1)
        zoom = f"1+{rate:.6f}*in" if motion == "zoom_in" else f"1.10-{rate:.6f}*in"
        vf += f",zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}"
    seek = ["-stream_loop", "-1"] if loop else ["-ss", f"{start:.3f}"]
    ff.run(
        *seek, "-i", asset.path, "-t", f"{seconds:.3f}", "-vf", f"{vf},{TO_BT709}",
        "-frames:v", str(frames), *INTERMEDIATE, "-an", out,
    )


def _camera(frames: int, z0: float, z1: float, x0: float, x1: float, y0: float, y1: float) -> str:
    """An eased, sub-pixel move over the plate: `perspective` samples between pixels, where `zoompan` steps a
    whole pixel at a time and stutters on slow moves."""
    p = f"(on/{max(frames - 1, 1)})"
    e = f"({p}*{p}*(3-2*{p}))"
    z = f"({z0}+({z1 - z0:.4f})*{e})"
    bw, bh = f"(W/{z})", f"(H/{z})"
    left = f"((W-{bw})*({x0}+({x1 - x0:.4f})*{e}))"
    top = f"((H-{bh})*({y0}+({y1 - y0:.4f})*{e}))"
    right, bottom = f"({left}+{bw})", f"({top}+{bh})"
    return (f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}'"
            f":x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}':interpolation=cubic:eval=frame")


def _still_clip(plate, frames: int, move: tuple, out: Path) -> None:
    still = out.with_suffix(".png")
    plate.save(still, compress_level=1)
    vf = f"loop=loop={frames - 1}:size=1:start=0,{_camera(frames, *move)},scale={W}:{H}:flags=lanczos,{TO_BT709}"
    ff.run("-framerate", str(FPS), "-i", still, "-vf", vf, "-frames:v", str(frames), *INTERMEDIATE, "-an", out)
    still.unlink(missing_ok=True)


def _cut_frame(beat: Beat, words: list, start: int, end: int) -> int:
    """Where a beat's second shot starts: on the first emphasized word in its middle stretch, else halfway."""
    wanted = {w for phrase in beat.emphasis for w in phrase.lower().split()}
    lo, hi = start + 0.35 * (end - start), start + 0.7 * (end - start)
    for word in words:
        at = round(word.start * FPS)
        if lo <= at <= hi and word.text.strip(".,;:!?\"“”\u2026\u0964\u0965").lower() in wanted:
            return at
    return round((start + end) / 2)


def _plan(index: int, beat: Beat, asset: visuals.Asset, start: int, end: int, words: list, first_plate: dict | None,
          bold: bool = False) -> list[dict]:
    """The shots a still beat is cut into: a plate, a camera move, and a length in frames each."""
    visual = beat.visual
    seconds = (end - start) / FPS
    if asset.kind == "card" and visual.card:
        plate = plates.designed(visual.card, seed=f"{index}:{visual.card.big}")
        return [{"kind": "designed", "plate": plate, "move": MOVES["zoom_in"], "frames": end - start}]
    if visual.reuse and first_plate:
        # The loop line returns to the hook's first framing, ending where the Short begins.
        z0, _, x0, _, y0, _ = first_plate["move"]
        return [{"kind": first_plate["kind"], "plate": first_plate["plate"], "frames": end - start,
                 "move": (min(z0 + 0.07, plates.OVERSCAN), z0, x0, x0, y0, y0)}]
    image = plates.open_image(asset.path, bold=bold)
    if visual.crop:
        image = plates.region(image, visual.crop)
    focus = (visual.focus_x, visual.focus_y)
    kind = plates.layout(image)
    wide = plates.fill(image, focus, visual.box) if bold and kind == "card" else None
    if wide is not None:
        kind = "cover"
    else:
        wide = plates.cover(image, focus) if kind == "cover" else plates.card(image, focus, plates.tilt_for(index))
    wide_move = MOVES[visual.motion if kind == "cover" else ("zoom_in" if index % 2 == 0 else "zoom_out")]
    close = None
    if visual.box and seconds >= DETAIL_SECONDS:
        close = ("detail", plates.detail(image, visual.box))
    elif kind == "card" and seconds >= REFRAME_SECONDS and image.height >= plates.DETAIL_MIN_HEIGHT * 1.6:
        close = ("detail", plates.detail(image, [max(0.0, visual.focus_x - 0.2), 0.0, min(1.0, visual.focus_x + 0.2), 1.0]))
    shots = [{"kind": kind, "plate": wide, "move": wide_move, "frames": end - start}]
    if close and close[1] is not None:
        cut = _cut_frame(beat, words, start, end)
        shots[0]["frames"] = cut - start
        shots.append({"kind": close[0], "plate": close[1], "frames": end - cut,
                      "move": (1.02, 1.09, 0.5, 0.5, 0.45, 0.5) if index % 2 else (1.09, 1.02, 0.5, 0.5, 0.5, 0.45)})
    return shots


def _concat(clips: list[Path], out: Path) -> Path:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    ff.run("-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", out)
    return out


def _sfx_events(spec: ShortSpec, narration: tts.Narration) -> list[tuple[float, Path, float]]:
    events = []
    for beat, (start, _) in zip(spec.beats, narration.beat_spans):
        if beat.sfx == "none":
            continue
        offset, gain = SFX_PLACEMENT[beat.sfx]
        events.append((max(start + offset, 0.0), sfx.ensure(beat.sfx, SFX_CACHE), gain))
    return events


def _mix(
    spec: ShortSpec,
    narration: tts.Narration,
    events: list[tuple[float, Path, float]],
    total: float,
    work: Path,
    base: Path,
) -> Path:
    prep = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    inputs: list[str | Path] = ["-i", narration.wav_path]
    graph = [f"[0:a]{prep},apad=whole_dur={total:.3f}[nar]"]
    labels: list[str] = []
    index = 1
    if spec.music.path:
        if spec.music.path == "tanpura":
            music = drone.ensure(SFX_CACHE)
        elif spec.music.path.split(":")[0] == "bed":
            music = bed.compose(spec.id, total, work / "bed.wav", spec.music.path.partition(":")[2] or "mystery")
        else:
            music = (base / spec.music.path).resolve()
        inputs += ["-stream_loop", "-1", "-i", music]
        graph.append("[nar]asplit=2[voice][key]")
        fade = min(spec.music.fade_out, total)
        fade_out = f",afade=t=out:st={total - fade:.3f}:d={fade:.3f}" if fade else ""
        graph.append(f"[{index}:a]{prep},atrim=0:{total:.3f},volume={spec.music.gain_db}dB{fade_out}[bed]")
        graph.append("[bed][key]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=250[duck]")
        labels += ["[voice]", "[duck]"]
        index += 1
    else:
        labels.append("[nar]")
    for at, path, gain in events:
        inputs += ["-i", path]
        delay = int(at * 1000)
        graph.append(f"[{index}:a]{prep},adelay=delays={delay}:all=1,volume={gain}dB[fx{index}]")
        labels.append(f"[fx{index}]")
        index += 1
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=first[out]")
    premix = work / "premix.wav"
    ff.run(*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-t", f"{total:.3f}", "-c:a", "pcm_f32le", premix)

    stats = ff.loudnorm_stats(premix, LOUDNORM)
    normalize = (
        f"{LOUDNORM}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}:linear=true"
    )
    out = work / "mix.wav"
    ff.run("-i", premix, "-af", f"{normalize},aresample=48000", "-c:a", "pcm_s16le", out)
    return out


def _stand_ins(spec: ShortSpec, assets: list) -> list:
    """A beat whose every search came up empty shows the nearest beat's picture (in its own motion) instead of a
    blank card; beats that ask for a color card keep it."""
    blank = [a.credit.get("source") == "generated gradient" for a in assets]
    pictures = [i for i, is_blank in enumerate(blank) if not is_blank]
    shown = list(assets)
    for i, beat in enumerate(spec.beats):
        if blank[i] and pictures and not beat.visual.reuse and beat.visual.source != "color":
            nearest = min(pictures, key=lambda j: (abs(j - i), j > i))
            log.warning("%s beat %d found no picture; it shows beat %d's", spec.id, i + 1, nearest + 1)
            shown[i] = assets[nearest]
    for i, beat in enumerate(spec.beats):
        if beat.visual.reuse:
            shown[i] = shown[beat.visual.reuse - 1]
    return shown


def make_short(spec_path: Path, out: Path | None = None) -> Path:
    spec_path = spec_path.resolve()
    spec = ShortSpec.load(spec_path)
    base = spec_path.parent
    work = base / "work"
    assets_dir = work / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    narration = tts.synthesize(spec, work)
    total = narration.duration + spec.tail
    if total > MAX_SHORT_SECONDS:
        raise ValueError(f"{spec.id} runs {total:.1f}s; a Short can be at most {MAX_SHORT_SECONDS}s")

    used = visuals.cached_keys(spec.beats, assets_dir)
    boundaries = [0, *(round(end * FPS) for _, end in narration.beat_spans[:-1]), round(total * FPS)]
    assets, rngs, clips = [], [], []
    for i, beat in enumerate(spec.beats):
        # One generator per beat, so editing one beat never changes what another beat gets.
        rngs.append(rng := random.Random(f"{spec.id}:{i}"))
        if beat.visual.reuse:
            assets.append(assets[beat.visual.reuse - 1])
        else:
            assets.append(visuals.fetch(beat, i, assets_dir, base, used, rng, (boundaries[i + 1] - boundaries[i]) / FPS))
    assets = _stand_ins(spec, assets)
    first_shots: dict[int, dict] = {}
    beat_shots: list[list[dict]] = []
    for i, beat in enumerate(spec.beats):
        start, end = boundaries[i], boundaries[i + 1]
        if assets[i].kind == "video":
            clip = work / f"clip{i:02d}.mp4"
            _render_clip(assets[i], beat.visual, end - start, clip, rngs[i])
            clips.append(clip)
            beat_shots.append([{"start": round(start / FPS, 3), "end": round(end / FPS, 3), "kind": "video"}])
            continue
        words = [w for w in narration.words if start <= round(w.start * FPS) < end]
        reused = first_shots.get(beat.visual.reuse - 1) if beat.visual.reuse else None
        shots = _plan(i, beat, assets[i], start, end, words, reused, bold=spec.look == "bold")
        first_shots[i] = shots[0]
        at, made = start, []
        for k, shot in enumerate(shots):
            clip = work / f"clip{i:02d}_{k}.mp4"
            deep = spec.depth_motion and shot["kind"] in ("cover", "detail")
            if deep:
                depth.still_clip(shot["plate"], shot["frames"], shot["move"], clip, (W, H), FPS, TO_BT709,
                                 INTERMEDIATE, drift_sign=1 if (i + k) % 2 else -1)
            else:
                _still_clip(shot["plate"], shot["frames"], shot["move"], clip)
            clips.append(clip)
            made.append({"start": round(at / FPS, 3), "end": round((at + shot["frames"]) / FPS, 3), "kind": shot["kind"],
                         "motion": "depth" if deep else "camera"})
            at += shot["frames"]
        beat_shots.append(made)
    first_shots.clear()

    video = _concat(clips, work / "video.mp4")
    audio = _mix(spec, narration, _sfx_events(spec, narration), total, work, base)
    emphasis = {i: {w for phrase in beat.emphasis for w in phrase.lower().split()} for i, beat in enumerate(spec.beats)}
    ass = work / "captions.ass"
    hook = (spec.hook_text, narration.beat_spans[0][1]) if spec.hook_text else None
    beat_y = {i: beat.caption_y for i, beat in enumerate(spec.beats) if beat.caption_y is not None}
    captions.write_ass(narration.words, spec.captions, ass, total, emphasis, FONTS_DIR, hook=hook, beat_y=beat_y)
    captions.write_srt(narration.words, work / "captions.srt")

    out = (out or base / f"{spec.id}.mp4").resolve()
    kbps = min(MAX_KBPS, int(MAX_UPLOAD_MB * 8000 * 0.95 / total) - 192)
    ff.run(
        "-i", video, "-i", audio,
        "-vf", f"{BOLD_GRADE if spec.look == 'bold' else GRADE},ass=filename={ff.filter_path(ass)}:fontsdir={ff.filter_path(FONTS_DIR)}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k",
        "-pix_fmt", "yuv420p", "-r", str(FPS), *BT709,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-shortest", out,
    )
    manifest = {
        "id": spec.id,
        "duration": round(total, 3),
        "beats": [
            {"start": round(s, 3), "end": round(e, 3), "text": beat.text, "asset": asset.credit, "shots": shots}
            for beat, (s, e), asset, shots in zip(spec.beats, narration.beat_spans, assets, beat_shots)
        ],
        "words": [{"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in narration.words],
    }
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for leftover in (*clips, video, video.with_suffix(".txt"), work / "premix.wav", audio):
        leftover.unlink(missing_ok=True)
    return out
