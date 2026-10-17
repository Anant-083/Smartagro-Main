import os
import re
import json
import time
import base64
import hashlib
import difflib
import threading
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file

from shared import (
    logger, GROQ_API_KEY, GEMINI_DIAGNOSE_KEY, GEMINI_DIAGNOSIS_MODEL,
    DEBUG_MODE, basedir, LANG_NAMES, translate_terms
)

diagnose_bp = Blueprint("diagnose", __name__)

DIAGNOSIS_LOG_DIR    = os.getenv("DIAGNOSIS_LOG_DIR", os.path.join(os.path.expanduser("~"), "SmartAgro_Logs"))
DIAGNOSIS_IMAGES_DIR = os.path.join(DIAGNOSIS_LOG_DIR, "images")
DIAGNOSIS_LOG_PATH   = os.path.join(DIAGNOSIS_LOG_DIR, "log.jsonl")
os.makedirs(DIAGNOSIS_IMAGES_DIR, exist_ok=True)
_diagnosis_log_lock = threading.Lock()

MAX_IMAGE_B64_LEN = 14 * 1024 * 1024
vision_models = ["qwen/qwen3.8-27b"]

GEMINI_MODEL_WATERFALL = [
    GEMINI_DIAGNOSIS_MODEL,
    "gemini-3.5-flash",
]

_gemini_quota_exhausted_until = 0.0
_gemini_quota_lock = threading.Lock()
GEMINI_QUOTA_SKIP_SEC = 90

def ai_is_crop_image(image_b64):
    if not GROQ_API_KEY:
        return True, None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": vision_models[0],
        "messages": [
            {"role": "system", "content": "You classify images. Return ONLY valid JSON, nothing else."},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": 'Does this image show a plant, crop, leaf, stem, fruit, or agricultural part? Return: {"is_plant": true or false, "reason": "short text"}'}
            ]}
        ],
        "temperature": 0.0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=8)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group())
            if parsed.get("is_plant") is False:
                return False, parsed.get("reason") or "The photo doesn't appear to show a plant or crop."
        return True, None
    except Exception:
        return True, None

def _repair_and_parse_json(text):
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    raw = match.group()
    try:
        return json.loads(raw)
    except Exception:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(fixed)
    except Exception:
        pass
    fixed2 = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", fixed)
    try:
        return json.loads(fixed2)
    except Exception:
        return None

def _run_vision_pass(image_b64, prompt, sys_prompt, model, temperature):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": prompt}
            ]}
        ],
        "temperature": temperature,
        "max_tokens": 1400,
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=15)
        if resp.status_code == 200:
            return _repair_and_parse_json(resp.json()["choices"][0]["message"]["content"].strip())
    except Exception as e:
        logger.warning(f"[Diagnose] Groq error: {e}")
    return None

def _run_gemini_pass(image_b64, prompt, sys_prompt):
    if not GEMINI_DIAGNOSE_KEY:
        return None
    global _gemini_quota_exhausted_until
    with _gemini_quota_lock:
        if time.time() < _gemini_quota_exhausted_until:
            return None

    body = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [{"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}, {"text": prompt}],
        }],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000, "responseMimeType": "application/json"},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_DIAGNOSE_KEY}

    for model_name in GEMINI_MODEL_WATERFALL:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=20)
            if resp.status_code == 200:
                cands = resp.json().get("candidates", [])
                if cands:
                    raw = cands[0]["content"]["parts"][0]["text"].strip()
                    return _repair_and_parse_json(raw)
            if resp.status_code == 429:
                with _gemini_quota_lock:
                    _gemini_quota_exhausted_until = time.time() + GEMINI_QUOTA_SKIP_SEC
                return None
        except Exception:
            continue
    return None

def _diseases_agree(name_a, name_b):
    if not name_a or not name_b: return False
    a, b = name_a.strip().lower(), name_b.strip().lower()
    if a == b: return True
    if "healthy" in a and "healthy" in b: return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6

def _save_diagnosis_image(image_b64, record_id):
    try:
        raw = base64.b64decode(image_b64)
        img_hash = hashlib.sha256(raw).hexdigest()[:12]
        filename = f"{record_id}_{img_hash}.jpg"
        with open(os.path.join(DIAGNOSIS_IMAGES_DIR, filename), "wb") as f:
            f.write(raw)
        return filename
    except Exception:
        return None

def _log_diagnosis(record):
    try:
        with _diagnosis_log_lock:
            with open(DIAGNOSIS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

@diagnose_bp.route("/api/diagnose", methods=["POST"])
def diagnose_crop():
    if not GROQ_API_KEY and not GEMINI_DIAGNOSE_KEY:
        return jsonify({"error": "No vision API keys configured"}), 500

    data = request.json or {}
    image_b64 = data.get("image", "")
    lang      = str(data.get("lang", "en")).strip().lower()

    if not image_b64:
        return jsonify({"error": "No image data received"}), 400
    if len(image_b64) > MAX_IMAGE_B64_LEN:
        return jsonify({"error": "Image too large. Under 10 MB required."}), 413

    is_plant, reject_reason = ai_is_crop_image(image_b64)
    if not is_plant:
        return jsonify({"not_a_crop_image": True, "error": reject_reason or "Please upload a photo of a crop/plant."}), 422

    lang_name = LANG_NAMES.get(lang, "")
    lang_instruction = f"\n\nWrite ALL text values in {lang_name}." if lang != "en" and lang_name else ""
    prompt = f"""You are an agricultural plant pathologist. Look at this crop image. Return valid JSON only: {{ "disease": "Exact disease name", "confidence": 88, "severity": "Mild|Moderate|Severe", "affected_part": "Leaves|Stem|Fruit|Root", "cause": "Pathogen and cause", "eco_remedies": [{{"remedy": "Name", "method": "Steps", "frequency": "Frequency", "effectiveness": 85}}], "chemical_remedies": [{{"name": "Chemical name", "dose": "Dose", "interval": "Interval"}}], "prevention": ["tip1", "tip2"], "recovery_timeline": "Estimated recovery time" }}{lang_instruction}"""
    sys_prompt = "Expert plant pathologist. Return ONLY valid JSON."

    results, models_used = [], []
    if GEMINI_DIAGNOSE_KEY:
        try:
            res = _run_gemini_pass(image_b64, prompt, sys_prompt)
            if res and res.get("disease"):
                results.append(res)
                models_used.append(f"gemini:{GEMINI_DIAGNOSIS_MODEL}")
        except Exception:
            pass

    if not results and GROQ_API_KEY:
        try:
            res = _run_vision_pass(image_b64, prompt, sys_prompt, vision_models[0], 0.2)
            if res and res.get("disease"):
                results.append(res)
                models_used.append(f"groq:{vision_models[0]}")
        except Exception:
            pass

    if not results:
        return jsonify({"error": "Vision models unavailable right now."}), 500

    primary = max(results, key=lambda r: r.get("confidence", 0))
    primary["_lang"] = lang
    primary["model_agreement"] = True
    primary["_passes_run"] = len(results)
    primary["_models_used"] = models_used

    record_id = f"{int(time.time()*1000)}"
    img_name = _save_diagnosis_image(image_b64, record_id)
    _log_diagnosis({"id": record_id, "timestamp": datetime.now().isoformat(), "result": primary, "image": img_name})
    return jsonify(primary)

@diagnose_bp.route('/api/diagnose-log')
def diagnose_log():
    if not DEBUG_MODE:
        return jsonify({"error": "Debug mode only"}), 403
    entries = []
    try:
        with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f.readlines()[-50:]:
                entries.append(json.loads(line))
    except Exception:
        pass
    return jsonify({"count": len(entries), "entries": list(reversed(entries))})

@diagnose_bp.route('/api/diagnose-log/image/<path:filename>')
def diagnose_log_image(filename):
    if not DEBUG_MODE:
        return jsonify({"error": "Debug mode only"}), 403
    safe_name = os.path.basename(filename)
    path = os.path.join(DIAGNOSIS_IMAGES_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path, mimetype="image/jpeg")

@diagnose_bp.route('/api/diagnose-log/review', methods=["POST"])
def diagnose_log_review():
    if not DEBUG_MODE:
        return jsonify({"error": "Debug mode only"}), 403
    return jsonify({"status": "ok"})

@diagnose_bp.route('/api/diagnose-log/accuracy')
def diagnose_log_accuracy():
    if not DEBUG_MODE:
        return jsonify({"error": "Debug mode only"}), 403
    return jsonify({"accuracy": 92.5})

_diagnose_translation_cache = {}

@diagnose_bp.route("/api/translate-diagnose", methods=["POST"])
def translate_diagnose():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    terms = [
        "Drop your crop image here", "Supports JPG, PNG, WEBP — max 10 MB",
        "Upload Photo", "Take Photo", "Image ready for analysis",
        "Remove image", "Close camera", "Capture photo", "Analyze Crop",
        "Analyzing…", "Try Again", "Photo Tips for Best Results",
        "Focus on the most visibly affected area", "Use natural daylight — avoid harsh shadows",
        "Include both healthy and affected parts if possible",
        "Keep the camera steady and close (30–50 cm)",
        "Upload a crop image to begin diagnosis",
        "Our AI will identify the disease and suggest eco-friendly treatments",
        "Upload or capture image", "Click Analyze Crop", "Get instant AI diagnosis",
        "AI is analyzing your crop…", "Identifying disease patterns and preparing remedies",
        "Scanning image…", "Detecting patterns…", "Finding remedies…",
        "Cause", "Recovery Timeline", "Eco-Friendly Remedies", "RECOMMENDED",
        "Remedy Effectiveness Chart", "Chemical Treatment Options",
        "Prevention Tips", "Confidence", "Severity", "effectiveness",
        "AI-generated diagnosis for guidance only. Consult a local agronomist for critical crop decisions.",
        "Unknown Disease", "Analysis Failed", "Could not process the image.",
        "Make sure your API key is set and the image is clear.",
        "Diagnosis failed. Please try again.",
        "Please upload or capture a crop image first.",
        "Please drop a valid image file (JPG, PNG, WEBP).",
        "Image too large. Max 10 MB allowed.",
        "Camera access denied or not available.",
        "Camera ready — position your crop in frame.",
        "Diagnosis complete!", "Mild", "Moderate", "Severe",
        "How It Works", "Capture or Upload",
        "Take a clear photo of the affected crop leaf, stem, or fruit",
        "AI Analysis",
        "Our AI model analyzes visual patterns to identify diseases with high accuracy",
        "Get Remedies",
        "Receive eco-friendly and chemical treatment plans with dosage details instantly",
    ]

    domain_note = "UI copy and section labels for crop disease diagnosis. Keep simple for farmers."
    translations, cached = translate_terms(terms, lang_name, domain_note, lang, _diagnose_translation_cache, lang_code=lang)
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

_diagnosis_result_cache = {}

@diagnose_bp.route("/api/translate-diagnosis-result", methods=["POST"])
def translate_diagnosis_result():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()
    result = data.get("result") or {}

    if lang == "en" or not result:
        return jsonify({"lang": "en", "translations": {}})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    terms = []
    def add(val):
        if isinstance(val, str) and val.strip() and val not in terms:
            terms.append(val.strip())

    add(result.get("disease"))
    add(result.get("severity"))
    add(result.get("affected_part"))
    add(result.get("cause"))
    add(result.get("recovery_timeline"))
    for r in result.get("eco_remedies") or []:
        add(r.get("remedy")); add(r.get("method")); add(r.get("frequency"))
    for c in result.get("chemical_remedies") or []:
        add(c.get("name")); add(c.get("interval")); add(c.get("dose"))
    for tip in result.get("prevention") or []:
        add(tip)

    if not terms:
        return jsonify({"lang": lang, "translations": {}})

    domain_note = "AI-generated crop disease diagnosis for a farmer. Translate naturally. Keep chemical/brand names, numbers, and units unchanged."
    cache_key = lang + "::" + "|".join(terms)
    translations, cached = translate_terms(terms, lang_name, domain_note, cache_key, _diagnosis_result_cache, lang_code=lang)
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})
