"""
Image identification using mandatory local TFLite classification, with an
explicit optional OpenAI or Claude fallback. Hermes SSH is queued by the watcher.
"""

import os
import json
import re
import time
import base64
import io

import requests
from PIL import Image

DEFAULT_PROMPT = (
    'Examine the attached image carefully. Is a bird visible? If so, identify '
    'which bird and species it is from visible features, giving its common '
    'name and scientific name only when supported by the image. Do not assume '
    'a bird is present or infer a species from the feeder or background. '
    'If blur, occlusion, or image quality prevents deciding whether a bird is '
    'present, use inconclusive rather than empty. '
    'Return ONLY a JSON object with status (empty, identified, inconclusive), '
    'common_name (string or null), name (Latin binomial or null), category '
    '(bird or unknown), confidence (0 to 1), and notes. '
    'Identify birds only; no bird means empty. For identified use category bird. '
    'If a bird is visible but its species is uncertain, use common_name '
    '"Unidentified bird" and name null; never invent a scientific name. '
    'For empty/inconclusive use null names, category unknown and confidence 0.'
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH) as _f:
    _config = json.load(_f)
    _local_config = _config.get("local_classifier", {})
    _remote_config = _config.get("identification", {})

REMOTE_PROVIDER = _remote_config.get("provider", "none")
REMOTE_ENABLED = REMOTE_PROVIDER in ("openai", "claude")
REMOTE_QUEUED = REMOTE_PROVIDER == "hermes_ssh"

LOCAL_MIN_CONFIDENCE = _local_config.get("min_confidence", 0.3)

# Species aliases — normalizes model output to canonical names
_ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "species_aliases.json")
_species_aliases = {}
if _local_config.get("normalize_species", True) and os.path.isfile(_ALIASES_PATH):
    with open(_ALIASES_PATH) as _af:
        _species_aliases = {k: v for k, v in json.load(_af).items() if not k.startswith("_")}


def _apply_alias(result):
    """Normalize common_name using the species alias table."""
    if not result or not _species_aliases:
        return result
    alias = _species_aliases.get(result.get("common_name"))
    if alias:
        result["common_name"] = alias[0]
        result["name"] = alias[1]
    return result

# Lazy import — only loaded if local classifier is used
_bird_classify = None


def _get_classifier():
    """Lazy-load the bird_classify module."""
    global _bird_classify
    if _bird_classify is None:
        try:
            import bird_classify
            if bird_classify.is_available():
                _bird_classify = bird_classify
            else:
                _bird_classify = False
                print("[BIRD_ID] Local classifier model not found; identification unavailable.")
        except ImportError as e:
            _bird_classify = False
            print(f"[BIRD_ID] Local classifier not available ({e}); identification unavailable.")
    return _bird_classify if _bird_classify else None


def _try_local(image_path):
    """
    Try local TFLite bird classifier. Returns result dict or None.
    """
    classifier = _get_classifier()
    if not classifier:
        return None

    predictions = classifier.classify(image_path, top_k=3)
    if not predictions:
        return None

    best = predictions[0]
    if best["score"] < LOCAL_MIN_CONFIDENCE:
        print(f"[BIRD_ID] Local classifier low confidence: "
              f"{best['name']} ({best['score']:.0%}); no reliable local identification.")
        return None

    result = {
        "name": best["name"],
        "common_name": best.get("common_name", best["name"]),
        "category": "bird",
        "score": best["score"],
        "source": "local",
    }
    runner_up = predictions[1].get("common_name", predictions[1]["name"]) if len(predictions) > 1 else ""
    print(f"[BIRD_ID] [Local] {result['common_name']} ({best['name']}) conf={best['score']:.0%}"
          f"{f' (runner-up: {runner_up})' if runner_up else ''}")
    return _apply_alias(result)


def encode_image(image_path):
    """Return a base64 JPEG bounded to 1280 pixels on its longest side."""
    if os.path.getsize(image_path) > 10 * 1024 * 1024:
        raise ValueError("Input image exceeds 10 MB")
    with Image.open(image_path) as image:
        if image.width * image.height > 25_000_000:
            raise ValueError("Input image exceeds 25 megapixels")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        image.convert("RGB").save(encoded, format="JPEG", quality=85)
    return base64.b64encode(encoded.getvalue()).decode("ascii")


def _remote_outcome(status, source):
    return {"status": status, "common_name": None, "name": None, "score": 0,
            "category": "unknown", "source": source}


def _parse_remote_result(content, source):
    data = json.loads(content)
    if not isinstance(data, dict) or data.get("status") not in ("identified", "empty", "inconclusive"):
        raise ValueError("Invalid identification status")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("Invalid confidence")
    if data["status"] != "identified":
        if data.get("common_name") is not None or data.get("name") is not None or confidence != 0 or data.get("category") != "unknown":
            raise ValueError("Unresolved frames must not claim species")
        return _remote_outcome(data["status"], source)
    common_name = data.get("common_name")
    category = data.get("category")
    if not isinstance(common_name, str) or not common_name.strip():
        raise ValueError("Missing common name")
    if category != "bird":
        raise ValueError("Invalid category")
    name = data.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][a-z]+ [a-z]+(?: [a-z]+)?", name) or name.casefold() == common_name.casefold():
        name = "Unknown"
    return _apply_alias({"status": "identified", "common_name": common_name, "name": name,
                         "score": float(confidence), "category": category, "source": source})


def _try_vision_api(image_path, provider):
    failure = _remote_outcome("error", provider)
    settings = _config.get(provider, {})
    key_variable = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    key = os.environ.get(key_variable) or settings.get("api_key")
    if not key:
        print(f"[BIRD_ID] {provider} API key is missing; no request sent.")
        return failure
    try:
        timeout = float(_remote_config.get("timeout", 30))
        attempts = _remote_config.get("max_attempts", 2)
        if not 0 < timeout <= 60 or isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
            raise ValueError("Invalid remote timeout/retry settings")
        image = encode_image(image_path)
        prompt = _remote_config.get("prompt") or DEFAULT_PROMPT
        if prompt != DEFAULT_PROMPT:
            prompt += "\n" + DEFAULT_PROMPT
        headers = {"Content-Type": "application/json"}
        if provider == "openai":
            endpoint = settings.get("endpoint", "https://api.openai.com/v1/chat/completions")
            headers["Authorization"] = f"Bearer {key}"
            payload = {"model": settings.get("model") or "gpt-4.1-mini", "max_completion_tokens": 512,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": prompt},
                           {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                       ]}]}
        else:
            endpoint = settings.get("endpoint", "https://api.anthropic.com/v1/messages")
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            payload = {"model": settings.get("model") or "claude-haiku-4-5", "max_tokens": 512,
                       "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
                           {"type": "text", "text": prompt},
                       ]}]}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(attempts):
            try:
                response = requests.post(endpoint, data=body, headers=headers,
                                         timeout=(5, timeout), allow_redirects=False)
                try:
                    if response.status_code == 200:
                        data = response.json()
                        if provider == "openai":
                            content = data["choices"][0]["message"]["content"]
                        else:
                            content = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
                        return _parse_remote_result(content, provider)
                    print(f"[BIRD_ID] {provider} HTTP {response.status_code} (attempt {attempt + 1}/{attempts}).")
                    if response.status_code not in (408, 429) and not 500 <= response.status_code <= 599:
                        return failure
                finally:
                    response.close()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                print(f"[BIRD_ID] {provider} transport failed ({type(error).__name__}, attempt {attempt + 1}/{attempts}).")
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    except Exception as error:
        print(f"[BIRD_ID] {provider} identification failed ({type(error).__name__}); media will be retained.")
    return failure


def _try_remote(image_path):
    if REMOTE_PROVIDER in ("openai", "claude"):
        return _try_vision_api(image_path, REMOTE_PROVIDER)
    return None


def identify_image(image_path, detected_label="bird", _retry=0, *, local_only=False, return_outcome=False):
    """
    Identify what's in an image.

    Always tries local classification, then only the selected remote provider.

    Args:
        image_path: path to the image file
        detected_label: what the IMX500 model detected (e.g. 'bird', 'person', 'dog')
        local_only: return without remote requests when the local model cannot identify it
        return_outcome: also return empty/inconclusive/error results for the watcher

    Returns:
        dict with 'name', 'common_name', 'score' on success
        None if not configured or on failure
    """
    try:
        local_result = _try_local(image_path)
    except Exception as error:
        print(f"[BIRD_ID] Local classifier failed ({type(error).__name__}).")
        local_result = None
    if local_result:
        return local_result
    if local_only:
        return None

    if not REMOTE_ENABLED:
        print("[BIRD_ID] No local identification; remote identification is disabled.")
        return None

    result = _try_remote(image_path)
    return result if return_outcome or result.get("status") == "identified" else None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <image_path>")
        print("Tests bird identification on a single image.")
        sys.exit(1)

    result = identify_image(sys.argv[1])
    if result:
        print(f"\nSpecies: {result['common_name']} ({result['name']})")
        print(f"Score:   {result['score']:.0%}")
        print(f"Source:  {result.get('source', 'unknown')}")
    else:
        print("\nNo identification returned.")
