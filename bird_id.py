"""
Image identification using local TFLite classifier (primary) and
GitHub Models API (fallback).

For birds: tries the local AIY bird classifier first, falls back to GPT
if confidence is low or the model is unavailable.
For non-birds: uses GPT directly (when identify_all is enabled).

Configuration is loaded from config.json (github section).
Get a token at: https://github.com/settings/tokens
"""

import os
import json
import base64
import time
import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH) as _f:
    _config = json.load(_f)
    _gh_config = _config.get("github", {})
    _local_config = _config.get("local_classifier", {})

GH_TOKEN = _gh_config.get("token", "")
GH_MODEL = _gh_config.get("model", "gpt-4o-mini")
GH_ENDPOINT = "https://models.github.ai/inference/chat/completions"
IDENTIFY_ALL = _gh_config.get("identify_all", True)

LOCAL_ENABLED = _local_config.get("enabled", True)
LOCAL_MIN_CONFIDENCE = _local_config.get("min_confidence", 0.3)

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
                print("[BIRD_ID] Local classifier model not found, using GPT only.")
        except ImportError as e:
            _bird_classify = False
            print(f"[BIRD_ID] Local classifier not available ({e}), using GPT only.")
    return _bird_classify if _bird_classify else None


def _is_gpt_configured():
    return bool(GH_TOKEN)


def _try_local(image_path):
    """
    Try local TFLite bird classifier. Returns result dict or None.
    """
    if not LOCAL_ENABLED:
        return None

    classifier = _get_classifier()
    if not classifier:
        return None

    predictions = classifier.classify(image_path, top_k=3)
    if not predictions:
        return None

    best = predictions[0]
    if best["score"] < LOCAL_MIN_CONFIDENCE:
        print(f"[BIRD_ID] Local classifier low confidence: "
              f"{best['name']} ({best['score']:.0%}), falling back to GPT.")
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
    return result


def identify_image(image_path, detected_label="bird", _retry=0):
    """
    Identify what's in an image.

    For birds: tries local TFLite classifier first, falls back to GPT.
    For non-birds: uses GPT directly.

    Args:
        image_path: path to the image file
        detected_label: what the IMX500 model detected (e.g. 'bird', 'person', 'dog')

    Returns:
        dict with 'name', 'common_name', 'score' on success
        None if not configured or on failure
    """
    # Always try local bird classifier first (spike may be any COCO label)
    local_result = _try_local(image_path)
    if local_result:
        return local_result

    # Fall through to GPT if local failed/disabled
    if not _is_gpt_configured():
        print("[BIRD_ID] GPT not configured and local classifier didn't match.")
        return None

    if not IDENTIFY_ALL and detected_label != "bird":
        return None

    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        prompt = (
            "Look at this image and identify what animal or person is in it. "
            f"The camera's AI chip detected it as '{detected_label}', but it may be wrong. "
            "Respond ONLY with a JSON object (no markdown, no explanation) with these fields:\n"
            '  "common_name": the common English name (e.g. Red-shouldered Hawk, Domestic Cat, Person),\n'
            '  "name": the scientific/Latin name (or "Homo sapiens" for person, "Unknown" if unsure),\n'
            '  "category": one of "bird", "person", "dog", "cat", "other_animal", "unknown",\n'
            '  "confidence": your confidence from 0.0 to 1.0,\n'
            '  "notes": a very brief note (max 15 words)\n'
            "If there is nothing identifiable in the image, return "
            '{"common_name": null, "name": null, "category": "unknown", "confidence": 0, "notes": "Nothing identifiable"}'
        )

        resp = requests.post(
            GH_ENDPOINT,
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "model": GH_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_data}",
                                },
                            },
                        ],
                    }
                ],
                "temperature": 0.1,
                "max_tokens": 256,
            },
            timeout=60,
        )
        resp.raise_for_status()

        text = resp.json()["choices"][0]["message"]["content"]
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        data = json.loads(text)

        if not data.get("common_name"):
            print("[BIRD_ID] No bird identified in image.")
            return None

        result = {
            "name": data.get("name", "Unknown"),
            "common_name": data.get("common_name", "Unknown"),
            "category": data.get("category", detected_label),
            "score": float(data.get("confidence", 0)),
            "source": "gpt",
        }

        notes = data.get("notes", "")
        print(f"[BIRD_ID] [GPT] {result['common_name']} ({result['name']}) "
              f"conf={result['score']:.0%} — {notes}")

        return result

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[BIRD_ID] Failed to parse response: {e}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"[BIRD_ID] API error: {e}")
        if e.response is not None:
            print(f"[BIRD_ID] Response body: {e.response.text[:500]}")
            if e.response.status_code == 429 and _retry < 3:
                wait = 30 * (2 ** _retry)  # 30s, 60s, 120s
                print(f"[BIRD_ID] Rate limited. Retrying in {wait}s (attempt {_retry+1}/3)...")
                time.sleep(wait)
                return identify_image(image_path, detected_label, _retry=_retry+1)
        return None
    except Exception as e:
        print(f"[BIRD_ID] Error: {e}")
        return None


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
