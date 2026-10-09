"""Depth motion for a still picture: a depth model tells near from far, and as the camera moves, near parts
shift and grow more than far ones, so a painting moves like a scene rather than a flat print."""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from . import ff

# Apache-2.0; the Base and Large weights of the same model are for non-commercial use only.
MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
# Over a whole shot, how far the nearest part travels against the farthest, as a share of the frame: sideways,
# and in size on a zoom. More than this tears the edges of figures.
SHIFT = 0.035
DOLLY = 0.06
# A still zoom gets this much sideways drift too, so its depth shows.
DRIFT = 0.4
# The camera keeps this much room inside the plate, so shifted parts never sample past its edge.
MIN_ZOOM = 1.03


@cache
def _estimator():
    from transformers import pipeline

    return pipeline("depth-estimation", model=MODEL, device="cpu")


def nearness(image: Image.Image) -> np.ndarray:
    """0 for the farthest part of the picture to 1 for the nearest, at the picture's size, softened so that
    shifting it bends the edges of figures instead of tearing them."""
    depth = _estimator()(image.convert("RGB"))["depth"].convert("L").resize(image.size, Image.Resampling.BILINEAR)
    depth = depth.filter(ImageFilter.GaussianBlur(radius=max(2, image.width // 90)))
    values = np.asarray(depth, dtype=np.float32)
    lo, hi = np.percentile(values, [2, 98])
    return np.clip((values - lo) / max(hi - lo, 1.0), 0.0, 1.0).astype(np.float32)


def _flow(move: tuple, drift_sign: float) -> tuple[float, float, float]:
    """Which way near parts slide as the shot runs (against the camera's travel), and whether they grow."""
    z0, z1, x0, x1, y0, y1 = move
    fx, fy = -(x1 - x0), -(y1 - y0)
    length = (fx * fx + fy * fy) ** 0.5
    fx, fy = (fx / length, fy / length) if length > 1e-6 else (DRIFT * drift_sign, 0.0)
    return fx, fy, float(np.sign(z1 - z0))


def still_clip(plate: Image.Image, frames: int, move: tuple, out: Path, size: tuple[int, int], fps: int,
               vf: str, encode: list[str], drift_sign: float = 1.0) -> None:
    """Render the camera's eased move over the plate (as render._camera does) with depth, straight to video."""
    import torch
    import torch.nn.functional as F

    pw, ph = plate.size
    w, h = size
    near = torch.from_numpy(nearness(plate))[None, None]
    ref = float(near.median())
    picture = torch.from_numpy(np.asarray(plate.convert("RGB"), dtype=np.float32) / 255.0).permute(2, 0, 1)[None]
    fx, fy, dolly = _flow(move, drift_sign)
    z0, z1, x0, x1, y0, y1 = move
    z0, z1 = max(z0, MIN_ZOOM), max(z1, MIN_ZOOM)
    gy, gx = torch.meshgrid((torch.arange(h) + 0.5) / h, (torch.arange(w) + 0.5) / w, indexing="ij")

    def grid(x, y):
        return torch.stack((2 * x / pw - 1, 2 * y / ph - 1), dim=-1)[None]

    proc = subprocess.Popen(
        [ff.exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-vf", vf, "-frames:v", str(frames), *encode, "-an", str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with torch.inference_mode():
            for f in range(frames):
                p = f / max(frames - 1, 1)
                e = p * p * (3 - 2 * p)
                z = z0 + (z1 - z0) * e
                bw, bh = pw / z, ph / z
                left, top = (pw - bw) * (x0 + (x1 - x0) * e), (ph - bh) * (y0 + (y1 - y0) * e)
                x, y = left + gx * bw, top + gy * bh
                d = F.grid_sample(near, grid(x, y), mode="bilinear", padding_mode="border", align_corners=False)[0, 0] - ref
                t = e - 0.5
                scale = 1 + DOLLY * dolly * t * d
                cx, cy = left + bw / 2, top + bh / 2
                sx = cx + (x - cx) / scale - SHIFT * bw * fx * t * d
                sy = cy + (y - cy) / scale - SHIFT * bw * fy * t * d
                frame = F.grid_sample(picture, grid(sx, sy), mode="bicubic", padding_mode="border", align_corners=False)
                proc.stdin.write((frame[0].clamp(0, 1).permute(1, 2, 0) * 255 + 0.5).to(torch.uint8).numpy().tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.read().decode()[-3000:]}")
