"""Score how well candidate pictures match what each beat should show, with Google's SigLIP (Apache-2.0) on the
CPU, so the picker's language model only looks at the best few per beat.

It also says how likely each picture is a historical image (an engraving, painting, drawing, or black-and-white
photo) rather than a modern color photo, which catches pictures from the wrong period.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

MODEL = "google/siglip-base-patch16-224"
SIZE = 224
TEXT_TOKENS = 64
BATCH = 32
HISTORICAL = ("a recent color photo taken with a digital camera", "an old black and white photograph, engraving, or painting")
_lock = threading.Lock()
_loaded: dict = {}


def _load():
    with _lock:
        if not _loaded:
            import torch
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            from transformers import AutoModel

            torch.set_num_threads(max(1, os.cpu_count() or 1))
            _loaded["model"] = AutoModel.from_pretrained(MODEL).eval()
            # The model's own tokenizer.json (lowercasing, punctuation removal, a closing </s>), read directly
            # so SentencePiece isn't needed.
            tokenizer = Tokenizer.from_file(hf_hub_download(MODEL, "tokenizer.json"))
            tokenizer.enable_truncation(max_length=TEXT_TOKENS)
            tokenizer.enable_padding(length=TEXT_TOKENS, pad_id=1, pad_token="</s>")
            _loaded["tokenizer"] = tokenizer
    return _loaded["model"], _loaded["tokenizer"]


def _canonical(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def _pixels(images: list[bytes]):
    import numpy as np
    import torch
    from PIL import Image

    arrays = []
    for data in images:
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB").resize((SIZE, SIZE), Image.Resampling.BICUBIC)
        except Exception:
            image = Image.new("RGB", (SIZE, SIZE), "gray")
        arrays.append((np.asarray(image, dtype=np.float32) / 255.0 - 0.5) / 0.5)
    return torch.from_numpy(np.stack(arrays)).permute(0, 3, 1, 2).contiguous()


def _features(output):
    # Newer transformers return a model output rather than the pooled tensor itself.
    return output if hasattr(output, "norm") else getattr(output, "pooler_output", output[0])


def score(texts: list[str], images: list[bytes]) -> dict:
    """{"match": [[p for each image] for each text], "historical": [p for each image]}; p runs 0 to 1."""
    import torch

    model, tokenizer = _load()
    with torch.inference_mode():
        ids = torch.tensor([e.ids for e in tokenizer.encode_batch([_canonical(t) for t in [*texts, *HISTORICAL]])])
        text = _features(model.get_text_features(input_ids=ids))
        text = text / text.norm(dim=-1, keepdim=True)
        chunks = []
        for start in range(0, len(images), BATCH):
            image = _features(model.get_image_features(pixel_values=_pixels(images[start:start + BATCH])))
            chunks.append(image / image.norm(dim=-1, keepdim=True))
        pictures = torch.cat(chunks) if chunks else torch.zeros((0, text.shape[1]))
        logits = text @ pictures.T * model.logit_scale.exp() + model.logit_bias
    match = torch.sigmoid(logits[: len(texts)]).tolist()
    era = torch.softmax(logits[len(texts):], dim=0)[1].tolist() if len(images) else []
    return {"match": match, "historical": era}


def remote_or_here(texts: list[str], images: list[bytes]) -> dict | None:
    """Scores from the cloud's ranker when this machine can reach it, else from this machine; None if neither
    works (the picker then keeps the search order)."""
    if not images:
        return {"match": [[] for _ in texts], "historical": []}
    if (os.environ.get("MODAL_TOKEN_ID") or os.environ.get("YTC_ON_MODAL")) and os.environ.get("YTC_RANK_HERE") != "1":
        try:
            import modal

            from .cloud import APP_NAME

            return modal.Function.from_name(APP_NAME, "rank_pictures").remote(texts, images)
        except Exception as err:
            if os.environ.get("YTC_ON_MODAL"):
                # A studio worker is sized for waiting on Gemini, not for running a vision model.
                log.warning("the cloud ranker failed (%s); keeping the search order", err)
                return None
            log.warning("the cloud ranker failed (%s); ranking on this machine", err)
    try:
        return score(texts, images)
    except Exception as err:
        log.warning("ranking pictures failed (%s); keeping the search order", err)
        return None
