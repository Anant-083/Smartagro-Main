import os
import re
import json
import time
import threading
import requests
import concurrent.futures
from datetime import datetime
from flask import Blueprint, request, jsonify

from shared import (
    logger, DATA_GOV_API_KEY, DEBUG_MODE, basedir,
    CITY_STATE, _bounded_cache_set, LANG_NAMES, translate_terms
)

market_bp = Blueprint("market", __name__)

AGMARKNET_RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
AGMARKNET_URL = f"https://api.data.gov.in/resource/{AGMARKNET_RESOURCE_ID}"

_agmark_session = requests.Session()
_agmark_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
})
_agmark_retry = requests.adapters.Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
_agmark_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=_agmark_retry))

_NON_CROP_COMMODITY_RX = re.compile(
    r"\b(ox|oxen|bullock|bull|cow|buffalo|goat|sheep|pig|poultry|chicken|broiler|hen|cock|rooster|duck|turkey|egg|eggs|wool|milk|ghee|fish|prawn|cattle|livestock|hide|skin|manure|dung|calf|lamb|mutton|meat|carnation|chrysanthemum|jasmine|lilly|lily|lotus|marigold|rose|tuberose|raibel|gladiolus|dahlia|gerbera|orchid|bop|wood|firewood|timber|bamboo)\b",
    re.IGNORECASE,
)

def _is_crop_commodity(raw_name: str) -> bool:
    return not _NON_CROP_COMMODITY_RX.search(raw_name or "")

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
}

_AGMARK_HISTORY_PATH = os.path.join(basedir, "market_history_cache.json")
_agmark_history_lock = threading.Lock()
_agmark_fetch_cache = {}
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

def _build_results_from_history_cache(state: str) -> list:
    try:
        with _agmark_history_lock:
            cache = _load_history_cache()
        state_hist = cache.get(state, {})
        if not state_hist:
            return []
        results = []
        for commodity, hist in state_hist.items():
            if not hist:
                continue
            hist_sorted = sorted(hist, key=lambda h: h["date"])
            prices = [h["price"] for h in hist_sorted]
            if len(prices) >= 2:
                prev_price = prices[-2]
                today_price = prices[-1]
                change = round(((today_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0
            else:
                change = 0.0
            results.append({
                "crop":         commodity,
                "crop_key":     commodity,
                "price":        int(round(prices[-1])),
                "change":       change,
                "history":      prices,
                "unit":         "Rs/quintal",
                "source":       "history_cache",
                "market":       "",
                "district":     "",
                "arrival_date": hist_sorted[-1]["date"],
            })
        return results
    except Exception as e:
        logger.warning(f"[Market] History cache error: {e}")
        return []

def _get_history_last_updated(state: str) -> str:
    try:
        with _agmark_history_lock:
            cache = _load_history_cache()
        state_hist = cache.get(state, {})
        latest = ""
        for hist in state_hist.values():
            for entry in hist:
                d = entry.get("date", "")
                if d and d > latest:
                    latest = d
        return latest
    except Exception:
        return ""

def _field(record: dict, *keys):
    for k in keys:
        v = record.get(k)
        if v not in (None, ""):
            return v
    return None

def _parse_arrival_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

STATE_NAME_CANDIDATES = {
    "Delhi": ["Delhi", "NCT of Delhi"],
    "Odisha": ["Odisha", "Orissa"],
    "Uttarakhand": ["Uttarakhand", "Uttaranchal"],
    "Jammu and Kashmir": ["Jammu and Kashmir", "Jammu & Kashmir"],
    "Puducherry": ["Puducherry", "Pondicherry"],
}

def fetch_agmarknet_prices(state: str) -> list:
    now = time.monotonic()
    cached = _agmark_fetch_cache.get(state)
    if cached and (now - cached[0]) < AGMARK_CACHE_TTL_SEC:
        return cached[1]

    records = []
    for candidate in STATE_NAME_CANDIDATES.get(state, [state]):
        params = {"api-key": DATA_GOV_API_KEY, "format": "json", "limit": 400, "filters[state]": candidate}
        try:
            resp = _agmark_session.get(AGMARKNET_URL, params=params, timeout=15)
            if resp.status_code == 200:
                body = resp.json()
                records = body.get("records", [])
                if records:
                    break
        except Exception as e:
            logger.warning(f"[Market] Fetch error for {candidate}: {e}")
            continue

    if not records:
        fallback = _build_results_from_history_cache(state)
        if fallback:
            _bounded_cache_set(_agmark_fetch_cache, state, (time.monotonic(), fallback), max_entries=60)
        return fallback

    by_commodity_date = {}
    latest_meta = {}
    for r in records:
        raw_name = str(_field(r, "commodity", "Commodity") or "").strip()
        if not _is_crop_commodity(raw_name):
            continue
        modal = _field(r, "modal_price", "Modal_x0020_Price", "Modal Price")
        if not raw_name or modal is None:
            continue
        try:
            modal_price = float(modal)
        except (TypeError, ValueError):
            continue
        if modal_price <= 0:
            continue

        raw_date = _field(r, "arrival_date", "Arrival_Date")
        iso_date = _parse_arrival_date(raw_date)
        if not iso_date:
            continue

        display_name = AGMARK_COMMODITY_ALIASES.get(raw_name.lower(), raw_name.title())
        by_commodity_date.setdefault(display_name, {}).setdefault(iso_date, []).append(modal_price)
        meta = latest_meta.get(display_name)
        if not meta or iso_date >= meta["_iso"]:
            latest_meta[display_name] = {
                "market": _field(r, "market", "Market") or "",
                "district": _field(r, "district", "District") or "",
                "arrival_date": raw_date or "",
                "_iso": iso_date,
            }

    latest_by_commodity = {}
    for display_name, date_map in by_commodity_date.items():
        per_date_avg = {d: round(sum(prices) / len(prices), 2) for d, prices in date_map.items()}
        meta = latest_meta[display_name]
        latest_by_commodity[display_name] = {
            "market": meta["market"], "district": meta["district"],
            "arrival_date": meta["arrival_date"], "per_date": per_date_avg,
        }

    results = []
    try:
        with _agmark_history_lock:
            cache = _load_history_cache()
            state_hist = cache.setdefault(state, {})
            cache_changed = False
            for display_name, rec in latest_by_commodity.items():
                hist = state_hist.setdefault(display_name, [])
                existing_dates = {h["date"] for h in hist}
                for d, price in rec["per_date"].items():
                    if d not in existing_dates:
                        hist.append({"date": d, "price": price})
                        existing_dates.add(d)
                        cache_changed = True
                hist.sort(key=lambda h: h["date"])
                hist[:] = hist[-30:]
                change = 0.0
                if len(hist) >= 2:
                    prev_price = hist[-2]["price"]
                    today_price = hist[-1]["price"]
                    change = round(((today_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0
                history_prices = [h["price"] for h in hist]
                display_price = hist[-1]["price"] if hist else next(iter(rec["per_date"].values()))
                results.append({
                    "crop": display_name, "crop_key": display_name, "price": int(round(display_price)),
                    "change": change, "history": history_prices, "unit": "Rs/quintal", "source": "agmarknet_live",
                    "market": rec["market"], "district": rec["district"], "arrival_date": rec["arrival_date"],
                })
            if cache_changed:
                _save_history_cache(cache)
    except Exception as e:
        logger.warning(f"[Market] History cache error: {e}")

    _bounded_cache_set(_agmark_fetch_cache, state, (time.monotonic(), results), max_entries=60)
    return results

def get_demand(price, change):
    if change >= 2.0: return "Very High"
    elif change >= 0.5: return "High"
    elif change >= -1.5: return "Medium"
    else: return "Low"

@market_bp.route('/api/market')
def get_market_data():
    cities = list(CITY_STATE.keys())
    location = request.args.get('location', '').strip().lower()
    if location:
        cities = [c for c in cities if location in c.lower()]

    if location and not cities:
        return jsonify({
            "markets": {}, "locations": [], "live_count": 0, "empty_cities": [],
            "fetched_at": datetime.now().isoformat(), "note": f'No market mapping for "{location}".'
        }), 200

    markets = {}
    city_meta = {}
    live_cities, history_cities, no_data_cities = [], [], []
    live_total = 0

    try:
        unique_states = sorted({CITY_STATE.get(c, "") for c in cities if CITY_STATE.get(c, "")})
        state_results_cache = {}
        if unique_states:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(unique_states))) as executor:
                future_to_state = {executor.submit(fetch_agmarknet_prices, s): s for s in unique_states}
                for future in concurrent.futures.as_completed(future_to_state):
                    state = future_to_state[future]
                    try:
                        state_results_cache[state] = future.result()
                    except Exception:
                        state_results_cache[state] = []

        for city in cities:
            state = CITY_STATE.get(city, "")
            crops = list(state_results_cache.get(state, []))
            if not crops:
                crops = _build_results_from_history_cache(state)
                status_val = "history" if crops else "no_data"
            else:
                status_val = "live"

            if crops:
                city_crops = [{**c, "demand": get_demand(c["price"], c["change"])} for c in crops]
                city_crops.sort(key=lambda x: ({"Very High": 3, "High": 2, "Medium": 1, "Low": 0}.get(x["demand"], 0), x["price"]), reverse=True)
                markets[city] = city_crops
                city_meta[city] = {"data_status": status_val, "last_updated": _get_history_last_updated(state)}
                if status_val == "live":
                    live_cities.append(city)
                    live_total += len(city_crops)
                else:
                    history_cities.append(city)
            else:
                markets[city] = []
                city_meta[city] = {"data_status": "no_data", "last_updated": ""}
                no_data_cities.append(city)

        return jsonify({
            "markets": markets, "city_meta": city_meta, "locations": cities,
            "live_count": live_total, "live_cities": live_cities, "history_cities": history_cities,
            "empty_cities": no_data_cities, "fetched_at": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.warning(f"[Market] Error: {e}")
        return jsonify({"markets": {}, "locations": [], "error": str(e)}), 500

@market_bp.route('/api/debug-market')
def debug_market():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production"}), 403
    state = request.args.get('state', 'Delhi')
    crops = fetch_agmarknet_prices(state)
    return jsonify({"state": state, "count": len(crops), "crops": crops[:10]})

_market_translation_cache = {}

@market_bp.route("/api/translate-market", methods=["POST"])
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
    translations, cached = translate_terms(terms, lang_name, domain_note, lang, _market_translation_cache, lang_code=lang)
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

@market_bp.route("/api/translate-market/clear", methods=["POST"])
def clear_translation_cache():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production"}), 403
    _market_translation_cache.clear()
    return jsonify({"status": "cache cleared"})
