"""The studio's language model: Gemini through Google AI Studio's free tier first, then free models from other
providers through their OpenAI-compatible APIs (PROVIDERS), and Cursor's agent models (cursor_llm.py, with the
channel's Cursor key) when none of those can answer. YTC_LLM_FIRST=cursor puts Cursor first.

Gemini answers in about 15 seconds and Cursor in 2 to 5 minutes, which is most of what a Short costs on the runner.

Each Gemini model has its own free quota on the channel's `days-of-odd` project (Flash models: 5 requests a minute
and 20 a day; Flash Lite: 15 a minute and 500 a day), so a run steps down a ladder as models run out. Everything
starts on Flash but the picture checks, which start on Flash Lite; once Flash is spent, the review waits for its
reset while plenty of Shorts are ready (studio.review), and the rest goes on with the models below it.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

from . import cursor_llm

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models"
# Each ladder's Gemini models end with Google's alias for its newest model, so it still answers after the pinned
# models retire. Gemma 4 (Apache-2.0) in the same project has its own free quota and reads images, but takes at
# most 16,000 input tokens a minute, so it only answers prompts that small: scripts, picture checks, reviews.
FLASH = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
         "gemini-flash-latest")
FLASH_LITE = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest")
# (The faster gemma-4-26b-a4b-it got simple facts wrong in testing.)
GEMMA = ("gemma-4-31b-it",)

# Free models from other providers, named "provider:model", through OpenAI-compatible chat APIs. A provider's
# models are skipped while its key isn't set (key None: it needs none). gap: seconds between one process's requests
# to a model; rest: how long a model that keeps failing or stays rate-limited is skipped.
PROVIDERS = {
    # OVHcloud AI Endpoints without an account: 2 requests a minute per model for each address and no daily cap,
    # but everyone on the same address shares those 2. From Modal on 2026-09-28 the busiest model (Qwen3.5-397B)
    # answered 1 try in 9 and the 27B models about every other try.
    "ovh": {"url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "key": None, "gap": 31.0, "rest": 180},
    # Mistral's free plan (training on the account's API calls is turned off): only the Ministral models answer,
    # 30 requests a minute each; its Small, Medium and Large models allow none on this plan (2026-09-28).
    "mistral": {"url": "https://api.mistral.ai/v1", "key": "YTC_MISTRAL_API_KEY", "gap": 2.5, "rest": 120},
    # OpenRouter's free models on the channel's account: 20 requests a minute and 50 a day between all of them
    # (buying 10 credits would make it 1,000). Its free Qwen3.8 27B was "rate-limited upstream" 4 tries in 4 and its
    # Gemma 4 31B 8 in 10, and Inkling answers only "agentic harnesses" (2026-09-28), so only Dots3 is listed.
    "openrouter": {"url": "https://openrouter.ai/api/v1", "key": "YTC_OPENROUTER_API_KEY", "gap": 3.5, "rest": 300},
}
# What each backup takes: input tokens (a little under its context), whether it reads pictures and how many at once,
# whether it's strong enough to write ahead of Gemma (the rest step in after it) and whether it may judge, whether
# it may write scripts at all, and anything its requests need. OVH's Qwen models reason until they run out of output
# tokens (16,384 on a script, 2026-09-28) unless told to keep it low. Ministral 14B broke the script rules (hook and
# word count) through both fix rounds in testing, so it only researches, picks and checks. Dots3 wrote ep036's
# script to the rules in one call and kept more of its facts than Flash Lite did (2026-09-28), but hasn't been
# tried as a judge.
BACKUPS = {
    "ovh:Qwen3.5-397B-A17B": {"context": 250_000, "vision": True, "strong": True, "extra": {"reasoning_effort": "low"}},
    "openrouter:dots-studio/dots-3-note-preview:free": {"context": 250_000, "vision": True, "strong": True, "judges": False,
                                                        "extra": {"reasoning": {"effort": "low"}}},
    "ovh:Qwen3.8-27B": {"context": 250_000, "vision": True, "extra": {"reasoning_effort": "low"}},
    "ovh:Qwen3.6-27B": {"context": 250_000, "vision": True, "extra": {"reasoning_effort": "low"}},
    "mistral:ministral-14b-2512": {"context": 250_000, "vision": True, "images": 8, "writes": False},
}
BACKUP_STRONG = tuple(m for m, spec in BACKUPS.items() if spec.get("strong"))
BACKUP_LIGHT = tuple(m for m, spec in BACKUPS.items() if not spec.get("strong"))
BACKUP_WRITERS = tuple(m for m in BACKUP_LIGHT if BACKUPS[m].get("writes", True))
# Backups trusted to judge a Short (studio.review) once every Flash model has run out.
JUDGES = tuple(m for m in BACKUP_STRONG if BACKUPS[m].get("judges", True))

# The ladders. STRONG writes (scripts, learnings), LIGHT checks (Flash Lite has 1,000 requests a day between its
# models), BROAD is for research, picture picks and topic ideas: the best model with quota left, then any. With Flash
# spent, Flash Lite wrote ep036's script to the rules at once and got its facts right; Gemma needed a fix round and
# turned "11,000 pounds" into "11,000 dollars" (2026-09-28).
STRONG = (*FLASH, *FLASH_LITE, *BACKUP_STRONG, *GEMMA, *BACKUP_WRITERS)
LIGHT = (*FLASH_LITE, *GEMMA, *BACKUP_STRONG, *BACKUP_LIGHT)
BROAD = (*FLASH, *FLASH_LITE, *BACKUP_STRONG, *GEMMA, *BACKUP_LIGHT)
# Input tokens a minute on the free tier, where it's too small for some prompts (the 429 names the real value).
INPUT_CAP = {"gemma-4-31b-it": 16000}
# Roughly what an image costs in input tokens: Gemini 3 reads a picture at up to 1,120, Gemma 4 at about 280.
IMAGE_TOKENS, GEMMA_IMAGE_TOKENS = 1120, 300
# Pictures sent to a backup are cut to this many pixels on their longer side (OVH takes 10 MB a request), and cost
# about this many tokens each; a backup may write this many tokens, its reasoning included.
BACKUP_IMAGE_SIDE, BACKUP_IMAGE_TOKENS, BACKUP_OUTPUT_TOKENS = 1280, 1300, 16384
# 5 requests a minute per Flash model, with a little slack.
MIN_GAP = 13.0
# A model that keeps failing (new models are often overloaded) is skipped for this long.
REST_SECONDS = 600
FAILURES_BEFORE_REST = 2
# When every model is overloaded at once, pause and retry the ladder for up to this long.
LADDER_PAUSE = 90
OVERLOAD_WAIT = 1500
# Whether Cursor answers when no Gemini model can (quota spent, or all overloaded). A Short started while plenty
# are ready runs without it and stops instead (OutOfQuota or Overloaded), to resume after Gemini's reset: 2 of
# the 17 Shorts Cursor made passed the review. A prompt Gemini refuses still goes to Cursor.
CURSOR_FALLBACK = True
# The YouTube studio's own accounts, which a run for the Instagram brand never uses (see use_brand_project).
STUDIO_KEYS = ("YTC_CURSOR_API_KEY", *(spec["key"] for spec in PROVIDERS.values() if spec["key"]))

_spent: set[str] = set()
_resting: dict[str, float] = {}
_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()
calls: list[dict] = []
_answered = threading.local()


class OutOfQuota(RuntimeError):
    """Every model in the ladder has used up today's free quota."""


class Overloaded(RuntimeError):
    """Every model with quota left stayed overloaded (5xx) for OVERLOAD_WAIT seconds."""


def _key() -> str:
    # Not GEMINI_API_KEY: on the owner's Mac that name holds an unrelated work key.
    if key := os.environ.get("YTC_GEMINI_API_KEY"):
        return key
    raise RuntimeError("YTC_GEMINI_API_KEY is not set (AI Studio key for the days-of-odd project)")


def _media_part(media: Path | bytes) -> dict:
    data = media.read_bytes() if isinstance(media, Path) else media
    if data[:4] == b"\x89PNG":
        mime = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        mime = "audio/wav"
    else:
        mime = "image/jpeg"
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}


def _parts(prompt: str | list) -> list[dict]:
    """Text and images in order: strings become text parts, paths and bytes become images."""
    items = [prompt] if isinstance(prompt, str) else prompt
    return [{"text": item} if isinstance(item, str) else _media_part(item) for item in items]


def _tokens(body: dict, model: str = "") -> int:
    """About how many input tokens a request is for this model: 3.5 characters a token, plus its cost per image."""
    parts = [p for c in body["contents"] for p in c["parts"]]
    image = GEMMA_IMAGE_TOKENS if model.startswith("gemma") else IMAGE_TOKENS
    return int(sum(len(p.get("text", "")) for p in parts) / 3.5) + image * sum("inline_data" in p for p in parts)


def _json(text: str):
    """The JSON object in a reply, allowing a code fence or trailing text around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise
        return json.JSONDecoder().raw_decode(text[min(starts):])[0]


def _pace(model: str, gap: float = MIN_GAP) -> None:
    # Shorts made side by side share each model's gap, so a caller books its slot before waiting for it.
    with _pace_lock:
        start = max(time.monotonic(), _last_call.get(model, 0) + gap)
        _last_call[model] = start
    time.sleep(max(0.0, start - time.monotonic()))


def _quota_kind(error: dict) -> tuple[str, float]:
    """('day' or 'minute', seconds to wait) from a 429 body."""
    kind, delay = "minute", 20.0
    for detail in error.get("details", []):
        for violation in detail.get("violations", []):
            if "PerDay" in violation.get("quotaId", ""):
                kind = "day"
        if retry := detail.get("retryDelay"):
            delay = float(retry.rstrip("s") or 20) + 1
    return kind, delay


def _day_limit(error: dict) -> str:
    """The daily quota a 429 names, as ' (20 a day: <quota id>)', or ''."""
    for detail in error.get("details", []):
        for violation in detail.get("violations", []):
            if "PerDay" in violation.get("quotaId", ""):
                return f" ({violation.get('quotaValue', '?')} a day: {violation['quotaId']})"
    return ""


def _input_cap(error: dict) -> int | None:
    """The input-tokens-a-minute limit a 429 names, if that's the limit it hit."""
    for detail in error.get("details", []):
        for violation in detail.get("violations", []):
            if "InputTokens" in violation.get("quotaId", "") and str(violation.get("quotaValue", "")).isdigit():
                return int(violation["quotaValue"])
    return None


def _complete(answer, schema: dict) -> bool:
    """The parsed answer has the top-level fields the schema requires (Gemma doesn't always keep to it)."""
    if schema.get("type") == "object":
        return isinstance(answer, dict) and all(key in answer for key in schema.get("required", []))
    if schema.get("type") == "array":
        return isinstance(answer, list)
    return True


def _configured(model: str) -> bool:
    """A Gemini model, or a backup whose provider's key is set (or needs none)."""
    if ":" not in model:
        return True
    spec = PROVIDERS.get(model.split(":", 1)[0])
    return bool(spec) and model in BACKUPS and (spec["key"] is None or bool(os.environ.get(spec["key"])))


def _fits(model: str, body: dict) -> bool:
    """The model can take the prompt: few enough input tokens, and it reads pictures if the prompt has any."""
    if ":" not in model:
        return _tokens(body, model) <= INPUT_CAP.get(model, float("inf"))
    parts = body["contents"][0]["parts"]
    images = sum("inline_data" in p for p in parts)
    if images and (not BACKUPS[model]["vision"] or images > BACKUPS[model].get("images", images)):
        return False
    return int(sum(len(p.get("text", "")) for p in parts) / 3.5) + BACKUP_IMAGE_TOKENS * images <= BACKUPS[model]["context"]


def generate(
    prompt: str | list,
    *,
    schema: dict | None = None,
    models: tuple[str, ...] = STRONG,
    temperature: float | None = None,
    thinking: str | None = None,
    purpose: str = "",
) -> str | dict | list:
    """Ask the first model in the ladder with quota left, then Cursor. With a JSON schema, returns the parsed object.

    When no Gemini model answers and Cursor can't either, waits while any model with quota left is overloaded and
    tries again for up to OVERLOAD_WAIT seconds before raising Overloaded (or OutOfQuota once all are spent).
    """
    cursor_first = os.environ.get("YTC_LLM_FIRST") == "cursor"
    if cursor_first and (answer := _cursor(prompt, schema, thinking, purpose)) is not _NO_ANSWER:
        return answer
    config: dict = {"maxOutputTokens": 32768}
    if temperature is not None:
        config["temperature"] = temperature
    if thinking:
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    if schema:
        config |= {"responseMimeType": "application/json", "responseJsonSchema": schema}
    body = {"contents": [{"role": "user", "parts": _parts(prompt)}], "generationConfig": config}
    models = tuple(m for m in dict.fromkeys(models) if _configured(m))
    models = tuple(m for m in models if _fits(m, body)) or tuple(m for m in models if ":" not in m)
    give_up = time.monotonic() + OVERLOAD_WAIT
    while True:
        for model in models:
            if model in _spent or _resting.get(model, 0) > time.monotonic():
                continue
            try:
                answer = _ask_backup(model, body, schema, purpose) if ":" in model else _ask(model, body, schema, purpose)
                if answer is not _NO_ANSWER:
                    _answered.model = model
            except RuntimeError as err:  # Gemini refused the prompt itself (a 4xx other than quota)
                if cursor_first or (answer := _cursor(prompt, schema, thinking, purpose)) is _NO_ANSWER:
                    raise
                log.warning("%s; Cursor answered instead", str(err)[:200])
            if answer is not _NO_ANSWER:
                return answer
        if not cursor_first and CURSOR_FALLBACK and (answer := _cursor(prompt, schema, thinking, purpose)) is not _NO_ANSWER:
            return answer
        if all(model in _spent for model in models):
            raise OutOfQuota(f"no model in {models} has free quota left today")
        if time.monotonic() > give_up:
            raise Overloaded(f"every model in {models} with quota left stayed overloaded for {OVERLOAD_WAIT // 60} minutes")
        log.warning("every model with quota left is overloaded; trying the ladder again in %ds", LADDER_PAUSE)
        time.sleep(LADDER_PAUSE)
        for model in models:
            _resting.pop(model, None)


def gemini_spent() -> bool:
    """Every Flash model (the review needs one while plenty of Shorts are ready) has run out of today's quota."""
    return all(model in _spent for model in FLASH)


def answered_by() -> str:
    """The model that answered this thread's last prompt."""
    return getattr(_answered, "model", "")


def use_brand_project(env=None):
    """Put an environment (this process's by default) on the brand's own Gemini project, without the studio's backup
    providers or Cursor: once the brand's ladder is spent, its run stops until the reset."""
    env = os.environ if env is None else env
    env["YTC_GEMINI_API_KEY"] = env["IG_GEMINI_API_KEY"]
    for key in STUDIO_KEYS:
        env.pop(key, None)
    return env


_NO_ANSWER = object()


def _cursor(prompt, schema, thinking, purpose):
    if not (found := cursor_llm.ask(prompt, schema=schema, thinking=thinking, purpose=purpose)):
        return _NO_ANSWER
    answer, model, seconds = found
    calls.append({"model": f"cursor/{model}", "purpose": purpose, "seconds": round(seconds, 1),
                  "input_tokens": None, "output_tokens": None, "thinking_tokens": None})
    _answered.model = f"cursor/{model}"
    return answer


def _ask(model: str, body: dict, schema: dict | None, purpose: str):
    if model.startswith("gemma"):  # Gemma takes no thinking level but "high" (400: not supported)
        config = {k: v for k, v in body["generationConfig"].items() if k != "thinkingConfig"}
        body = {**body, "generationConfig": config}
    failures = 0
    for _attempt in range(5):
        if failures >= FAILURES_BEFORE_REST:
            log.warning("%s keeps failing; using the next model for %d minutes", model, REST_SECONDS // 60)
            _resting[model] = time.monotonic() + REST_SECONDS
            return _NO_ANSWER
        _pace(model)
        started = time.monotonic()
        try:
            response = requests.post(
                f"{API}/{model}:generateContent", headers={"x-goog-api-key": _key()}, json=body, timeout=(20, 300)
            )
        except requests.RequestException as err:
            log.warning("%s: %s", model, err)
            failures += 1
            time.sleep(10)
            continue
        if response.status_code == 200:
            reply = response.json()
            candidate = (reply.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
            usage = reply.get("usageMetadata", {})
            calls.append(
                {
                    "model": model,
                    "purpose": purpose,
                    "seconds": round(time.monotonic() - started, 1),
                    "input_tokens": usage.get("promptTokenCount"),
                    "output_tokens": usage.get("candidatesTokenCount"),
                    "thinking_tokens": usage.get("thoughtsTokenCount"),
                }
            )
            if not text.strip():
                log.warning("%s returned no text (finish reason %s); retrying", model, candidate.get("finishReason"))
                failures += 1
                continue
            log.info("%s answered %s in %.0fs", model, purpose or "a prompt", time.monotonic() - started)
            if not schema:
                return text
            try:
                answer = _json(text)
            except json.JSONDecodeError:
                answer = None
            if answer is None or not _complete(answer, schema):
                log.warning("%s returned malformed JSON; retrying", model)
                failures += 1
                continue
            return answer
        try:
            error = response.json().get("error", {}) if response.content else {}
        except ValueError:
            error = {"message": response.text[:200]}
        if response.status_code == 429:
            kind, delay = _quota_kind(error)
            if kind == "day":
                log.info("%s is out of today's free quota%s", model, _day_limit(error))
                _spent.add(model)
                return _NO_ANSWER
            if (cap := _input_cap(error)) and _tokens(body, model) > cap * 0.8:
                log.info("%s takes %d input tokens a minute, too few for this prompt", model, cap)
                INPUT_CAP[model] = cap
                return _NO_ANSWER
            log.info("%s rate-limited; waiting %.0fs", model, delay)
            time.sleep(min(delay, 70))
            continue
        if response.status_code == 404 or (response.status_code == 400 and model.startswith("gemma")):
            log.warning("%s is not available: %s", model, error.get("message", "")[:120])
            _spent.add(model)
            return _NO_ANSWER
        if response.status_code >= 500:
            log.warning("%s %s: %s", model, response.status_code, error.get("message", "")[:120])
            failures += 1
            time.sleep(10)
            continue
        raise RuntimeError(f"Gemini {model} {response.status_code}: {error.get('message', response.text[:300])}")
    _resting[model] = time.monotonic() + REST_SECONDS
    return _NO_ANSWER


def _image_url(part: dict) -> dict:
    """A Gemini image part as an OpenAI-style data URL, cut to BACKUP_IMAGE_SIDE pixels on its longer side."""
    from PIL import Image

    data, mime = base64.b64decode(part["inline_data"]["data"]), part["inline_data"]["mime_type"]
    with Image.open(io.BytesIO(data)) as picture:
        if max(picture.size) > BACKUP_IMAGE_SIDE or mime == "image/webp":
            image = picture.convert("RGB")
            image.thumbnail((BACKUP_IMAGE_SIDE, BACKUP_IMAGE_SIDE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=85)
            data, mime = buffer.getvalue(), "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


def _messages(body: dict, schema: dict | None) -> list[dict]:
    """The prompt as one OpenAI-style user message, with the schema spelled out for models that don't enforce it."""
    content = [{"type": "text", "text": p["text"]} if "text" in p else _image_url(p) for p in body["contents"][0]["parts"]]
    if schema:
        content.append({"type": "text", "text": "\nReply with only JSON that matches this JSON schema:\n" + json.dumps(schema)})
    if all(c["type"] == "text" for c in content):
        return [{"role": "user", "content": "\n".join(c["text"] for c in content)}]
    return [{"role": "user", "content": content}]


# Reasoning some models write into their answer.
_THINKING = re.compile(r"<think>.*?</think>", re.S)
# A 429 meaning a daily or monthly allowance is used up, rather than a limit a minute.
_ALLOWANCE = re.compile(r"per[ -](day|month)|daily|monthly|\bRPD\b|\bTPD\b|quota|credit|neurons|billing|payment", re.I)


def _retry_after(response: requests.Response) -> float:
    try:
        return float(response.headers.get("retry-after", "")) + 1
    except ValueError:
        return 30.0


def _ask_backup(model: str, body: dict, schema: dict | None, purpose: str):
    provider, name = model.split(":", 1)
    spec = PROVIDERS[provider]
    headers = {"Authorization": f"Bearer {os.environ[spec['key']]}"} if spec["key"] else {}
    request = {"model": name, "messages": _messages(body, schema),
               "max_tokens": BACKUPS[model].get("output", BACKUP_OUTPUT_TOKENS), **BACKUPS[model].get("extra", {})}
    if (temperature := body["generationConfig"].get("temperature")) is not None:
        request["temperature"] = temperature
    if schema:
        request["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
    failures = 0
    while failures < FAILURES_BEFORE_REST:
        _pace(model, spec["gap"])
        started = time.monotonic()
        try:
            response = requests.post(f"{spec['url']}/chat/completions", headers=headers, json=request, timeout=(20, 600))
        except requests.RequestException as err:
            log.warning("%s: %s", model, err)
            failures += 1
            time.sleep(10)
            continue
        if response.status_code == 200:
            reply = response.json()
            choice = (reply.get("choices") or [{}])[0]
            text = _THINKING.sub("", (choice.get("message") or {}).get("content") or "").strip()
            usage = reply.get("usage") or {}
            calls.append({"model": model, "purpose": purpose, "seconds": round(time.monotonic() - started, 1),
                          "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                          "thinking_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")})
            if not text:
                log.warning("%s returned no text (finish reason %s); retrying", model, choice.get("finish_reason"))
                failures += 1
                continue
            log.info("%s answered %s in %.0fs", model, purpose or "a prompt", time.monotonic() - started)
            if not schema:
                return text
            try:
                answer = _json(text)
            except json.JSONDecodeError:
                answer = None
            if answer is None or not _complete(answer, schema):
                log.warning("%s returned malformed JSON; retrying", model)
                failures += 1
                continue
            return answer
        message = response.text[:400]
        if response.status_code == 429:
            if _ALLOWANCE.search(message):
                log.info("%s is out of its free allowance: %s", model, message[:160])
                _spent.add(model)
                return _NO_ANSWER
            delay = _retry_after(response)
            if delay > 10:
                # OVH's limit is shared by everyone on the same address, so it's often busy for most of a minute.
                log.info("%s is rate-limited for %.0fs; using the next model", model, delay)
                _resting[model] = time.monotonic() + delay
                return _NO_ANSWER
            log.info("%s rate-limited; waiting %.0fs", model, delay)
            failures += 1
            time.sleep(delay)
            continue
        if response.status_code == 400 and "response_format" in request and re.search(r"response_format|json_schema", message, re.I):
            log.info("%s takes no JSON schema; asking for JSON in the prompt only", model)
            del request["response_format"]
            continue
        if response.status_code in (400, 413) and re.search(r"context|too long|too large|payload|too many (tokens|images)|exceeds the maximum", message, re.I):
            log.info("%s can't take this prompt: %s", model, message[:160])
            return _NO_ANSWER
        if response.status_code >= 500:
            log.warning("%s %s: %s", model, response.status_code, message[:120])
            failures += 1
            time.sleep(10)
            continue
        log.warning("%s is not available (%s): %s", model, response.status_code, message[:160])
        _spent.add(model)
        return _NO_ANSWER
    _resting[model] = time.monotonic() + spec["rest"]
    return _NO_ANSWER
