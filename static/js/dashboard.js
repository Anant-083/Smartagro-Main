/* ── Dashboard Translation State ─────────────── */
window._dashTrans = {}; // holds current translations
window._lastCropData = null; // cache last API response for re-render
window._dashTranslateInProgress = false; // guards against overlapping requests

function smoothJump(anchorId) {
    const el = document.getElementById(anchorId);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return false;
}

function dt(key) {
    return window._dashTrans[key] || key;
}

/* ── Native names shown in the "Translating to…" overlay ───── */
const DASH_LANG_DISPLAY_NAMES = {
    hi: 'हिन्दी',
    bn: 'বাংলা',
    te: 'తెలుగు',
    mr: 'मराठी',
    ta: 'தமிழ்',
    gu: 'ગુજરાતી',
    kn: 'ಕನ್ನಡ',
    ml: 'മലയാളം',
    pa: 'ਪੰਜਾਬੀ',
    or: 'ଓଡ଼ିଆ',
    as: 'অসমীয়া',
    ur: 'اردو',
    mai: 'मैथिली',
    sat: 'ᱥᱟᱱᱛᱟᱲᱤ',
    ks: 'کٲشُر',
    ne: 'नेपाली',
    sd: 'سنڌي',
    kok: 'कोंकणी',
    mni: 'মৈতৈলোন্',
    bodo: 'बड़ो',
    doi: 'डोगरी',
    sa: 'संस्कृत',
    en: 'English',
};

/* ── Buffering / loading overlay for slow first-time translations ─────
   Translation calls an LLM on the backend and can take several seconds
   the first time a language is requested (subsequent switches hit the
   server-side cache and are fast). This overlay gives clear feedback
   instead of leaving the page looking stuck. ────────────────────────── */
function ensureDashTranslateOverlayStyles() {
    if (document.getElementById('dashTranslateOverlayStyle')) return;
    const style = document.createElement('style');
    style.id = 'dashTranslateOverlayStyle';
    style.textContent = `
    .dash-translate-overlay {
        position: fixed; inset: 0; z-index: 9999;
        display: flex; align-items: center; justify-content: center;
        background: rgba(10, 16, 12, 0.55);
        backdrop-filter: blur(3px);
        opacity: 0; pointer-events: none;
        transition: opacity 0.2s ease;
    }
    .dash-translate-overlay.visible { opacity: 1; pointer-events: all; }
    .dash-translate-box {
        background: var(--bg-1, #102013);
        border: 1px solid var(--green, #4ade80);
        border-radius: 16px;
        padding: 28px 32px;
        max-width: 320px;
        text-align: center;
        box-shadow: 0 10px 40px rgba(0,0,0,0.35);
        animation: dashTransPopIn 0.25s ease;
    }
    @keyframes dashTransPopIn {
        from { transform: scale(0.92); opacity: 0; }
        to { transform: scale(1); opacity: 1; }
    }
    .dash-translate-spinner {
        width: 38px; height: 38px; margin: 0 auto 14px;
        border: 3px solid rgba(74, 222, 128, 0.25);
        border-top-color: var(--green, #4ade80);
        border-radius: 50%;
        animation: dashTransSpin 0.8s linear infinite;
    }
    @keyframes dashTransSpin { to { transform: rotate(360deg); } }
    .dash-translate-title {
        color: var(--text-1, #f1f5f1);
        font-weight: 600; font-size: 0.95rem; margin-bottom: 6px;
    }
    .dash-translate-sub {
        color: var(--text-3, #94a3a0);
        font-size: 0.78rem; line-height: 1.4;
    }
    .dash-translate-dots span {
        display: inline-block; opacity: 0.3;
        animation: dashTransDot 1.2s infinite;
    }
    .dash-translate-dots span:nth-child(2) { animation-delay: 0.2s; }
    .dash-translate-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dashTransDot { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
    `;
    document.head.appendChild(style);
}

function showDashTranslateOverlay(langCode) {
    if (document.getElementById('txTimerOverlay')?.classList.contains('visible')) return;
    ensureDashTranslateOverlayStyles();
    let overlay = document.getElementById('dashTranslateOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'dashTranslateOverlay';
        overlay.className = 'dash-translate-overlay';
        document.body.appendChild(overlay);
    }
    const name = DASH_LANG_DISPLAY_NAMES[langCode] || langCode.toUpperCase();
    overlay.innerHTML = `
      <div class="dash-translate-box" style="position:relative">
        <button onclick="if (window._dashTranslateController) window._dashTranslateController.abort(); document.getElementById('dashTranslateOverlay').classList.remove('visible')" style=
        <div class="dash-translate-spinner"></div>
        <div class="dash-translate-title">Translating to ${name}<span class="dash-translate-dots"><span>.</span><span>.</span><span>.</span></span></div>
        <div class="dash-translate-sub">First-time translation can take a few seconds. It'll be instant after this.</div>
      </div>`;
    requestAnimationFrame(() => overlay.classList.add('visible'));
}

function hideDashTranslateOverlay() {
    const overlay = document.getElementById('dashTranslateOverlay');
    if (overlay) overlay.classList.remove('visible');
}

async function applyDashboardLanguage(langCode) {
    if (window._dashTranslateInProgress) return;
    window._dashTranslateController = null;

    if (langCode === 'en') {
        window._dashTrans = {};
        window._dashTransLang = null;
        retranslateStaticUI();
        if (window._lastCropData) {
            renderCrops(window._lastCropData);
            renderCalendar(window._lastCropData.calendar);
            renderPesticides(window._lastCropData.pesticides);
            const label = document.getElementById('seasonLabel');
            if (label && window._lastCropData.season) {
                label.textContent = `${dt('Season')}: ${dt(window._lastCropData.season)}`;
            }
        }
        return;
    }

    window._dashTranslateInProgress = true;
    const isGlobal = document.getElementById('txTimerOverlay')?.classList.contains('visible');
    if (!isGlobal) showDashTranslateOverlay(langCode);

   const controller = new AbortController();
    window._dashTranslateController = controller;
    const timeoutId = setTimeout(() => controller.abort(), 60000); 

    try {
        const res = await fetch('/api/translate-dashboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang: langCode }),
            signal: controller.signal,
        });
        const data = await res.json();
       if (data && data.translations && Object.keys(data.translations).length) {
            window._dashTrans = data.translations;
            window._dashTransLang = langCode;
        } else {
            window._dashTrans = {};
            window._dashTransLang = null;
        }
        } catch (err) {
        console.warn('[DashTranslate] error:', err);
        window._dashTrans = {};
    } finally {
        clearTimeout(timeoutId);
        retranslateStaticUI();
        if (window._lastCropData) {
            renderCrops(window._lastCropData);
            renderCalendar(window._lastCropData.calendar);
            renderPesticides(window._lastCropData.pesticides);
            const label = document.getElementById('seasonLabel');
            if (label && window._lastCropData.season) {
                label.textContent = `${dt('Season')}: ${dt(window._lastCropData.season)}`;
            }
        }
        if (!isGlobal) hideDashTranslateOverlay();
        window._dashTranslateInProgress = false;
        window._dashTranslateController = null;
    }
}
function retranslateStaticUI(skipWeatherRender = false) {
    // Section headers
    const map = {
        'section_weather': 'Current Weather Conditions',
        'section_weather_sub': 'Live data from your location',
        'forecast_title': '6-Day Forecast',
        'stat_temp': 'Temperature',
        'stat_humidity': 'Humidity',
        'stat_wind': 'Wind',
        'stat_visibility': 'Visibility',
        'stat_pressure': 'Pressure',
        'section_crops': 'Crop Recommendations',
        'section_crops_sub': 'Based on your climate & location',
        'section_advisory': 'Crop Advisory Calendar',
        'section_advisory_sub': 'Week-by-week action plan for your crops',
        'section_pest': 'Pesticide & Pest Control Guide',
        'section_pest_sub': 'Safe and effective crop protection plan',
        'section_quick': 'Quick Actions',
        'quick_diagnose': 'Diagnose Crop Disease',
        'quick_diagnose_sub': 'Upload or take a photo of your crop',
        'quick_market': 'Check Market Prices',
        'quick_market_sub': 'Live mandi prices across India',
        'quick_alerts': 'View Active Alerts',
        'quick_alerts_sub': 'Weather & pest warnings for your area',
        'footer_text': 'Empowering farmers with AI-driven precision agriculture',
        'section_map': 'Satellite View & Vegetation Health',
        'section_map_sub': 'Live environmental context from your location',
    };
    Object.entries(map).forEach(([key, engVal]) => {
        const el = document.querySelector(`[data-translate="${key}"]`);
        if (el) el.textContent = dt(engVal);
    });
    // Stat labels
    document.querySelectorAll('.stat-label[data-translate]').forEach(el => {
        const key = el.getAttribute('data-translate');
        if (map[key]) el.textContent = dt(map[key]);
    });

    // Re-render dynamic weather components so translated descriptions appear.
    // Skipped during initial load (caller renders them right after this call).
    if (!skipWeatherRender && window.weatherData && window.weatherData.current) {
        renderHeroCard(window.weatherData.current);
        renderWeatherSection(window.weatherData.current, window.weatherData.forecast);
        renderStatBar(window.weatherData.current);
    }
}
/* ── Entry point: Get Location ──────────────── */
function requestLocation() {
    // Use navigator directly here for dashboard
    const btn = document.getElementById('locationBtn');
    if (btn) {
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> <span>Getting location...</span>`;
        btn.disabled = true;
    }

    if (!navigator.geolocation) {
        showToast('Geolocation not supported. Using default location.', 'warning');
        loadWeatherAndCrops(28.6139, 77.2090); // Delhi fallback
        return;
    }

    navigator.geolocation.getCurrentPosition(
        pos => {
            showToast('📍 Location detected!', 'success');
            if (btn) {
                btn.innerHTML = `<i class="fas fa-check"></i> <span>Location Found</span>`;
                btn.style.background = 'linear-gradient(135deg,#166534,#22c55e)';
            }
            saveUserLocation(pos.coords.latitude, pos.coords.longitude, null);
            loadWeatherAndCrops(pos.coords.latitude, pos.coords.longitude);
        },
        () => {
            showToast('Using default location (Delhi).', 'warning');
            if (btn) {
                btn.innerHTML = `<i class="fas fa-location-crosshairs"></i> <span>Get My Location</span>`;
                btn.disabled = false;
            }
            saveUserLocation(28.6139, 77.2090, 'Delhi');
            loadWeatherAndCrops(28.6139, 77.2090);
        }, {
            timeout: 15000, // Gives the browser 15 seconds to find a position
            enableHighAccuracy: false, // Desktop browsers fail high accuracy if they lack a GPS chip
            maximumAge: 60000 // Allows utilizing a recently cached location asset
        }
    );
}

/* ── On dashboard load: initialize buffering animation & load saved location ──── */
document.addEventListener('DOMContentLoaded', () => {
    showDashboardBuffering();
    const saved = getUserLocation();
    if (!saved) return;

    const btn = document.getElementById('locationBtn');
    if (btn) {
        btn.innerHTML = `<i class="fas fa-check"></i> <span>Location Found</span>`;
        btn.style.background = 'linear-gradient(135deg,#166534,#22c55e)';
    }
    loadWeatherAndCrops(saved.lat, saved.lon);
});

/* ── Load weather then crops ────────────────── */
async function loadWeatherAndCrops(lat, lon) {
    showHeroLoading();

    const lang = (localStorage.getItem('agrosmart_lang') || 'en').toLowerCase().trim();
    // Kick off weather and translations in parallel.
    // The loading spinner stays visible until BOTH are done, so the very
    // first render is already in the user's selected language.
    const weatherPromise = fetchWeather(lat, lon);

    let translationsPromise = Promise.resolve(null);
    if (lang !== 'en' && !window._dashTranslateInProgress) {
        // Only reuse the cache if it was built for THIS language
        const cached = (window._dashTransLang === lang && Object.keys(window._dashTrans || {}).length > 0)
            ? Promise.resolve({ translations: window._dashTrans, lang })
            : null;

        if (cached) {
            translationsPromise = cached;
        } else {
            showDashTranslateOverlay(lang); // extends the loading buffer
            translationsPromise = fetch('/api/translate-dashboard', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lang }),
            }).then(r => r.json()).then(d => (d ? { ...d, lang } : d)).catch(() => null);
        }
    }

   const [data, txData] = await Promise.all([weatherPromise, translationsPromise]);
    if (!data) { hideDashTranslateOverlay(); return; }
    const cropPromise = loadCropRecommendations(data.current);

// Apply translations before first render so every dt() call is resolved
if (txData && txData.translations && Object.keys(txData.translations).length) {
        window._dashTrans = txData.translations;
        window._dashTransLang = txData.lang || lang;
    }
    hideDashTranslateOverlay();

    // Now that we know the city name, complete the saved location record
    saveUserLocation(lat, lon, data.current.city);

    // Cache for re-render on language change
    window.weatherData = data;

    // Cache the 7-day forecast so the Alerts page can reuse it
    try {
        sessionStorage.setItem('smartagro_forecast_cache', JSON.stringify({
            current: data.current,
            forecast: data.forecast,
            ts: Date.now(),
        }));
    } catch (e) { /* non-fatal */ }

    // First paint — already translated
    renderHeroCard(data.current);
    renderWeatherSection(data.current, data.forecast);
    renderStatBar(data.current);
    retranslateStaticUI(true); // update [data-translate] section headers; skip weather re-render
    
    // Map and Vegetation Health
    const mapSection = document.getElementById('locationMapSection');
    if (mapSection) mapSection.style.display = '';
    initSatelliteMap(lat, lon);
    // Vegetation is now its own request (see /api/vegetation on the backend).
    // A real satellite lookup can take a few seconds, so it must NOT hold up
    // weather/crops from showing — fetch it in the background and fill the
    // card in whenever it resolves, instead of awaiting it up front.
    loadVegetationHealth(lat, lon);
}

/* ── Vegetation health: fetched separately so a slow satellite lookup
   never blocks the rest of the homepage from rendering ─────────────── */
async function loadVegetationHealth(lat, lon) {
    const container = document.getElementById('vegHealthContainer');
    if (container) {
        container.innerHTML = `
        <div class="veg-loading" style="text-align:center;padding:24px 0;opacity:.7">
            <div class="loading-spinner" style="width:24px;height:24px;margin:0 auto 8px;"></div>
            <span>${dt('Checking satellite imagery…')}</span>
        </div>`;
    }
    try {
        const res = await fetch(`/api/vegetation?lat=${lat}&lon=${lon}`);
        const vegData = await res.json();
        if (vegData) renderVegetationHealth(vegData);
    } catch (e) {
        // Non-fatal — leave the card showing "Data Unavailable" rather than
        // breaking the rest of the page.
        renderVegetationHealth({ ndvi: null, status: 'Data Unavailable', obs_date: null, source: 'Satellite', cloud_pct: null });
    }
}

/* ── Hero weather card ──────────────────────── */
function showHeroLoading() {
    const card = document.getElementById('heroWeatherCard');
    if (card) card.innerHTML = `
    <div class="hwc-loading">
      <div class="loading-spinner" style="width:32px;height:32px;margin:0 auto 8px;"></div>
      <span style="color:var(--text-2);font-size:0.85rem">Fetching weather...</span>
    </div>`;
}

function renderHeroCard(w) {
    const card = document.getElementById('heroWeatherCard');
    if (!card) return;
    card.innerHTML = `
    <div class="hwc-loaded">
      <div class="hwc-city">
        <i class="fas fa-location-dot" style="color:var(--green)"></i> ${w.city}
      </div>
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div class="hwc-temp">${w.temp}°</div>
          <div class="hwc-desc">${dt(capitalize(w.description)) || capitalize(w.description)}</div>
        </div>
        <div class="hwc-icon-large">${getWeatherEmoji(w.icon)}</div>
      </div>
      <div class="hwc-stats">
        <div class="hwc-stat"><i class="fas fa-droplets"></i> ${w.humidity}% ${dt('Humidity')}</div>
        <div class="hwc-stat"><i class="fas fa-wind"></i> ${w.wind_speed} m/s ${dt('Wind Speed')}</div>
        <div class="hwc-stat"><i class="fas fa-temperature-half"></i> ${dt('Feels like')} ${w.feels_like}°C</div>
        <div class="hwc-stat"><i class="fas fa-gauge-high"></i> ${w.pressure} hPa</div>
      </div>
    </div>`;
    card.style.animation = 'fadeInUp 0.5s ease';
}

/* ── Full weather section ───────────────────── */
function renderWeatherSection(current, forecast) {
    const section = document.getElementById('weatherSection');
    if (section) section.style.display = '';

    const mainEl = document.getElementById('weatherMain');
    if (mainEl) {
        mainEl.innerHTML = `
      <!-- Primary card -->
      <div class="weather-primary-card">
        <div>
          <div style="font-size:4.5rem;line-height:1">${getWeatherEmoji(current.icon)}</div>
        </div>
        <div>
          <div class="wpc-temp">${current.temp}°C</div>
          <div class="wpc-city"><i class="fas fa-location-dot" style="color:var(--green);margin-right:4px"></i>${current.city}</div>
          <div class="wpc-desc">${dt(capitalize(current.description)) || capitalize(current.description)}</div>
          <div class="wpc-feels">${dt('Feels like')} ${current.feels_like}°C</div>
        </div>
      </div>
      <!-- Stat cards -->
      <div class="weather-stat-card">
        <div class="wsc-icon"><i class="fas fa-droplets"></i></div>
        <div class="wsc-label">${dt('Humidity')}</div>
        <div class="wsc-val">${current.humidity}<span class="wsc-unit">%</span></div>
        <div style="margin-top:auto">
          ${getHumidityBar(current.humidity)}
        </div>
      </div>
      <div class="weather-stat-card">
        <div class="wsc-icon"><i class="fas fa-wind"></i></div>
        <div class="wsc-label">${dt('Wind Speed')}</div>
        <div class="wsc-val">${current.wind_speed}<span class="wsc-unit"> m/s</span></div>
        <div style="font-size:0.75rem;color:var(--text-3);margin-top:4px">${dt(getWindDesc(current.wind_speed)) || getWindDesc(current.wind_speed)}</div>
      </div>
      <div class="weather-stat-card">
        <div class="wsc-icon"><i class="fas fa-gauge-high"></i></div>
        <div class="wsc-label">${dt('Pressure')}</div>
        <div class="wsc-val">${current.pressure}<span class="wsc-unit"> hPa</span></div>
      </div>
      <div class="weather-stat-card">
        <div class="wsc-icon"><i class="fas fa-eye"></i></div>
        <div class="wsc-label">${dt('Visibility')}</div>
        <div class="wsc-val">${current.visibility.toFixed(1)}<span class="wsc-unit"> km</span></div>
      </div>
    `;
    }

    // 7-day forecast
    const forecastGrid = document.getElementById('forecastGrid');
    if (forecastGrid && forecast) {
        const todayStr = new Date().toISOString().split('T')[0];
        forecastGrid.innerHTML = forecast.map((day, i) => `
      <div class="forecast-card ${day.date === todayStr ? 'today' : ''}" style="animation-delay:${i * 0.06}s">
        <div class="fc-day">${getDayName(day.date)}</div>
        <div class="fc-icon">${getWeatherEmoji(day.icon)}</div>
        <div class="fc-desc">${dt(capitalize(day.description)) || capitalize(day.description)}</div>
        <div class="fc-temps">
          <span class="fc-max">${Math.round(day.temp_max)}°</span>
          <span class="fc-min">${Math.round(day.temp_min)}°</span>
        </div>
        <div style="font-size:0.68rem;color:var(--text-3);margin-top:4px">
          <i class="fas fa-droplets" style="color:#38bdf8"></i> ${day.humidity}%
        </div>
      </div>
    `).join('');
    }
}

function getHumidityBar(h) {
    const pct = Math.min(100, h);
    const color = h > 80 ? '#38bdf8' : h > 60 ? 'var(--green)' : 'var(--amber)';
    return `
    <div style="height:4px;background:var(--bg-2);border-radius:2px;overflow:hidden;margin-top:8px">
      <div style="height:100%;width:${pct}%;background:${color};border-radius:2px;transition:width 1s ease"></div>
    </div>`;
}

function getWindDesc(speed) {
    if (speed < 1) return 'Calm';
    if (speed < 6) return 'Light breeze';
    if (speed < 14) return 'Moderate breeze';
    if (speed < 25) return 'Strong breeze';
    return 'Storm warning';
}

/* ── Stats bar ──────────────────────────────── */
function renderStatBar(w) {
    const bar = document.getElementById('statsBar');
    if (bar) bar.style.display = '';

    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };
    setVal('statTemp', `${w.temp}°C`);
    setVal('statHumidity', `${w.humidity}%`);
    setVal('statWind', `${w.wind_speed} m/s`);
    setVal('statVisibility', `${w.visibility.toFixed(1)} km`);
    setVal('statPressure', `${w.pressure} hPa`);
}

/* ── Dashboard Buffering Animation ─────────────── */
function showDashboardBuffering() {
    const cropSection = document.getElementById('cropSection');
    const cropsGrid = document.getElementById('cropsGrid');
    if (cropSection) cropSection.style.display = '';
    if (cropsGrid && !window._lastCropData) {
        cropsGrid.innerHTML = `
        <div class="crop-buffering-grid">
          <div class="crop-buffering-box">
            <div class="buffering-spinner"></div>
            <div>
              <div style="font-weight:700;color:var(--text-1);margin-bottom:4px">
                <i class="fas fa-seedling" style="color:var(--green);margin-right:6px"></i>
                ${dt('Analyzing Climate & Soil Conditions...')}
              </div>
              <div style="font-size:0.83rem;color:var(--text-3)">
                ${dt('Generating location-tailored crop recommendations and yield calculations.')}
              </div>
            </div>
          </div>
          <div class="crop-buffering-box hidden-mobile">
            <div class="buffering-spinner"></div>
            <div>
              <div style="font-weight:700;color:var(--text-1);margin-bottom:4px">
                <i class="fas fa-microscope" style="color:var(--green);margin-right:6px"></i>
                ${dt('Matching High-Profit Crops...')}
              </div>
              <div style="font-size:0.83rem;color:var(--text-3)">
                ${dt('Evaluating market demand and seasonal suitability for your farm.')}
              </div>
            </div>
          </div>
        </div>`;
    }

    const advisorySection = document.getElementById('advisorySection');
    const timeline = document.getElementById('calendarTimeline');
    if (advisorySection) advisorySection.style.display = '';
    if (timeline && !window._lastCropData) {
        timeline.innerHTML = `
        <div class="calendar-buffering-box">
          <div class="buffering-spinner"></div>
          <div>
            <div style="font-weight:700;color:var(--text-1);margin-bottom:4px">
              <i class="fas fa-calendar-days" style="color:#fbbf24;margin-right:6px"></i>
              ${dt('Preparing Crop Advisory Calendar...')}
            </div>
            <div style="font-size:0.83rem;color:var(--text-3)">
              ${dt('Formulating week-by-week sowing, irrigation, fertilizer, and harvest timelines.')}
            </div>
          </div>
        </div>`;
    }

    const pestSection = document.getElementById('pestSection');
    const pestCards = document.getElementById('pestCards');
    if (pestSection) pestSection.style.display = '';
    if (pestCards && !window._lastCropData) {
        pestCards.innerHTML = `
        <div class="pest-buffering-box">
          <div class="buffering-spinner"></div>
          <div>
            <div style="font-weight:700;color:var(--text-1);margin-bottom:4px">
              <i class="fas fa-bug" style="color:var(--amber);margin-right:6px"></i>
              ${dt('Preparing Pesticide & Safety Guide...')}
            </div>
            <div style="font-size:0.83rem;color:var(--text-3)">
              ${dt('Calculating eco-friendly dosage and safe chemical options for your location.')}
            </div>
          </div>
        </div>`;
    }
}

/* ── Crop Recommendations ───────────────────── */
async function loadCropRecommendations(current) {
    showDashboardBuffering();
    try {
        const res = await fetch('/api/crop-recommendations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                temp: current.temp,
                humidity: current.humidity,
                rain: current.rain || 0,
                city: current.city,
                lat: current.lat,
                lon: current.lon,
            })
        });
        const data = await res.json();
        window._lastCropData = data; // cache for re-render on language change

        renderCrops(data, current);
        renderCalendar(data.calendar);
        renderPesticides(data.pesticides);

        // Cache for the Alerts page's "Check Upcoming Risks" feature
        try {
            sessionStorage.setItem('smartagro_crop_cache', JSON.stringify({
                data,
                lat: current.lat,
                lon: current.lon,
                ts: Date.now(),
            }));
        } catch (e) { /* non-fatal */ }

        const label = document.getElementById('seasonLabel');
        const cityName = data.city || current.city || 'Your Location';
        if (label) {
            label.innerHTML = `<i class="fas fa-location-dot" style="color:#4ade80;margin-right:4px"></i> <strong>${cityName}</strong> &bull; ${dt(data.season)} (${current.temp}°C, ${current.humidity}% Humidity)`;
        }
    } catch (err) {
        console.error('Crop API error:', err);
        showToast('Could not load crop recommendations.', 'error');
    }
}

/* ── Render crop cards ──────────────────────── */
function renderCrops(data, current) {
    const section = document.getElementById('cropSection');
    const grid = document.getElementById('cropsGrid');
    if (!section || !grid) return;
    section.style.display = '';

    const crops = data.crops || [];
    const cityName = data.city || (current && current.city) || 'Your Region';

    grid.innerHTML = crops.map((crop, i) => `
    <div class="crop-card" style="animation-delay:${i * 0.07}s">
      <div class="crop-card-top">
        <div class="crop-emoji">${crop.icon}</div>
        <div class="crop-match-badge">
          <i class="fas fa-check-circle"></i> ${crop.match} ${dt('Match')}
        </div>
      </div>
      <div class="crop-name">${dt(crop.name)}</div>
      <div class="verified-badge">
        <i class="fas fa-shield-check"></i> ${dt('Location Verified')}
      </div>
      
      <!-- Location & Weather Suitability Badges -->
      <div class="crop-suitability-box">
        <div class="cs-badge cs-location">
          <i class="fas fa-map-marker-alt"></i>
          <span>${crop.location_suitability ? dt(crop.location_suitability) : dt('Suited to ' + cityName + ' region')}</span>
        </div>
        <div class="cs-badge cs-weather">
          <i class="fas fa-cloud-sun"></i>
          <span>${crop.weather_suitability ? dt(crop.weather_suitability) : dt('Matches current weather')}</span>
        </div>
      </div>

      <div class="crop-desc">${dt(crop.description)}</div>
      <div class="crop-meta">
        <div class="cm-item">
          <span class="cm-label">${dt('Season')}</span>
          <span class="cm-val">${dt((crop.season || '').split(' ')[0])}</span>
        </div>
        <div class="cm-item">
          <span class="cm-label">${dt('Water Need')}</span>
          <span class="cm-val">${dt(crop.water)}</span>
        </div>
        <div class="cm-item">
          <span class="cm-label">${dt('Expected Yield')}</span>
          <span class="cm-val">${crop.yield}</span>
        </div>
        <div class="cm-item">
          <span class="cm-label">${dt('Duration')}</span>
          <span class="cm-val">${crop.duration}</span>
        </div>
        <div class="cm-item">
          <span class="cm-label">${dt('Soil Type')}</span>
          <span class="cm-val">${dt(crop.soil)}</span>
        </div>
        <div class="cm-item">
          <span class="cm-label">${dt('Fertilizer')}</span>
          <span class="cm-val">${crop.fertilizer}</span>
        </div>
      </div>
      <div class="crop-profit">
        <i class="fas fa-indian-rupee-sign"></i>
        ${dt('Estimated Profit')}: ${crop.profit}
      </div>
    </div>
  `).join('');

    setTimeout(() => observeAnimations(), 100);
}

/* ── Render advisory calendar ───────────────── */
function renderCalendar(calendar) {
    const section = document.getElementById('advisorySection');
    const timeline = document.getElementById('calendarTimeline');
    if (!section || !timeline) return;
    section.style.display = '';

    timeline.innerHTML = calendar.map((item, i) => `
    <div class="timeline-item" style="animation-delay:${i * 0.05}s">
      <div class="timeline-dot ${item.type}"></div>
      <div class="timeline-card">
        <div class="tc-date">
          <span>${item.date}</span>
          <span class="tc-week">${dt('Week')} ${item.week}</span>
        </div>
        <div class="tc-activity">
          <i class="${getActivityIcon(item.type)}" style="margin-right:6px;color:${getActivityColor(item.type)}"></i>
          ${dt(item.activity)}
        </div>
        <span class="tc-type ${item.type}">${dt(item.type)}</span>
      </div>
    </div>
  `).join('');
}

function getActivityIcon(type) {
    const icons = {
        preparation: 'fas fa-shovel',
        sowing: 'fas fa-seedling',
        irrigation: 'fas fa-faucet-drip',
        fertilizer: 'fas fa-flask',
        maintenance: 'fas fa-scissors',
        pesticide: 'fas fa-spray-can-sparkles',
        harvest: 'fas fa-wheat-awn',
    };
    return icons[type] || 'fas fa-circle';
}

function getActivityColor(type) {
    const colors = {
        preparation: 'var(--teal)',
        sowing: 'var(--green)',
        irrigation: '#38bdf8',
        fertilizer: 'var(--amber)',
        maintenance: 'var(--green-2)',
        pesticide: 'var(--red)',
        harvest: '#a78bfa',
    };
    return colors[type] || 'var(--text-3)';
}

/* ── Render pesticide guide ─────────────────── */
function renderPesticides(pesticides) {
    const section = document.getElementById('pestSection');
    const cards = document.getElementById('pestCards');
    if (!section || !cards) return;
    section.style.display = '';

    if (!pesticides || pesticides.length === 0) {
        cards.innerHTML = `
        <div class="pest-buffering-box">
          <div>
            <div style="font-weight:700;color:var(--text-1);margin-bottom:4px">
              <i class="fas fa-circle-info" style="color:var(--text-3);margin-right:6px"></i>
              ${dt('No pesticide guide available for the recommended crops yet.')}
            </div>
          </div>
        </div>`;
        return;
    }

    cards.innerHTML = pesticides.map(p => `
    <div class="pest-crop-card">
      <div class="pcc-header">
        <span>🌾</span> ${dt(p.crop)} — ${dt('Pest Control Plan')}
      </div>
      <div class="pcc-items">
        ${p.guides.map(g => `
          <div class="pcc-item">
            <div class="pcc-pest"><i class="fas fa-bug" style="color:var(--amber);margin-right:6px"></i>${dt(g.pest)}</div>
            <div class="pcc-meta">
              <span><i class="fas fa-flask"></i> ${g.pesticide}</span>
              <span><i class="fas fa-scale-balanced"></i> ${g.dose}</span>
            </div>
            <div style="font-size:0.75rem;color:var(--text-3);margin-top:4px">
              <i class="fas fa-clock"></i> ${dt('Timing')}: ${g.timing}
            </div>
            <div class="pcc-eco eco-${g.eco}">
              ${g.eco
                ? `<i class="fas fa-leaf"></i> ${dt('Eco-Friendly')}`
                : `<i class="fas fa-flask"></i> ${dt('Chemical')}`}
            </div>
          </div>
        `).join('')}
      </div>
    </div>
  `).join('');
}

/* ── MAP & VEGETATION HEALTH ────────────────── */
let _satelliteMap = null;
let _satelliteMarker = null;
let _currentMapLat = null;
let _currentMapLon = null;

function initSatelliteMap(lat, lon) {
    const container = document.getElementById('satelliteMapContainer');
    if (!container) return;
    
    // Default to Delhi if invalid
    const mapLat = lat || 28.6139;
    const mapLon = lon || 77.2090;
    _currentMapLat = mapLat;
    _currentMapLon = mapLon;

    if (!_satelliteMap) {
        // Initialize Map
        _satelliteMap = L.map('satelliteMapContainer').setView([mapLat, mapLon], 16); // closer zoom
        
        // Add Esri World Imagery (Satellite)
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: 'Tiles &copy; Esri',
            maxZoom: 18
        }).addTo(_satelliteMap);
        
        // Add a marker for the user's location
        _satelliteMarker = L.marker([mapLat, mapLon]).addTo(_satelliteMap)
            .bindPopup(dt('Your Exact Location'))
            .openPopup();

        // Add a custom recenter button
        const recenterControl = L.control({ position: 'bottomright' });
        recenterControl.onAdd = function() {
            const div = L.DomUtil.create('div', 'leaflet-bar leaflet-control leaflet-control-custom');
            div.innerHTML = `<a href="#" title="Recenter to My Location" style="background-color: var(--bg-1, #102013); color: #4ade80; width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; font-size: 1.1rem; border: 1px solid rgba(74, 222, 128, 0.2);"><i class="fas fa-location-crosshairs"></i></a>`;
            div.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                if (_satelliteMap && _currentMapLat && _currentMapLon) {
                    _satelliteMap.setView([_currentMapLat, _currentMapLon], 18);
                }
            };
            return div;
        };
        recenterControl.addTo(_satelliteMap);

    } else {
        _satelliteMap.setView([mapLat, mapLon], 16);
        if (_satelliteMarker) {
            _satelliteMarker.setLatLng([mapLat, mapLon]);
        }
    }
}

function renderVegetationHealth(vegData) {
    const container = document.getElementById('vegHealthContainer');
    if (!container || !vegData) return;

    const ndvi     = vegData.ndvi;
    const hasData  = ndvi != null && !!vegData.obs_date;   // true only when a real Sentinel-2 reading came back
    const source   = vegData.source    || 'Sentinel-2 L2A';
    const cloudPct = vegData.cloud_pct != null ? `${vegData.cloud_pct}% cloud` : '';
    const barWidth = ndvi != null ? Math.max(0, Math.min(100, ndvi * 100)) : 0;
    const ndviDisplay = ndvi != null ? ndvi.toFixed(3) : '—';

    // Short label for the subtitle badge
    const sourceShort = source.includes('Sentinel') ? 'Sentinel-2 NDVI' : 'Satellite NDVI';

    // Only claim an observation date / describe it as "real satellite reflectance"
    // when we actually got one back from the server. Never fabricate a date.
    const lastObservedLine = hasData
        ? `Last observed: ${vegData.obs_date}${cloudPct ? ' &nbsp;·&nbsp; ' + cloudPct : ''}`
        : `No recent cloud-free imagery available for this location`;

    const descLine = hasData
        ? `Real satellite reflectance at your exact location — Copernicus ${source.replace('Copernicus ', '')} via Earth Search STAC (no login required).`
        : `We couldn't retrieve a real Sentinel-2 reading for this exact spot right now (often due to persistent cloud cover or no recent pass). This is not a fabricated value — try again later.`;

    container.innerHTML = `
        <div class="veg-icon-wrapper">
            <i class="fas fa-seedling"></i>
        </div>
        <div class="veg-title-wrapper">
            <span class="veg-title-main">${dt('Vegetation Health')}</span>
            <span class="veg-title-sub">(${sourceShort})</span>
        </div>
        <div class="veg-score-main">${ndviDisplay}</div>
        <div class="veg-status-main">${dt(vegData.status) || vegData.status}</div>
        
        <div class="veg-progress-bar">
            <div class="veg-progress-fill" style="width: ${barWidth}%"></div>
        </div>
        
        <div class="veg-last-observed">
            ${lastObservedLine}
        </div>
        <div class="veg-desc-main">
            ${descLine}
        </div>
    `;
}
