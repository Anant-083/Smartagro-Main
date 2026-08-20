from flask import Flask, render_template, request, jsonify, g, send_file
from concurrent.futures import ThreadPoolExecutor
import requests
import logging
import uuid

# ── Windows fix: force IPv4 for outbound requests ───────────────────────────
# If a browser reaches a URL instantly but Python's `requests` times out on 
# the exact same URL, it is almost always because requests/urllib3 tries
# IPv6 first and your network's IPv6 path is broken or very slow, while the
# browser silently falls back to IPv4 in milliseconds. This forces Python's
# HTTP stack to only use IPv4, matching what the browser effectively does.
try:
    import socket
    import urllib3.util.connection as urllib3_conn

    def _allowed_gai_family():
        return socket.AF_INET  # IPv4 only

    urllib3_conn.allowed_gai_family = _allowed_gai_family
except Exception as _e:
    logger.info(f"[AgroSmart] Could not force IPv4 (non-fatal): {_e}")
import os
import json
import re
import time
import calendar
import base64
import hashlib
import difflib
import threading
import concurrent.futures
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ── Optional heavy deps for Sentinel-2 NDVI ─────────────────────────────────
try:
    import rasterio
    import numpy as np
    from rasterio.transform import rowcol
    from rasterio.warp import transform as rasterio_warp
    _RASTERIO_AVAILABLE = True
except ImportError:
    _RASTERIO_AVAILABLE = False
    logger.info("[AgroSmart] rasterio not installed – NDVI will fall back to estimation")

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# ── Global request body size cap ────────────────────────────────────────────
# The diagnose route already checks image size manually, but every other
# JSON-accepting route (chat, alerts, translate-*) had no cap at all, so an
# oversized POST body could tie up memory/CPU on our constrained Render
# instance. 12 MB covers the largest legitimate payload (diagnose image
# base64, ~10MB cap) with headroom, and rejects anything bigger before Flask
# even parses it.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB


@app.errorhandler(413)
def _request_too_large(e):
    return jsonify({"error": "Request body too large."}), 413

# ── Structured logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smartagro")


# ── Bounded in-memory cache helper ──────────────────────────────────────────
# Several endpoints keep small in-memory dicts (translation results, weather,
# crop-AI suggestions, market prices, alerts) that live for the lifetime of
# the process. None of them ever evicted, so on a long-running Render
# instance with limited RAM they grow without bound as more
# languages/cities/crops/images get requested over days/weeks. This caps
# each cache dict at `max_entries` by evicting the oldest-inserted key once
# the limit is hit (simple FIFO — good enough for these use cases, no need
# for real LRU tracking).
def _bounded_cache_set(cache_dict: dict, key, value, max_entries: int = 200):
    if key not in cache_dict and len(cache_dict) >= max_entries:
        oldest_key = next(iter(cache_dict))
        cache_dict.pop(oldest_key, None)
    cache_dict[key] = value


@app.before_request
def _request_start():
    g._start = time.monotonic()
    g.request_id = uuid.uuid4().hex[:10]


@app.after_request
def _log_request(response):
    dur_ms = (time.monotonic() - g.get("_start", time.monotonic())) * 1000
    logger.info("%s %s -> %s (%.0fms) rid=%s",
                request.method, request.path, response.status_code, dur_ms,
                g.get("request_id", "-"))
    return response

# ── Build version for cache-busting ─────────────────────────────────────────
# Static JS/CSS is cached by browsers for 7 days (see below). Without a
# version marker, a fresh deploy's fixes silently never reach anyone whose
# browser already cached the old files. This value changes every time the
# server restarts (i.e. every deploy), so appending it as a query string
# (?v=...) on every <script>/<link> tag forces a fresh download exactly once
# per deploy, while still letting the 7-day cache work normally in between.
BUILD_VERSION = str(int(time.time()))


@app.context_processor
def inject_build_version():
    return {"build_version": BUILD_VERSION}


# ── Gzip/Brotli-compress every response (HTML/JS/CSS/JSON) ─────────────────
# translations.js alone is ~177KB uncompressed; gzip typically cuts JS/JSON
# text payloads by 70-80%. This is the single biggest lever for "everything
# is slow to fetch" on mobile/rural connections, and costs almost nothing
# in CPU. Falls back gracefully if the package isn't installed yet.
try:
    from flask_compress import Compress
    app.config["COMPRESS_ALGORITHM"] = ["br", "gzip"]
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "text/xml", "application/json",
        "application/javascript", "text/javascript",
    ]
    Compress(app)
except ImportError:
    logger.warning("[AgroSmart] flask-compress not installed — responses will NOT be gzipped. "
          "Run: pip install flask-compress")

# ── Long-lived caching for static assets ────────────────────────────────────
# Flask's default static handler doesn't set a real Cache-Control header, so
# every page navigation (dashboard -> market -> alerts) re-downloads the same
# 330KB+ of JS from scratch. This lets the browser cache it for a week.
@app.after_request
def _add_static_cache_headers(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "")
NINJA_API_KEY       = os.getenv("NINJA_API_KEY", "")  # no longer used by /api/market (kept for backward-compat only)
DEBUG_MODE          = os.getenv("FLASK_DEBUG", "0") == "1"

# Gemini is used as a genuinely INDEPENDENT second vision model in the crop
# diagnosis ensemble. Only active when GEMINI_API_KEY is set in .env.
GEMINI_DIAGNOSIS_MODEL = os.getenv("GEMINI_DIAGNOSIS_MODEL", "gemini-3.1-flash-lite")

# ── Per-feature usage analytics ─────────────────────────────────────────────
# Tracks how often each SmartAgro feature is used (page views + API calls) as
# aggregate counters — NO personal data, NO IPs, NO message content. Counters
# live in memory and are persisted atomically to a JSON file in batches.
USAGE_LOG_PATH = os.path.join(basedir, "usage_stats.json")
_USAGE_SAVE_INTERVAL = 30  # seconds between automatic disk writes

_usage_stats = None
_usage_lock = threading.Lock()
_last_usage_save = 0.0

_USAGE_FEATURE_LABELS = {
    "index":                       ("Dashboard Page",               "page"),
    "diagnose":                    ("Crop Diagnosis Page",         "page"),
    "market":                      ("Market Page",                 "page"),
    "alerts":                      ("Alerts Page",                 "page"),
    "offline":                     ("Offline Page",                "page"),
    "get_ndvi":                    ("Satellite NDVI",              "api"),
    "get_weather":                 ("Live Weather",                "api"),
    "crop_recommendations":        ("Crop Recommendations",        "api"),
    "get_market_data":             ("Mandi Market Prices",         "api"),
    "debug_market":                ("Market Debug",                "api"),
    "kisan_chat":                  ("Kisan Helper Chat",           "api"),
    "speech_to_text":              ("Voice Input (STT)",           "api"),
    "diagnose_crop":               ("Crop Diagnosis",              "api"),
    "diagnose_log":                ("Diagnosis QA Log",            "api"),
    "diagnose_log_image":          ("Diagnosis QA Image",          "api"),
    "diagnose_log_review":         ("Diagnosis Review",            "api"),
    "diagnose_log_accuracy":       ("Diagnosis Accuracy",          "api"),
    "get_alerts":                  ("Instant Alerts",              "api"),
    "alerts_forecast":             ("6-Day Forecast Alerts",       "api"),
    "monthly_alerts":              ("Monthly Outlook Alerts",      "api"),
    "seasonal_alerts":             ("Seasonal Advisories",         "api"),
    "crop_risk":                   ("Crop Risk / Harvest Window",  "api"),
    "translate_market":            ("Market Translation",          "api"),
    "clear_translation_cache":     ("Translation Cache Clear",     "api"),
    "translate_alerts":            ("Alerts Translation",          "api"),
    "translate_dashboard":         ("Dashboard Translation",       "api"),
    "translate_diagnose":          ("Diagnose Translation",        "api"),
    "translate_diagnosis_result":  ("Diagnosis Result Translation","api"),
}


def _load_usage_stats():
    global _usage_stats
    if _usage_stats is not None:
        return
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            _usage_stats = json.load(f)
    except Exception:
        _usage_stats = {}
    _usage_stats.setdefault("since", datetime.now().isoformat(timespec="seconds"))
    _usage_stats.setdefault("features", {})
    _usage_stats.setdefault("daily", {})


def _save_usage_stats():
    if _usage_stats is None:
        return
    try:
        tmp_path = USAGE_LOG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_usage_stats, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, USAGE_LOG_PATH)
    except Exception as e:
        logger.warning(f"[Usage] Could not persist usage stats: {e}")


def _flush_usage_stats():
    global _last_usage_save
    with _usage_lock:
        _load_usage_stats()
        _save_usage_stats()
        _last_usage_save = time.monotonic()


def _track_usage(endpoint, label=None, kind="api"):
    global _last_usage_save
    now = datetime.now()
    with _usage_lock:
        _load_usage_stats()
        feats = _usage_stats["features"]
        rec = feats.setdefault(endpoint, {
            "label": label or endpoint, "kind": kind, "count": 0,
        })
        rec["count"] += 1
        rec["last_used"] = now.isoformat(timespec="seconds")
        today = now.strftime("%Y-%m-%d")
        _usage_stats["daily"][today] = _usage_stats["daily"].get(today, 0) + 1
        if time.monotonic() - _last_usage_save >= _USAGE_SAVE_INTERVAL:
            _last_usage_save = time.monotonic()
            _save_usage_stats()


@app.before_request
def _track_usage_request():
    ep = request.endpoint or ""
    if not ep or ep == "static" or ep in ("usage", "usage_api", "usage_reset", "healthz", "readyz"):
        return
    label, kind = _USAGE_FEATURE_LABELS.get(ep, (None, "api"))
    _track_usage(ep, label if label is not None else ep, kind)

# ── Diagnosis QA logging ─────────────────────────────────────────────────
# Every diagnosis (image + full model output, from every ensemble pass) is
# persisted here so a human can spot-check the AI against the real photo
# later. This is the "proof of accuracy" pipeline.
DIAGNOSIS_LOG_DIR    = os.getenv("DIAGNOSIS_LOG_DIR", os.path.join(os.path.expanduser("~"), "SmartAgro_Logs"))
DIAGNOSIS_IMAGES_DIR = os.path.join(DIAGNOSIS_LOG_DIR, "images")
DIAGNOSIS_LOG_PATH   = os.path.join(DIAGNOSIS_LOG_DIR, "log.jsonl")
os.makedirs(DIAGNOSIS_IMAGES_DIR, exist_ok=True)
_diagnosis_log_lock = threading.Lock()

# Number of independent diagnosis passes to run and cross-check per image.
ENSEMBLE_PASSES = 2

_translation_cache = {}

LANG_NAMES = {
    "en":"English","hi":"Hindi","bn":"Bengali","te":"Telugu","mr":"Marathi",
    "ta":"Tamil","gu":"Gujarati","kn":"Kannada","ml":"Malayalam","pa":"Punjabi",
    "or":"Odia","as":"Assamese","ur":"Urdu","mai":"Maithili","sat":"Santali",
    "ks":"Kashmiri","ne":"Nepali","sd":"Sindhi","kok":"Konkani","mni":"Manipuri",
    "bodo":"Bodo","doi":"Dogri","sa":"Sanskrit",
}

logger.info(f"[AgroSmart] Groq key:    {'OK (' + GROQ_API_KEY[:8] + '...)' if GROQ_API_KEY else 'MISSING'}")
logger.info(f"[AgroSmart] Weather key: {'OK' if OPENWEATHER_API_KEY else 'MISSING'}")
logger.info(f"[AgroSmart] Ninja key:   {'OK (' + NINJA_API_KEY[:8] + '...)' if NINJA_API_KEY else 'MISSING'}")
logger.info(f"[AgroSmart] Sentinel-2 NDVI: {'ENABLED (rasterio available)' if _RASTERIO_AVAILABLE else 'DISABLED (install rasterio)'}")


# ─── Sentinel-2 Real NDVI (via Earth Search STAC + COG pixel read) ───────────
# Cache: keyed by rounded lat/lon grid (0.01° ≈ 1 km), TTL = 6 hours
_NDVI_CACHE_PATH = os.path.join(basedir, "ndvi_cache.json")
_NDVI_CACHE_TTL = 6 * 3600  # seconds

def _load_ndvi_cache():
    try:
        with open(_NDVI_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ndvi_cache(cache):
    try:
        with open(_NDVI_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.info(f"[NDVI] Could not persist cache: {e}")

_ndvi_cache: dict = _load_ndvi_cache()

def _ndvi_status(ndvi: float) -> str:
    """Convert NDVI value to human-readable vegetation status label."""
    if ndvi < 0.0:
        return "Water / Cloud / No Data"
    elif ndvi < 0.1:
        return "Bare Soil / Urban / Rock"
    elif ndvi < 0.2:
        return "Sparse Vegetation / Bare Soil"
    elif ndvi < 0.4:
        return "Moderate Vegetation"
    elif ndvi < 0.6:
        return "Good Vegetation Cover"
    else:
        return "Dense / Healthy Vegetation"


def get_sentinel2_ndvi(lat: float, lon: float) -> dict | None:
    """
    Fetch a real NDVI value at (lat, lon) from Sentinel-2 L2A imagery.

    Pipeline:
      1. Query the free Element84 Earth Search STAC API for the 10 most recent
         Sentinel-2 L2A scenes that cover the point, filtered to ≤40 % cloud cover.
         Falls back to ≤80 % cloud if nothing cleaner is available.
      2. Pick the scene with the lowest cloud cover.
      3. Use rasterio's /vsicurl/ driver to open the Red (B04) and NIR (B08)
         Cloud-Optimized GeoTIFFs directly over HTTP without downloading the full
         tile – only a small 5×5 pixel window centred on the point is streamed.
      4. Compute NDVI = (NIR − Red) / (NIR + Red) and return metadata.

    Returns a dict with keys: ndvi, status, obs_date, source, cloud_pct
    or None on any failure (the caller should degrade gracefully).
    """
    if not _RASTERIO_AVAILABLE:
        return None  # rasterio not installed

    # ── Cache lookup ─────────────────────────────────────────────────────────
    cache_key = f"{round(lat, 2)},{round(lon, 2)}"
    now = time.monotonic()
    cached = _ndvi_cache.get(cache_key)
    if cached and (now - cached["ts"]) < _NDVI_CACHE_TTL:
        logger.info(f"[NDVI] Cache hit for {cache_key}")
        return cached["data"]

    STAC_URL = "https://earth-search.aws.element84.com/v1/search"

    def _query_stac(max_cloud: int) -> list:
        payload = {
            "collections": ["sentinel-2-l2a"],
            "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "limit": 10,
            "query": {"eo:cloud_cover": {"lte": max_cloud}},
        }
        try:
            resp = requests.post(STAC_URL, json=payload, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("features", [])
        except Exception as exc:
            logger.warning(f"[NDVI] STAC query error: {exc}")
        return []

    # Try progressively relaxed cloud thresholds
    features = _query_stac(30)
    if not features:
        features = _query_stac(50)
    if not features:
        features = _query_stac(80)
    if not features:
        logger.info("[NDVI] No Sentinel-2 scenes found for location")
        return None

    # Best = least cloudy available
    item = min(features, key=lambda x: x["properties"].get("eo:cloud_cover", 100))
    obs_date = item["properties"]["datetime"][:10]
    cloud_pct = round(item["properties"].get("eo:cloud_cover", 0), 1)
    red_url = item["assets"]["red"]["href"]    # B04 – 10 m COG
    nir_url = item["assets"]["nir"]["href"]    # B08 – 10 m COG
    logger.info(f"[NDVI] Using scene {obs_date}, cloud={cloud_pct}%, loc=({lat},{lon})")

    def _read_cog_pixel(cog_url: str) -> float:
        """Open a COG via /vsicurl/ and stream just a 5×5 pixel neighbourhood."""
        with rasterio.open(f"/vsicurl/{cog_url}") as src:
            # Reproject WGS-84 lat/lon to the image's native CRS (usually UTM)
            xs, ys = rasterio_warp("EPSG:4326", src.crs, [lon], [lat])
            col_px, row_px = rowcol(src.transform, xs[0], ys[0])
            win = rasterio.windows.Window(
                max(0, col_px - 2), max(0, row_px - 2), 5, 5
            )
            data = src.read(1, window=win).astype(float)
        valid = data[data > 0]
        return float(np.nanmean(valid)) if valid.size > 0 else 0.0

    try:
        env_opts = {
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_USE_HEAD":       "NO",
            "GDAL_HTTP_TIMEOUT":            "20",
        }
        with rasterio.Env(**env_opts):
            with ThreadPoolExecutor(max_workers=2) as ex:
                red_future = ex.submit(_read_cog_pixel, red_url)
                nir_future = ex.submit(_read_cog_pixel, nir_url)
                red_val = red_future.result()
                nir_val = nir_future.result()
    except Exception as exc:
        logger.warning(f"[NDVI] COG read error: {exc}")
        return None

    if (nir_val + red_val) == 0:
        logger.info("[NDVI] Zero-valued pixels – possibly outside scene bounds")
        return None

    raw_ndvi = (nir_val - red_val) / (nir_val + red_val)
    # Clamp to valid NDVI range (cloud/water artefacts can go < -1)
    ndvi = round(max(-1.0, min(1.0, raw_ndvi)), 3)
    status = _ndvi_status(ndvi)

    result = {
        "ndvi":      ndvi,
        "status":    status,
        "obs_date":  obs_date,
        "source":    "Copernicus Sentinel-2 L2A",
        "cloud_pct": cloud_pct,
    }
    _bounded_cache_set(_ndvi_cache, cache_key, {"ts": now, "data": result}, max_entries=500)
    _save_ndvi_cache(_ndvi_cache)
    logger.info(f"[NDVI] Result: NDVI={ndvi}, status='{status}'")
    return result


# ─── Routes ──────────────────────────────────────────────────────────────────
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

@app.route('/offline')
def offline():
    return render_template('offline.html')


# ─── Usage Analytics (per-feature) ───────────────────────────────────────────
@app.route("/usage")
def usage():
    return render_template("usage.html")


@app.route("/api/usage")
def usage_api():
    _flush_usage_stats()
    with _usage_lock:
        stats = json.loads(json.dumps(_usage_stats))
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
    global _usage_stats, _last_usage_save
    with _usage_lock:
        _usage_stats = {
            "since": datetime.now().isoformat(timespec="seconds"),
            "features": {},
            "daily": {},
        }
        _save_usage_stats()
        _last_usage_save = time.monotonic()
    return jsonify({"ok": True})


# ─── Health / readiness probes ───────────────────────────────────────────────
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@app.route("/readyz")
def readyz():
    return jsonify({
        "status":      "ok",
        "groq":        bool(GROQ_API_KEY),
        "openweather": bool(OPENWEATHER_API_KEY),
        "gemini":      bool(GEMINI_API_KEY),
        "ndvi":        _RASTERIO_AVAILABLE,
    }), 200


# ─── Weather API ─────────────────────────────────────────────────────────────
# ─── Open-Meteo — real extended forecast (free, no key, up to 16 days) ──────
# OpenWeather's free tier only gives ~5-6 real forecast days. Open-Meteo is
# free with no API key and no rate limit, and genuinely provides up to 16
# real daily forecast days — used here to extend the Alerts 30-day calendar
# with real data for roughly half its span instead of leaving those days
# blank/unavailable. Days beyond 16 still have no real forecast anywhere and
# are honestly reported as unavailable, never invented.
_WMO_CODE_MAP = {
    0:  ("clear sky", "01"),          1:  ("mainly clear", "01"),
    2:  ("partly cloudy", "02"),      3:  ("overcast", "04"),
    45: ("fog", "50"),                48: ("depositing rime fog", "50"),
    51: ("light drizzle", "09"),      53: ("moderate drizzle", "09"),
    55: ("dense drizzle", "09"),      56: ("light freezing drizzle", "09"),
    57: ("dense freezing drizzle", "09"),
    61: ("slight rain", "10"),        63: ("moderate rain", "10"),
    65: ("heavy rain", "10"),         66: ("light freezing rain", "10"),
    67: ("heavy freezing rain", "10"),
    71: ("slight snow fall", "13"),   73: ("moderate snow fall", "13"),
    75: ("heavy snow fall", "13"),    77: ("snow grains", "13"),
    80: ("slight rain showers", "09"),   81: ("moderate rain showers", "09"),
    82: ("violent rain showers", "09"),
    85: ("slight snow showers", "13"),   86: ("heavy snow showers", "13"),
    95: ("thunderstorm", "11"),
    96: ("thunderstorm with slight hail", "11"),
    99: ("thunderstorm with heavy hail", "11"),
}


def _openmeteo_wmo_info(code):
    desc, icon_base = _WMO_CODE_MAP.get(code, ("", "01"))
    return desc, icon_base + "d"


# Simple in-memory cache for Open-Meteo forecasts, keyed by location rounded
# to ~1km precision. Open-Meteo's free tier has a daily request quota that's
# shared across everyone on the same hosting IP (common on free-tier hosts
# like Render), so caching is essential — without it, every single page
# load/navigation was making a fresh call and exhausting the quota fast.
_openmeteo_cache = {}
_openmeteo_rate_limited_until = 0  # monotonic timestamp; skip calls until this passes
_OPENMETEO_CACHE_TTL = 6 * 3600  # 6 hours — forecasts don't change that fast


def _fetch_openmeteo_forecast(lat, lon):
    """Real daily forecast (up to 16 days) from Open-Meteo. Returns [] on any
    failure — callers must treat a missing day as unavailable, never guess."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    # Rounded to 1 decimal (~11km) rather than 2 (~1.1km) so nearby farmers
    # share the same cache entry — this matters a lot on the free tier,
    # since every distinct rounded coordinate burns a separate call against
    # Open-Meteo's daily quota. Precision at this level is still far tighter
    # than a day-level forecast actually varies over 11km.
    cache_key = (round(lat_f, 1), round(lon_f, 1))
    cached = _openmeteo_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _OPENMETEO_CACHE_TTL:
        return cached["days"]

    # Already know we're rate-limited for today — don't waste a call
    # confirming that again, just serve whatever's cached (even if stale)
    # or give up honestly.
    global _openmeteo_rate_limited_until
    if time.monotonic() < _openmeteo_rate_limited_until:
        return cached["days"] if cached else []

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,windspeed_10m_max,relative_humidity_2m_mean"
            "&timezone=auto&forecast_days=16"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code == 429:
            # Daily quota genuinely exhausted — stop retrying for a few
            # hours instead of burning more of tomorrow's quota re-checking
            # a limit we already know is hit.
            _openmeteo_rate_limited_until = time.monotonic() + 3 * 3600
            logger.warning(f"[Open-Meteo] 429 rate limited — pausing calls for 3h. {resp.text[:200]}")
            return cached["days"] if cached else []
        if resp.status_code != 200:
            # Other failure — serve stale cached data if we have any rather
            # than nothing, since a forecast from a few hours ago is still
            # far more useful than no data at all.
            logger.info(f"[Open-Meteo] non-200 status {resp.status_code}: {resp.text[:200]}")
            return cached["days"] if cached else []
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        out = []
        for i, date_str in enumerate(dates):
            try:
                code = daily["weathercode"][i]
                desc, icon = _openmeteo_wmo_info(code)
                out.append({
                    "date":        date_str,
                    "temp_max":    daily["temperature_2m_max"][i],
                    "temp_min":    daily["temperature_2m_min"][i],
                    "description": desc,
                    "icon":        icon,
                    "humidity":    (daily.get("relative_humidity_2m_mean") or [60]*len(dates))[i],
                    "wind_speed":  (daily.get("windspeed_10m_max") or [10]*len(dates))[i],
                    "rain":        (daily.get("precipitation_sum") or [0]*len(dates))[i],
                    "source":      "open-meteo",
                })
            except (IndexError, KeyError, TypeError):
                continue
        _bounded_cache_set(_openmeteo_cache, cache_key, {"ts": time.time(), "days": out}, max_entries=300)
        return out
    except Exception as e:
        logger.warning(f"[Open-Meteo error] {e}")
        return cached["days"] if cached else []


@app.route("/api/debug-openmeteo")
def debug_openmeteo():
    """Diagnostic-only endpoint — open this URL directly in a browser to see
    exactly what Open-Meteo returns (or what error it throws) from this
    server, without needing to dig through Render's log viewer.
    Gated behind DEBUG_MODE like /api/debug-market, so it isn't reachable
    in production."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    lat = request.args.get("lat", "22.57")
    lon = request.args.get("lon", "88.36")
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,windspeed_10m_max,relative_humidity_2m_mean"
            "&timezone=auto&forecast_days=16"
        )
        resp = requests.get(url, timeout=8)
        raw_snippet = resp.text[:500]
        days = _fetch_openmeteo_forecast(lat, lon)
        return jsonify({
            "request_url":      url,
            "http_status":      resp.status_code,
            "raw_response_snippet": raw_snippet,
            "parsed_day_count": len(days),
            "parsed_days":      days[:3],
        })
    except Exception as e:
        return jsonify({"error": str(e), "error_type": type(e).__name__}), 500


@app.route("/api/weather")
def get_weather():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400


    current_url  = (f"https://api.openweathermap.org/data/2.5/weather"
                    f"?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric")
    forecast_url = (f"https://api.openweathermap.org/data/2.5/forecast"
                    f"?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric&cnt=56")

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            current_future   = ex.submit(requests.get, current_url,  timeout=10)
            forecast_future   = ex.submit(requests.get, forecast_url, timeout=10)
            openmeteo_future  = ex.submit(_fetch_openmeteo_forecast, lat, lon)
            current_resp  = current_future.result()
            forecast_resp = forecast_future.result()
            openmeteo_days = openmeteo_future.result()

        if current_resp.status_code == 429 or forecast_resp.status_code == 429:
            return jsonify({
                "error": "OpenWeather API Rate Limit Reached!",
                "limit_reached": True,
                "api_name": "OpenWeather API",
                "details": "OpenWeather API returned an HTTP 429 Rate Limit error. Free tier daily or minute quota reached."
            }), 429

        if current_resp.status_code != 200:
            return jsonify({"error": f"Weather API error: {current_resp.text}"}), 500

        current_data  = current_resp.json()
        forecast_data = forecast_resp.json()

        daily = {}
        if forecast_data.get("list"):
            for item in forecast_data["list"]:
                day = datetime.fromtimestamp(item["dt"]).strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {
                        "date":        day,
                        "temp_max":    item["main"]["temp_max"],
                        "temp_min":    item["main"]["temp_min"],
                        "description": item["weather"][0]["description"],
                        "icon":        item["weather"][0]["icon"],
                        "humidity":    item["main"]["humidity"],
                        "wind_speed":  item["wind"]["speed"],
                        "rain":        item.get("rain", {}).get("3h", 0),
                    }
                else:
                    if item["main"]["temp_max"] > daily[day]["temp_max"]:
                        daily[day]["temp_max"] = item["main"]["temp_max"]
                    if item["main"]["temp_min"] < daily[day]["temp_min"]:
                        daily[day]["temp_min"] = item["main"]["temp_min"]

        # Real OpenWeather days first (more precise, updated more often) —
        # never fabricated. The free /forecast endpoint returns 3-hour steps
        # up to 5 days (~5-6 daily buckets depending on the hour of the
        # request), so this length can legitimately vary.
        openweather_days = sorted(daily.values(), key=lambda d: d["date"])
        for d in openweather_days:
            d["source"] = "openweather"

        # Extend with real Open-Meteo days for any date OpenWeather doesn't
        # already cover, up to 16 total days. Still real forecast data, not
        # invented — just a second free, keyless source with longer real
        # range than OpenWeather's free tier gives.
        covered_dates = {d["date"] for d in openweather_days}
        extra_days = [d for d in openmeteo_days if d["date"] not in covered_dates]
        forecast_list = sorted(openweather_days + extra_days, key=lambda d: d["date"])[:16]
        logger.info(f"[Weather] openweather_days={len(openweather_days)} "
              f"openmeteo_days={len(openmeteo_days)} "
              f"merged_total={len(forecast_list)}")

        return jsonify({
            "current": {
                "city":        current_data.get("name", "Your Location"),
                "lat":         float(lat),
                "lon":         float(lon),
                "temp":        round(current_data["main"]["temp"]),
                "feels_like":  round(current_data["main"]["feels_like"]),
                "humidity":    current_data["main"]["humidity"],
                "description": current_data["weather"][0]["description"],
                "icon":        current_data["weather"][0]["icon"],
                "wind_speed":  current_data["wind"]["speed"],
                "pressure":    current_data["main"]["pressure"],
                "visibility":  current_data.get("visibility", 0) / 1000,
                "rain":        current_data.get("rain", {}).get("1h", 0),
            },
            "forecast": forecast_list,
        })
    except Exception as e:
        logger.warning(f"[Weather error] {e}")
        return jsonify({"error": str(e)}), 500


# ─── Vegetation / NDVI ────────────────────────────────────────────────────
# Split out from /api/weather on purpose: a real Sentinel-2 lookup can involve
# several sequential network calls to STAC + streamed satellite image reads.
# That's fine as its OWN request, loaded in the background after the page's
# main content (weather, crop recs) is already showing — but it must never
# be allowed to hold up the initial page load itself, which is what was
# happening when this ran inline inside /api/weather.
_NDVI_REQUEST_BUDGET_SEC = 8  # hard cap: give up and report "unavailable" past this


@app.route("/api/vegetation")
def get_vegetation():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400

    def _fallback():
        return {
            "ndvi": None, "status": "Data Unavailable", "obs_date": None,
            "source": "Satellite", "cloud_pct": None,
        }

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(get_sentinel2_ndvi, float(lat), float(lon))
            try:
                ndvi_result = future.result(timeout=_NDVI_REQUEST_BUDGET_SEC)
            except concurrent.futures.TimeoutError:
                # Real satellite pipelines can occasionally stall on a slow
                # upstream host — report honestly rather than hang the tab.
                logger.info(f"[NDVI] Timed out after {_NDVI_REQUEST_BUDGET_SEC}s for ({lat},{lon})")
                return jsonify(_fallback())

        if not ndvi_result:
            return jsonify(_fallback())

        return jsonify({
            "ndvi":      ndvi_result["ndvi"],
            "status":    ndvi_result["status"],
            "obs_date":  ndvi_result["obs_date"],
            "source":    ndvi_result["source"],
            "cloud_pct": ndvi_result.get("cloud_pct", None),
        })
    except Exception as e:
        logger.warning(f"[Vegetation error] {e}")
        return jsonify(_fallback())


# ─── Crop Recommendations ────────────────────────────────────────────────────
_crop_ai_cache = {}
CROP_AI_CACHE_TTL_SEC = 3 * 60 * 60  # 3 hours — same city/season/weather bucket repeats a lot in a day


def ai_recommend_crops(city, lat, lon, temp, humidity, rain, season):
    """Ask Groq for crops genuinely suited to THIS location's climate, soil
    region and season. Returns None on any failure so the caller can fall
    back to rule-based recommend_crops() and the dashboard never breaks."""
    if not GROQ_API_KEY:
        return None

    cache_key = f"{city}|{round((lat or 0), 1)}|{round((lon or 0), 1)}|{season}|{round(temp/3)*3}|{round(humidity/10)*10}"
    now = time.monotonic()
    cached = _crop_ai_cache.get(cache_key)
    if cached and (now - cached[0]) < CROP_AI_CACHE_TTL_SEC:
        return cached[1]

    prompt = f"""You are an expert Indian agronomist advising a farmer in India.

Location / Place: {city or "an unspecified Indian region"} (approx. lat {lat}, lon {lon})
Current season: {season}
Current live weather right now: {temp} deg C, {humidity}% humidity, {rain} mm recent rainfall

CRITICAL INSTRUCTION: You MUST recommend EXACTLY 6 DIFFERENT crops best suited to THIS exact location's climate, soil region, and live weather. Do NOT return only 1 or 2 crops!

Do NOT recommend tobacco, opium poppy, cannabis/hemp, or any other controlled, licensed-only, or health-sensitive crop, even if agronomically suited to the region — this app only recommends common food, cash, and commercial crops a general farmer can grow without special government licensing.

Use your knowledge of Indian agro-climatic zones (e.g. black cotton soil across Maharashtra/Deccan, alluvial soil in the Indo-Gangetic plain, laterite soil along coastal belts, arid/sandy soil in Rajasthan, red soil in South India, etc.) to pick 6 realistic, regionally-appropriate crops, ordered from best to weakest fit for THIS location.

ACCURACY RULES — read carefully:
- Every field below must be YOUR OWN genuine analysis for each specific crop at this specific location and weather. Do not reuse the same numbers across crops, and do not default to round or "typical-looking" numbers — compute each field based on that crop's real agronomic profile.
- "match": your own honest 0-100% suitability score for THIS crop at THIS location/weather/season, decreasing from crop 1 (best fit) to crop 6 (weakest of your 6 picks). Two different crops should essentially never share the same score by coincidence.
- "yield", "profit", "duration", "fertilizer": use realistic figures specific to that crop's real agronomy — these vary a lot crop to crop (e.g. sugarcane duration is far longer than tomato; rice fertilizer needs differ from mustard's) so do not copy generic filler ranges.
- "location_suitability" and "weather_suitability" must each cite the ACTUAL current numbers given above (this location's real soil/region, and the real {temp}°C / {humidity}% figures), not generic language.

Respond ONLY with a JSON object, no preamble, no markdown fences, matching exactly this shape (values below are placeholders showing the expected TYPE/FORMAT only — replace every one with your own real analysis):
{{
  "crops": [
    {{
      "name": "<crop name>",
      "icon": "<one relevant emoji>",
      "match": "<your computed 0-100 suitability score>%",
      "description": "<short explanation of why it suits this location & weather>",
      "location_suitability": "<specific reason tied to {city or 'this place'}'s real soil & region>",
      "weather_suitability": "<specific reason tied to the real current {temp}°C temp & {humidity}% humidity>",
      "season": "Kharif (Monsoon) | Rabi (Winter) | Zaid (Summer)",
      "water": "High | Medium | Low",
      "yield": "<realistic yield range for this crop, e.g. 'X-Y tonnes/ha'>",
      "profit": "<realistic profit range for this crop, e.g. 'Rs X,000-Y,000/ha'>",
      "duration": "<this crop's real growth duration, e.g. 'X-Y days'>",
      "soil": "<this crop's actual preferred soil type>",
      "fertilizer": "<this crop's actual real NPK recommendation, e.g. 'NPK X:Y:Z kg/ha'>"
    }}
  ]
}}
The "crops" array must contain exactly 6 such objects, each for a different crop, each independently reasoned."""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model":       "openai/gpt-oss-20b",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens":  3500,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = _post_to_groq(body, headers)
        if resp is None or resp.status_code != 200:
            err_text = resp.text if resp else "no-response"
            logger.info(f"[CropAI] Groq HTTP {getattr(resp, 'status_code', 'None')} for {city} | {err_text}")
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Remove reasoning block if model is a thinking model
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        try:
            parsed = json.loads(match.group() if match else cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"[CropAI] JSON error for {city}: {e}\n[RAW OUTPUT] {raw[:500]}")
            return None
        crops = parsed.get("crops")
        if not isinstance(crops, list) or not crops:
            return None

        # Hard safety net — prompt instructions alone aren't 100% reliable
        # for LLMs, so filter out any sensitive/controlled crop the AI
        # might still suggest, rather than relying only on the prompt.
        _BLOCKED_CROPS = ("tobacco", "opium", "poppy", "cannabis", "hemp", "marijuana", "ganja")
        crops = [c for c in crops if not any(b in (c.get("name") or "").lower() for b in _BLOCKED_CROPS)]
        if not crops:
            return None

        for c in crops:
            c.setdefault("icon", "🌱")
            c.setdefault("location_suitability", f"Adapted to {city or 'local'} soil & region")
            c.setdefault("weather_suitability", f"Matches {temp}°C & {humidity}% humidity")
        _bounded_cache_set(_crop_ai_cache, cache_key, (now, crops), max_entries=300)
        logger.info(f"[CropAI] OK for {city}: {len(crops)} crops")
        return crops
    except Exception as e:
        logger.warning(f"[CropAI] error for {city}: {e}")
        return None


@app.route("/api/crop-recommendations", methods=["POST"])
def crop_recommendations():
    data     = request.json or {}
    temp     = data.get("temp", 25)
    humidity = data.get("humidity", 60)
    rain     = data.get("rain", 0)
    city     = data.get("city", "")
    lat      = data.get("lat")
    lon      = data.get("lon")
    season   = get_season(datetime.now().month)

    ai_crops = ai_recommend_crops(city, lat, lon, temp, humidity, rain, season)
    fallback_crops = recommend_crops(temp, humidity, rain, season, city, lat, lon)

    if ai_crops and len(ai_crops) >= 4:
        crops, source = ai_crops, "ai"
    elif ai_crops:
        # Merge AI crops with fallback crops to ensure at least 5-6 crops
        existing_names = {c.get("name", "").strip().lower() for c in ai_crops}
        merged = list(ai_crops)
        for fc in fallback_crops:
            if fc.get("name", "").strip().lower() not in existing_names:
                merged.append(fc)
            if len(merged) >= 6:
                break
        crops, source = merged, "ai_hybrid"
    else:
        crops, source = fallback_crops[:6], "rule_based"

    calendar = generate_advisory_calendar(crops[:3])
    return jsonify({
        "season":     season,
        "city":       city,
        "crops":      crops,
        "calendar":   calendar,
        "pesticides": get_pesticide_guide(crops[:3]),
        "source":     source,   # "ai" = location-aware, "rule_based" = offline fallback
    })


def get_season(month):
    if month in [6, 7, 8, 9]:
        return "Kharif (Monsoon)"
    elif month in [10, 11, 12, 1, 2]:
        return "Rabi (Winter)"
    else:
        return "Zaid (Summer)"


def recommend_crops(temp, humidity, rain, season, city="", lat=None, lon=None):
    all_crops = [
        {"name":"Rice","icon":"🌾","temp_range":(20,38),"humidity_range":(70,100),"season":"Kharif (Monsoon)","water":"High","yield":"3-5 tonnes/ha","profit":"Rs45,000-65,000/ha","duration":"90-150 days","description":"Ideal for high humidity, alluvial soil & heavy monsoon rains","soil":"Clay loam, alluvial","fertilizer":"NPK 120:60:60 kg/ha","region_lat":(10,30)},
        {"name":"Wheat","icon":"🌿","temp_range":(10,25),"humidity_range":(40,65),"season":"Rabi (Winter)","water":"Medium","yield":"4-6 tonnes/ha","profit":"Rs50,000-75,000/ha","duration":"100-150 days","description":"Thrives in Indo-Gangetic plains & cool winter climate","soil":"Well-drained loam","fertilizer":"NPK 120:60:40 kg/ha","region_lat":(20,35)},
        {"name":"Maize","icon":"🌽","temp_range":(18,35),"humidity_range":(50,80),"season":"Kharif (Monsoon)","water":"Medium","yield":"5-8 tonnes/ha","profit":"Rs40,000-60,000/ha","duration":"80-110 days","description":"Versatile crop for warm humid weather and well-drained soil","soil":"Sandy loam to clay loam","fertilizer":"NPK 150:75:75 kg/ha","region_lat":(12,32)},
        {"name":"Cotton","icon":"☁️","temp_range":(25,40),"humidity_range":(40,70),"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3 tonnes/ha","profit":"Rs60,000-90,000/ha","duration":"150-180 days","description":"Thrives in black cotton soil across Maharashtra, Gujarat & MP","soil":"Black cotton soil","fertilizer":"NPK 90:45:45 kg/ha","region_lat":(15,26)},
        {"name":"Tomato","icon":"🍅","temp_range":(18,30),"humidity_range":(60,80),"season":"Zaid (Summer)","water":"Medium","yield":"20-40 tonnes/ha","profit":"Rs80,000-1,50,000/ha","duration":"60-80 days","description":"High value vegetable crop for mild warm weather","soil":"Sandy loam, rich organic matter","fertilizer":"NPK 100:60:60 kg/ha","region_lat":(8,32)},
        {"name":"Sugarcane","icon":"🎋","temp_range":(24,38),"humidity_range":(75,90),"season":"Kharif (Monsoon)","water":"Very High","yield":"70-100 tonnes/ha","profit":"Rs70,000-1,00,000/ha","duration":"300-360 days","description":"Requires tropical hot climate, rich soil & heavy irrigation","soil":"Deep loam, good drainage","fertilizer":"NPK 250:80:100 kg/ha","region_lat":(12,28)},
        {"name":"Soybean","icon":"🫘","temp_range":(20,32),"humidity_range":(60,80),"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3 tonnes/ha","profit":"Rs35,000-55,000/ha","duration":"90-120 days","description":"Nitrogen-fixing legume highly suited to Central Indian plains","soil":"Well-drained loam","fertilizer":"NPK 30:60:40 kg/ha","region_lat":(18,26)},
        {"name":"Mustard","icon":"🌻","temp_range":(10,25),"humidity_range":(40,60),"season":"Rabi (Winter)","water":"Low","yield":"1-2 tonnes/ha","profit":"Rs25,000-40,000/ha","duration":"90-110 days","description":"Cool weather oilseed crop for North & West Indian winter","soil":"Sandy loam, well-drained","fertilizer":"NPK 80:40:40 kg/ha","region_lat":(22,32)},
        {"name":"Potato","icon":"🥔","temp_range":(15,25),"humidity_range":(50,75),"season":"Rabi (Winter)","water":"Medium","yield":"20-30 tonnes/ha","profit":"Rs60,000-1,00,000/ha","duration":"80-100 days","description":"High yielding tuber crop for fertile loose soils in winter","soil":"Sandy loam, well-drained","fertilizer":"NPK 180:80:100 kg/ha","region_lat":(18,32)},
        {"name":"Onion","icon":"🧅","temp_range":(15,28),"humidity_range":(45,70),"season":"Rabi (Winter)","water":"Medium","yield":"15-25 tonnes/ha","profit":"Rs50,000-90,000/ha","duration":"120-150 days","description":"Essential commercial crop suited for well-drained loamy soils","soil":"Loamy soil with good drainage","fertilizer":"NPK 100:50:50 kg/ha","region_lat":(15,30)},
        {"name":"Groundnut","icon":"🥜","temp_range":(22,32),"humidity_range":(50,75),"season":"Kharif (Monsoon)","water":"Low to Medium","yield":"2-3.5 tonnes/ha","profit":"Rs40,000-65,000/ha","duration":"100-120 days","description":"Important oilseed legume thriving in light sandy loams","soil":"Sandy loam, well-drained","fertilizer":"NPK 25:50:40 kg/ha","region_lat":(10,25)},
        {"name":"Chana (Chickpea)","icon":"🫛","temp_range":(15,28),"humidity_range":(35,60),"season":"Rabi (Winter)","water":"Low","yield":"1.5-2.5 tonnes/ha","profit":"Rs35,000-55,000/ha","duration":"90-110 days","description":"Drought-resistant pulse crop for dry Rabi winter season","soil":"Deep fertile black or loamy soil","fertilizer":"NPK 20:50:20 kg/ha","region_lat":(15,30)},
        {"name":"Bajra (Pearl Millet)","icon":"🌾","temp_range":(25,38),"humidity_range":(30,60),"season":"Kharif (Monsoon)","water":"Low","yield":"2-3 tonnes/ha","profit":"Rs25,000-45,000/ha","duration":"75-90 days","description":"Hardy millet thriving in arid, low rainfall and hot climates","soil":"Light sandy soil","fertilizer":"NPK 80:40:40 kg/ha","region_lat":(18,32)},
    ]
    scored = []
    loc_name = city or "your location"
    for crop in all_crops:
        score = 0
        if crop["temp_range"][0] <= temp <= crop["temp_range"][1]:
            score += 40
        elif abs(temp - sum(crop["temp_range"]) / 2) < 5:
            score += 20
        if crop["humidity_range"][0] <= humidity <= crop["humidity_range"][1]:
            score += 30
        if crop["season"] == season:
            score += 20
        if lat and crop.get("region_lat") and (crop["region_lat"][0] <= lat <= crop["region_lat"][1]):
            score += 10

        crop["score"] = score
        crop["match"] = f"{min(98, max(75, score))}%"
        crop["location_suitability"] = f"Suited to {loc_name}'s soil & regional climate"
        crop["weather_suitability"] = f"Matches current {temp}°C temp & {humidity}% humidity"
        scored.append(crop)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def generate_advisory_calendar(crops):
    today = datetime.now()
    activities = [
        {"week":1,  "activity":"Soil preparation & ploughing",  "type":"preparation"},
        {"week":2,  "activity":"Seed treatment & sowing",       "type":"sowing"},
        {"week":3,  "activity":"First irrigation",              "type":"irrigation"},
        {"week":4,  "activity":"Apply basal fertilizer (NPK)",  "type":"fertilizer"},
        {"week":6,  "activity":"Weeding & thinning",            "type":"maintenance"},
        {"week":8,  "activity":"Apply Urea (top dressing)",     "type":"fertilizer"},
        {"week":10, "activity":"Pest & disease inspection",     "type":"pesticide"},
        {"week":12, "activity":"Spray fungicide if required",   "type":"pesticide"},
        {"week":16, "activity":"Foliar spray micronutrients",   "type":"fertilizer"},
        {"week":20, "activity":"Pre-harvest irrigation stop",   "type":"irrigation"},
        {"week":22, "activity":"Harvest preparation",           "type":"harvest"},
    ]
    calendar = []
    for act in activities:
        date = today + timedelta(weeks=act["week"])
        calendar.append({
            "date":     date.strftime("%d %b %Y"),
            "activity": act["activity"],
            "type":     act["type"],
            "week":     act["week"]
        })
    return calendar


def get_pesticide_guide(crops):
    guides = {
        "Rice":   [{"pest":"Brown Plant Hopper","pesticide":"Imidacloprid 17.8 SL","dose":"125 ml/ha","timing":"At 30 & 60 days after transplanting","eco":False},{"pest":"Leaf folder","pesticide":"Neem Oil 5%","dose":"2.5 L/ha","timing":"At first sign of damage","eco":True}],
        "Wheat":  [{"pest":"Aphids","pesticide":"Dimethoate 30 EC","dose":"1 L/ha","timing":"At tillering stage","eco":False},{"pest":"Yellow rust","pesticide":"Propiconazole 25 EC","dose":"500 ml/ha","timing":"At boot leaf stage","eco":False}],
        "Maize":  [{"pest":"Fall Armyworm","pesticide":"Spinetoram 11.7 SC","dose":"450 ml/ha","timing":"7-10 days after infestation","eco":False},{"pest":"Stem borer","pesticide":"Emamectin Benzoate 5 SG","dose":"220 g/ha","timing":"At whorl stage","eco":False}],
        "Cotton": [{"pest":"Bollworm","pesticide":"Chlorpyriphos 20 EC","dose":"2.5 ml/L","timing":"At first boll formation","eco":False},{"pest":"Whitefly","pesticide":"Neem Oil 5%","dose":"5 ml/L","timing":"Every 7 days","eco":True}],
        "Soybean":   [{"pest":"Girdle Beetle","pesticide":"Thiamethoxam 25 WG","dose":"100 g/ha","timing":"At 15-20 days after sowing","eco":False},{"pest":"Semi-looper","pesticide":"Neem Oil 5%","dose":"2.5 L/ha","timing":"At first sign of damage","eco":True}],
        "Mustard":   [{"pest":"Aphids","pesticide":"Imidacloprid 17.8 SL","dose":"100 ml/ha","timing":"At flowering stage","eco":False},{"pest":"Painted bug","pesticide":"Neem Oil 5%","dose":"2.5 L/ha","timing":"At first sign of damage","eco":True}],
        "Potato":    [{"pest":"Aphids","pesticide":"Thiamethoxam 25 WG","dose":"100 g/ha","timing":"At 30 days after planting","eco":False},{"pest":"Late blight","pesticide":"Mancozeb 75 WP","dose":"2.5 g/L","timing":"At first sign of disease","eco":False}],
        "Onion":     [{"pest":"Thrips","pesticide":"Fipronil 5 SC","dose":"1 L/ha","timing":"At 30-45 days after transplanting","eco":False},{"pest":"Purple blotch","pesticide":"Mancozeb 75 WP","dose":"2.5 g/L","timing":"At first sign of disease","eco":False}],
        "Chilli":    [{"pest":"Thrips & Mites","pesticide":"Abamectin 1.8 EC","dose":"0.5 ml/L","timing":"At first sign of infestation","eco":False},{"pest":"Fruit rot","pesticide":"Neem Oil 5%","dose":"5 ml/L","timing":"Every 7 days","eco":True}],
        "Groundnut": [{"pest":"Leaf miner","pesticide":"Emamectin Benzoate 5 SG","dose":"220 g/ha","timing":"At first sign of damage","eco":False},{"pest":"Tikka leaf spot","pesticide":"Mancozeb 75 WP","dose":"2.5 g/L","timing":"At first sign of disease","eco":False}],
        "Sugarcane": [{"pest":"Early Shoot Borer","pesticide":"Chlorpyriphos 20 EC","dose":"2.5 ml/L","timing":"At 30-45 days after planting","eco":False},{"pest":"Pyrilla","pesticide":"Neem Oil 5%","dose":"5 ml/L","timing":"Every 7 days","eco":True}],
        "Tomato":    [{"pest":"Fruit Borer","pesticide":"Emamectin Benzoate 5 SG","dose":"220 g/ha","timing":"At flowering stage","eco":False},{"pest":"Whitefly","pesticide":"Neem Oil 5%","dose":"5 ml/L","timing":"Every 7 days","eco":True}],
    }
    generic_guide = [{"pest": "General pests & fungal disease", "pesticide": "Neem Oil 5%", "dose": "5 ml/L", "timing": "At first sign of pest or disease activity", "eco": True}]

    result = []
    for crop in crops:
        name = crop.get("name", "")
        result.append({"crop": name, "guides": guides.get(name, generic_guide)})
    return result


# ─── Market Data — Agmarknet (Govt. of India, official) ──────────────────────
# Data source: "Current Daily Price of Various Commodities from Various
# Markets (Mandi)" — published by the Directorate of Marketing & Inspection,
# Ministry of Agriculture & Farmers Welfare, via data.gov.in (open data,
# Govt. of India). This is the SAME data Agmarknet.gov.in itself is built on
# — it is the authoritative, official source for Indian mandi prices, unlike
# global commodity-futures APIs (which price Chicago wheat/corn, not Indian
# mandi produce) or hand-typed reference tables.
#
# Get a free personal key at https://data.gov.in (Sign Up -> My Account ->
# API keys) and set it as DATA_GOV_API_KEY in your .env. Until you do, this
# falls back to data.gov.in's shared public test key, which is rate-limited
# and NOT meant for production — replace it as soon as you can.
DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY", "")
if not DATA_GOV_API_KEY:
    logger.info("[AgroSmart] WARNING: DATA_GOV_API_KEY not set — /api/market will use MSP reference prices only")
AGMARKNET_RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
AGMARKNET_URL = f"https://api.data.gov.in/resource/{AGMARKNET_RESOURCE_ID}"

# Reusable session with automatic retries — helps ride out brief network
# hiccups instead of failing on the first slow attempt.
_agmark_session = requests.Session()
# data.gov.in's server silently hangs (never responds) on requests carrying the
# default "python-requests/x.x" User-Agent — confirmed by testing: the exact
# same request succeeds in ~1s with a browser UA and times out after 15s+
# with no UA override. This is NOT a bug in our request logic — it's the
# gov server/WAF fingerprinting and stalling non-browser clients.
_agmark_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
})
_agmark_retry = requests.adapters.Retry(
    total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]
)
_agmark_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=_agmark_retry))

# Agmarknet's commodity names vs. the display names SmartAgro already uses
# in the UI/translations. Extend this as you add more crops.
AGMARK_COMMODITY_ALIASES = {
    "wheat": "Wheat", "rice": "Rice", "maize": "Maize (Corn)",
    "mustard": "Mustard", "groundnut": "Groundnut", "onion": "Onion",
    "potato": "Potato", "tomato": "Tomato", "green chilli": "Chilli",
    "chilli": "Chilli", "sugarcane": "Sugarcane",
    "arhar (tur/red gram)(whole)": "Arhar (Tur)", "arhar": "Arhar (Tur)",
    "green gram (moong)(whole)": "Moong", "moong": "Moong",
    "black gram (urad beans)(whole)": "Urad", "urad": "Urad",
    "soyabean": "Soybean", "soybean": "Soybean", "cotton": "Cotton",
    "jowar(sorghum)": "Jowar", "jowar": "Jowar",
    "bajra(pearl millet/cumbu)": "Bajra", "bajra": "Bajra",
    "bengal gram(gram)(whole)": "Bengal Gram", "bengal gram": "Bengal Gram",
    "sesamum(sesame,gingelly,til)": "Sesamum (Til)", "sesamum": "Sesamum (Til)",
    "bhindi(ladies finger)": "Bhindi", "bhindi": "Bhindi",
    "mousambi(sweet lime)": "Mousambi", "mousambi": "Mousambi",
    "french beans(frasbean)": "French Beans", "french beans": "French Beans",
    "cucumbar(kheera)": "Cucumber", "cucumber": "Cucumber",
    "jamun(narala hannu)": "Jamun", "jamun": "Jamun",
    "karbuja(musk melon)": "Musk Melon", "water melon": "Watermelon",
    "tuber rose(single)": "Tuber Rose", "tuber rose(double)": "Tuber Rose",
    "chrysanthemum(loose)": "Chrysanthemum", "rose(loose)": "Rose",
    "marigold(loose)": "Marigold", "pear(marasebu)": "Pear",
    "jack fruit(ripe)": "Jackfruit", "amla(nelli kai)": "Amla",
    "pea pod/pea cod/hari matar": "Peas", "peas wet": "Peas",
    "garlic": "Garlic", "ginger(green)": "Ginger", "ginger": "Ginger",
    "turmeric": "Turmeric", "cumin(jeera)": "Cumin (Jeera)",
    "coriander(leaves)": "Coriander", "coriander": "Coriander",
    "banana": "Banana", "mango": "Mango",
}

CITY_STATE = {
    # ── Previously supported ──
    "Delhi":         "Delhi",
    "Mumbai":        "Maharashtra",
    "Kolkata":       "West Bengal",
    "Chennai":       "Tamil Nadu",
    "Hyderabad":     "Telangana",
    "Pune":          "Maharashtra",
    "Ahmedabad":     "Gujarat",
    "Lucknow":       "Uttar Pradesh",
    "Jaipur":        "Rajasthan",
    "Bhopal":        "Madhya Pradesh",
    "Patna":         "Bihar",
    "Nagpur":        "Maharashtra",
    "Indore":        "Madhya Pradesh",
    "Surat":         "Gujarat",
    "Kanpur":        "Uttar Pradesh",
    "Coimbatore":    "Tamil Nadu",
    "Visakhapatnam": "Andhra Pradesh",
    "Bhubaneswar":   "Odisha",
    "Guwahati":      "Assam",
    "Amritsar":      "Punjab",

    # ── More cities in already-covered states (no extra API calls — same
    #    state fetch is reused) ──
    "Vadodara":      "Gujarat",
    "Rajkot":        "Gujarat",
    "Nashik":        "Maharashtra",
    "Aurangabad":    "Maharashtra",
    "Varanasi":      "Uttar Pradesh",
    "Agra":          "Uttar Pradesh",
    "Meerut":        "Uttar Pradesh",
    "Prayagraj":     "Uttar Pradesh",
    "Ludhiana":      "Punjab",
    "Jalandhar":     "Punjab",
    "Kota":          "Rajasthan",
    "Udaipur":       "Rajasthan",
    "Jodhpur":       "Rajasthan",
    "Gwalior":       "Madhya Pradesh",
    "Jabalpur":      "Madhya Pradesh",
    "Cuttack":       "Odisha",
    "Siliguri":      "West Bengal",
    "Durgapur":      "West Bengal",
    "Asansol":       "West Bengal",
    "Gaya":          "Bihar",
    "Vijayawada":    "Andhra Pradesh",
    "Guntur":        "Andhra Pradesh",
    "Tirupati":      "Andhra Pradesh",
    "Warangal":      "Telangana",
    "Nizamabad":     "Telangana",
    "Madurai":       "Tamil Nadu",
    "Salem":         "Tamil Nadu",
    "Tiruchirappalli": "Tamil Nadu",
    "Silchar":       "Assam",
    "Dibrugarh":     "Assam",

    # ── New states/UTs not previously covered ──
    "Bengaluru":     "Karnataka",
    "Mysuru":        "Karnataka",
    "Hubli":         "Karnataka",
    "Kochi":         "Kerala",
    "Thiruvananthapuram": "Kerala",
    "Kozhikode":     "Kerala",
    "Gurugram":      "Haryana",
    "Faridabad":     "Haryana",
    "Karnal":        "Haryana",
    "Raipur":        "Chhattisgarh",
    "Bilaspur":      "Chhattisgarh",
    "Ranchi":        "Jharkhand",
    "Jamshedpur":    "Jharkhand",
    "Dehradun":      "Uttarakhand",
    "Haridwar":      "Uttarakhand",
    "Shimla":        "Himachal Pradesh",
    "Panaji":        "Goa",
    "Chandigarh":    "Chandigarh",
    "Srinagar":      "Jammu and Kashmir",
    "Jammu":         "Jammu and Kashmir",
    "Puducherry":    "Puducherry",
    "Agartala":      "Tripura",
}

# Real daily price history, built up one genuine data point per day as the
# app runs (no fabricated numbers). Persisted to disk so it survives restarts.
_AGMARK_HISTORY_PATH = os.path.join(basedir, "market_history_cache.json")
_agmark_history_lock = threading.Lock()
_agmark_fetch_cache = {}          # {state: (timestamp, results)} — in-memory
# With ~30 unique states/UTs now in play and mandi prices only updating once
# a day, a short cache would cause excessive API calls if the page gets
# steady traffic. 1 hour keeps calls reasonable while still refreshing
# several times a day. 
AGMARK_CACHE_TTL_SEC = 60 * 60


def _load_history_cache():
    try:
        with open(_AGMARK_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f) 
    except Exception:
        return {}


def _save_history_cache(cache):
    try:
        with open(_AGMARK_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.info(f"[Market] Could not persist history cache: {e}")


def _field(record: dict, *keys):
    """data.gov.in resources don't always serve field names consistently
    (snake_case vs the legacy CKAN 'Modal_x0020_Price' style, or different
    capitalisation) — try every known variant before giving up."""
    for k in keys:
        v = record.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_arrival_date(date_str):
    """Normalize Agmarknet's arrival_date (commonly DD/MM/YYYY) to a sortable
    ISO YYYY-MM-DD string, so multiple real reporting dates in one response
    can be deduped and ordered correctly. Returns None if unrecognized —
    callers skip records whose date we can't trust rather than guessing."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# A handful of states are recorded under a different name than their
# common name (same place, different label) — try each candidate in
# order until one returns records. NOTE: this must only contain true
# synonyms for the same region, never a different-but-nearby state —
# e.g. Telangana and Andhra Pradesh have been separate states since 2014,
# so they are deliberately NOT listed as fallbacks for each other; doing
# so would silently show one state's real prices mislabeled as another's.
STATE_NAME_CANDIDATES = {
    "Delhi":               ["Delhi", "NCT of Delhi"],
    "Odisha":              ["Odisha", "Orissa"],
    "Uttarakhand":         ["Uttarakhand", "Uttaranchal"],
    "Jammu and Kashmir":   ["Jammu and Kashmir", "Jammu & Kashmir"],
    "Puducherry":          ["Puducherry", "Pondicherry"],
}


def fetch_agmarknet_prices(state: str) -> list:
    """Fetch REAL, government-reported mandi (wholesale market) prices for a
    state from data.gov.in's official Agmarknet dataset. Returns [] if the
    feed has nothing usable right now (caller reports this city as having
    no live data — no fabricated numbers are substituted)."""
    now = time.monotonic()
    cached = _agmark_fetch_cache.get(state)
    if cached and (now - cached[0]) < AGMARK_CACHE_TTL_SEC:
        return cached[1]


    records = []
    for candidate in STATE_NAME_CANDIDATES.get(state, [state]):
        params = {
            "api-key": DATA_GOV_API_KEY,
            "format": "json",
            "limit": 400,
            "filters[state]": candidate,
        }
        try:
            resp = _agmark_session.get(AGMARKNET_URL, params=params, timeout=15)
            if resp.status_code == 429:
                logger.info(f"[Market] Agmarknet HTTP 429 Rate Limit for state='{candidate}'")
                continue
            if resp.status_code != 200:
                logger.info(f"[Market] Agmarknet HTTP {resp.status_code} for state='{candidate}': {resp.text[:200]}")
                continue

            body = resp.json()
            records = body.get("records", [])
            if records:
                logger.info(f"[Market] Agmarknet: {len(records)} raw records for state='{candidate}' "
                      f"(total available: {body.get('total', '?')})")
                break
            else:
                logger.info(f"[Market] Agmarknet: 0 records for state='{candidate}' — trying next candidate if any")
        except Exception as e:
            logger.warning(f"[Market] Agmarknet error for state='{candidate}': {e}")
            continue

    if not records:
        logger.info(f"[Market] Agmarknet: no usable records for {state} after trying all name variants")
        return []

    # Log the exact keys of the first record once, so if parsing still
    # fails you can see the real field names by checking your app logs.
    logger.info(f"[Market] Sample record keys for {state}: {list(records[0].keys())}")

    # Agmarknet's response usually covers several recent reporting dates,
    # not just today — a state has many markets/varieties reporting the
    # same commodity across those dates. Group by (commodity, date) so we
    # can compute a REAL day-over-day change straight from this batch
    # whenever more than one date is already present, instead of only
    # discarding everything but the single latest record.
    by_commodity_date = {}   # display_name -> {iso_date: [modal_price, ...]}
    latest_meta = {}         # display_name -> {"market", "district", "arrival_date" (raw), "_iso"}
    skipped_no_price = 0
    skipped_bad_date = 0

    for r in records:
        raw_name = str(_field(r, "commodity", "Commodity") or "").strip()
        modal = _field(r, "modal_price", "Modal_x0020_Price", "Modal Price", "modal price")
        if not raw_name or modal is None:
            skipped_no_price += 1
            continue
        try:
            modal_price = float(modal)
        except (TypeError, ValueError):
            skipped_no_price += 1
            continue
        if modal_price <= 0:
            continue

        raw_date = _field(r, "arrival_date", "Arrival_Date")
        iso_date = _parse_arrival_date(raw_date)
        if not iso_date:
            skipped_bad_date += 1
            continue

        display_name = AGMARK_COMMODITY_ALIASES.get(raw_name.lower(), raw_name.title())
        by_commodity_date.setdefault(display_name, {}).setdefault(iso_date, []).append(modal_price)

        meta = latest_meta.get(display_name)
        if not meta or iso_date >= meta["_iso"]:
            latest_meta[display_name] = {
                "market":       _field(r, "market", "Market") or "",
                "district":     _field(r, "district", "District") or "",
                "arrival_date": raw_date or "",
                "_iso":         iso_date,
            }

    logger.info(f"[Market] {state}: parsed {len(by_commodity_date)} commodities, "
          f"skipped {skipped_no_price} (missing/invalid price), "
          f"{skipped_bad_date} (unparseable date)")

    # Diagnostic: how many distinct calendar dates actually showed up in this
    # single fetch? If Agmarknet's feed is a single-day snapshot (common for
    # this dataset), every commodity will have exactly 1 date here — real
    # day-over-day change then only becomes available once our own disk
    # cache has accumulated a 2nd real day, not from one fetch alone.
    all_dates_seen = set()
    multi_date_commodities = 0
    for date_map in by_commodity_date.values():
        all_dates_seen.update(date_map.keys())
        if len(date_map) >= 2:
            multi_date_commodities += 1
    logger.info(f"[Market] {state}: distinct dates in this fetch = {sorted(all_dates_seen)} "
          f"| commodities with 2+ dates in this single fetch: {multi_date_commodities}/{len(by_commodity_date)}")

    # Average modal price across markets reporting the same commodity on the
    # same date — still real, government-reported figures; this just applies
    # the same state-level averaging the app already does, per date instead
    # of picking one arbitrary record.
    latest_by_commodity = {}
    for display_name, date_map in by_commodity_date.items():
        per_date_avg = {d: round(sum(prices) / len(prices), 2) for d, prices in date_map.items()}
        meta = latest_meta[display_name]
        latest_by_commodity[display_name] = {
            "market":       meta["market"],
            "district":     meta["district"],
            "arrival_date": meta["arrival_date"],
            "per_date":     per_date_avg,   # {iso_date: avg_modal_price} — real, multi-day
        }

    today_key = datetime.now().strftime("%Y-%m-%d")
    results = []
    try:
        with _agmark_history_lock:
            cache = _load_history_cache()
            state_hist = cache.setdefault(state, {})
            cache_changed = False  # only rewrite the file if something is actually new

            for display_name, rec in latest_by_commodity.items():
                hist = state_hist.setdefault(display_name, [])
                existing_dates = {h["date"] for h in hist}

                # Merge in every real date this batch reported that we don't
                # already have cached. If Agmarknet's response spans several
                # days, this immediately gives a genuine multi-day trend
                # instead of waiting on our own daily cache to accumulate it.
                for d, price in rec["per_date"].items():
                    if d not in existing_dates:
                        hist.append({"date": d, "price": price})
                        existing_dates.add(d)
                        cache_changed = True
                hist.sort(key=lambda h: h["date"])
                new_len = min(len(hist), 30)
                if new_len != len(hist):
                    cache_changed = True
                hist[:] = hist[-30:]  # keep the last 30 real daily points

                if len(hist) < 2:
                    change = 0.0
                else:
                    prev_price = hist[-2]["price"]
                    today_price = hist[-1]["price"]
                    change = round(((today_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0
                history_prices = [h["price"] for h in hist]
                display_price = hist[-1]["price"] if hist else next(iter(rec["per_date"].values()))

                results.append({
                    "crop":         display_name,
                    "crop_key":     display_name,
                    "price":        int(round(display_price)),
                    "change":       change,
                    "history":      history_prices,
                    "unit":         "Rs/quintal",
                    "source":       "agmarknet_live",
                    "market":       rec["market"],
                    "district":     rec["district"],
                    "arrival_date": rec["arrival_date"],
                })
            # Cache hits (which are the common case within the 1-hour TTL)
            # now skip this write entirely instead of rewriting the whole
            # 50KB+ file on every single request that touches this lock.
            if cache_changed:
                _save_history_cache(cache)
    except Exception as e:
        # Never let a disk/cache problem take down live pricing — just skip
        # persistence for this call and still return what we parsed, using
        # whatever real multi-day data this batch itself contained.
        logger.warning(f"[Market] History cache error for {state} (non-fatal): {e}")
        if not results:
            for display_name, rec in latest_by_commodity.items():
                dates_sorted = sorted(rec["per_date"].items())
                if len(dates_sorted) >= 2:
                    prev_price = dates_sorted[-2][1]
                    today_price = dates_sorted[-1][1]
                    change = round(((today_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0
                    history_prices = [p for _, p in dates_sorted]
                    display_price = today_price
                else:
                    change = 0.0
                    only_price = dates_sorted[0][1] if dates_sorted else 0
                    history_prices = [only_price]
                    display_price = only_price

                results.append({
                    "crop":         display_name,
                    "crop_key":     display_name,
                    "price":        int(round(display_price)),
                    "change":       change,
                    "history":      history_prices,
                    "unit":         "Rs/quintal",
                    "source":       "agmarknet_live",
                    "market":       rec["market"],
                    "district":     rec["district"],
                    "arrival_date": rec["arrival_date"],
                })

    _bounded_cache_set(_agmark_fetch_cache, state, (now, results), max_entries=60)  # ~36 states/UTs
    with_history = sum(1 for r in results if len(r.get("history", [])) >= 2)
    logger.info(f"[Market] Agmarknet OK for {state}: {len(results)} commodities "
          f"({with_history} have 2+ real cached days -> real change%, "
          f"{len(results) - with_history} still on day 1 -> change=0.0 until next real day)")
    return results


def get_demand(price: int, change: float) -> str:
    if change >= 2.0:
        return "Very High"
    elif change >= 0.5:
        return "High"
    elif change >= -1.5:
        return "Medium"
    else:
        return "Low"


@app.route('/api/market')
def get_market_data():
    cities = list(CITY_STATE.keys())
    location = request.args.get('location', '').strip().lower()
    if location:
        cities = [c for c in cities if location in c.lower()]

    # A searched city we don't have a state mapping for simply has no data —
    # we no longer invent a city entry with fabricated prices for it.
    if location and not cities:
        return jsonify({
            "markets":     {},
            "locations":   [],
            "live_count":  0,
            "empty_cities": [],
            "fetched_at":  datetime.now().isoformat(),
            "data_source": "Agmarknet — Ministry of Agriculture & Farmers Welfare, Govt. of India (data.gov.in)",
            "note":        f'No market mapping for "{location}". Try a supported city name.',
        }), 200

    markets = {}
    empty_cities = []
    live_total = 0

    try:
        # Fetch every unique state IN PARALLEL instead of one-by-one — with
        # ~13 unique states and a government API that can be slow/overloaded,
        # doing this sequentially could mean the whole page waits 12s x 13
        # states in the worst case. Parallel fetching caps total wait time to
        # roughly one slowest request instead of the sum of all of them.
        unique_states = sorted({CITY_STATE.get(c, "") for c in cities if CITY_STATE.get(c, "")})
        state_results_cache = {}
        if unique_states:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(unique_states))) as executor:
                future_to_state = {executor.submit(fetch_agmarknet_prices, s): s for s in unique_states}
                for future in concurrent.futures.as_completed(future_to_state):
                    state = future_to_state[future]
                    try:
                        state_results_cache[state] = future.result()
                    except Exception as e:
                        logger.warning(f"[Market] Unexpected error fetching {state}: {e}")
                        state_results_cache[state] = []

        for city in cities:
            state = CITY_STATE.get(city, "")
            try:
                crops = list(state_results_cache.get(state, []))
            except Exception:
                crops = []

            if not crops:
                # Live Agmarknet feed genuinely has nothing for this state
                # right now — report it as empty rather than filling the gap
                # with fabricated numbers.
                markets[city] = []
                empty_cities.append(city)
                continue

            city_crops = []
            for crop in crops:
                demand = get_demand(crop["price"], crop["change"])
                city_crops.append({**crop, "demand": demand})

            city_crops.sort(
                key=lambda x: ({"Very High": 3, "High": 2, "Medium": 1, "Low": 0}.get(x["demand"], 0), x["price"]),
                reverse=True
            )
            markets[city] = city_crops
            live_total += len(city_crops)

        return jsonify({
            "markets":      markets,
            "locations":    list(markets.keys()),
            "live_count":   live_total,
            "empty_cities": empty_cities,
            "fetched_at":   datetime.now().isoformat(),
            "data_source":  "Agmarknet — Ministry of Agriculture & Farmers Welfare, Govt. of India (data.gov.in)",
        })

    except Exception as e:
        # Something unexpected blew up (network, parsing, threading, etc).
        # Report the failure honestly instead of masking it with fabricated
        # data — the frontend should show a clear "couldn't load live data"
        # state rather than numbers that look real but aren't.
        logger.warning(f"[Market] /api/market failed: {e}")
        return jsonify({
            "markets":     {},
            "locations":   [],
            "live_count":  0,
            "empty_cities": cities,
            "fetched_at":  datetime.now().isoformat(),
            "data_source": "Agmarknet — Ministry of Agriculture & Farmers Welfare, Govt. of India (data.gov.in)",
            "error":       "Could not fetch live market data right now. Please try again shortly.",
        }), 502


# ─── Debug endpoint ───────────────────────────────────────────────────────────
@app.route('/api/debug-market')
def debug_market():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    state = request.args.get('state', 'Delhi')
    try:
        resp = _agmark_session.get(
            AGMARKNET_URL,
            params={"api-key": DATA_GOV_API_KEY, "format": "json", "limit": 20, "filters[state]": state},
            timeout=15
        )
        if not resp.ok:
            return jsonify({"http_status": resp.status_code, "raw_response": resp.text[:1000]})
        body = resp.json()
        records = body.get("records", [])
        return jsonify({
            "http_status":      resp.status_code,
            "total_available":  body.get("total"),
            "records_returned": len(records),
            "sample_record":    records[0] if records else None,
            "sample_keys":      list(records[0].keys()) if records else [],
            "note": "If records_returned is 0, try a different 'state' value (e.g. ?state=Maharashtra). "
                    "If sample_record exists but crops still don't show on /market, compare sample_keys "
                    "against the field names read in fetch_agmarknet_prices().",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Chatbot fallback (Groq down/erroring) ──────────────────────────────────
# The frontend displays whatever string comes back here directly as the
# bot's own chat bubble, so this needs to read as a real, helpful sentence —
# not a raw error code — and in the farmer's own language, since Groq being
# down also means we can't call it to translate this message on the fly.
_CHAT_FALLBACK_MSG = {
    "en": "Sorry, I'm having trouble connecting right now. Please try again in a moment.",
    "hi": "क्षमा करें, अभी मुझे जुड़ने में समस्या हो रही है। कृपया थोड़ी देर बाद फिर से प्रयास करें।",
    "bn": "দুঃখিত, এখন সংযোগে সমস্যা হচ্ছে। অনুগ্রহ করে একটু পরে আবার চেষ্টা করুন।",
    "te": "క్షమించండి, ప్రస్తుతం కనెక్ట్ కావడంలో సమస్య ఉంది. దయచేసి కొద్దిసేపటి తర్వాత మళ్లీ ప్రయత్నించండి.",
    "mr": "क्षमस्व, सध्या कनेक्ट होण्यात अडचण येत आहे. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा.",
    "ta": "மன்னிக்கவும், தற்போது இணைப்பதில் சிக்கல் உள்ளது. சிறிது நேரம் கழித்து மீண்டும் முயற்சிக்கவும்.",
    "gu": "માફ કરશો, અત્યારે કનેક્ટ થવામાં તકલીફ આવી રહી છે. કૃપા કરી થોડી વાર પછી ફરી પ્રયાસ કરો.",
    "kn": "ಕ್ಷಮಿಸಿ, ಈಗ ಸಂಪರ್ಕಿಸಲು ತೊಂದರೆಯಾಗುತ್ತಿದೆ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
    "ml": "ക്ഷമിക്കണം, ഇപ്പോൾ ബന്ധിപ്പിക്കുന്നതിൽ പ്രശ്നമുണ്ട്. ദയവായി കുറച്ച് സമയത്തിന് ശേഷം വീണ്ടും ശ്രമിക്കുക.",
    "pa": "ਮੁਆਫ਼ ਕਰਨਾ, ਹੁਣ ਕਨੈਕਟ ਕਰਨ ਵਿੱਚ ਸਮੱਸਿਆ ਆ ਰਹੀ ਹੈ। ਕਿਰਪਾ ਕਰਕੇ ਥੋੜ੍ਹੀ ਦੇਰ ਬਾਅਦ ਦੁਬਾਰਾ ਕੋਸ਼ਿਸ਼ ਕਰੋ।",
    "ur": "معذرت، ابھی رابطہ کرنے میں مسئلہ ہو رہا ہے۔ براہ کرم تھوڑی دیر بعد دوبارہ کوشش کریں۔",
}


def _chat_fallback_reply(messages, lang):
    """Friendly, localized reply for when Groq itself is unreachable/erroring.
    Adds a simple keyword-based pointer to a relevant app section so the
    farmer still gets *something* useful instead of a dead end."""
    base = _CHAT_FALLBACK_MSG.get(lang, _CHAT_FALLBACK_MSG["en"])

    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = (m.get("content") or "").lower()
            break

    if any(k in last_user_msg for k in ["disease", "pest", "spot", "infect", "bimari", "keet", "rog"]):
        suggestion = " [Diagnose Crop](/diagnose)"
    elif any(k in last_user_msg for k in ["price", "mandi", "rate", "sell", "bhav", "daam"]):
        suggestion = " [Market Prices](/market)"
    elif any(k in last_user_msg for k in ["weather", "rain", "temperature", "mausam", "barish"]):
        suggestion = " [Dashboard](/)"
    else:
        suggestion = ""

    return base + suggestion


def _gemini_chat_reply(system_prompt, messages):
    """Fallback for the chatbot when Groq fails — sends the exact same
    system prompt and conversation to Gemini instead, so the farmer gets a
    real, context-aware answer rather than a canned apology. Returns None
    (not raises) on any failure, so the caller can fall through to the
    final localized fallback message."""
    if not GEMINI_API_KEY:
        return None
    try:
        gemini_contents = []
        for m in messages:
            role = "model" if m.get("role") == "assistant" else "user"
            content = (m.get("content") or "").strip()
            if content:
                gemini_contents.append({"role": role, "parts": [{"text": content}]})
        if not gemini_contents:
            return None

        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": gemini_contents,
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 700},
        }
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=body, timeout=30
        )
        if resp.status_code == 200:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return text or None
        logger.info(f"[Chat] Gemini fallback returned {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        logger.warning(f"[Chat] Gemini fallback exception: {e}")
    return None


# ─── Chatbot topic gate ──────────────────────────────────────────────────────
# The system prompt already tells the model to only answer farming
# questions, but prompt instructions alone aren't 100% reliable for LLMs —
# same reasoning as the restricted-crops hard intercept above. This adds a
# real pre-filter: obviously off-topic messages (movies, coding help,
# politics, general chit-chat unrelated to farming) never reach Groq at
# all, so the app can't be steered into acting as a general-purpose
# chatbot in front of an audience.
def _compile_word_matchers(words):
    """Builds a single regex that matches any of `words` as whole words
    (word-boundary), case-insensitive. Returns (compiled_regex, raw_list)."""
    escaped = [re.escape(w) for w in words]
    rx = re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)
    return rx, words


_CHAT_OFF_TOPIC_WORDS = [
    "movie", "film", "actor", "actress", "cricket", "football", "ipl",
    "song", "music", "lyrics", "celebrity", "politics", "election",
    "girlfriend", "boyfriend", "relationship advice", "joke", "riddle",
    "write code", "python code", "javascript", "html", "programming",
    "homework", "math problem", "essay", "poem about", "recipe for cake",
    "who is the prime minister", "who is the president", "stock market",
    "cryptocurrency", "bitcoin", "share price",
]
_CHAT_ON_TOPIC_WORDS = [
    "crop", "farm", "soil", "seed", "pest", "fertiliz", "irrigat", "mandi",
    "kisan", "khet", "fasal", "beej", "rain", "weather", "mausam", "yield",
    "disease", "harvest", "sow", "cultivat", "agri", "manure", "compost",
    "pmfby", "pm-kisan", "kcc", "scheme", "price", "bhav", "market",
]
_CHAT_OFF_TOPIC_RX, _CHAT_OFF_TOPIC_RAW = _compile_word_matchers(_CHAT_OFF_TOPIC_WORDS)
_CHAT_ON_TOPIC_RX, _CHAT_ON_TOPIC_RAW = _compile_word_matchers(_CHAT_ON_TOPIC_WORDS)


def _chat_message_on_topic(text: str) -> bool:
    """Returns False only when the message clearly matches an off-topic
    keyword AND has no farming signal at all. Defaults to True (on-topic)
    for anything ambiguous or short, since a false "off-topic" refusal is
    far more annoying to a genuine farmer than an occasional off-topic
    message slipping through to the (still-instructed) LLM."""
    if not text or len(text.strip()) < 2:
        return True
    low = text.lower()
    if _CHAT_ON_TOPIC_RX.search(low):
        return True
    if _CHAT_OFF_TOPIC_RX.search(low):
        return False
    return True


# Pre-canned polite refusals (localized) for the off-topic case, so we don't
# spend a Groq call just to say no, and it reads naturally in the farmer's
# own language even if Groq/Gemini are both down.
_OFF_TOPIC_REPLIES = {
    "en": "I'm Kisan Helper — I can only help with farming, crops, weather, market prices, and government schemes. Ask me something about your farm and I'll do my best to help!",
    "hi": "मैं किसान हेल्पर हूं — मैं केवल खेती, फसल, मौसम, मंडी भाव और सरकारी योजनाओं में मदद कर सकता हूं। अपने खेत के बारे में कुछ पूछें, मैं मदद करूंगा!",
    "bn": "আমি কিষান হেল্পার — আমি শুধু কৃষি, ফসল, আবহাওয়া, বাজার দর এবং সরকারি প্রকল্প নিয়ে সাহায্য করতে পারি। আপনার খামার সম্পর্কে কিছু জিজ্ঞাসা করুন!",
    "te": "నేను కిసాన్ హెల్పర్ — నేను వ్యవసాయం, పంటలు, వాతావరణం, మార్కెట్ ధరలు మరియు ప్రభుత్వ పథకాల గురించి మాత్రమే సహాయం చేయగలను. మీ పొలం గురించి అడగండి!",
    "mr": "मी किसान हेल्पर आहे — मी फक्त शेती, पिके, हवामान, बाजारभाव आणि सरकारी योजनांबाबत मदत करू शकतो. आपल्या शेताबद्दल काही विचारा!",
    "ta": "நான் கிசான் ஹெல்பர் — விவசாயம், பயிர்கள், வானிலை, சந்தை விலைகள் மற்றும் அரசு திட்டங்கள் பற்றி மட்டுமே உதவ முடியும். உங்கள் விவசாயம் பற்றி கேளுங்கள்!",
    "gu": "હું કિસાન હેલ્પર છું — હું ફક્ત ખેતી, પાક, હવામાન, બજાર ભાવ અને સરકારી યોજનાઓમાં મદદ કરી શકું છું. તમારા ખેતર વિશે કંઈક પૂછો!",
    "kn": "ನಾನು ಕಿಸಾನ್ ಹೆಲ್ಪರ್ — ನಾನು ಕೃಷಿ, ಬೆಳೆಗಳು, ಹವಾಮಾನ, ಮಾರುಕಟ್ಟೆ ಬೆಲೆಗಳು ಮತ್ತು ಸರ್ಕಾರಿ ಯೋಜನೆಗಳ ಬಗ್ಗೆ ಮಾತ್ರ ಸಹಾಯ ಮಾಡಬಲ್ಲೆ. ನಿಮ್ಮ ಹೊಲದ ಬಗ್ಗೆ ಕೇಳಿ!",
    "ml": "ഞാൻ കിസാൻ ഹെൽപ്പർ ആണ് — എനിക്ക് കൃഷി, വിളകൾ, കാലാവസ്ഥ, മാർക്കറ്റ് വിലകൾ, സർക്കാർ പദ്ധതികൾ എന്നിവയിൽ മാത്രമേ സഹായിക്കാൻ കഴിയൂ. നിങ്ങളുടെ കൃഷിയെക്കുറിച്ച് ചോദിക്കൂ!",
    "pa": "ਮੈਂ ਕਿਸਾਨ ਹੈਲਪਰ ਹਾਂ — ਮੈਂ ਸਿਰਫ਼ ਖੇਤੀ, ਫਸਲਾਂ, ਮੌਸਮ, ਮੰਡੀ ਭਾਅ ਅਤੇ ਸਰਕਾਰੀ ਸਕੀਮਾਂ ਵਿੱਚ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ। ਆਪਣੇ ਖੇਤ ਬਾਰੇ ਕੁਝ ਪੁੱਛੋ!",
    "ur": "میں کسان ہیلپر ہوں — میں صرف کاشتکاری، فصلوں، موسم، منڈی کے نرخوں اور سرکاری اسکیموں میں مدد کر سکتا ہوں۔ اپنے کھیت کے بارے میں کچھ پوچھیں!",
}


def _off_topic_reply(lang: str) -> str:
    return _OFF_TOPIC_REPLIES.get(lang, _OFF_TOPIC_REPLIES["en"])


# ─── Chatbot tool-calling gateway ───────────────────────────────────────────
# Instead of letting the LLM guess at market prices, seasonal advice, or
# crop suggestions, detect what the farmer is actually asking for and fetch
# the SAME live data the rest of the app uses, then inject it into the
# system prompt as ground truth. Also returns short "summaries" that get
# appended to the reply directly, so the farmer is guaranteed to see the
# real numbers even if the model's wording glosses over them.
_CHAT_INTENT_KEYWORDS = {
    "market":   ["price", "mandi", "rate", "sell", "bhav", "daam", "market", "kharid"],
    "crops":    ["what to grow", "which crop", "recommend crop", "suggest crop",
                 "what should i plant", "which seed", "best crop"],
    "seasonal": ["season", "calendar", "when to sow", "when to plant", "monsoon prep",
                 "rabi", "kharif", "zaid"],
    "weather":  ["weather", "rain", "mausam", "barish", "temperature", "forecast",
                 "will it rain", "humid", "garmi", "thand"],
    "alerts":   ["alert", "warning", "risk", "danger", "chetavani", "khatra",
                 "any warning", "safe today"],
}


def _chat_detect_intents(text: str) -> set:
    low = (text or "").lower()
    intents = set()
    for intent, keywords in _CHAT_INTENT_KEYWORDS.items():
        if any(k in low for k in keywords):
            intents.add(intent)
    return intents


def _geocode_city(name):
    """Best-effort city name -> (lat, lon, resolved name, state) using
    Open-Meteo's free geocoding endpoint (no API key needed, no extra
    account to manage). Returns None on any failure so callers can fall
    back to the dashboard's known location instead of guessing."""
    if not name:
        return None
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "countryCode": "IN"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results")
        if not results:
            return None
        r = results[0]
        return {"lat": r["latitude"], "lon": r["longitude"],
                "name": r.get("name", name), "state": r.get("admin1")}
    except Exception as e:
        logger.warning(f"[ChatGateway] geocoding failed for '{name}': {e}")
        return None


# Matches "in Lucknow", "at Pune", "near Nashik", "for Indore" etc. so the
# gateway can tell WHICH city a message is actually asking about, instead
# of always assuming the farmer's current dashboard location.
_CHAT_LOCATION_RX = re.compile(
    r"\b(?:in|at|near|for)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})\b", re.IGNORECASE
)
# Common words that follow "in/at/near/for" without naming a place
# ("in the morning", "for me", "near here") — reject these so they don't
# get treated as a city name and geocoded.
_CHAT_LOCATION_STOPWORDS = {
    "the", "my", "our", "your", "this", "that", "these", "those", "here",
    "there", "me", "us", "today", "tomorrow", "morning", "evening", "night",
    "future", "general", "case", "some", "any", "field", "farm",
}


def _chat_detect_city(text: str):
    m = _CHAT_LOCATION_RX.search(text or "")
    if not m:
        return None
    candidate = m.group(1).strip()
    first_word = candidate.split()[0].lower()
    if first_word in _CHAT_LOCATION_STOPWORDS:
        return None
    return candidate


# Common crop name -> aliases (English + Hindi transliteration + regional
# spelling variants), enough to catch the vast majority of real farmer
# questions without needing a huge/complete taxonomy.
_CHAT_COMMODITY_ALIASES = {
    "Wheat": ["wheat", "gehun", "gehu"],
    "Rice": ["rice", "paddy", "chawal", "dhan"],
    "Maize": ["maize", "corn", "makka", "bhutta"],
    "Potato": ["potato", "aloo", "alu"],
    "Onion": ["onion", "pyaz", "piyaz"],
    "Tomato": ["tomato", "tamatar"],
    "Cotton": ["cotton", "kapas"],
    "Sugarcane": ["sugarcane", "ganna"],
    "Soybean": ["soybean", "soya"],
    "Mustard": ["mustard", "sarson"],
    "Groundnut": ["groundnut", "peanut", "mungfali"],
    "Gram": ["gram", "chana", "chickpea"],
    "Turmeric": ["turmeric", "haldi"],
    "Chilli": ["chilli", "chili", "mirch"],
    "Banana": ["banana", "kela"],
    "Mango": ["mango", "aam"],
}


def _chat_detect_commodity(text: str):
    low = (text or "").lower()
    for name, aliases in _CHAT_COMMODITY_ALIASES.items():
        if any(a in low for a in aliases):
            return name
    return None


def _fetch_current_weather(lat, lon):
    """Standalone current-conditions fetch used by the chat tool gateway.
    Kept separate from the main /api/weather route (which also merges in
    forecast + Open-Meteo data via a ThreadPoolExecutor) so this stays a
    fast, single-purpose call — chat needs "what's it like right now",
    not the full multi-source forecast merge."""
    if not lat or not lon:
        return None
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        d = resp.json()
        return {
            "city": d.get("name", "Your Location"),
            "temp": round(d["main"]["temp"]),
            "feels_like": round(d["main"]["feels_like"]),
            "humidity": d["main"]["humidity"],
            "description": d["weather"][0]["description"],
            "wind_speed": d["wind"]["speed"],
            "rain": d.get("rain", {}).get("1h", 0),
        }
    except Exception as e:
        logger.warning(f"[ChatGateway] weather fetch failed: {e}")
        return None


def _resolve_chat_location(city_hint, lat, lon):
    """If the message names a specific city, geocode it and use THAT
    location. Otherwise fall back to whatever the dashboard already sent
    (current location). Returns (lat, lon, display_name)."""
    if city_hint:
        geo = _geocode_city(city_hint)
        if geo:
            return geo["lat"], geo["lon"], geo["name"]
    return lat, lon, city_hint


def _chat_weather_tool(city_hint, lat, lon):
    """Live current weather for the chat gateway, resolved independently
    of the dashboard — so chat can answer "will it rain in Lucknow"
    correctly even if the farmer's dashboard location is somewhere else
    entirely, or if they open chat without ever loading the dashboard."""
    r_lat, r_lon, r_name = _resolve_chat_location(city_hint, lat, lon)
    if not r_lat or not r_lon:
        return {"ok": False}
    wx = _fetch_current_weather(r_lat, r_lon)
    if not wx:
        return {"ok": False}
    return {"ok": True, "city": r_name or wx["city"], "weather": wx}


def _chat_alerts_tool(city_hint, lat, lon):
    """Real, rule-based risk alerts for the chat gateway — same engine
    (_compute_alerts_for_conditions) your dedicated Alerts page uses,
    driven by freshly-fetched live weather rather than the LLM guessing
    at risk. Lets a farmer ask "any warnings today?" directly in chat."""
    r_lat, r_lon, r_name = _resolve_chat_location(city_hint, lat, lon)
    if not r_lat or not r_lon:
        return {"ok": False}
    wx = _fetch_current_weather(r_lat, r_lon)
    if not wx:
        return {"ok": False}
    try:
        alerts = _compute_alerts_for_conditions(
            wx["temp"], wx["humidity"], wx["wind_speed"], wx["rain"], wx["description"]
        )
    except Exception as e:
        logger.warning(f"[ChatGateway] alerts tool failed: {e}")
        return {"ok": False}
    titles = [a.get("title") for a in alerts if a.get("title")]
    return {"ok": True, "city": r_name or wx["city"], "alerts": titles}


def _chat_fetch_market_rows(state, commodity=None):
    """Three-tier market lookup so a chat price question never hangs or
    comes back empty:
      1. Shared cache (instant, same cache /api/market already uses)
      2. A fresh live fetch, capped to 6s so chat stays responsive even
         if the government feed is slow
      3. If both fail, return [] honestly rather than fabricating a
         price — the reply will fall back to general advice instead."""
    now = time.monotonic()
    cached = _agmark_fetch_cache.get(state)
    if cached and (now - cached[0]) < AGMARK_CACHE_TTL_SEC:
        rows = cached[1]
    else:
        rows = None
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(fetch_agmarknet_prices, state)
                rows = fut.result(timeout=6)
        except Exception as e:
            logger.warning(f"[ChatGateway] live market fetch timed out/failed for {state}: {e}")
            rows = None

    if not rows:
        return []

    if commodity:
        filtered = [r for r in rows
                    if commodity.lower() in str(r.get("crop", "")).lower()]
        if filtered:
            rows = filtered

    return rows


def _chat_market_tool(state: str, commodity: str = None):
    """Live mandi prices for a state, optionally filtered to one commodity
    the farmer actually asked about, reusing the exact same government
    data source and cache as /api/market."""
    if not state:
        return {"ok": False}
    rows = _chat_fetch_market_rows(state, commodity)
    if not rows:
        return {"ok": False}
    top = rows[:5]
    return {"ok": True, "state": state, "rows": top}


def _chat_crops_tool(temp, humidity, rain, city, lat, lon):
    """Rule-based crop suggestions (no extra LLM call — keeps this tool
    fast and cheap) using the exact same logic as /api/crop-recommendations'
    offline fallback."""
    try:
        season = get_season(datetime.now().month)
        crops = recommend_crops(temp or 25, humidity or 60, rain or 0, season, city, lat, lon)
        return {"ok": True, "season": season, "crops": [c.get("name") for c in crops[:5] if c.get("name")]}
    except Exception as e:
        logger.warning(f"[ChatGateway] crops tool failed: {e}")
        return {"ok": False}


def _chat_seasonal_tool(city, humidity):
    try:
        season, alerts = _seasonal_advisories(city or "your area", humidity)
        return {"ok": True, "season": season, "alerts": [a["message"] for a in alerts]}
    except Exception as e:
        logger.warning(f"[ChatGateway] seasonal tool failed: {e}")
        return {"ok": False}


def _chat_run_gateway(intents, context_data, message_text=""):
    """Execute the requested tools and return (sections, summaries).
    sections  — [] str, appended to the LLM's context so the reply uses LIVE data.
    summaries — [] str, brief one-line LIVE-data notes appended to the reply so
                the farmer is guaranteed to see the fetched numbers even if
                the model's wording omits them."""
    city = context_data.get("city") or ""
    lat = context_data.get("lat")
    lon = context_data.get("lon")
    temp = context_data.get("temp")
    humidity = context_data.get("humidity")
    rain = context_data.get("rain")

    # A city NAMED in the message (e.g. "wheat price in Lucknow") always
    # wins over the dashboard's current location — a farmer asking about
    # somewhere else shouldn't get data for where they're currently
    # standing.
    named_city = _chat_detect_city(message_text)
    commodity = _chat_detect_commodity(message_text)

    # For the market tool specifically: Agmarknet's API filters by STATE
    # name, not city. "Lucknow" would silently return zero records — it
    # needs to resolve to "Uttar Pradesh" first. Open-Meteo's geocoding
    # response includes admin1 (the state) alongside lat/lon, so reuse
    # that instead of passing the raw city name straight through.
    named_state = None
    if named_city:
        geo = _geocode_city(named_city)
        if geo and geo.get("state"):
            named_state = geo["state"]
    state = named_state or context_data.get("state") or city

    sections = []
    summaries = []

    if "market" in intents and state:
        res = _chat_market_tool(state, commodity)
        if res.get("ok"):
            lines = "; ".join(
                f"{r.get('crop', '?')}: ₹{r.get('price', '?')}/quintal"
                for r in res["rows"]
            )
            sections.append(f"[LIVE MANDI PRICES for {state}] {lines}")
            summaries.append(f"💰 Live prices for {state}: {lines}")

    if "crops" in intents:
        res = _chat_crops_tool(temp, humidity, rain, city, lat, lon)
        if res.get("ok") and res["crops"]:
            crop_list = ", ".join(res["crops"])
            sections.append(f"[LIVE CROP SUGGESTIONS] Season: {res['season']} | Suggested: {crop_list}")

    if "seasonal" in intents:
        res = _chat_seasonal_tool(city, humidity)
        if res.get("ok"):
            sections.append(f"[LIVE SEASONAL ADVISORY] Season: {res['season']} | " + " ".join(res["alerts"]))

    if "weather" in intents:
        res = _chat_weather_tool(named_city, lat, lon)
        if res.get("ok"):
            w = res["weather"]
            line = (f"{w['temp']}°C (feels {w['feels_like']}°C), {w['description']}, "
                    f"humidity {w['humidity']}%, wind {w['wind_speed']} km/h, rain {w['rain']}mm")
            sections.append(f"[LIVE WEATHER for {res['city']}] {line}")
            summaries.append(f"🌤️ Current weather in {res['city']}: {line}")

    if "alerts" in intents:
        res = _chat_alerts_tool(named_city, lat, lon)
        if res.get("ok"):
            if res["alerts"]:
                sections.append(f"[LIVE RISK ALERTS for {res['city']}] " + "; ".join(res["alerts"]))
            else:
                sections.append(f"[LIVE RISK ALERTS for {res['city']}] No active risk alerts right now.")

    return sections, summaries


@app.route("/api/chat", methods=["POST"])
def kisan_chat():
    model = "openai/gpt-oss-120b"   # replaces deprecated llama-3.3-70b-versatile (shut down 08/16/26)

    ip = request.remote_addr or "unknown"
    if _is_rate_limited_chat(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.json or {}
    messages = data.get("messages", [])
    lang = data.get("lang", "en")
    if not messages:
        return jsonify({"error": "No messages"}), 400

    lang_name = LANG_NAMES.get(lang, "English")

    # ── Hard intercept for restricted crops ──────────────────────────────────
    # Prompt instructions alone aren't 100% reliable for LLMs, especially
    # live in front of an audience — this check runs BEFORE calling Groq at
    # all, so the app can never accidentally give cultivation advice for a
    # controlled/licensed-only crop, regardless of how the AI would have
    # responded.
    _RESTRICTED_CROP_TERMS = ("tobacco", "opium", "poppy", "cannabis", "hemp", "marijuana", "ganja")
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = (m.get("content") or "").lower()
            break
    if any(term in last_user_msg for term in _RESTRICTED_CROP_TERMS):
        _RESTRICTED_REPLY = {
            "en": "This app focuses on common food and commercial crops and doesn't advise on tobacco, opium, cannabis/hemp, or other licensed/controlled crops. I'd be happy to suggest a suitable alternative crop for your area instead!",
            "hi": "यह ऐप सामान्य खाद्य और वाणिज्यिक फसलों पर केंद्रित है और तंबाकू, अफीम, भांग/गांजा जैसी लाइसेंस-नियंत्रित फसलों पर सलाह नहीं देता। मैं आपके क्षेत्र के लिए एक उपयुक्त वैकल्पिक फसल सुझाने में खुशी से मदद करूंगा!",
        }
        return jsonify({"reply": _RESTRICTED_REPLY.get(lang, _RESTRICTED_REPLY["en"])})

    # ── Hard topic gate ────────────────────────────────────────────────────
    # Runs before any Groq/Gemini call, same reasoning as the restricted-crop
    # intercept above: a keyword-only check the model can't be talked around.
    if not _chat_message_on_topic(last_user_msg):
        return jsonify({"reply": _off_topic_reply(lang)})

    # ── Build location/weather context string from dashboard data ──────────────
    wx = data.get("weather_context") or {}
    location_block = ""
    if wx:
        city    = wx.get("city") or wx.get("location") or ""
        temp    = wx.get("temp") or wx.get("temperature") or ""
        humidity= wx.get("humidity", "")
        rain    = wx.get("rain") or wx.get("rain_mm") or wx.get("precipitation") or ""
        desc    = wx.get("description") or wx.get("weather_desc") or ""
        season  = wx.get("season", "")
        lat     = wx.get("lat") or wx.get("latitude") or ""
        lon     = wx.get("lon") or wx.get("longitude") or ""

        parts = []
        if city:     parts.append(f"Location: {city}")
        if lat and lon: parts.append(f"Coordinates: {lat}, {lon}")
        if temp:     parts.append(f"Temperature: {temp}°C")
        if humidity: parts.append(f"Humidity: {humidity}%")
        if rain:     parts.append(f"Rain: {rain}mm")
        if desc:     parts.append(f"Weather: {desc}")
        if season:   parts.append(f"Season: {season}")

        if parts:
            location_block = (
                "\n\nFARMER'S CURRENT LOCATION & WEATHER (from Dashboard):\n"
                + "\n".join(parts)
                + "\nUse this data directly when the user asks about their location, weather, or what to grow."
            )
    else:
        city = temp = humidity = rain = ""
        lat = lon = ""

    # ── Tool-calling gateway: detect intent, fetch LIVE data, inject as context ──
    intents = _chat_detect_intents(last_user_msg)
    gateway_summaries = []
    if intents:
        gateway_sections, gateway_summaries = _chat_run_gateway(intents, {
            "city": city, "lat": lat, "lon": lon,
            "temp": temp, "humidity": humidity, "rain": rain,
            "state": city,  # fetch_agmarknet_prices resolves common city/state names internally
        }, message_text=last_user_msg)
        if gateway_sections:
            location_block += "\n\n" + "\n".join(gateway_sections)

    system_prompt = f"""You are Kisan Helper, a smart AI assistant for Indian farmers in the SmartAgro app.
Answer ONLY: Agriculture, Crops, Soil, Pest Control, Fertilizers, Irrigation, Water Management, Govt schemes (PM-KISAN, PMFBY, KCC), SmartAgro app features.
For anything unrelated, politely refuse in {lang_name}.
Answer in {lang_name} (native script). Be SHORT and COMPLETE: max 4-5 bullet points or 3 sentences. Never leave an answer unfinished.

App Navigation: If the user asks about checking features, provide direct Markdown links to navigate there. Use EXACTLY these formats:
- Dashboard/Home/Location: [Dashboard](/)
- Crop Health/Disease/Upload Photo: [Diagnose Crop](/diagnose)
- Market Prices/Mandi: [Market Prices](/market)
- Weather Alerts/Forecast: [Alerts](/alerts)
No markdown headers (#, ##). No asterisks for bullets — use • instead.{location_block}

SOIL KNOWLEDGE: You know about soil types (clay, loamy, sandy, silt, black, red, alluvial, laterite), pH levels, nutrients (NPK), organic matter, soil testing, and which crops suit which soil.

APP SECTION RULES — only suggest a section when it is DIRECTLY relevant:
• Suggest [Diagnose Crop](/diagnose) ONLY if the user asks about crop disease, leaf spots, pest infestation, plant infection, or crop health problems.
• Suggest [Market Prices](/market) ONLY if the user asks about mandi rates, selling price, MSP, commodity prices, or where to sell crops.
• Suggest [Dashboard](/) ONLY if the user asks about weather forecast, rain, temperature, or local weather conditions.
• Suggest [Alerts](/alerts) ONLY if the user asks about severe weather warnings, flood, frost, storm, or pest outbreak warnings.
• Mention the Helpline (1800-180-1551, bottom-left button) ONLY if the user needs expert phone support.
• For general farming questions (how to grow, fertilizer, irrigation, soil, seasons) — answer directly WITHOUT suggesting any app section unless it truly helps.

LOCATION ANSWERS: If the farmer asks what to grow, is this good weather, or questions about their location — use the FARMER'S CURRENT LOCATION & WEATHER data above to give a specific, direct answer.

RESTRICTED CROPS: Never give cultivation advice, growing steps, or encouragement for tobacco, opium poppy, cannabis/hemp, or other controlled/licensed-only crops, even if agronomically asked about or technically legal with a special government license. If asked, briefly note that this app focuses on common food and commercial crops and doesn't advise on licensed/controlled crops, then offer to help with a suitable alternative crop for their location instead."""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model":       model,
        "messages":    [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.5,
        "max_tokens":  700,
        "stream":      False
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            res_json = resp.json()
            reply = res_json["choices"][0]["message"]["content"].strip()
            if gateway_summaries:
                reply += "\n\n" + "\n".join(gateway_summaries)
            return jsonify({"reply": reply})

        # Groq unavailable (rate-limited, model error, etc.) — try Gemini
        # with the exact same prompt/context before giving up, so the
        # farmer gets a real answer instead of a canned message whenever
        # possible.
        logger.info(f"[Chat] Groq returned {resp.status_code}, trying Gemini fallback")
        gemini_reply = _gemini_chat_reply(system_prompt, messages)
        if gemini_reply:
            if gateway_summaries:
                gemini_reply += "\n\n" + "\n".join(gateway_summaries)
            return jsonify({"reply": gemini_reply})

        return jsonify({"error": _chat_fallback_reply(messages, lang)}), 500
    except Exception as e:
        logger.warning(f"[Chat error] {e} — trying Gemini fallback")
        gemini_reply = _gemini_chat_reply(system_prompt, messages)
        if gemini_reply:
            if gateway_summaries:
                gemini_reply += "\n\n" + "\n".join(gateway_summaries)
            return jsonify({"reply": gemini_reply})
        return jsonify({"error": _chat_fallback_reply(messages, lang)}), 500


# ─── Kisan Helper — Speech-to-Text (Groq Whisper) ────────────────────────────
MAX_AUDIO_B64_LEN = 8 * 1024 * 1024  # ~6 MB raw audio


@app.route("/api/stt", methods=["POST"])
def speech_to_text():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY not set in .env"}), 500

    ip = request.remote_addr or "unknown"
    if _is_rate_limited_stt(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "No audio received"}), 400

    audio_bytes = audio_file.read()
    if len(audio_bytes) > MAX_AUDIO_B64_LEN:
        return jsonify({"error": "Recording too long. Please keep it under ~60 seconds."}), 413
    if len(audio_bytes) < 500:
        return jsonify({"error": "Recording too short or empty. Please try again."}), 400

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {
        "file": (audio_file.filename or "voice.webm", audio_bytes, audio_file.mimetype or "audio/webm"),
    }
    form_data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "json",
        "temperature": 0,
    }

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=form_data, timeout=30
        )
        if resp.status_code == 429:
            return jsonify({
                "error": "Whisper STT Limit Reached!",
                "limit_reached": True,
                "api_name": "Groq Whisper Large V3",
                "details": "Daily request limit or hourly audio limit reached for Whisper STT."
            }), 429
        if resp.status_code != 200:
            logger.warning(f"[STT error] {resp.status_code}: {resp.text[:300]}")
            return jsonify({"error": "Could not transcribe audio"}), 500


        text = resp.json().get("text", "").strip()
        return jsonify({"text": text})
    except Exception as e:
        logger.warning(f"[STT exception] {e}")
        return jsonify({"error": str(e)}), 500


# ─── Shared sliding-window rate limiter ───────────────────────────────────────
# Chat, STT, diagnosis, and translation endpoints each throttle per-IP with the
# SAME sliding-window logic. The state lives in-process, so it is only
# consistent under a SINGLE gunicorn worker (see Dockerfile: --workers 1).
_rate_limit_state = {}
_rate_limit_state_lock = threading.Lock()

CHAT_LIMIT    = 20
STT_LIMIT     = 20
DIAGNOSE_LIMIT = 10


def _rate_limit(action: str, ip: str, limit: int, window_seconds: int = 60) -> bool:
    """Sliding-window rate limit. Returns True if allowed, False if exceeded."""
    now = datetime.now().timestamp()
    with _rate_limit_state_lock:
        bucket = _rate_limit_state.setdefault(action, {})
        times = [t for t in bucket.get(ip, []) if now - t < window_seconds]
        if len(times) >= limit:
            bucket[ip] = times
            return False
        times.append(now)
        bucket[ip] = times
        return True


def _is_rate_limited_chat(ip: str) -> bool:
    return not _rate_limit("chat", ip, CHAT_LIMIT)

def _is_rate_limited_stt(ip: str) -> bool:
    return not _rate_limit("stt", ip, STT_LIMIT)

def _is_rate_limited_diagnose(ip: str) -> bool:
    return not _rate_limit("diagnose", ip, DIAGNOSE_LIMIT)


# ─── Diagnose Crop via ensemble (Groq + Gemini) ───────────────────────────────
MAX_IMAGE_B64_LEN = 14 * 1024 * 1024  # ~10 MB raw image

# Vision-capable models tried per ensemble pass. Today Groq only has one
# production-viable multimodal model on the general tier — meta-llama/llama-4-
# scout-17b-16e-instruct was deprecated June 17, 2026. Add a second entry here
# as soon as one exists; no other code needs to change.
vision_models = [
    "qwen/qwen3.6-27b",
]


def ai_is_crop_image(image_b64):
    """Fast, low-token sanity check BEFORE running the full diagnosis prompt:
    does this photo actually show a plant/crop part? Without this, the main
    prompt will happily hallucinate a plausible-sounding disease name for a
    photo of a hand, a sack of grain, or a selfie — which is worse than
    useless for a farmer trying to protect a crop.
    Fails OPEN (assumes "yes, it's a plant") on any error/timeout/missing
    key, so a flaky classifier call never blocks a genuine diagnosis.
    Returns (is_plant: bool, reason: str | None)."""
    if not GROQ_API_KEY:
        return True, None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": vision_models[0],
        "messages": [
            {"role": "system", "content": "You classify images. Return ONLY valid JSON, nothing else."},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": (
                    "Does this image show a plant, crop, leaf, stem, fruit, root, or "
                    "other plant/agricultural material (even if diseased, damaged, or "
                    "unclear)? Respond with ONLY this JSON: "
                    '{"is_plant": true or false, "reason": "very short reason if false"}'
                )},
            ]}
        ],
        "temperature": 0.0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=body, timeout=8,
        )
        if resp.status_code != 200:
            return True, None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        parsed = _extract_json_object(raw)
        if not parsed or "is_plant" not in parsed:
            return True, None
        if parsed.get("is_plant") is False:
            return False, parsed.get("reason") or "The photo doesn't appear to show a plant or crop."
        return True, None
    except Exception as e:
        logger.warning(f"[Diagnose] ai_is_crop_image check failed (failing open): {e}")
        return True, None


def _run_vision_pass(image_b64, prompt, sys_prompt, model, temperature):
    """Run one diagnosis pass against one Groq vision model and return parsed
    JSON, or None if that pass failed."""
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
        "reasoning_effort": "none",
    }
    resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers=headers, json=body, timeout=45)
    if resp.status_code != 200:
        return None
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group())


def _run_gemini_pass(image_b64, prompt, sys_prompt):
    """Run one diagnosis pass against Google's Gemini API and return parsed
    JSON, or None on any failure. When GEMINI_API_KEY is configured this gives
    the ensemble a genuinely INDEPENDENT second model (different vendor,
    different weights) so an agreement between Groq & Gemini is real
    cross-model evidence."""
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_DIAGNOSIS_MODEL}:generateContent")
    body = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 3000,
                             "responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json",
                            "x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=45)
        if resp.status_code != 200:
            logger.warning(f"[Diagnose] Gemini HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        return json.loads(match.group())
    except Exception as e:
        logger.warning(f"[Diagnose] Gemini exception: {e}")
        return None


def _diseases_agree(name_a, name_b):
    """Fuzzy-match two disease name strings so small phrasing differences
    between passes still count as agreement, while genuinely different
    diagnoses are correctly flagged as a disagreement."""
    if not name_a or not name_b:
        return False
    a, b = name_a.strip().lower(), name_b.strip().lower()
    if a == b:
        return True
    if "healthy" in a and "healthy" in b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


# ── QA logging: store every image + result for human spot-checking ──────
def _save_diagnosis_image(image_b64, record_id):
    """Persist the uploaded image next to its diagnosis record so a
    reviewer can see exactly what the model saw. Returns the saved filename,
    or None on failure (non-fatal)."""
    try:
        raw = base64.b64decode(image_b64)
        img_hash = hashlib.sha256(raw).hexdigest()[:12]
        filename = f"{record_id}_{img_hash}.jpg"
        with open(os.path.join(DIAGNOSIS_IMAGES_DIR, filename), "wb") as f:
            f.write(raw)
        return filename
    except Exception as e:
        logger.warning(f"[DiagnosisLog] Could not save image: {e}")
        return None


def _log_diagnosis(record):
    """Append one diagnosis record (image ref + every ensemble pass' raw
    output + the merged final answer) to a JSONL audit log."""
    try:
        with _diagnosis_log_lock:
            with open(DIAGNOSIS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[DiagnosisLog] Could not write log entry: {e}")


@app.route("/api/diagnose", methods=["POST"])
def diagnose_crop():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY not set in .env"}), 500

    ip = request.remote_addr or "unknown"
    if _is_rate_limited_diagnose(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.json or {}
    image_b64 = data.get("image", "")
    lang      = str(data.get("lang", "en")).strip().lower()

    if not image_b64:
        return jsonify({"error": "No image data received"}), 400
    if len(image_b64) > MAX_IMAGE_B64_LEN:
        return jsonify({"error": "Image too large. Please use an image under 10 MB."}), 413

    # ── Step 1: decode + validate the image ──────────────────────────────
    try:
        image_raw = base64.b64decode(image_b64)
    except Exception:
        return jsonify({"error": "Image data is not valid base64"}), 400
    image_sha256 = hashlib.sha256(image_raw).hexdigest()

    # ── Step 1b: sanity-check the photo actually shows a plant ────────────
    # Cheap classifier pass before spending tokens on the full diagnosis
    # prompt. Fails open, so this never blocks a genuine diagnosis if the
    # check itself errors or times out.
    is_plant, reject_reason = ai_is_crop_image(image_b64)
    if not is_plant:
        return jsonify({
            "not_a_crop_image": True,
            "error": reject_reason or "This doesn't look like a photo of a plant or crop. "
                     "Please upload a clear photo of the affected leaf, stem, fruit, or root.",
        }), 422

    lang_name = LANG_NAMES.get(lang, "")
    if lang != "en" and lang_name:
        lang_instruction = (
            f"\n\nIMPORTANT: Write ALL text values in {lang_name} "
            f"(except JSON keys, numbers, chemical/brand names, units such as "
            f"kg/ha, ml/L, g/ha, %, SL, EC, SC, WP, SG, NPK, and dose figures — "
            f"keep those in English/digits as-is)."
        )
    else:
        lang_instruction = ""

    prompt = f"""You are an expert agricultural plant pathologist AI. Look very carefully at this crop image.
Respond ONLY with valid JSON, no markdown or backticks:
{{
  "disease": "Exact disease name",
  "confidence": 88,
  "severity": "Mild or Moderate or Severe",
  "affected_part": "Leaves/Stem/Fruit/Root/Cob",
  "cause": "Specific pathogen and spread method",
  "eco_remedies": [{{"remedy": "Remedy", "method": "Steps", "frequency": "How often", "effectiveness": 80}}],
  "chemical_remedies": [{{"name": "Chemical", "dose": "Dose per litre", "interval": "Days between sprays"}}],
  "prevention": ["tip1", "tip2", "tip3"],
  "recovery_timeline": "Weeks for recovery"
}}{lang_instruction}"""

    sys_prompt = "Expert plant pathologist. Return ONLY valid JSON."
    if lang != "en" and lang_name:
        sys_prompt += f" All free-text values must be in {lang_name}."

    # ── Step 2: ensemble / self-consistency passes ──────────────────────
    # With a single Groq vision model configured (today's reality) this runs
    # that model twice at different temperatures as a self-consistency
    # cross-check. When a Gemini key is configured, a genuinely INDEPENDENT
    # second model is appended to the ensemble — so an agreement between
    # Groq & Gemini is real cross-model evidence.
    pass_temperatures = [0.2, 0.6, 0.9]
    pass_plan = [
        (i, vision_models[i % len(vision_models)], pass_temperatures[i % len(pass_temperatures)])
        for i in range(ENSEMBLE_PASSES)
    ]
    if GEMINI_API_KEY:
        pass_plan.append((len(pass_plan), "gemini", 0.3))
    pass_outcomes = [None] * len(pass_plan)

    def _run_pass(i, model, temp):
        try:
            if model == "gemini":
                return _run_gemini_pass(image_b64, prompt, sys_prompt)
            return _run_vision_pass(image_b64, prompt, sys_prompt, model, temp)
        except Exception as e:
            logger.warning(f"[Diagnose] pass {i} ({model}) failed: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pass_plan)) as executor:
        future_to_pass = {
            executor.submit(_run_pass, i, model, temp): (i, model)
            for i, model, temp in pass_plan
        }
        for future in concurrent.futures.as_completed(future_to_pass):
            i, model = future_to_pass[future]
            parsed = future.result()
            if parsed and parsed.get("disease"):
                pass_outcomes[i] = (parsed, model)

    results, models_used = [], []
    gemini_display = f"gemini:{GEMINI_DIAGNOSIS_MODEL}"
    for outcome in pass_outcomes:
        if outcome is not None:
            parsed, model = outcome
            results.append(parsed)
            models_used.append(gemini_display if model == "gemini" else model)

    if not results:
        if not GEMINI_API_KEY:
            return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 500
        return jsonify({"error": "All vision models failed. Check your GROQ_API_KEY in .env"}), 500

    # ── Step 3: merge / vote across passes ───────────────────────────────
    primary = max(results, key=lambda r: r.get("confidence", 0))
    others  = [r for r in results if r is not primary]

    agreement = True
    alternate_diagnosis = None
    if others:
        agree_flags = [_diseases_agree(primary.get("disease", ""), o.get("disease", "")) for o in others]
        agreement = all(agree_flags)
        if agreement:
            confidences = [r.get("confidence", 0) for r in results]
            primary["confidence"] = min(99, round(sum(confidences) / len(confidences)) + 5)
        else:
            primary["confidence"] = max(30, round(primary.get("confidence", 50) * 0.7))
            disagreeing = next((o for o, f in zip(others, agree_flags) if not f), None)
            if disagreeing:
                alternate_diagnosis = disagreeing.get("disease")

    primary["_lang"] = lang
    primary["model_agreement"] = agreement
    primary["_passes_run"] = len(results)
    primary["_models_used"] = models_used
    if alternate_diagnosis:
        primary["alternate_diagnosis"] = alternate_diagnosis

    # ── Step 4: log image + full result for human spot-checking ─────────
    record_id = f"{int(time.time()*1000)}_{ip.replace('.', '-').replace(':', '-')}"
    image_filename = _save_diagnosis_image(image_b64, record_id)
    _log_diagnosis({
        "id":                   record_id,
        "timestamp":            datetime.now().isoformat(),
        "ip":                   ip,
        "lang":                 lang,
        "image_sha256":         image_sha256,
        "models_used":          models_used,
        "passes_run":           len(results),
        "model_agreement":      agreement,
        "final_disease":        primary.get("disease"),
        "final_confidence":     primary.get("confidence"),
        "alternate_diagnosis":  alternate_diagnosis,
        "severity":             primary.get("severity"),
        "image_file":           image_filename,
        "raw_results":          results,
        "response":             primary,
        "human_reviewed":       False,
        "human_verdict":        None,
    })

    return jsonify(primary)


# ─── Diagnosis QA review endpoints (internal, DEBUG_MODE only) ──────────────
@app.route('/api/diagnose-log')
def diagnose_log():
    """Lets a human reviewer list recent diagnoses to spot-check the AI
    against the real uploaded photo. Gated behind FLASK_DEBUG=1."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    limit = min(int(request.args.get('limit', 50)), 500)
    entries = []
    try:
        with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    return jsonify({"count": len(entries), "entries": entries})


@app.route('/api/diagnose-log/image/<path:filename>')
def diagnose_log_image(filename):
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    safe_name = os.path.basename(filename)
    path = os.path.join(DIAGNOSIS_IMAGES_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path, mimetype="image/jpeg")


@app.route('/api/diagnose-log/review', methods=["POST"])
def diagnose_log_review():
    """Lets a reviewer record a verdict against a logged diagnosis by
    rewriting its line in the JSONL file atomically."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    data = request.json or {}
    record_id = data.get("id")
    verdict = data.get("verdict")
    if not record_id or verdict not in ("correct", "incorrect", "uncertain"):
        return jsonify({"error": "Provide id and verdict (correct|incorrect|uncertain)"}), 400

    try:
        with _diagnosis_log_lock:
            with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            updated = False
            for i, line in enumerate(lines):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == record_id:
                    rec["human_reviewed"] = True
                    rec["human_verdict"] = verdict
                    lines[i] = json.dumps(rec, ensure_ascii=False) + "\n"
                    updated = True
                    break
            if updated:
                with open(DIAGNOSIS_LOG_PATH + ".tmp", "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(DIAGNOSIS_LOG_PATH + ".tmp", DIAGNOSIS_LOG_PATH)
        return jsonify({"status": "ok" if updated else "not_found"}), (200 if updated else 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/diagnose-log/accuracy')
def diagnose_log_accuracy():
    """Computes real accuracy from whatever human verdicts have been recorded."""
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    total = reviewed = correct = 0
    agreement_correct = agreement_total = 0
    try:
        with open(DIAGNOSIS_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("cached"):
                    continue
                total += 1
                if rec.get("human_reviewed"):
                    reviewed += 1
                    is_correct = rec.get("human_verdict") == "correct"
                    if is_correct:
                        correct += 1
                    if rec.get("model_agreement"):
                        agreement_total += 1
                        if is_correct:
                            agreement_correct += 1
    except FileNotFoundError:
        pass

    return jsonify({
        "total_logged":            total,
        "human_reviewed":          reviewed,
        "accuracy_reviewed_only":  round(correct / reviewed, 3) if reviewed else None,
        "agreement_case_accuracy": round(agreement_correct / agreement_total, 3) if agreement_total else None,
        "note": "accuracy_reviewed_only is ONLY meaningful once a human has reviewed "
                "a reasonably-sized, representative sample via /api/diagnose-log/review.",
    })


# ─── Alerts ──────────────────────────────────────────────────────────────────
def _compute_alerts_for_conditions(temp, humidity, wind_speed, rain, description, date_str=None):
    """Upgraded rule engine for a single day of weather. Generates distinct,
    location-and-weather-specific alerts for any range of weather parameters."""
    description = (description or "").lower()
    alerts = []
    d_label = f" ({date_str})" if date_str else ""

    # 1. Temperature-based advisories
    if temp > 38:
        alerts.append({
            "type": "danger", "category": "Weather", "icon": "🌡️",
            "title": f"Extreme Heat Warning{d_label}",
            "message": f"Peak temperature forecast to reach {round(temp)}°C. High risk of crop sun scald and moisture stress.",
            "action": "Increase irrigation frequency to every 3-4 hours. Provide green shade nets for sensitive crops."
        })
    elif temp > 31:
        alerts.append({
            "type": "warning", "category": "Weather", "icon": "☀️",
            "title": f"Moderate Heat Advisory{d_label}",
            "message": f"Warm weather around {round(temp)}°C. Increased crop evapotranspiration expected.",
            "action": "Irrigate during early morning or evening hours to minimize water evaporation."
        })
    elif temp < 10:
        alerts.append({
            "type": "warning", "category": "Weather", "icon": "❄️",
            "title": f"Chill & Frost Alert{d_label}",
            "message": f"Low temperature of {round(temp)}°C detected. Risk of cold injury in young saplings.",
            "action": "Cover nursery beds with plastic mulch or straw. Apply light evening irrigation."
        })
    else:
        alerts.append({
            "type": "info", "category": "Crop Advisory", "icon": "🌱",
            "title": f"Favorable Growth Climate{d_label}",
            "message": f"Comfortable temperature of {round(temp)}°C supporting active photosynthesis.",
            "action": "Favorable day for field cultivation, top-dressing nitrogen, and weeding."
        })

    # 2. Humidity & Pest/Disease advisories
    if humidity > 75:
        alerts.append({
            "type": "warning", "category": "Disease", "icon": "🍄",
            "title": f"Fungal Blight Risk ({humidity}% Humidity)",
            "message": f"Relative humidity at {humidity}%. Moist microclimate promotes fungal spore multiplication.",
            "action": "Apply preventive systemic fungicide (Mancozeb 75 WP at 2.5 g/L) immediately."
        })
    elif humidity > 55:
        alerts.append({
            "type": "warning", "category": "Pest", "icon": "🐛",
            "title": f"Sap-Sucking Pest Watch ({humidity}% Humidity)",
            "message": f"Humidity of {humidity}% with {round(temp)}°C temp favors aphid and whitefly activity.",
            "action": "Set up yellow sticky traps (10/acre) or spray Neem oil (5 ml/L) at dusk."
        })
    else:
        alerts.append({
            "type": "warning", "category": "Pest", "icon": "🕷️",
            "title": f"Mite & Thrips Alert ({humidity}% Humidity)",
            "message": f"Dry atmospheric conditions ({humidity}% humidity) increase spider mite reproduction.",
            "action": "Spray Abamectin 1.8 EC (0.5 ml/L) and maintain soil moisture levels."
        })

    # 3. Wind & Rain advisories
    if rain > 25:
        alerts.append({
            "type": "danger", "category": "Weather", "icon": "🌧️",
            "title": f"Heavy Rain & Drainage Alert ({round(rain)} mm)",
            "message": f"Expected rainfall of {round(rain)} mm. Risk of waterlogging and root asphyxiation.",
            "action": "Ensure field drainage channels are open. Pause all chemical spraying operations."
        })
    elif rain > 2:
        alerts.append({
            "type": "info", "category": "Weather", "icon": "🌦️",
            "title": f"Light Rain Forecast ({round(rain)} mm)",
            "message": f"Intermittent light showers expected ({round(rain)} mm). Replenishes topsoil moisture.",
            "action": "Hold off on routine irrigation for 24-48 hours."
        })
    else:
        if wind_speed > 22:
            alerts.append({
                "type": "warning", "category": "Weather", "icon": "💨",
                "title": f"High Wind Warning ({round(wind_speed)} km/h)",
                "message": f"Wind speeds up to {round(wind_speed)} km/h may cause lodging in tall standing crops.",
                "action": "Avoid foliar pesticide spraying to prevent chemical drift. Stake tall crops."
            })
        else:
            alerts.append({
                "type": "info", "category": "Crop Advisory", "icon": "🌾",
                "title": f"Clear Field Work Window ({round(wind_speed)} km/h wind)",
                "message": f"Dry conditions with gentle wind ({round(wind_speed)} km/h).",
                "action": "Ideal time for fertilizer application, harvesting, and crop drying."
            })

    return alerts


# ─── AI-Enhanced Alerts via Groq ─────────────────────────────────────────────
_ai_alerts_cache = {}
AI_ALERTS_CACHE_TTL_SEC = 3 * 60 * 60  # 3 hours


def _ai_alerts_for_today(city, lat, lon, temp, humidity, wind_speed, rain, description):
    """Ask Groq for location-specific, weather-aware alerts for today."""
    if not GROQ_API_KEY:
        return None

    cache_key = f"today|{city}|{round(lat or 0, 1)}|{round(lon or 0, 1)}|{round(temp/3)*3}|{round(humidity/10)*10}"
    now = time.monotonic()
    cached = _ai_alerts_cache.get(cache_key)
    if cached and (now - cached[0]) < AI_ALERTS_CACHE_TTL_SEC:
        return cached[1]

    prompt = f"""You are an expert agricultural meteorologist for India.

Location: {city or 'Unknown'} (lat {lat}, lon {lon})
Today's weather: {temp}°C, {humidity}% humidity, wind {wind_speed} m/s, {rain} mm rain, {description}

Generate 3–6 specific, actionable agricultural alerts for a farmer at this location
based on TODAY's weather conditions. Each alert must be SPECIFIC to these exact
conditions — do not produce generic alerts.

Categories: Weather, Pest, Disease, Crop Advisory
Types: danger (life/crop threatening), warning (needs attention), info (advisory)

Respond ONLY with a JSON object, no markdown, no backticks:
{{
  "alerts": [
    {{
      "type": "danger|warning|info",
      "category": "Weather|Pest|Disease|Crop Advisory",
      "icon": "one emoji",
      "title": "Short alert title",
      "message": "Detailed description of the risk (1-2 sentences)",
      "action": "Specific action the farmer should take (1-2 sentences)"
    }}
  ]
}}"""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model":       "openai/gpt-oss-20b",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens":  3500,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = _post_to_groq(body, headers)
        if resp is None or resp.status_code != 200:
            err_text = resp.text if resp else "no-response"
            logger.info(f"[AlertsAI] Groq HTTP {getattr(resp, 'status_code', 'None')} for today/{city} | {err_text}")
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Remove reasoning block if model is a thinking model
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        try:
            parsed = json.loads(match.group() if match else cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"[AlertsAI] JSON error for today/{city}: {e}\n[RAW OUTPUT] {raw[:500]}")
            return None
        alerts = parsed.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            return None
        valid = []
        for a in alerts:
            if all(k in a for k in ("type", "category", "icon", "title", "message", "action")):
                valid.append(a)
        if not valid:
            return None
        _bounded_cache_set(_ai_alerts_cache, cache_key, (now, valid), max_entries=300)
        logger.info(f"[AlertsAI] OK for today/{city}: {len(valid)} alerts")
        return valid
    except Exception as e:
        logger.warning(f"[AlertsAI] error for today/{city}: {e}")
        return None


def _ai_alerts_for_forecast(city, lat, lon, forecast_days):
    """Ask Groq for location-specific alerts for ALL forecast days in a single prompt."""
    if not GROQ_API_KEY or not forecast_days:
        return None

    days_summary = "\n".join([
        f"- Date {d.get('date','?')}: Temp {d.get('temp_min',20)}°C to {d.get('temp_max',25)}°C, "
        f"Humidity {d.get('humidity',60)}%, Wind {d.get('wind_speed',10)} km/h, Rain {d.get('rain',0)} mm, {d.get('description','')}"
        for d in forecast_days
    ])

    cache_key = f"forecast|{city}|{round(lat or 0, 1)}|{round(lon or 0, 1)}|{hashlib.md5(days_summary.encode()).hexdigest()[:12]}"
    now = time.monotonic()
    cached = _ai_alerts_cache.get(cache_key)
    if cached and (now - cached[0]) < AI_ALERTS_CACHE_TTL_SEC:
        return cached[1]

    prompt = f"""You are an agricultural officer for {city or 'India'}.
Analyze this {len(forecast_days)}-day weather forecast for local farmers:
{days_summary}

Provide specific, realistic agricultural alerts tailored to EACH day's exact weather.
You MUST generate custom alerts for EVERY SINGLE DATE listed above.
Do NOT repeat the exact same alert across multiple days.

Respond ONLY with valid JSON:
{{
  "days": [
    {{
      "date": "YYYY-MM-DD",
      "alerts": [
        {{
          "type": "danger|warning|info",
          "category": "Weather|Pest|Disease|Crop Advisory",
          "icon": "relevant emoji",
          "title": "Clear, specific alert title",
          "message": "Scientific yet practical advisory for this day's weather",
          "action": "Actionable farmer recommendation with exact chemical/organic dose if applicable"
        }}
      ]
    }}
  ]
}}"""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model":       "openai/gpt-oss-20b",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens":  7000,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = _post_to_groq(body, headers)
        if resp is None or resp.status_code != 200:
            err_text = resp.text if resp else "no-response"
            logger.info(f"[AlertsAI] Groq HTTP {getattr(resp, 'status_code', 'None')} for forecast/{city} | {err_text}")
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Remove reasoning block if model is a thinking model
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        try:
            parsed = json.loads(match.group() if match else cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"[AlertsAI] JSON error for forecast/{city}: {e}\n[RAW OUTPUT] {raw[:500]}")
            return None
        days_data = parsed.get("days")
        if not isinstance(days_data, list):
            return None

        result = {}
        for day_obj in days_data:
            d_str = day_obj.get("date")
            day_alerts = day_obj.get("alerts", [])
            valid = []
            for a in day_alerts:
                if all(k in a for k in ("type", "category", "icon", "title", "message", "action")):
                    valid.append(a)
            if d_str and valid:
                result[d_str] = valid

        _bounded_cache_set(_ai_alerts_cache, cache_key, (now, result), max_entries=300)
        logger.info(f"[AlertsAI] OK for forecast/{city}: {len(result)} dates with AI alerts")
        return result
    except Exception as e:
        logger.warning(f"[AlertsAI] error for forecast/{city}: {e}")
        return None


@app.route("/api/alerts", methods=["POST"])
def get_alerts():
    data        = request.json or {}
    temp        = data.get("temp", 25)
    humidity    = data.get("humidity", 60)
    wind_speed  = data.get("wind_speed", 10)
    rain        = data.get("rain", 0)
    description = data.get("description", "")
    city        = data.get("city", "")
    lat         = data.get("lat")
    lon         = data.get("lon")

    ai_alerts = _ai_alerts_for_today(city, lat, lon, temp, humidity, wind_speed, rain, description)
    if ai_alerts:
        alerts = ai_alerts
    else:
        alerts = _compute_alerts_for_conditions(temp, humidity, wind_speed, rain, description)

    return jsonify({"alerts": alerts, "total": len(alerts)})


# ─── 6-Day Forecast Alerts (day-wise) ────────────────────────────────────────
@app.route("/api/alerts-forecast", methods=["POST"])
def get_alerts_forecast():
    """Takes the 6-day forecast list (as returned by /api/weather) and generates
    AI-powered, location-specific alerts for each day. Falls back to the rule
    engine if the AI call fails."""
    data     = request.json or {}
    forecast = data.get("forecast", [])
    city     = data.get("city", "")
    lat      = data.get("lat")
    lon      = data.get("lon")
    if not isinstance(forecast, list) or not forecast:
        return jsonify({"error": "forecast array required"}), 400

    today_alerts = data.get("today_alerts")
    
    if today_alerts and len(forecast) > 0:
        today_date = forecast[0].get("date")
        forecast_for_ai = forecast[1:]
        ai_per_day_map = _ai_alerts_for_forecast(city, lat, lon, forecast_for_ai)
        if ai_per_day_map is None:
            ai_per_day_map = {}
        ai_per_day_map[today_date] = today_alerts
    else:
        ai_per_day_map = _ai_alerts_for_forecast(city, lat, lon, forecast)

    daily = []
    danger_days = []

    for i, day in enumerate(forecast):
        date        = day.get("date", "")
        temp_max    = day.get("temp_max", 25)
        temp_min    = day.get("temp_min", 20)
        temp_avg    = (temp_max + temp_min) / 2
        humidity    = day.get("humidity", 60)
        wind_speed  = day.get("wind_speed", 10)
        rain        = day.get("rain", 0)
        description = day.get("description", "")

        # Try date match from AI map, then index match, then rule engine
        day_alerts = None
        if ai_per_day_map and isinstance(ai_per_day_map, dict):
            day_alerts = ai_per_day_map.get(date)

        if not day_alerts:
            day_alerts = _compute_alerts_for_conditions(temp_avg, humidity, wind_speed, rain, description, date_str=date)

        danger_count  = sum(1 for a in day_alerts if a["type"] == "danger")
        warning_count = sum(1 for a in day_alerts if a["type"] == "warning")
        info_count    = sum(1 for a in day_alerts if a["type"] == "info")

        day_entry = {
            "date":          date,
            "temp_max":      temp_max,
            "temp_min":      temp_min,
            "description":   description,
            "icon":          day.get("icon", ""),
            "alerts":        day_alerts,
            "danger_count":  danger_count,
            "warning_count": warning_count,
            "info_count":    info_count,
            "total":         len(day_alerts),
        }
        daily.append(day_entry)

        if danger_count > 0:
            danger_days.append({
                "date":  date,
                "titles": [a["title"] for a in day_alerts if a["type"] == "danger"],
            })

    return jsonify({
        "daily": daily,
        "summary": {
            "total_danger_days": len(danger_days),
            "danger_days":       danger_days,
        }
    })


# ─── Crop Risk vs 6-Day Forecast ─────────────────────────────────────────────
# Threshold table for common Indian crops — used to score how many of the
# next 6 forecast days fall outside the crop's safe growing conditions.
CROP_RISK_THRESHOLDS = {
    "rice":       {"min_temp": 20, "max_temp": 38, "min_humidity": 70, "max_wind": 45, "flood_ok": True},
    "wheat":      {"min_temp": 10, "max_temp": 25, "min_humidity": 40, "max_wind": 45, "flood_ok": False},
    "maize":      {"min_temp": 18, "max_temp": 35, "min_humidity": 50, "max_wind": 40, "flood_ok": False},
    "cotton":     {"min_temp": 25, "max_temp": 40, "min_humidity": 40, "max_wind": 40, "flood_ok": False},
    "tomato":     {"min_temp": 18, "max_temp": 30, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "sugarcane":  {"min_temp": 24, "max_temp": 38, "min_humidity": 75, "max_wind": 45, "flood_ok": True},
    "soybean":    {"min_temp": 20, "max_temp": 32, "min_humidity": 60, "max_wind": 40, "flood_ok": False},
    "mustard":    {"min_temp": 10, "max_temp": 25, "min_humidity": 40, "max_wind": 40, "flood_ok": False},
    "potato":     {"min_temp": 10, "max_temp": 22, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "onion":      {"min_temp": 13, "max_temp": 28, "min_humidity": 50, "max_wind": 35, "flood_ok": False},
    "chilli":     {"min_temp": 20, "max_temp": 35, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "groundnut":  {"min_temp": 22, "max_temp": 36, "min_humidity": 50, "max_wind": 40, "flood_ok": False},
    "bajra":      {"min_temp": 20, "max_temp": 42, "min_humidity": 30, "max_wind": 45, "flood_ok": False},
    "jowar":      {"min_temp": 18, "max_temp": 38, "min_humidity": 35, "max_wind": 45, "flood_ok": False},
    "gram":       {"min_temp": 10, "max_temp": 27, "min_humidity": 35, "max_wind": 40, "flood_ok": False},
}

# Generic fallback thresholds keyed by the "water need" label the AI-generated
# crop list uses, for crops that don't match the table above by name.
WATER_NEED_FALLBACK = {
    "low":        {"min_temp": 10, "max_temp": 42, "min_humidity": 25, "max_wind": 45, "flood_ok": False},
    "medium":     {"min_temp": 12, "max_temp": 38, "min_humidity": 40, "max_wind": 42, "flood_ok": False},
    "high":       {"min_temp": 15, "max_temp": 38, "min_humidity": 55, "max_wind": 42, "flood_ok": False},
    "very high":  {"min_temp": 18, "max_temp": 38, "min_humidity": 65, "max_wind": 42, "flood_ok": True},
}


def _find_crop_thresholds(name, water_need):
    name_l = (name or "").lower()
    for key, thresholds in CROP_RISK_THRESHOLDS.items():
        if key in name_l or name_l in key:
            return thresholds, True
    fallback_key = (water_need or "medium").lower()
    return WATER_NEED_FALLBACK.get(fallback_key, WATER_NEED_FALLBACK["medium"]), False


@app.route("/api/crop-risk", methods=["POST"])
def crop_risk():
    """Cross-references the dashboard's recommended crops against the 6-day
    forecast and returns a danger percentage per crop, so a farmer can see
    whether it's still a good idea to plant something given what's coming."""
    data     = request.json or {}
    crops    = data.get("crops", [])
    forecast = data.get("forecast", [])

    if not isinstance(crops, list) or not crops:
        return jsonify({"error": "crops array required"}), 400
    if not isinstance(forecast, list) or not forecast:
        return jsonify({"error": "forecast array required"}), 400

    total_days = len(forecast)
    results = []

    for crop in crops:
        name       = crop.get("name", "Unknown")
        icon       = crop.get("icon", "🌱")
        water_need = crop.get("water", "Medium")
        thresholds, matched = _find_crop_thresholds(name, water_need)

        risky_days = []
        for day in forecast:
            temp_avg   = (day.get("temp_max", 25) + day.get("temp_min", 20)) / 2
            humidity   = day.get("humidity", 60)
            wind_speed = day.get("wind_speed", 10)
            rain       = day.get("rain", 0)

            reasons = []
            if temp_avg < thresholds["min_temp"]:
                reasons.append(f"Too cold ({round(temp_avg)}°C, needs {thresholds['min_temp']}°C+)")
            if temp_avg > thresholds["max_temp"]:
                reasons.append(f"Too hot ({round(temp_avg)}°C, tolerates up to {thresholds['max_temp']}°C)")
            if humidity < thresholds["min_humidity"]:
                reasons.append(f"Humidity too low ({humidity}%, needs {thresholds['min_humidity']}%+)")
            if wind_speed > thresholds["max_wind"]:
                reasons.append(f"Damaging winds expected ({round(wind_speed)} m/s)")
            if rain > 50 and not thresholds.get("flood_ok"):
                reasons.append(f"Heavy rain risk of waterlogging ({round(rain)} mm)")

            if reasons:
                risky_days.append({"date": day.get("date", ""), "reasons": reasons})

        risky_count    = len(risky_days)
        danger_percent = round((risky_count / total_days) * 100) if total_days else 0
        risk_level     = "High" if danger_percent >= 60 else "Medium" if danger_percent >= 30 else "Low"

        results.append({
            "name":           name,
            "icon":           icon,
            "matched_crop_db": matched,
            "danger_percent": danger_percent,
            "risk_level":     risk_level,
            "safe_days":      total_days - risky_count,
            "risky_days":     risky_days,
            "total_days":     total_days,
        })

    return jsonify({"crops": results})

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
TRANSLATE_MODELS = [
    "openai/gpt-oss-120b",   # primary: fast & multilingual
    "openai/gpt-oss-20b",    # fallback 1: very fast, lightweight
    "qwen/qwen3.6-27b",      # fallback 2: genuinely different model, not just a repeat of primary
]
TRANSLATE_CHUNK_SIZE = 40   
TRANSLATE_MAX_WORKERS = 4  
TRANSLATE_STAGGER_SEC = 0.15 
# NOTE: this used to be 1.5s. With 4 worker threads all calling the SAME
# model, a 1.5s minimum gap between calls forces them back into near-serial
# execution (4 chunks x 1.5s = 6s+ of pure throttling before any actual
# network time), even though the ThreadPoolExecutor above looks parallel.
# Groq's actual per-model rate limit is well above 1 req/0.4s for these
# model tiers, and _post_to_groq() already retries with backoff on a real
# HTTP 429 — so this only needs to prevent accidental bursts, not add a
# blanket 1.5s tax to every translated page load.
MIN_CALL_INTERVAL_SEC = 0.4

_model_last_call = {}
_model_throttle_lock = threading.Lock()


def _throttle_model(model):
    """Make sure consecutive calls to the same Groq model are spaced out,
    even across concurrent threads, so a burst of chunk requests doesn't
    look like a rate-limit-violating spike to Groq."""
    with _model_throttle_lock:
        now = time.monotonic()
        next_slot = max(now, _model_last_call.get(model, 0) + MIN_CALL_INTERVAL_SEC)
        _model_last_call[model] = next_slot
        wait = next_slot - now
    if wait > 0:
        time.sleep(wait)


def _post_to_groq(body, headers, max_retries=3):
    """POST to Groq with throttling + exponential backoff specifically for
    HTTP 429 (rate limit). Returns the final requests.Response."""
    model = body.get("model")
    resp = None
    for attempt in range(max_retries + 1):
        _throttle_model(model)
        resp = requests.post(GROQ_CHAT_URL, headers=headers, json=body, timeout=45)
        if resp.status_code != 429:
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else (1.5 * (attempt + 1))
        except (TypeError, ValueError):
            wait = 2.0 * (attempt + 1)
        if attempt < max_retries:
            time.sleep(wait)  # Respect actual retry_after header
    return resp


def _extract_json_object(raw_text):
    """Pull a usable {term: translation} dict out of model output, repairing
    the truncation/formatting issues that show up almost exclusively with
    high-token-cost scripts."""
    text = re.sub(r"```(?:json)?", "", raw_text).replace("```", "").strip()
    text = (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))

    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group() if match else text

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    pairs = re.findall(r'"((?:[^"\\]|\\.)+?)"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if pairs:
        return {k: v for k, v in pairs}
    return None


def _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code=""):
    terms_json = json.dumps(terms_chunk, ensure_ascii=False)

    script_note = ""
    if lang_code in ["hi", "mai", "ne", "sd", "doi", "sa"]:
        script_note = "MUST write EXCLUSIVELY in Devanagari script (देवनागरी). Do NOT output Punjabi (Gurmukhi), Bengali, or Gujarati letters."
    elif lang_code in ["bn", "as", "mni"]:
        script_note = "MUST write EXCLUSIVELY in Bengali script (বাংলা)."
    elif lang_code == "pa":
        script_note = "MUST write EXCLUSIVELY in Gurmukhi script (ਗੁਰਮੁਖੀ)."
    elif lang_code == "te":
        script_note = "MUST write EXCLUSIVELY in Telugu script (తెలుగు)."
    elif lang_code == "ta":
        script_note = "MUST write EXCLUSIVELY in Tamil script (தமிழ்)."
    elif lang_code == "gu":
        script_note = "MUST write EXCLUSIVELY in Gujarati script (ગુજરાતી)."
    elif lang_code == "kn":
        script_note = "MUST write EXCLUSIVELY in Kannada script (ಕನ್ನಡ)."
    elif lang_code == "ml":
        script_note = "MUST write EXCLUSIVELY in Malayalam script (മലയാളം)."
    elif lang_code in ["ur", "ks"]:
        script_note = "MUST write EXCLUSIVELY in Urdu/Perso-Arabic script (اردو)."
    elif lang_code == "or":
        script_note = "MUST write EXCLUSIVELY in Odia script (ଓଡ଼ିଆ)."

    return f"""You are an expert translator for Indian regional languages. Translate each English term below to {lang_name} ({lang_code}).

CRITICAL RULES:
1. Target Language: {lang_name} ({lang_code}). {script_note}
2. Return ONLY a raw JSON object mapping each input term to its {lang_name} translation. No markdown, no backticks, no explanation.
3. Every single key from the input list MUST appear in the output JSON, exactly as written.
4. Keep unchanged: chemical/brand names, numbers, and units (kg/ha, Rs, days, ml/L, g/ha, quintal, SL, EC, SC, WP, SG, NPK).
5. {domain_note}
6. Use natural everyday terms a {lang_name}-speaking farmer would recognize.

Input terms (translate ALL of these):
{terms_json}

Output: a single JSON object only."""


def _translate_terms_chunk(terms_chunk, lang_name, domain_note, lang_code=""):
    prompt = _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code)
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    max_tokens = min(4096, 300 + len(terms_chunk) * 150)

    last_error = None
    for model in TRANSLATE_MODELS:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": f"You are an expert Indian regional language translator. You MUST respond with valid JSON only, no other text. Translate everything to {lang_name} ({lang_code}) using its correct native script."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = _post_to_groq(body, headers)
            if resp.status_code == 400:
                body.pop("response_format", None)
                resp = _post_to_groq(body, headers)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                continue

            raw = resp.json()["choices"][0]["message"]["content"].strip()
            translations = _extract_json_object(raw)
            if not translations:
                last_error = "No JSON found/parsable in response"
                continue

            for term in terms_chunk:
                if term not in translations or not translations[term]:
                    translations[term] = term
            return translations

        except Exception as e:
            last_error = str(e)
            continue

    logger.warning(f"[Translate] chunk of {len(terms_chunk)} terms to {lang_name} failed on all models: {last_error}")
    return {term: term for term in terms_chunk}


def _translate_terms(terms, lang_name, domain_note, cache_key, cache_dict, lang_code=""):
    """Translate a full term list via small, gently-paced parallel chunks,
    with caching and a cleanup retry pass for chunks that failed outright."""
    if cache_key in cache_dict:
        return cache_dict[cache_key], True

    chunks = [terms[i:i + TRANSLATE_CHUNK_SIZE] for i in range(0, len(terms), TRANSLATE_CHUNK_SIZE)]
    results = [None] * len(chunks)

    def run_chunk(idx):
        results[idx] = _translate_terms_chunk(chunks[idx], lang_name, domain_note, lang_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(TRANSLATE_MAX_WORKERS, len(chunks))) as executor:
        futures = []
        for i in range(len(chunks)):
            if i > 0:
                time.sleep(TRANSLATE_STAGGER_SEC)  # avoid firing every chunk in the same instant
            futures.append(executor.submit(run_chunk, i))
        concurrent.futures.wait(futures)
    # Guard against a thread that never populated its slot (e.g. an
    # unexpected exception escaping run_chunk). _translate_terms_chunk
    # normally always returns a dict, but this keeps a future refactor
    # from turning into a silent AttributeError crash for the whole request.
    for i, chunk in enumerate(chunks):
        if results[i] is None:
            results[i] = {term: term for term in chunk}
            continue
        if all(results[i].get(term) == term for term in chunk):
            time.sleep(1.5)
            retried = _translate_terms_chunk(chunk, lang_name, domain_note, lang_code)
            if any(retried.get(term) != term for term in chunk):
                results[i] = retried

    translations = {}
    for r in results:
        translations.update(r)

    _bounded_cache_set(cache_dict, cache_key, translations, max_entries=500)
    return translations, False


# ─── Market Translation ───────────────────────────────────────────────────────

@app.route("/api/translate-market", methods=["POST"])
def translate_market():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    terms = [
        "Wheat","Rice","Paddy (Rice)","Maize (Corn)","Mustard","Groundnut",
        "Onion","Potato","Tomato","Chilli","Sugarcane","Arhar (Tur)","Moong",
        "Urad","Soybean","Soybean Oil","Soybean Meal","Cotton","Jowar (Sorghum)",
        "Bajra (Pearl Millet)","Bengal Gram (Chana)","Garlic","Ginger","Turmeric",
        "Cumin (Jeera)","Coriander","Sunflower","Sesame (Til)","Linseed","Castor Seed",
        "Banana","Mango","Apple","Grapes","Pomegranate","Cabbage","Cauliflower",
        "Brinjal (Eggplant)","Ladyfinger (Okra)","Spinach","Bitter Gourd","Bottle Gourd",
        "Ridge Gourd","Ash Gourd","Palm Oil","Oats","Coffee","Cocoa","Rubber","Lumber",
        "Very High","High","Medium","Low","Price Rising","Price Falling",
        "Very High Demand","All","Crop","Price","Change","Demand",
        "Trend","Comparison","Demand Map","Search","30-Day Price Trend",
        "Current Prices","Demand Intensity","Price Momentum","Price Graph","Comparison Table",
        "Showing all major Indian markets","quintal","Searching","Loading markets",
        "Live","MSP Reference","crops",
    ]

    lang_name = LANG_NAMES.get(lang, "Hindi")
    domain_note = "Crop names should be the common local/mandi name a farmer would recognize, not a literal translation."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _translation_cache, lang_code=lang)

    logger.info(f"[Translate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


@app.route("/api/translate-market/clear", methods=["POST"])
def clear_translation_cache():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production"}), 403
    _translation_cache.clear()
    return jsonify({"status": "cache cleared"})


# ─── Alerts Translation ───────────────────────────────────────────────────────
_alerts_translation_cache = {}

@app.route("/api/translate-alerts", methods=["POST"])
def translate_alerts():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # Alert titles
        "Extreme Heat Alert", "Frost Warning", "High Fungal Disease Risk",
        "High Wind Speed Alert", "Heavy Rainfall Alert", "Thunderstorm Warning",
        "Aphid & Whitefly Risk", "Spider Mite Alert", "Crops at Risk in Current Conditions",
        # Alert messages
        "Temperature above 40°C. Provide shade netting and increase irrigation frequency.",
        "Sub-zero temperatures expected. Frost can destroy standing crops overnight.",
        "Humidity above 85% creates ideal conditions for fungal diseases.",
        "Strong winds can cause lodging in tall crops like maize and wheat.",
        "Excessive rain may cause waterlogging and root rot.",
        "Thunderstorm conditions detected. Risk of lightning and hail damage.",
        "Warm humid conditions are ideal for aphid multiplication.",
        "Hot dry conditions favour rapid spider mite population growth.",
        # Alert actions
        "Schedule irrigation every 4-5 hours. Avoid afternoon spraying.",
        "Cover crops with frost cloth. Use smudge pots or sprinkler irrigation.",
        "Apply preventive fungicide (Mancozeb 75 WP at 2.5 g/L) immediately.",
        "Avoid spraying. Support tall crops with stakes.",
        "Ensure field drainage channels are open. Pause irrigation.",
        "Stay indoors. Secure farm equipment.",
        "Spray Neem oil (5 ml/L) or Imidacloprid 0.3 ml/L at dusk.",
        "Apply Abamectin 1.8 EC (0.5 ml/L). Increase soil moisture.",
        "Consider alternate crops better suited to current climate.",
        # Alert UI labels
        "Action", "Critical", "Warning", "Advisory", "Danger",
        "All Alerts", "Warnings", "Advisories", "Weather", "Pest", "Crop Advisory",
        "Disease",
        # Pest calendar
        "Brown Plant Hopper", "Aphids", "Fall Armyworm", "Whitefly",
        "Red Spider Mite", "Stem Borer", "Thrips", "Mealy Bug",
        "Kharif (Jun–Oct)", "Rabi (Nov–Feb)", "Kharif (Jul–Sep)",
        "Year-round", "Zaid (Mar–May)", "Kharif (Jun–Sep)", "Rabi & Zaid",
        "Rice, Paddy", "Wheat, Mustard, Vegetables", "Maize, Sorghum",
        "Cotton, Tomato, Chilli", "Soybean, Cotton, Brinjal",
        "Rice, Sugarcane, Maize", "Onion, Chilli, Groundnut", "Cotton, Grapes, Papaya",
        "Feeds on rice plants causing \"hopperburn\". Thrives in humid conditions above 75%.",
        "Suck plant sap, transmit viral diseases. High risk in mild temperatures (15–25°C).",
        "Causes significant leaf damage and can destroy entire crops within days.",
        "Transmits leaf curl virus to cotton. Population explosion in dry hot weather.",
        "Causes bronzing/yellowing of leaves. Severe in hot, dry weather above 32°C.",
        "Bores into stems causing \"dead heart\" in vegetative stage and \"white ear\" at heading.",
        "Causes silvery white patches on leaves. Severe in dry weather.",

        "Forms white waxy colonies on plant parts. Excretes honeydew causing sooty mould.",
        "Use resistant varieties. Avoid excess nitrogen. Keep fields drained.",
        "Neem oil spray. Release ladybird beetles as biocontrol.",
        "Early detection critical. Bt-based bioinsecticide spray.",
        "Yellow sticky traps. Reflective mulch. Imidacloprid at threshold level.",
        "Increase irrigation. Abamectin 1.8 EC spray. Avoid dust on leaves.",
        "Pheromone traps. Chlorpyriphos 20 EC. Remove crop residues after harvest.",
        "Spinosad spray. Blue sticky traps. Avoid drought stress.",
        "Buprofezin spray. Introduce Cryptolaemus beetles as biocontrol.",
        "High", "Medium", "Low", "Risk", "Active Now", "Affects",
        # Pesticide guide
        "Chlorpyriphos 20 EC", "Imidacloprid 17.8 SL", "Mancozeb 75 WP",
        "Neem Oil 5% EC (Organic)", "Propiconazole 25 EC", "Emamectin Benzoate 5 SG",
        "Stem borer, Aphids, Termites", "Whitefly, Aphids, Brown Plant Hopper",
        "Leaf blight, Early blight, Rust, Downy mildew",
        "Aphids, Whitefly, Mites, Fungal diseases",
        "Yellow rust, Brown rust, Sheath blight",
        "Fall Armyworm, Diamond back moth, Leaf miner",
        "Target Pest", "Safe Dose", "Max Limit", "Interval", "Pre-Harvest", "PPE Required",
        "Every 14 days", "Every 21 days max", "Every 7–10 days",
        "Every 5–7 days", "Max 2 sprays per season", "Every 10–14 days",
        "15 days before harvest", "21 days before harvest", "7 days before harvest",
        "No waiting period — organic",
        "Gloves, Mask, Goggles, Full sleeve clothing",
        "Gloves, Mask, Full body protection", "Gloves, Goggles, Dust Mask",
        "Basic gloves recommended", "Full protective gear, closed shoes",
        "Full PPE, respiratory protection",
        "Highly toxic to fish and bees. Do not spray near water bodies or during flowering.",
        "Do NOT spray during bee activity (morning/evening). Highly toxic to pollinators.",
        "Causes skin and eye irritation. Do not spray on edible parts 7 days before harvest.",
        "Safe for humans and beneficial insects. May cause phytotoxicity in direct sunlight. Spray at dusk.",
        "Do not mix with alkaline pesticides. Causes groundwater contamination if overused.",
        "Highly toxic to aquatic organisms. Dispose empty containers safely. Do not reuse containers.",
        # Harmful/safe crops
        "Rice", "Wheat", "Maize", "Cotton", "Tomato", "Sugarcane",
        "Soybean", "Mustard", "Potato", "Onion", "Chilli", "Groundnut",
        "Risky", "Safe", "Suitable for", "humidity",
        "No harmful crops identified for current conditions.",
        "No fully safe crops identified — check crop calendar.",
        # Risk chart
        "Heat Stress", "Humidity Risk", "Wind Damage", "Pest Risk",
        "Disease Risk", "Pest Activity", "Overall Risk", "Current Risk Level (%)",
        # Crop risk section
        "Safe to grow all 6 upcoming days",
        # Toast / UI messages
        "Checking forecast...", "Upcoming Risks Checked",
        "Check Upcoming Risks (6-Day Forecast)",
        "No critical weather risks in the next 6 days.",
        # Reason strings
        "Too cold (min 10°C needed)", "Too cold (min 13°C needed)",
        "Too cold (min 18°C needed)", "Too cold (min 20°C needed)",
        "Too cold (min 22°C needed)", "Too cold (min 24°C needed)",
        "Too cold (min 25°C needed)",
        "Too hot (max 22°C tolerated)", "Too hot (max 25°C tolerated)",
        "Too hot (max 28°C tolerated)", "Too hot (max 30°C tolerated)",
        "Too hot (max 32°C tolerated)", "Too hot (max 35°C tolerated)",
        "Too hot (max 36°C tolerated)", "Too hot (max 38°C tolerated)",
        "Too hot (max 40°C tolerated)",
        "Humidity too low (min 40% needed)", "Humidity too low (min 50% needed)",
        "Humidity too low (min 60% needed)", "Humidity too low (min 70% needed)",
        "Humidity too low (min 75% needed)",
    ]

    domain_note = (
        "This is for an agricultural alerts page for Indian farmers. "
        "Translate accurately preserving technical terms like pesticide names, "
        "dosage numbers, and units (ml/L, g/L, EC, WP, SL, SG, °C, %) in their original form."
    )
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _alerts_translation_cache, lang_code=lang)

    logger.info(f"[AlertsTranslate] {len(translations)} terms for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

# ─── Dashboard Translation ────────────────────────────────────────────────────
_dashboard_translation_cache = {}

@app.route("/api/translate-dashboard", methods=["POST"])
def translate_dashboard():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # UI Labels
        "Dashboard","Diagnose Crop","Market Prices","Alerts","Get My Location",
        "Location Found","Fetching weather...","Awaiting location...",
        "Current Weather Conditions","Live data from your location",
        "6-Day Forecast","Temperature","Humidity","Wind","Visibility","Pressure",
        "Feels like","Calm","Light breeze","Moderate breeze","Strong breeze","Storm warning",
        "Crop Recommendations","Based on your climate & location",
        "Season","Water Need","Expected Yield","Duration","Soil Type","Fertilizer",
        "Estimated Profit","Match",
        "Crop Advisory Calendar","Week-by-week action plan for your crops",
        "Pesticide & Pest Control Guide","Safe and effective crop protection plan",
        "Quick Actions","Diagnose Crop Disease","Upload or take a photo of your crop",
        "Check Market Prices","Live mandi prices across India",
        "View Active Alerts","Weather & pest warnings for your area",
        "Empowering farmers with AI-driven precision agriculture",
        "Eco-Friendly","Chemical","Week",
        # Seasons
        "Kharif (Monsoon)","Rabi (Winter)","Zaid (Summer)",
        # Water levels
        "Very High","High","Medium","Low",
        # Activity types
        "preparation","sowing","irrigation","fertilizer","maintenance","pesticide","harvest",
        # Calendar activities
        "Soil preparation & ploughing","Seed treatment & sowing","First irrigation",
        "Apply basal fertilizer (NPK)","Weeding & thinning","Apply Urea (top dressing)",
        "Pest & disease inspection","Spray fungicide if required",
        "Foliar spray micronutrients","Pre-harvest irrigation stop","Harvest preparation",
        # Crop names
        "Rice","Wheat","Maize","Cotton","Tomato","Sugarcane","Soybean","Mustard",
        # Crop descriptions
        "Ideal for high humidity and warm conditions",
        "Best suited for cool, dry winters",
        "Versatile crop for warm humid weather",
        "Thrives in hot dry spells with moderate rain",
        "High value crop for moderate climates",
        "Requires hot climate and heavy rainfall",
        "Nitrogen-fixing legume for warm monsoon",
        "Cool weather oil seed crop",
        # Soil types
        "Clay loam, alluvial","Well-drained loam","Sandy loam to clay loam",
        "Black cotton soil","Sandy loam, rich organic matter","Deep loam, good drainage",
        "Well-drained loam","Sandy loam, well-drained",
        # Pest names
        "Brown Plant Hopper","Leaf folder","Aphids","Yellow rust",
        "Fall Armyworm","Stem borer","Bollworm","Whitefly",
        # Pesticide section labels
        "Pest Control Plan","Crop","Timing",
    ]

    domain_note = "Crop, pest, and field-activity names should be the common name farmers actually use, not a literal translation."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _dashboard_translation_cache, lang_code=lang)

    logger.info(f"[DashboardTranslate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

# ─── Diagnose Page Translation (static UI text) ───────────────────────────────

_diagnose_translation_cache = {}

@app.route("/api/translate-diagnose", methods=["POST"])
def translate_diagnose():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()

    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")

    terms = [
        # Upload panel
        "Drop your crop image here", "Supports JPG, PNG, WEBP — max 10 MB",
        "Upload Photo", "Take Photo", "Image ready for analysis",
        "Remove image", "Close camera", "Capture photo", "Analyze Crop",
        "Analyzing…", "Try Again",
        # Tips card
        "Photo Tips for Best Results",
        "Focus on the most visibly affected area",
        "Use natural daylight — avoid harsh shadows",
        "Include both healthy and affected parts if possible",
        "Keep the camera steady and close (30–50 cm)",
        # Results placeholder
        "Upload a crop image to begin diagnosis",
        "Our AI will identify the disease and suggest eco-friendly treatments",
        "Upload or capture image", "Click Analyze Crop", "Get instant AI diagnosis",
        # Analyzing loader
        "AI is analyzing your crop…",
        "Identifying disease patterns and preparing remedies",
        "Scanning image…", "Detecting patterns…", "Finding remedies…",
        # Results content section headers
        "Cause", "Recovery Timeline", "Eco-Friendly Remedies", "RECOMMENDED",
        "Remedy Effectiveness Chart", "Chemical Treatment Options",
        "Prevention Tips", "Confidence", "Severity", "effectiveness",
        "AI-generated diagnosis for guidance only. Consult a local agronomist for critical crop decisions.",
        "Unknown Disease",
        # Error / failure states
        "Analysis Failed", "Could not process the image.",
        "Make sure your API key is set and the image is clear.",
        "Diagnosis failed. Please try again.",
        "Please upload or capture a crop image first.",
        "Please drop a valid image file (JPG, PNG, WEBP).",
        "Image too large. Max 10 MB allowed.",
        "Camera access denied or not available.",
        "Camera ready — position your crop in frame.",
        "Diagnosis complete!",
        # Severity levels (also used as data values from Groq)
        "Mild", "Moderate", "Severe",
        # How It Works section
        "How It Works", "Capture or Upload",
        "Take a clear photo of the affected crop leaf, stem, or fruit",
        "AI Analysis",
        "Our AI model analyzes visual patterns to identify diseases with high accuracy",
        "Get Remedies",
        "Receive eco-friendly and chemical treatment plans with dosage details instantly",
    ]

    domain_note = "This is UI copy and section labels for a crop-disease-diagnosis app. Keep tone simple and clear for farmers; keep numbers/units/file types (JPG, PNG, WEBP, MB, cm) unchanged."
    translations, cached = _translate_terms(terms, lang_name, domain_note, lang, _diagnose_translation_cache, lang_code=lang)

    logger.info(f"[DiagnoseTranslate] {len(translations)} terms ready for {lang_name} ({'cache' if cached else 'fresh'})")
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


# ─── Dynamic Diagnosis Result Translation ─────────────────────────────────────
_diagnosis_result_cache = {}

@app.route("/api/translate-diagnosis-result", methods=["POST"])
def translate_diagnosis_result():
    data = request.json or {}
    lang   = data.get("lang", "en").strip().lower()
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
        add(c.get("name")); add(c.get("interval"))
        add(c.get("dose"))
    for tip in result.get("prevention") or []:
        add(tip)

    if not terms:
        return jsonify({"lang": lang, "translations": {}})

    domain_note = ("This is an AI-generated crop disease diagnosis for a farmer. "
                   "Translate naturally using terms a farmer would recognize. "
                   "Keep chemical/brand names, numbers, and units (kg/ha, Rs, days, ml/L, g/ha, "
                   "quintal, %, SL, EC, SC, WP, SG, NPK) unchanged.")
    cache_key = lang + "::" + "|".join(terms)
    translations, cached = _translate_terms(terms, lang_name, domain_note, cache_key, _diagnosis_result_cache, lang_code=lang)

    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})


@app.route("/api/monthly-alerts", methods=["POST"])
def get_monthly_alerts():
    data = request.json or {}
    forecast = data.get("forecast", [])
    
    monthly = []
    base_date = datetime.now()
    if forecast and isinstance(forecast, list) and "date" in forecast[0]:
        try:
            base_date = datetime.strptime(forecast[0]["date"], "%Y-%m-%d")
        except ValueError:
            pass
    daily_alerts = { d.get("date"): d for d in data.get("daily_alerts", []) }

    # Show only the rest of the CURRENT calendar month (today through the
    # month's last day) instead of a fixed rolling 30 days, so it never
    # spills into next month.
    days_in_month = calendar.monthrange(base_date.year, base_date.month)[1]
    days_remaining_in_month = days_in_month - base_date.day + 1

    for i in range(days_remaining_in_month):
        curr_date = base_date + timedelta(days=i)
        date_str = curr_date.strftime("%Y-%m-%d")
        
        if date_str in daily_alerts:
            d_alerts = daily_alerts[date_str]
            danger = d_alerts.get("danger_count", 0)
            warning = d_alerts.get("warning_count", 0)
            info = d_alerts.get("info_count", 0)
            
            if danger > 0:
                risk = "danger"
                risk_pct = min(100, 70 + danger*15)
            elif warning > 0:
                risk = "warning"
                risk_pct = min(65, 35 + warning*15)
            elif info > 0:
                risk = "warning"
                risk_pct = 25
            else:
                risk = "safe"
                risk_pct = 10
            
            alerts = [{"title": a.get("title", "")} for a in d_alerts.get("alerts", [])]
        else:
            # No real forecast/alert data exists for this day (beyond the
            # ~6-day window OpenWeather actually provides). We do NOT invent
            # a risk number or alert title here — that would be fabricated
            # data presented as if it were real. Instead this is reported
            # honestly as unavailable so the frontend can show "no data yet"
            # rather than a fake risk percentage.
            monthly.append({
                "date":          date_str,
                "risk":          "unavailable",
                "risk_pct":      None,
                "alerts":        [],
                "data_available": False,
            })
            continue

        monthly.append({
            "date":          date_str,
            "risk":          risk,
            "risk_pct":      risk_pct,
            "alerts":        alerts,
            "data_available": True,
        })
        
    return jsonify({"monthly": monthly})

def _seasonal_advisories(city, humidity=None):
    """Standalone season/alert logic shared by the /api/seasonal-alerts route
    and the chat tool gateway, so both stay in sync without duplicating the
    rules."""
    month = datetime.now().month
    if month in [3, 4, 5]:
        season = "Summer"
    elif month in [6, 7, 8, 9]:
        season = "Monsoon"
    elif month in [10, 11]:
        season = "Post-Monsoon"
    else:
        season = "Winter"

    # Only generic, season-driven guidance that doesn't assert facts we
    # haven't actually measured — no invented "high risk" claims.
    alerts = [
        {"type": "info", "icon": "ℹ️", "title": "Soil Preparation",
         "message": f"Good time to prepare soil for {season} crops in {city}."},
        {"type": "info", "icon": "🗓️", "title": "Seasonal Advisory",
         "message": f"General farming calendar guidance for the {season} season in {city}. "
                    f"See the Alerts and Dashboard pages for live, weather-specific risk data."},
    ]

    # A pest-risk claim is only added when real humidity data was actually
    # supplied and genuinely crosses a risk threshold — never asserted by
    # default.
    if isinstance(humidity, (int, float)) and humidity > 75:
        alerts.append({
            "type": "warning", "icon": "🐛", "title": "Pest & Fungal Risk",
            "message": f"Current humidity of {humidity}% is high enough to favor pest and fungal activity.",
        })

    return season, alerts


@app.route("/api/seasonal-alerts", methods=["POST"])
def get_seasonal_alerts():
    data     = request.json or {}
    city     = data.get("city", "Unknown")
    humidity = data.get("humidity")  # real value from /api/weather, if the caller has it

    season, alerts = _seasonal_advisories(city, humidity)

    return jsonify({
        "season": season,
        "city": city,
        "alerts": alerts,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=DEBUG_MODE)
