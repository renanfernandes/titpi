"""
Local bird species classifier using Google AIY Birds TFLite model.
Classifies cropped bird images into ~964 species (scientific names).

Requires:
  - tflite-runtime (pip3 install tflite-runtime)
  - Model file: aiy_birds_v1.tflite
  - Label map: aiy_birds_labelmap.csv

Both files should be in the same directory as this script.
"""

import os
import csv
import json
import numpy as np
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(SCRIPT_DIR, "aiy_birds_v1.tflite")
LABEL_PATH = os.path.join(SCRIPT_DIR, "aiy_birds_labelmap.csv")
COMMON_NAMES_PATH = os.path.join(SCRIPT_DIR, "bird_common_names.json")

_interpreter = None
_labels = None
_common_names = None


def _load_common_names():
    """Load scientific -> common name mapping from JSON."""
    global _common_names
    if _common_names is not None:
        return _common_names

    if os.path.isfile(COMMON_NAMES_PATH):
        with open(COMMON_NAMES_PATH) as f:
            _common_names = json.load(f)
    else:
        _common_names = {}
    return _common_names


def _load_labels():
    """Load the bird species label map from CSV."""
    global _labels
    if _labels is not None:
        return _labels

    labels = {}
    with open(LABEL_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            labels[int(row["id"])] = row["name"]
    _labels = labels
    return _labels


def _load_interpreter():
    """Lazy-load the TFLite interpreter (only once)."""
    global _interpreter
    if _interpreter is not None:
        return _interpreter

    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        try:
            from ai_edge_litert import interpreter as _ai
            Interpreter = _ai.Interpreter
        except ImportError:
            import tensorflow.lite as tflite
            Interpreter = tflite.Interpreter

    _interpreter = Interpreter(model_path=MODEL_PATH)
    _interpreter.allocate_tensors()
    return _interpreter


def is_available():
    """Check if the local classifier model and labels are present."""
    return os.path.isfile(MODEL_PATH) and os.path.isfile(LABEL_PATH)


def classify(image_path, top_k=3):
    """
    Classify a bird image using the local TFLite model.

    Args:
        image_path: path to JPEG image
        top_k: number of top predictions to return

    Returns:
        list of dicts with 'name' (scientific), 'common_name', 'score', 'class_id'
        sorted by score descending. Empty list on failure.
    """
    if not is_available():
        print("[BIRD_CLASSIFY] Model or labels not found.")
        return []

    try:
        interpreter = _load_interpreter()
        labels = _load_labels()
        common_names = _load_common_names()

        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()

        # Model expects 224x224 RGB uint8 [0-255]
        height = input_details[0]["shape"][1]
        width = input_details[0]["shape"][2]

        img = Image.open(image_path).convert("RGB")
        img = img.resize((width, height), Image.LANCZOS)
        input_data = np.expand_dims(np.array(img, dtype=np.uint8), axis=0)

        interpreter.set_tensor(input_details[0]["index"], input_data)
        interpreter.invoke()

        output = interpreter.get_tensor(output_details[0]["index"])[0]

        # Get top-k indices
        top_indices = np.argsort(output)[-top_k:][::-1]

        results = []
        for idx in top_indices:
            score = float(output[idx]) / 255.0
            species = labels.get(idx, "Unknown")
            if species == "background":
                continue
            common = common_names.get(species, species)
            results.append({
                "name": species,
                "common_name": common,
                "score": score,
                "class_id": int(idx),
            })

        return results

    except Exception as e:
        print(f"[BIRD_CLASSIFY] Error: {e}")
        return []


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <image_path>")
        sys.exit(1)

    if not is_available():
        print("Model or labels not found. Place aiy_birds_v1.tflite and "
              "aiy_birds_labelmap.csv in the script directory.")
        sys.exit(1)

    results = classify(sys.argv[1], top_k=5)
    if results:
        print("Top predictions:")
        for r in results:
            common = r.get('common_name', '')
            label = f"{common} ({r['name']})" if common != r['name'] else r['name']
            print(f"  {label:55s} {r['score']:.4f}")
    else:
        print("No predictions.")
