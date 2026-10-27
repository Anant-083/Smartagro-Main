import os
import sys
import time
import uuid
import json
import socket
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, g, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Force IPv4 on Windows ──────────────────────────────────────────────────
try:
    import urllib3.util.connection as urllib3_conn
    def _allowed_gai_family():
        return socket.AF_INET
    if sys.platform == "win32":
        urllib3_conn.allowed_gai_family = _allowed_gai_family
except Exception as _e:
    pass

# ── Import Shared Config & Services ─────────────────────────────────────────
from shared import (
    basedir, logger, BUILD_VERSION, DEBUG_MODE, ADMIN_SECRET_KEY,
    OPENWEATHER_API_KEY, GROQ_API_KEY, GEMINI_DIAGNOSE_KEY, GEMMA_DIAGNOSE_KEY,
    GEMINI_TRANSLATE_KEY, VISUALCROSSING_API_KEY, DATA_GOV_API_KEY,
    _RASTERIO_AVAILABLE, is_admin_authorized,
    _USAGE_FEATURE_LABELS, load_usage_stats, save_usage_stats,
    flush_usage_stats, track_usage, _usage_lock
)

# ── Import Modular Route Blueprints ─────────────────────────────────────────
from routes import (
    weather_bp,
    market_bp,
    diagnose_bp,
    chat_bp,
    alerts_bp,
)
from offline_diagnosis import MODEL_FILES as OFFLINE_MODEL_FILES

# ── Flask Application Factory & Configuration ───────────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# Gzip / Brotli compression if available
try:
    from flask_compress import Compress
    app.config["COMPRESS_ALGORITHM"] = ["br", "gzip"]
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "text/xml", "application/json",
        "application/javascript", "text/javascript",
    ]
    Compress(app)
except ImportError:
    logger.info("[SmartAgro] flask-compress not installed — running without gzip")

# ── Register Blueprints ─────────────────────────────────────────────────────
app.register_blueprint(weather_bp)
app.register_blueprint(market_bp)
app.register_blueprint(diagnose_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(alerts_bp)

# ── Context Processors & Lifecycle Hooks ────────────────────────────────────
@app.context_processor
def inject_globals():
    return {
        "build_version": BUILD_VERSION,
        "now": datetime.now(),
        "debug": DEBUG_MODE,
    }

@app.before_request
def _request_start():
    g._start = time.monotonic()
    g.request_id = uuid.uuid4().hex[:10]

    # Usage tracking
    if request.path.startswith(("/static/", "/translations/", "/healthz", "/readyz")) or request.path == "/favicon.ico":
        return
    ep = request.endpoint
    if not ep:
        return
    # Strip blueprint name prefix so e.g. 'weather.get_weather' matches 'get_weather'
    ep_key = ep.split(".")[-1]
    if ep_key in _USAGE_FEATURE_LABELS:
        label, kind = _USAGE_FEATURE_LABELS[ep_key]
        track_usage(ep_key, label=label, kind=kind)
    elif ep_key not in ("static", "service_worker", "usage", "usage_api", "usage_reset"):
        track_usage(ep_key, label=ep_key, kind="api" if request.path.startswith("/api/") else "page")

@app.after_request
def _request_end(response):
    dur_ms = (time.monotonic() - g.get("_start", time.monotonic())) * 1000
    logger.info("%s %s -> %s (%.0fms) rid=%s",
                request.method, request.path, response.status_code, dur_ms,
                g.get("request_id", "-"))
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    elif request.path.startswith("/translations/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response

# ── Error Handlers ──────────────────────────────────────────────────────────
@app.errorhandler(413)
def _request_too_large(e):
    return jsonify({"error": "Request body too large. Maximum 16 MB allowed."}), 413

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Endpoint not found"}), 404
    return render_template("offline.html"), 404

@app.errorhandler(500)
def _internal_error(e):
    logger.error(f"[App] Internal Server Error: {e}")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("offline.html"), 500

# ── Frontend Web Page Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/diagnose")
def diagnose():
    return render_template("diagnose.html")

@app.route("/market")
def market():
    return render_template("market.html")

@app.route("/alerts")
def alerts():
    return render_template("alerts.html")

@app.route("/offline")
def offline():
    return render_template("offline.html")

# ── Usage Analytics (Protected) ─────────────────────────────────────────────
@app.route("/usage")
def usage():
    if not is_admin_authorized(request):
        return jsonify({"error": "Forbidden: Usage dashboard is restricted to administrators."}), 403
    return render_template("usage.html")

@app.route("/api/usage")
def usage_api():
    if not is_admin_authorized(request):
        return jsonify({"error": "Forbidden: Unauthorized."}), 403
    flush_usage_stats()
    with _usage_lock:
        stats = json.loads(json.dumps(load_usage_stats()))
    features = stats.get("features", {})
    rows = sorted(features.items(), key=lambda kv: -kv[1]["count"])
    total = sum(rec["count"] for _, rec in rows)
    by_kind = {}
    for _, rec in rows:
        by_kind[rec["kind"]] = by_kind.get(rec["kind"], 0) + rec["count"]
    return jsonify({
        "since":   stats.get("since"),
        "total":   total,
        "by_kind": by_kind,
        "daily":   stats.get("daily", {}),
        "features": [
            {"endpoint": ep, "label": rec.get("label", ep), "kind": rec.get("kind", "api"),
             "count": rec.get("count", 0), "last_used": rec.get("last_used")}
            for ep, rec in rows
        ],
    })

@app.route("/api/usage/reset", methods=["POST"])
def usage_reset():
    if not is_admin_authorized(request):
        return jsonify({"error": "Forbidden: Resetting usage counters requires authorization."}), 403
    with _usage_lock:
        stats = load_usage_stats()
        stats["since"] = datetime.now().isoformat(timespec="seconds")
        stats["features"] = {}
        stats["daily"] = {}
        save_usage_stats()
    return jsonify({"ok": True})

# ── Health & Readiness Probes ───────────────────────────────────────────────
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route("/readyz")
def readyz():
    return jsonify({
        "status":           "ok",
        "groq":             bool(GROQ_API_KEY),
        "openweather":      bool(OPENWEATHER_API_KEY),
        "gemini_diagnose":  bool(GEMINI_DIAGNOSE_KEY),
        "gemma_diagnose":   bool(GEMMA_DIAGNOSE_KEY),
        "gemini_translate": bool(GEMINI_TRANSLATE_KEY),
        "ndvi":             _RASTERIO_AVAILABLE,
    }), 200


def log_local_startup_status():
    def configured(name, available):
        state = "READY: configured" if available else "NOT READY: key not set"
        logger.info("  %s: %s", name, state)

    offline_ready = all(path.is_file() for path in OFFLINE_MODEL_FILES)
    offline_state = (
        "READY: model files found (checksums are verified on first use)"
        if offline_ready else
        "NOT READY: model files missing; run scripts/download_offline_diagnosis_model.py"
    )

    logger.info("SmartAgro startup check (configuration only; external APIs are not being pinged):")
    logger.info("  Web app: READY")
    logger.info("  Weather: current weather %s; forecast %s",
                "configured" if OPENWEATHER_API_KEY else "key not set",
                "configured" if VISUALCROSSING_API_KEY else "key not set")
    logger.info("  Crop diagnosis providers, in order:")
    configured("Gemini", GEMINI_DIAGNOSE_KEY)
    configured("Groq", GROQ_API_KEY)
    configured("Gemma", GEMMA_DIAGNOSE_KEY)
    logger.info("  Offline crop model: %s", offline_state)
    configured("Translation with Gemini", GEMINI_TRANSLATE_KEY)
    logger.info("  Satellite crop health: %s",
                "READY: library installed" if _RASTERIO_AVAILABLE else "NOT READY: rasterio is not installed")
    logger.info("  Market prices: %s",
                "government API key configured" if DATA_GOV_API_KEY else "shared public key fallback will be used")


# ── PWA Service Worker ──────────────────────────────────────────────────────
@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory("static", "service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Content-Type"] = "application/javascript"
    return response

# ── Application Runner ──────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DEBUG_MODE or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        log_local_startup_status()
    app.run(host="0.0.0.0", port=7860, debug=DEBUG_MODE)