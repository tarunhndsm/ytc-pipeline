"""Cursor's agent models, through the Cursor SDK, for the prompts no Gemini model can answer (see llm.generate).

Each prompt goes to a fresh agent with no tools, so it can only answer in text: web pages quoted in a research
prompt can't make it run commands, read files, or see the environment's keys. The key's team decides which
models it may use, and can block a listed one; the first model that answers is kept for the rest of the process.
"""

from __future__ import annotations

import atexit
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Model families in order of preference, newest first within each; other models come next, and "default"
# (Cursor's own pick) last. YTC_CURSOR_MODEL pins an exact model id instead.
FAMILIES = ("opus", "sonnet", "gpt", "gemini", "grok")
# grok-4.7 has taken up to 296 seconds for a script or an image pick, and some scripts ran past 300.
ANSWER_SECONDS = 480
# Large payloads stall the SDK's upload (a 5 MB photo, or 24 pictures at 1024 px, never arrived; 800 px went
# through in seconds), so a prompt's pictures are shrunk until together they fit the budget.
MAX_IMAGE_SIDE = 1024
IMAGE_SIDES = (1024, 768, 512, 384)
IMAGE_BYTES = 2_500_000
# After this many failures in a row, the rest of the process uses Gemini only.
FAILURES_BEFORE_GIVING_UP = 3
PREAMBLE = "You have no tools; everything you need is below. Answer directly in your reply.\n\n"

_lock = threading.Lock()
# One bridge serves every lane: the SDK reaches it over a thread-safe HTTP client.
_bridge_lock = threading.Lock()
_client = None
_catalog: dict[str, list] | None = None
_chosen: str | None = None
_blocked: set[str] = set()
# Models the team allows only at their default settings (asking for a thinking effort gets "Model Blocked").
_plain: set[str] = set()
_failures = 0


class _Unusable(Exception):
    """This model can't be used with the key (blocked by the team, or not offered)."""


def _key() -> str:
    # Not CURSOR_API_KEY: on the owner's Mac that name holds a different key.
    return os.environ.get("YTC_CURSOR_API_KEY", "")


def available() -> bool:
    return bool(_key()) and _failures < FAILURES_BEFORE_GIVING_UP


def _bridge():
    global _client
    with _bridge_lock:
        if _client is None:
            from cursor_sdk import Client

            # The SDK authenticates calls that carry no key (cancelling a run) with CURSOR_API_KEY, which the
            # bridge reads from this process's environment; on the owner's Mac that name holds another key.
            os.environ["CURSOR_API_KEY"] = _key()
            _client = Client.launch_bridge(workspace=tempfile.mkdtemp(prefix="ytc-cursor-"),
                                           allow_api_key_env_fallback=True)
            atexit.register(_client.close)
    return _client


def _version(model_id: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", model_id))


def _candidates() -> list[str]:
    global _catalog
    if pinned := os.environ.get("YTC_CURSOR_MODEL"):
        return [pinned]
    if _catalog is None:
        _catalog = {m.id: list(m.parameters) for m in _bridge().models.list(api_key=_key())}

    def rank(model_id: str) -> tuple:
        name = model_id.lower()
        if name in ("default", "auto"):
            return (len(FAMILIES) + 1,)
        family = next((i for i, f in enumerate(FAMILIES) if f in name), len(FAMILIES))
        return (family, tuple(-v for v in _version(model_id)), name)

    return sorted(_catalog, key=rank)


def _selection(model_id: str, thinking: str | None):
    from cursor_sdk import ModelParameterValue, ModelSelection

    params = []
    for param in [] if model_id in _plain else (_catalog or {}).get(model_id, []):
        allowed = {getattr(v, "value", v) for v in (getattr(param, "values", None) or [])}
        if thinking and param.id in ("reasoning_effort", "effort") and thinking in allowed:
            params.append(ModelParameterValue(id=param.id, value=thinking))
    return ModelSelection(id=model_id, params=params)


def _jpeg(image: Path | bytes, side: int = MAX_IMAGE_SIDE) -> bytes:
    from PIL import Image

    data = image.read_bytes() if isinstance(image, Path) else image
    with Image.open(io.BytesIO(data)) as picture:
        picture = picture.convert("RGB")
        picture.thumbnail((side, side))
        out = io.BytesIO()
        picture.save(out, "JPEG", quality=85)
    return out.getvalue()


def _jpegs(pictures: list) -> list[bytes]:
    """The pictures as JPEGs, at the largest size in IMAGE_SIDES that keeps them within IMAGE_BYTES together."""
    for side in IMAGE_SIDES:
        encoded = [_jpeg(p, side) for p in pictures]
        if sum(map(len, encoded)) <= IMAGE_BYTES:
            break
    return encoded


def _message(prompt: str | list, schema: dict | None):
    """Text with an [image N] marker where each image sat in the prompt, and the images in that order."""
    import base64

    from cursor_sdk import SDKImage, UserMessage

    items = [prompt] if isinstance(prompt, str) else prompt
    jpegs = iter(_jpegs([item for item in items if not isinstance(item, str)]))
    texts, images = [], []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
        else:
            images.append(SDKImage.data_image(base64.b64encode(next(jpegs)).decode(), "image/jpeg"))
            texts.append(f"[image {len(images)}]")
    text = PREAMBLE + "\n".join(texts)
    if schema:
        text += ("\n\nReply with only a JSON value that matches this JSON Schema, with no other text and no code "
                 "fences:\n" + json.dumps(schema))
    return UserMessage(text=text, images=images) if images else text


def _problems(schema: dict, value, path: str = "$") -> list[str]:
    """Where `value` breaks the parts of JSON Schema our prompts use (type, properties, required, items, enum)."""
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    wanted = schema.get("type")
    options = wanted if isinstance(wanted, list) else [wanted] if wanted else []
    if options and not any(isinstance(value, kinds[k]) and not (k in ("integer", "number") and isinstance(value, bool))
                           for k in options if k in kinds):
        return [f"{path} should be {' or '.join(options)}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} should be one of {json.dumps(schema['enum'])}"]
    problems = []
    if isinstance(value, dict):
        problems += [f"{path}.{key} is missing" for key in schema.get("required", []) if key not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                problems += _problems(sub, value[key], f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problems += _problems(schema["items"], item, f"{path}[{i}]")
    return problems


def _parse(text: str, schema: dict):
    """The JSON value in a reply, and what's wrong with it (empty when it fits the schema)."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not starts:
        return None, ["the reply has no JSON"]
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[min(starts):])
    except json.JSONDecodeError as err:
        return None, [f"the JSON doesn't parse: {err}"]
    return value, _problems(schema, value)


def _run(agent, message) -> str:
    """Send one message and wait for the answer, giving up after ANSWER_SECONDS. Sending counts too: an upload
    can stall, and a daemon thread does the waiting so a stalled call can't keep the process from exiting."""
    box: dict = {}

    def work() -> None:
        try:
            run = box["run"] = agent.send(message)
            error = ""
            for event in run.messages():
                if getattr(event, "type", "") == "status" and getattr(event, "status", "") == "ERROR":
                    error = getattr(event, "message", "") or error
            box["result"], box["error"] = run.wait(), error
        except Exception as err:  # raised again in the caller's thread
            box["exception"] = err

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(ANSWER_SECONDS)
    if worker.is_alive():
        if run := box.get("run"):
            try:
                run.cancel()
            except Exception as err:  # the run keeps going on Cursor's side, and nothing waits for it
                log.info("couldn't cancel Cursor run %s: %s", getattr(run, "id", "?"), str(err)[:160])
        raise RuntimeError(f"no answer in {ANSWER_SECONDS}s")
    if "exception" in box:
        raise box["exception"]
    result, error = box["result"], box["error"]
    if result.status != "finished":
        if "blocked" in error.lower() or "cannot use this model" in error.lower():
            raise _Unusable(error)
        raise RuntimeError(f"run {result.status}: {error or 'no detail'}")
    return result.result or ""


def _answer(model_id: str, message, schema: dict | None, thinking: str | None):
    selection = _selection(model_id, thinking)
    try:
        return _ask_agent(selection, message, schema)
    except _Unusable:
        if not selection.params:
            raise
        _plain.add(model_id)
        return _ask_agent(_selection(model_id, None), message, schema)


def _ask_agent(selection, message, schema: dict | None):
    from cursor_sdk import Agent, AgentOptions, BadRequestError, LocalAgentOptions

    options = AgentOptions(model=selection, tools=[], local=LocalAgentOptions(cwd=tempfile.mkdtemp(prefix="ytc-cursor-")))
    try:
        agent = Agent.create(options, client=_bridge(), api_key=_key())
        with agent:
            return _reply(agent, message, schema)
    except BadRequestError as err:
        if "cannot use this model" in str(err).lower():
            raise _Unusable(str(err)) from err
        raise


def _reply(agent, message, schema: dict | None):
    """The answer, with one chance to fix JSON that doesn't fit the schema."""
    text = _run(agent, message)
    if not schema:
        return text
    value, problems = _parse(text, schema)
    if problems:
        text = _run(agent, "That reply can't be used: " + "; ".join(problems[:5]) + ". Reply again with only the JSON.")
        value, problems = _parse(text, schema)
    if problems:
        raise RuntimeError("no usable JSON: " + "; ".join(problems[:3]))
    return value


def ask(prompt: str | list, *, schema: dict | None = None, thinking: str | None = None, purpose: str = ""):
    """(answer, model id, seconds), or None when Cursor can't answer this prompt."""
    global _chosen, _failures
    if not available():
        return None
    started = time.monotonic()
    try:
        with _lock:
            order = ([_chosen] if _chosen else []) + [m for m in _candidates() if m != _chosen and m not in _blocked]
        message = _message(prompt, schema)
        for model_id in order:
            try:
                answer = _answer(model_id, message, schema, thinking)
            except _Unusable as err:
                log.info("Cursor model %s can't be used with this key: %s", model_id, str(err)[:160])
                _blocked.add(model_id)
                if _chosen == model_id:
                    _chosen = None
                continue
            _chosen, _failures = model_id, 0
            seconds = time.monotonic() - started
            log.info("cursor/%s answered %s in %.0fs", model_id, purpose or "a prompt", seconds)
            return answer, model_id, seconds
        raise RuntimeError("no model this key may use answered")
    except Exception as err:  # the SDK raises its own error types for auth, network, and rate limits
        _failures += 1
        log.warning("Cursor couldn't answer %s (%s): %s", purpose or "a prompt", type(err).__name__, str(err)[:200])
        if not available():
            log.warning("Cursor failed %d times in a row; it sits out the rest of this run", _failures)
        return None
