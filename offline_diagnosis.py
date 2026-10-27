"""Local ONNX crop-disease classifier used only after hosted vision APIs fail."""

import base64
import binascii
import hashlib
import io
import threading
from pathlib import Path

import numpy as np
import onnxruntime
from onnxruntime.capi import onnxruntime_pybind11_state
from PIL import Image, ImageOps, UnidentifiedImageError


MODEL_DIR = Path(__file__).resolve().parent / "models" / "offline_diagnosis"
MODEL_PATH = MODEL_DIR / "model.onnx"
MODEL_DATA_PATH = MODEL_DIR / "model.onnx.data"
MODEL_NAME = "ConCaPlant MobileNetV3 (PlantVillage)"
MODEL_FILES = {
    MODEL_PATH: (293463, "c6a962f699ddc820108231b23454b6a4e242407044c2c2514a0f6294d35428fc"),
    MODEL_DATA_PATH: (6160384, "c03d114add89db13d850e5bbe0a7c09040cf6caf1acfbbd489c21234fa6dcb5e"),
}
MIN_CLASS_SCORE = 0.60
CLASS_NAMES = (
    "Pepper__bell___Bacterial_spot",
    "Pepper__bell___healthy",
    "Potato___Early_blight",
    "Potato___Late_blight",
    "Potato___healthy",
    "Tomato_Bacterial_spot",
    "Tomato_Early_blight",
    "Tomato_Late_blight",
    "Tomato_Leaf_Mold",
    "Tomato_Septoria_leaf_spot",
    "Tomato_Spider_mites_Two_spotted_spider_mite",
    "Tomato__Target_Spot",
    "Tomato__Tomato_YellowLeaf__Curl_Virus",
    "Tomato__Tomato_mosaic_virus",
    "Tomato_healthy",
)

_session = None
_session_lock = threading.Lock()
_ONNX_RUNTIME_ERRORS = (
    onnxruntime_pybind11_state.Fail,
    onnxruntime_pybind11_state.InvalidArgument,
    onnxruntime_pybind11_state.InvalidGraph,
    onnxruntime_pybind11_state.NoSuchFile,
    onnxruntime_pybind11_state.RuntimeException,
)
_MODEL_LOAD_ERRORS = _ONNX_RUNTIME_ERRORS + (OSError,)


class OfflineDiagnosisUnavailable(RuntimeError):
    """Raised when the packaged offline classifier cannot produce a result."""


def _get_session():
    global _session
    if _session is not None:
        return _session

    with _session_lock:
        if _session is None:
            for path, (expected_size, expected_hash) in MODEL_FILES.items():
                if not path.is_file():
                    raise OfflineDiagnosisUnavailable(
                        "Offline diagnosis model files are missing. "
                        "Run scripts/download_offline_diagnosis_model.py."
                    )
                if path.stat().st_size != expected_size:
                    raise OfflineDiagnosisUnavailable(
                        f"Offline diagnosis model artifact has an invalid size: {path.name}"
                    )
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != expected_hash:
                    raise OfflineDiagnosisUnavailable(
                        f"Offline diagnosis model artifact failed checksum verification: {path.name}"
                    )
            try:
                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                _session = onnxruntime.InferenceSession(
                    str(MODEL_PATH),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            except _MODEL_LOAD_ERRORS as exc:
                raise OfflineDiagnosisUnavailable(
                    "Offline diagnosis model could not be loaded."
                ) from exc
    return _session


def _decode_image(image_b64):
    encoded = image_b64
    if encoded.startswith("data:"):
        _, separator, encoded = encoded.partition(",")
        if not separator:
            raise OfflineDiagnosisUnavailable("Invalid image data URL.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OfflineDiagnosisUnavailable("Invalid base64 image data.") from exc

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > 25_000_000:
                raise OfflineDiagnosisUnavailable("Image dimensions are too large.")
            image = ImageOps.exif_transpose(source).convert("RGB")
            return _preprocess_image(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise OfflineDiagnosisUnavailable("Could not decode the uploaded image.") from exc


def _preprocess_image(image):
    width, height = image.size
    if not width or not height:
        raise OfflineDiagnosisUnavailable("Uploaded image has invalid dimensions.")

    scale = 256 / min(width, height)
    resized = image.resize(
        (round(width * scale), round(height * scale)),
        Image.Resampling.BILINEAR,
    )
    left = (resized.width - 224) // 2
    top = (resized.height - 224) // 2
    cropped = resized.crop((left, top, left + 224, top + 224))

    pixels = np.asarray(cropped, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    normalized = (pixels - mean) / std
    return np.transpose(normalized, (2, 0, 1))[None, ...]


def _humanize_label(label):
    parts = label.replace("___", " - ").replace("__", " ").replace("_", " ").split()
    return " ".join(part.capitalize() for part in parts)


def predict_offline_diagnosis(image_b64):
    """Return a limited-class classifier result or raise if inference is unavailable."""
    input_tensor = _decode_image(image_b64)
    session = _get_session()
    input_name = session.get_inputs()[0].name
    try:
        output = session.run(None, {input_name: input_tensor})
    except _ONNX_RUNTIME_ERRORS as exc:
        raise OfflineDiagnosisUnavailable(
            "Offline diagnosis model inference failed."
        ) from exc
    if not output:
        raise OfflineDiagnosisUnavailable("Offline diagnosis model returned no scores.")

    logits = np.asarray(output[0], dtype=np.float64).reshape(-1)
    if logits.size != len(CLASS_NAMES) or not np.isfinite(logits).all():
        raise OfflineDiagnosisUnavailable("Offline diagnosis model returned invalid scores.")

    exp_scores = np.exp(logits - np.max(logits))
    probabilities = exp_scores / exp_scores.sum()
    class_index = int(np.argmax(probabilities))
    class_score = float(probabilities[class_index])
    confidence = round(class_score * 100)
    if class_score >= MIN_CLASS_SCORE:
        label = _humanize_label(CLASS_NAMES[class_index])
        notice = (
            "Offline prediction from a model covering tomato, potato, and bell pepper "
            "leaf classes only. Its confidence score is not a calibrated probability. "
            "It may be unreliable for other crops or field conditions; confirm the "
            "result with a local agricultural expert. This model does not provide "
            "treatment recommendations."
        )
    else:
        label = "Unable to identify from supported classes"
        notice = (
            "The offline model score was below 60%, so no disease class was assigned. "
            "This model only covers tomato, potato, and bell pepper leaf classes. "
            "Try again when a hosted vision API is available or consult a local "
            "agricultural expert."
        )
    result = {
        "disease": label,
        "confidence": confidence,
        "severity": "",
        "affected_part": "Leaf",
        "cause": "",
        "eco_remedies": [],
        "chemical_remedies": [],
        "prevention": [],
        "recovery_timeline": "",
        "offline_fallback": True,
        "offline_notice": notice,
    }
    result["_english_source"] = dict(result)
    return result
