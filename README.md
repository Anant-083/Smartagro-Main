<div align="center">

# 🌾 SmartAgro

**AI-powered agricultural advisory for Indian farmers**

Weather-driven alerts · Crop disease diagnosis · Live mandi prices · Satellite vegetation health · Multilingual AI chatbot

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Gunicorn](https://img.shields.io/badge/Gunicorn-21.2-499848?style=for-the-badge&logo=gunicorn&logoColor=white)](https://gunicorn.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Hugging Face Spaces](https://img.shields.io/badge/HF%20Spaces-deploy-FFD21E?style=for-the-badge&logo=huggingface&logoColor=black)](https://huggingface.co/spaces)

[![Groq](https://img.shields.io/badge/Groq-AI-F55036?style=flat-square&logo=groq&logoColor=white)](https://groq.com/)
[![Gemini](https://img.shields.io/badge/Gemini-Vision-8E75B2?style=flat-square&logo=googlegemini&logoColor=white)](https://ai.google.dev/)
[![OpenWeatherMap](https://img.shields.io/badge/OpenWeatherMap-Live-EB6E4B?style=flat-square&logo=openweathermap&logoColor=white)](https://openweathermap.org/)
[![Visual Crossing](https://img.shields.io/badge/Visual%20Crossing-Forecast-1E88E5?style=flat-square)](https://www.visualcrossing.com/)
[![data.gov.in](https://img.shields.io/badge/data.gov.in-Agmarknet-FF9933?style=flat-square)](https://data.gov.in/)
[![Sentinel--2](https://img.shields.io/badge/Sentinel--2-NDVI-0B3D91?style=flat-square)](https://earth-search.aws.element84.com/)
[![PWA](https://img.shields.io/badge/PWA-installable-5A0FC8?style=flat-square&logo=pwa&logoColor=white)](https://web.dev/progressive-web-apps/)

</div>

---

**Live stack:** Flask + Gunicorn, no database — data is fetched live from government/weather/AI APIs and cached in-memory, with disk-backed history where it matters (market prices).

---

## Table of Contents

- [Features](#-features)
- [Tech Stack](#-tech-stack)
- [External APIs Used](#-external-apis-used)
- [Setup](#-setup)
- [Deployment](#-deployment-hugging-face-spaces)
- [Project Structure](#-project-structure)
- [Key API Routes](#-key-api-routes)
- [Notes on Data Integrity](#-notes-on-data-integrity)

---

## ✨ Features

| | |
|---|---|
| 🏠 **Dashboard** | Current weather, multi-day forecast, crop recommendations for the farmer's location. |
| 🩺 **Diagnose Crop** (`/diagnose`) | Upload a photo of a diseased leaf/stem/fruit/root → disease name, severity, cause, eco-friendly + chemical remedies, and prevention tips. Runs a self-consistency ensemble (multiple passes) plus an optional independent second model for cross-checked confidence. |
| 🚨 **Alerts** (`/alerts`) | Day-by-day weather-driven agricultural risk alerts, a monthly risk calendar, and general seasonal advisories. |
| 💰 **Market Prices** (`/market`) | Live government mandi (wholesale market) prices for crops by state, sourced from the official Agmarknet dataset. Filtered to real crops only — livestock/poultry, ornamental flowers, and timber entries that appear in the raw government feed are excluded. |
| 🛰️ **Vegetation Health (NDVI)** | Satellite-derived crop health index from Sentinel-2 imagery. |
| 💬 **Kisan Helper Chatbot** | Multilingual AI assistant restricted to agriculture topics, with live weather/market data available to it as tools, voice input (speech-to-text), and full app navigation via chat. |
| 📲 **PWA support** | Installable, works offline via a service worker with an offline fallback page. |
| 📊 **Usage tracking** (`/usage`) | Dashboard of API call counts. |

---

## 🧰 Tech Stack

| Layer | Choice |
|---|---|
| Backend | Flask 3, Gunicorn (single worker, multi-threaded — see note below) |
| Frontend | Server-rendered HTML templates + vanilla JS (no framework) |
| Satellite/NDVI | `rasterio`, `numpy` over Sentinel-2 STAC data |
| Deployment target | Hugging Face Spaces (Docker), port `7860` |
| Python version | 3.11.9 (see `runtime.txt`); Docker image uses `python:3.10-slim` |

> **Why a single Gunicorn worker?** The app's in-memory caches (translation cache, crop-AI cache, Agmarknet fetch cache, weather cache) are plain Python dicts. Multiple worker *processes* would each get their own copy, silently halving the cache hit rate and doubling calls to slow/rate-limited external APIs. A single worker with multiple threads shares memory and still gets real concurrency, since this app is I/O-bound (waiting on external APIs), not CPU-bound.

---

## 🔌 External APIs Used

SmartAgro doesn't have its own database of weather, prices, or crop diseases — it calls out to these live sources:

| Provider | Powers | Key required? | Env var |
|---|---|---|---|
| **OpenWeatherMap** | Current conditions + ~5-6 day forecast | Yes (free tier) | `OPENWEATHER_API_KEY` |
| **Visual Crossing** | Extended forecast (out to ~15 days total), feeds the Alerts monthly calendar | Yes (free tier) | `VISUALCROSSING_API_KEY` |
| **Open-Meteo Geocoding** | Turns a typed city name into lat/lon for the chatbot | No (free, keyless) | — |
| **Groq** | Primary chatbot model, primary crop-disease vision model, voice transcription (Whisper) | Yes (free tier) | `GROQ_API_KEY` |
| **Google Gemini** | Fallback chat model, independent second vision model for diagnosis cross-checking, translation | Yes (free tier), optional | `GEMINI_API_KEY` |
| **data.gov.in / Agmarknet** | Official government mandi (market) price data | Yes (free), falls back to a shared rate-limited public test key if unset | `DATA_GOV_API_KEY` |
| **Earth Search STAC (AWS)** | Sentinel-2 satellite imagery for NDVI/vegetation health | No (free, keyless) | — |

See [`.env.example`](./.env.example) for sign-up links and setup notes for each.

---

## 🚀 Setup

### 1. Clone and install

```bash
git clone https://github.com/Anant-083/Smartagro-Main.git
cd Smartagro-Main
pip install -r requirements.txt
```

`rasterio` (used for NDVI) needs GDAL system libraries. On Debian/Ubuntu:

```bash
sudo apt-get install -y libgdal-dev gdal-bin
```

(The Docker image already installs these — see `Dockerfile`.)

### 2. Configure environment variables

```bash
cp .env.example .env
```

Fill in your own keys in `.env` (see the table above and the comments in `.env.example` for where to get each one for free). At minimum you need `GROQ_API_KEY` and `OPENWEATHER_API_KEY` for the app to be useful; the rest degrade gracefully but with reduced functionality if left unset.

**Never commit your real `.env` file** — it's already in `.gitignore`.

### 3. Run locally

```bash
python app.py
```

Or with Gunicorn (closer to production):

```bash
gunicorn --bind 0.0.0.0:7860 --workers 1 --threads 8 --timeout 60 app:app
```

Visit `http://localhost:7860` (or the port Flask prints in dev mode).

### 4. Docker

```bash
docker build -t smartagro .
docker run -p 7860:7860 --env-file .env smartagro
```

---

## ☁️ Deployment (Hugging Face Spaces)

The `Dockerfile` is set up for Hugging Face Spaces' Docker SDK, which expects the app to listen on port `7860`. Add your API keys as **Secrets** in the Space settings (not as plain variables) — they map to the same environment variables described above.

---

## 🗂️ Project Structure

```
Smartagro-Main/
├── app.py                      # All backend routes + logic (Flask)
├── requirements.txt            # Python dependencies
├── runtime.txt                 # Python version pin
├── Dockerfile                  # HF Spaces / Docker deployment
├── .env.example                # Environment variable template (copy to .env)
├── market_history_cache.json   # Persisted market price history (auto-updated)
├── api_usage_tracker.json      # Persisted API call counters
├── templates/
│   ├── index.html              # Dashboard
│   ├── diagnose.html           # Crop disease diagnosis page
│   ├── alerts.html             # Weather alerts (daily/monthly/seasonal)
│   ├── market.html             # Market prices
│   ├── usage.html              # API usage dashboard
│   └── offline.html            # PWA offline fallback
└── static/
    ├── js/
    │   ├── main.js              # Shared helpers (icons, date formatting, etc.)
    │   ├── dashboard.js
    │   ├── diagnose.js
    │   ├── alerts.js
    │   ├── market.js
    │   ├── market_translate.js
    │   ├── kisan-helper.js      # Chatbot frontend logic
    │   └── translations.js      # Multilingual UI strings
    ├── css/
    ├── icons/
    ├── manifest.json            # PWA manifest
    └── service-worker.js        # PWA offline support
```

---

## 🛣️ Key API Routes

| Route | Purpose |
|---|---|
| `GET /api/weather` | Current conditions + merged forecast (OpenWeatherMap + Visual Crossing) |
| `POST /api/alerts` | Today's weather-driven agricultural alerts |
| `POST /api/alerts-forecast` | Per-day alerts across the available forecast window |
| `POST /api/monthly-alerts` | Full-month risk calendar for the Alerts page |
| `POST /api/seasonal-alerts` | General season-driven advisories |
| `POST /api/diagnose` | Crop disease diagnosis from an uploaded photo |
| `GET /api/diagnose-log` / `POST /api/diagnose-log/review` | Diagnosis QA audit trail |
| `GET /api/market` | Live mandi prices for a state (Agmarknet) |
| `GET /api/vegetation` | NDVI / satellite vegetation health |
| `POST /api/chat` | Kisan Helper chatbot |
| `POST /api/stt` | Voice message transcription for the chatbot |
| `POST /api/crop-recommendations` | Suggested crops for the farmer's conditions |
| `POST /api/crop-risk` | Crop-specific risk calendar |
| `GET /api/translate-*` | Per-page translation endpoints (dashboard, market, alerts, diagnose) |
| `GET /api/usage` | API call usage stats |
| `GET /healthz`, `GET /readyz` | Health/readiness checks |

Debug-only routes (`/api/debug-market`, `/api/debug-extended-forecast`) are gated behind `FLASK_DEBUG=1` and return `403` in production.

---

## 🔒 Notes on Data Integrity

This app deliberately avoids fabricating data it doesn't have:

- Weather/forecast days beyond what a real provider returns are marked `unavailable` rather than guessed.
- Market prices only show real, government-reported Agmarknet data — non-crop entries (livestock, poultry, ornamental flowers, timber) that appear in the raw feed are filtered out (`_NON_CROP_COMMODITY_RX` in `app.py`).
- Seasonal advisories only assert a pest/fungal risk when real humidity data crosses a defined threshold — never as a default claim.

<div align="center">

Built for Indian farmers 🇮🇳 &nbsp;•&nbsp; Powered by open weather, satellite, and government data

</div>
