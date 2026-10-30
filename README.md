---
title: SmartAgro
emoji: 🌱
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# SmartAgro

SmartAgro is a multilingual agricultural advisory web app for Indian farmers. It combines weather and market data with crop risk guidance, AI-assisted crop diagnosis, satellite vegetation indicators, and a farming-focused chatbot.

**Core features**

- Local weather, forecasts, and crop recommendations
- Weather-based alerts, seasonal guidance, and crop risk calendars
- Crop photo diagnosis with hosted AI providers and an optional offline model
- Government mandi prices and saved price history
- Satellite-derived vegetation health indicators when imagery and dependencies are available
- Kisan Helper chat with Groq as the primary provider and Gemini as an optional fallback
- Voice input, multilingual interface, and installable progressive web app support

SmartAgro does not use a relational database. It calls external data providers and stores selected counters and market history in local JSON files. Some responses are cached in memory.

## Requirements

- Python 3.11 for local development, as specified by `runtime.txt`
- Docker for containerized deployment
- API keys for the hosted features you want to enable

The current Docker image uses Python 3.10. Local development uses the version in `runtime.txt`.

## Quick start

### 1. Get the code and install dependencies

```powershell
git clone https://github.com/Anant-083/SmartAgro-MAIN-2.git
cd SmartAgro-MAIN-2
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

On macOS or Linux, create and activate the environment with:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure API keys

Create a local environment file:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

Edit `.env` and add keys for the services you want. Never commit real API keys or the `.env` file.

| Environment variable | Used for | Required |
|---|---|---|
| `OPENWEATHER_API_KEY` | Current weather and forecast data | For live weather |
| `VISUALCROSSING_API_KEY` | Extended forecast and alerts calendar | Optional |
| `GROQ_API_KEY` | Primary chat provider, voice transcription, and crop diagnosis | Optional; recommended |
| `GEMINI_API_KEY_CHAT` | Chat fallback when Groq is unavailable or fails | Optional |
| `GEMINI_CHAT_MODEL` | Gemini chat model; defaults to `gemini-2.5-flash` | Optional |
| `GEMINI_API_KEY_DIAGNOSE` | Gemini crop diagnosis | Optional |
| `GEMINI_API_KEY_TRANSLATE` | Gemini translation helpers | Optional |
| `GEMINI_API_KEY` | Shared fallback key for Gemini integrations without a dedicated key | Optional |
| `GEMMA_API_KEY_DIAGNOSE` | Additional hosted crop diagnosis provider | Optional |
| `DATA_GOV_API_KEY` | Official Agmarknet market data | Optional |
| `ADMIN_SECRET_KEY` | Protects usage and diagnosis review tools | Recommended for deployments |

Dedicated Gemini keys take precedence over `GEMINI_API_KEY`. For example, chat uses `GEMINI_API_KEY_CHAT` when set and otherwise can use the shared Gemini key.

The app can start with missing optional keys, but the corresponding live or hosted features will be limited. The shared public Agmarknet key may be rate limited.

Crop diagnosis tries Gemini, Groq, and Google Gemma, then the local ONNX model when no hosted provider returns a diagnosis.

### 3. Start the app

```powershell
python app.py
```

Open [http://localhost:7860](http://localhost:7860). The startup log reports which integrations are configured; it does not make live API checks.

For a production-like local run:

```bash
gunicorn --bind 0.0.0.0:7860 --workers 1 --threads 8 --timeout 60 app:app
```

The single Gunicorn worker is intentional: in-memory caches are shared among threads in a worker, but not across worker processes.

## Pages and capabilities

| Page | Path | Description |
|---|---|---|
| Dashboard | `/` | Weather, forecast, and location-based crop guidance |
| Crop diagnosis | `/diagnose` | Submit a crop photo for hosted or offline diagnosis |
| Market prices | `/market` | Agmarknet mandi prices and available price history |
| Alerts | `/alerts` | Weather alerts, forecast risks, and seasonal guidance |
| Offline page | `/offline` | Fallback page when a page cannot be loaded |
| Usage dashboard | `/usage` | Protected usage statistics for administrators |

The interface supports English and 22 Indian languages. Translation quality can vary, so review machine-generated translations before relying on them for critical guidance.

### Crop diagnosis and offline model

Diagnosis tries configured hosted providers before the local ONNX model. The local model covers 15 leaf classes across tomato, potato, and bell pepper; it is not a general crop disease detector and provides no treatment advice. Confirm uncertain results with a qualified local agricultural expert.

The model is downloaded and checksum-verified during the Docker build. To download it for a local setup:

```bash
python scripts/download_offline_diagnosis_model.py
```

See [THIRD_PARTY_MODEL_LICENSES.txt](./THIRD_PARTY_MODEL_LICENSES.txt) for model licensing information.

### Translation data

Diagnosis-page translations and the crop disease glossary are stored under `translations/`. To generate or resume diagnosis translations:

```bash
python generate_diagnose_translations.py
```

You can optionally pass language codes, such as `python generate_diagnose_translations.py mai sa`.

To fill missing interface translations:

```bash
python fill_missing_translations.py
```

This helper supports optional language codes, preserves existing translations, and can be rerun to continue filling missing strings. Review generated translations with native speakers.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/weather` | Current conditions and forecast |
| `POST` | `/api/crop-recommendations` | Crop recommendations |
| `GET` | `/api/vegetation` | Satellite vegetation health |
| `POST` | `/api/alerts` | Current-condition agricultural alerts |
| `POST` | `/api/alerts-forecast` | Alerts across a forecast |
| `POST` | `/api/crop-risk` | Crop-specific weather risk |
| `POST` | `/api/monthly-alerts` | Monthly risk calendar |
| `POST` | `/api/seasonal-alerts` | Seasonal advisories |
| `POST` | `/api/diagnose` | Diagnose an uploaded crop image |
| `GET` | `/api/market` | Market prices by location |
| `POST` | `/api/chat` | Kisan Helper chatbot |
| `POST` | `/api/stt` | Transcribe chatbot voice input |
| `GET` | `/healthz` | Liveness check |
| `GET` | `/readyz` | Integration configuration summary |

Translation and diagnosis review endpoints are also available in the route modules. Usage statistics and diagnosis review data are restricted to administrators. Debug endpoints are disabled unless `FLASK_DEBUG=1`; do not enable debug mode in production.

## Tests

Run the test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

## Docker

Build and run the image from the repository root:

```bash
docker build -t smartagro .
docker run --rm -p 7860:7860 --env-file .env smartagro
```

The container listens on port `7860`. The Docker image installs the system libraries needed by `rasterio` and downloads the offline diagnosis model during its build.

## Hugging Face Spaces

The Docker configuration targets Hugging Face Spaces on port `7860`. Add API keys as Space Secrets using the same names listed above. Do not put keys in source files or commit them to the repository.

## Project structure

```text
app.py                              Flask application, page routes, health checks
shared.py                           Shared configuration, translations, and utilities
routes/
  alerts.py                         Weather alerts and crop risk endpoints
  chat.py                           Chatbot and speech-to-text endpoints
  diagnose.py                       Crop diagnosis and diagnosis translations
  market.py                         Agmarknet prices and market translations
  weather.py                        Weather, recommendations, and vegetation
templates/                          Server-rendered page templates
static/                             CSS, JavaScript, icons, and PWA assets
translations/                       Per-language diagnosis translations
tests/                              Application, diagnosis, and translation tests
scripts/                            Model download and maintenance scripts
models/offline_diagnosis/            Optional local ONNX diagnosis model
```

## Data and limitations

- Weather, forecasts, and market prices depend on third-party services and their availability.
- Missing forecast data is not a forecast and should not be treated as one.
- Market data is sourced from Agmarknet; it can be delayed, incomplete, or rate limited.
- Satellite imagery may be unavailable because of location, cloud cover, or provider availability.
- The offline diagnosis model supports only its listed classes and may be inaccurate on field photos.
- AI-generated recommendations are informational and should not replace local agronomic expertise.
