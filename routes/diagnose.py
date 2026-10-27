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
from flask import Blueprint, request, jsonify, send_file, send_from_directory

from shared import (
    logger, GROQ_API_KEY, GEMINI_DIAGNOSE_KEY, GEMINI_DIAGNOSIS_MODEL,
    GEMMA_DIAGNOSE_KEY, GEMMA_DIAGNOSIS_MODEL,
    DEBUG_MODE, basedir, LANG_NAMES, translate_terms
)
from diagnose_translation_terms import DIAGNOSE_UI_TERMS
from offline_diagnosis import OfflineDiagnosisUnavailable, predict_offline_diagnosis

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
        logger.info(
            "[Diagnosis step 1/5] Groq plant-image check: skipped "
            "(GROQ_API_KEY is not configured); continuing without this check."
        )
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
    t0 = time.time()
    logger.info(
        "[Diagnosis step 1/5] Groq plant-image check: calling model=%s",
        vision_models[0],
    )
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=8)
        ms = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = json.loads(match.group()) if match else None
            if isinstance(parsed, dict) and isinstance(parsed.get("is_plant"), bool):
                is_plant = parsed["is_plant"]
                logger.info(
                    "[Diagnosis step 1/5] Groq plant-image check result: "
                    "%s (model=%s, %sms)",
                    "PLANT" if is_plant else "NOT A PLANT",
                    vision_models[0],
                    ms,
                )
                if not is_plant:
                    return False, parsed.get("reason") or "The photo doesn't appear to show a plant or crop."
                return True, None
            logger.warning(
                "[Diagnosis step 1/5] Groq plant-image check: model=%s returned "
                "no valid true/false result (%sms); continuing without this check.",
                vision_models[0],
                ms,
            )
        else:
            logger.warning(
                "[Diagnosis step 1/5] Groq plant-image check: model=%s returned "
                "HTTP %s (%sms); continuing without this check.",
                vision_models[0],
                resp.status_code,
                ms,
            )
        return True, None
    except Exception as e:
        logger.warning(
            "[Diagnosis step 1/5] Groq plant-image check failed (%s); "
            "continuing without this check.",
            e,
        )
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
    t0 = time.time()
    logger.info(f"[Diagnose] Calling Groq for diagnosis model={model}")
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=15)
        ms = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            parsed = _repair_and_parse_json(resp.json()["choices"][0]["message"]["content"].strip())
            if parsed:
                logger.info(f"[Diagnose] Groq OK model={model} ({ms}ms)")
                return parsed
            logger.warning(f"[Diagnose] Groq returned unparseable JSON ({ms}ms)")
        else:
            logger.warning(f"[Diagnose] Groq HTTP {resp.status_code} ({ms}ms)")
    except Exception as e:
        logger.warning(f"[Diagnose] Groq error: {e}")
    return None


def _run_gemini_pass(image_b64, prompt, sys_prompt):
    """Returns (parsed_result, model_name_that_answered) or (None, None)."""
    global _gemini_quota_exhausted_until
    if not GEMINI_DIAGNOSE_KEY:
        logger.info("[Diagnose] Gemini skipped: no key set")
        return None, None
    with _gemini_quota_lock:
        remaining = _gemini_quota_exhausted_until - time.time()
    if remaining > 0:
        logger.info(f"[Diagnose] Gemini skipped: paused {int(remaining)}s more after 429")
        return None, None

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
        t0 = time.time()
        logger.info(f"[Diagnose] Calling Gemini model={model_name}")
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=20)
            ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                cands = resp.json().get("candidates", [])
                if cands:
                    raw = cands[0]["content"]["parts"][0]["text"].strip()
                    parsed = _repair_and_parse_json(raw)
                    if parsed:
                        logger.info(f"[Diagnose] Gemini OK model={model_name} ({ms}ms)")
                        return parsed, model_name
                    logger.warning(f"[Diagnose] Gemini {model_name} returned unparseable JSON ({ms}ms)")
                else:
                    logger.warning(f"[Diagnose] Gemini {model_name} returned no candidates ({ms}ms)")
                continue
            if resp.status_code == 429:
                with _gemini_quota_lock:
                    _gemini_quota_exhausted_until = time.time() + GEMINI_QUOTA_SKIP_SEC
                logger.warning(f"[Diagnose] Gemini 429 on {model_name}: pausing Gemini for {GEMINI_QUOTA_SKIP_SEC}s")
                return None, None
            logger.warning(f"[Diagnose] Gemini HTTP {resp.status_code} on {model_name} ({ms}ms), trying next model")
        except Exception as e:
            logger.warning(f"[Diagnose] Gemini error on {model_name}: {e}")
            continue
    logger.warning("[Diagnose] Gemini FAILED on all models")
    return None, None


def _run_gemma_pass(image_b64, prompt, sys_prompt):
    if not GEMMA_DIAGNOSE_KEY:
        logger.info("[Diagnose] Gemma skipped: no key set")
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMMA_DIAGNOSIS_MODEL}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMMA_DIAGNOSE_KEY,
    }
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                {"text": f"{sys_prompt}\n\n{prompt}"},
            ],
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2000},
    }
    started_at = time.time()
    logger.info("[Diagnose] Calling Gemma model=%s", GEMMA_DIAGNOSIS_MODEL)
    try:
        response = requests.post(url, headers=headers, json=body, timeout=20)
        elapsed_ms = int((time.time() - started_at) * 1000)
        if response.status_code == 200:
            candidates = response.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                raw = "".join(
                    part.get("text", "") for part in parts if isinstance(part, dict)
                ).strip()
                parsed = _repair_and_parse_json(raw)
                if parsed and parsed.get("disease"):
                    logger.info("[Diagnose] Gemma OK model=%s (%sms)", GEMMA_DIAGNOSIS_MODEL, elapsed_ms)
                    return parsed
            logger.warning("[Diagnose] Gemma returned no usable diagnosis (%sms)", elapsed_ms)
        else:
            logger.warning(
                "[Diagnose] Gemma HTTP %s (%sms)",
                response.status_code,
                elapsed_ms,
            )
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("[Diagnose] Gemma request failed: %s", exc)
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
    data = request.json or {}
    image_b64 = data.get("image", "")
    lang      = str(data.get("lang", "en")).strip().lower()

    if not image_b64:
        return jsonify({"error": "No image data received"}), 400
    if len(image_b64) > MAX_IMAGE_B64_LEN:
        return jsonify({"error": "Image too large. Under 10 MB required."}), 413

    logger.info(f"[Diagnose] --- New diagnosis request lang={lang} ---")

    is_plant, reject_reason = ai_is_crop_image(image_b64)
    if not is_plant:
        logger.info("[Diagnose] Rejected: not a crop image")
        return jsonify({"not_a_crop_image": True, "error": reject_reason or "Please upload a photo of a crop/plant."}), 422

    lang_name = LANG_NAMES.get(lang, "")
    prompt = f"""You are an agricultural plant pathologist. Look at this crop image. Return valid JSON only with a diagnosis object using this schema: {{ "disease": "Exact disease name", "confidence": 88, "severity": "Mild|Moderate|Severe", "affected_part": "Leaves|Stem|Fruit|Root", "cause": "Pathogen and cause", "eco_remedies": [{{"remedy": "Name", "method": "Steps", "frequency": "Frequency", "effectiveness": 85}}], "chemical_remedies": [{{"name": "Chemical name", "dose": "Dose", "interval": "Interval"}}], "prevention": ["tip1", "tip2"], "recovery_timeline": "Estimated recovery time", "_english_source": {{ "disease": "English disease name", "confidence": 88, "severity": "English severity", "affected_part": "English plant part", "cause": "English cause", "eco_remedies": [{{"remedy": "English name", "method": "English steps", "frequency": "English frequency", "effectiveness": 85}}], "chemical_remedies": [{{"name": "English chemical name", "dose": "English dose", "interval": "English interval"}}], "prevention": ["English tip"], "recovery_timeline": "English recovery time" }} }}. Write all user-facing text in the diagnosis object in {lang_name or "English"}. The _english_source must describe the same diagnosis and contain all user-facing text in English. Numeric fields must match in both. Do not include analysis or other fields."""
    sys_prompt = "Expert plant pathologist. Return ONLY valid JSON."

    results, models_used = [], []
    if GEMINI_DIAGNOSE_KEY:
        logger.info("[Diagnosis step 2/5] Gemini diagnosis: trying configured model(s).")
        res, used_model = _run_gemini_pass(image_b64, prompt, sys_prompt)
        if res and res.get("disease"):
            results.append(res)
            models_used.append(f"gemini:{used_model}")
            logger.info("[Diagnosis step 2/5] Gemini diagnosis succeeded: model=%s", used_model)
        else:
            logger.warning("[Diagnosis step 2/5] Gemini gave no diagnosis; continuing to Groq.")
    else:
        logger.info("[Diagnosis step 2/5] Gemini diagnosis: skipped (key not configured).")

    if results:
        logger.info("[Diagnosis step 3/5] Groq diagnosis: skipped (Gemini already answered).")
    elif GROQ_API_KEY:
        logger.info(
            "[Diagnosis step 3/5] Groq diagnosis: trying model=%s.",
            vision_models[0],
        )
        res = _run_vision_pass(image_b64, prompt, sys_prompt, vision_models[0], 0.2)
        if res and res.get("disease"):
            results.append(res)
            models_used.append(f"groq:{vision_models[0]}")
            logger.info("[Diagnosis step 3/5] Groq diagnosis succeeded: model=%s", vision_models[0])
        else:
            logger.warning("[Diagnosis step 3/5] Groq gave no diagnosis; continuing to Gemma.")
    else:
        logger.info("[Diagnosis step 3/5] Groq diagnosis: skipped (key not configured).")

    if results:
        logger.info("[Diagnosis step 4/5] Gemma diagnosis: skipped (an earlier provider answered).")
    elif GEMMA_DIAGNOSE_KEY:
        logger.info(
            "[Diagnosis step 4/5] Gemma diagnosis: trying model=%s.",
            GEMMA_DIAGNOSIS_MODEL,
        )
        res = _run_gemma_pass(image_b64, prompt, sys_prompt)
        if res and res.get("disease"):
            results.append(res)
            models_used.append(f"gemma:{GEMMA_DIAGNOSIS_MODEL}")
            logger.info(
                "[Diagnosis step 4/5] Gemma diagnosis succeeded: model=%s",
                GEMMA_DIAGNOSIS_MODEL,
            )
        else:
            logger.warning("[Diagnosis step 4/5] Gemma gave no diagnosis; continuing to offline model.")
    else:
        logger.info("[Diagnosis step 4/5] Gemma diagnosis: skipped (key not configured).")

    used_offline_fallback = not results
    if used_offline_fallback:
        logger.warning(
            "[Diagnosis step 5/5] Hosted providers gave no diagnosis; "
            "trying offline model=plantvillage-15-class."
        )
        try:
            primary = predict_offline_diagnosis(image_b64)
        except OfflineDiagnosisUnavailable as exc:
            logger.error(
                "[Diagnosis step 5/5] Offline model=plantvillage-15-class "
                "failed: %s",
                exc,
            )
            return jsonify({
                "error": "Vision models are unavailable and the local fallback could not process this image."
            }), 503
        models_used.append("offline:plantvillage-15-class")
        logger.info("[Diagnosis step 5/5] Offline model=plantvillage-15-class completed.")
    else:
        logger.info("[Diagnosis step 5/5] Offline model: skipped (hosted provider already answered).")
        primary = max(results, key=lambda r: r.get("confidence", 0))

    answer_source = models_used[0] if models_used else "unknown"
    logger.info(
        "[Diagnosis result] Answered by %s | disease=%s | confidence=%s%%",
        answer_source,
        primary.get("disease", "unknown"),
        primary.get("confidence", "unknown"),
    )

    primary["_lang"] = "en" if used_offline_fallback else lang
    primary["model_agreement"] = not used_offline_fallback
    primary["_passes_run"] = 0 if used_offline_fallback else len(results)
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
    return jsonify({
        "accuracy": None,
        "status": "not_measured",
        "message": "No labeled evaluation dataset has been measured by SmartAgro.",
    })


_diagnose_translation_cache = {}

@diagnose_bp.route("/api/translate-diagnose", methods=["POST"])
def translate_diagnose():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    domain_note = "UI copy and section labels for crop disease diagnosis. Keep simple for farmers."
    translations, _ = translate_terms(DIAGNOSE_UI_TERMS, lang_name, domain_note, lang, _diagnose_translation_cache, lang_code=lang)
    complete = lang in _diagnose_translation_cache
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": complete})


@diagnose_bp.route("/translations/<lang>.json")
def diagnose_translation_file(lang):
    lang = lang.lower()
    if lang not in LANG_NAMES or lang == "en":
        return jsonify({"error": "Translation file not found"}), 404

    path = os.path.join(basedir, "translations", f"{lang}.json")
    if not os.path.isfile(path):
        return jsonify({"error": "Translation file not found"}), 404

    response = send_from_directory(os.path.join(basedir, "translations"), f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as file:
            saved_terms = json.load(file)
        complete = isinstance(saved_terms, dict) and all(
            isinstance(saved_terms.get(term), str) and saved_terms[term]
            for term in DIAGNOSE_UI_TERMS
        )
    except (OSError, json.JSONDecodeError):
        logger.warning("[DiagnoseTranslate] Could not verify translation completeness for %s", lang)
        complete = False
    response.headers["X-Diagnose-Translations-Complete"] = "true" if complete else "false"
    return response


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
        if isinstance(val, str) and val.strip():
            normalized = val.strip()
            if normalized not in terms:
                terms.append(normalized)

    add(result.get("disease"))
    add(result.get("severity"))
    add(result.get("affected_part"))
    add(result.get("cause"))
    add(result.get("recovery_timeline"))
    add(result.get("offline_notice"))
    for r in result.get("eco_remedies") or []:
        add(r.get("remedy")); add(r.get("method")); add(r.get("frequency"))
    for c in result.get("chemical_remedies") or []:
        add(c.get("name")); add(c.get("interval")); add(c.get("dose"))
    for tip in result.get("prevention") or []:
        add(tip)

    if not terms:
        return jsonify({"lang": lang, "translations": {}})

    domain_note = "AI-generated crop disease diagnosis for a farmer. Translate naturally. Keep chemical/brand names, numbers, and units unchanged."
    result_payload = json.dumps(terms, ensure_ascii=False, separators=(",", ":"))
    result_hash = hashlib.sha256(result_payload.encode("utf-8")).hexdigest()
    cache_key = f"{lang}::{result_hash}"
    translations, _ = translate_terms(terms, lang_name, domain_note, cache_key, _diagnosis_result_cache, lang_code=lang)
    complete = cache_key in _diagnosis_result_cache
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": complete})