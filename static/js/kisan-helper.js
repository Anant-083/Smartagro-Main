/* ═══════════════════════════════════════════════
   alerts.js — Alerts page logic
   Handles: location → weather → alerts rendering,
            pest calendar, pesticide safety,
            harmful/safe crops, risk chart,
            full 23-language dynamic translation
═══════════════════════════════════════════════ */

let allAlerts = [];
let activeFilter = 'all';
let riskChartInst = null;
let currentWeather = null;
let currentForecast = null; // 6-day forecast, cached once weather is fetched
let dailyAlertsData = null; // per-day alerts from /api/alerts-forecast
let selectedDayIndex = 0; // which day's alerts are currently shown
let upcomingRisksChecked = false;
let weeklyDangerDays = [];
let weeklyDangerPopupShown = false;

/* ══════════════════════════════════════════════
   ALERTS TRANSLATION SYSTEM
══════════════════════════════════════════════ */
let _alertsTx = {};
const _alertsTxCache = {};
let _alertsTxInProgress = false;

function _at(key) {
    if (!key) return '';
    return _alertsTx[key] || key;
}

/* ── Language display names ─────────────────── */
const ALERTS_LANG_NAMES = {
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

/* ── Buffering overlay ──────────────────────── */
function _ensureAlertsOverlayCSS() {
    if (document.getElementById('alertsTxOverlayStyle')) return;
    const s = document.createElement('style');
    s.id = 'alertsTxOverlayStyle';
    s.textContent = `
    .alerts-tx-overlay{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;
        justify-content:center;background:rgba(10,16,12,0.55);backdrop-filter:blur(3px);
        opacity:0;pointer-events:none;transition:opacity 0.2s ease}
    .alerts-tx-overlay.visible{opacity:1;pointer-events:all}
    .alerts-tx-box{background:var(--bg-1,#102013);border:1px solid var(--green,#4ade80);
        border-radius:16px;padding:28px 32px;max-width:320px;text-align:center;
        box-shadow:0 10px 40px rgba(0,0,0,0.35);animation:atxPopIn 0.25s ease}
    @keyframes atxPopIn{from{transform:scale(0.92);opacity:0}to{transform:scale(1);opacity:1}}
    .alerts-tx-spinner{width:38px;height:38px;margin:0 auto 14px;
        border:3px solid rgba(74,222,128,0.25);border-top-color:var(--green,#4ade80);
        border-radius:50%;animation:atxSpin 0.8s linear infinite}
    @keyframes atxSpin{to{transform:rotate(360deg)}}
    .alerts-tx-title{color:var(--text-1,#f1f5f1);font-weight:600;font-size:0.95rem;margin-bottom:6px}
    .alerts-tx-sub{color:var(--text-3,#94a3a0);font-size:0.78rem;line-height:1.4}
    .alerts-tx-dots span{display:inline-block;opacity:0.3;animation:atxDot 1.2s infinite}
    .alerts-tx-dots span:nth-child(2){animation-delay:0.2s}
    .alerts-tx-dots span:nth-child(3){animation-delay:0.4s}
    @keyframes atxDot{0%,100%{opacity:0.3}50%{opacity:1}}
    `;
    document.head.appendChild(s);
}

function showAlertsOverlay(langCode) {
    if (document.getElementById('txTimerOverlay')?.classList.contains('visible')) return;
    _ensureAlertsOverlayCSS();
    let ov = document.getElementById('alertsTxOverlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'alertsTxOverlay';
        ov.className = 'alerts-tx-overlay';
        document.body.appendChild(ov);
    }
    const name = ALERTS_LANG_NAMES[langCode] || langCode.toUpperCase();
    ov.innerHTML = `<div class="alerts-tx-box" style="position:relative">
        <button onclick="document.getElementById('alertsTxOverlay').classList.remove('visible')" style="position:absolute;top:8px;right:10px;background:rgba(255,255,255,.1);border:none;color:#fff;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:1.1rem;display:flex;align-items:center;justify-content:center;transition:background .2s" onmouseover="this.style.background='rgba(248,113,113,.4)'" onmouseout="this.style.background='rgba(255,255,255,.1)'" title="Cancel">&times;</button>
        <div class="alerts-tx-spinner"></div>
        <div class="alerts-tx-title">Translating to ${name}<span class="alerts-tx-dots"><span>.</span><span>.</span><span>.</span></span></div>
        <div class="alerts-tx-sub">First-time translation can take a few seconds. It'll be instant after this.</div>
    </div>`;
    requestAnimationFrame(() => ov.classList.add('visible'));
}

function hideAlertsOverlay() {
    const ov = document.getElementById('alertsTxOverlay');
    if (ov) ov.classList.remove('visible');
}

/* ── Load translations from server ─────────── */
async function loadAlertsTranslations(lang) {
    lang = (lang || localStorage.getItem('smartagro_lang') || localStorage.getItem('agrosmart_lang') || 'en').toLowerCase().trim();

    if (lang === 'en') {
        _alertsTx = {};
        reRenderAlerts();
        return;
    }

    if (_alertsTxCache[lang]) {
        _alertsTx = _alertsTxCache[lang];
        reRenderAlerts();
        return;
    }

    if (_alertsTxInProgress) return;
    _alertsTxInProgress = true;

    const isGlobal = document.getElementById('txTimerOverlay')?.classList.contains('visible');
    if (!isGlobal) showAlertsOverlay(lang);

    // Collect dynamic terms from currently loaded alerts
    const dynamicTerms = new Set();
    if (Array.isArray(allAlerts)) {
        allAlerts.forEach(a => {
            if (a.title) dynamicTerms.add(a.title);
            if (a.message) dynamicTerms.add(a.message);
            if (a.description) dynamicTerms.add(a.description);
            if (a.action) dynamicTerms.add(a.action);
            if (a.category) dynamicTerms.add(a.category);
        });
    }

    try {
        const res = await fetch('/api/translate-alerts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang, dynamic_terms: Array.from(dynamicTerms) })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        _alertsTx = data.translations || {};
        _alertsTxCache[lang] = _alertsTx;
    } catch (e) {
        console.warn('[Alerts] Translation failed:', e);
        _alertsTx = {};
    }

    reRenderAlerts();
    if (!isGlobal) hideAlertsOverlay();
    _alertsTxInProgress = false;
}

/* ── Re-render all dynamic sections ────────── */
function reRenderAlerts() {
    if (!currentWeather) return;
    renderAlertsList(allAlerts);
    renderPesticideSafety(currentWeather);
    renderHarmfulSafeCrops(currentWeather);
    renderRiskChart(currentWeather, allAlerts);
    if (dailyAlertsData) renderDayTabs(dailyAlertsData);
    // Also re-apply any [data-translate] static strings
    if (typeof applyTranslations === 'function') applyTranslations();
}

/* ── Hook into global language switcher ─────── */
document.addEventListener('langChanged', (e) => {
    loadAlertsTranslations(e.detail?.lang || 'en');
});

/* ── Quick-jump nav (hero "Crops at Risk" / "Safe to Grow") ─── */
function smoothJump(anchorId) {
    const el = document.getElementById(anchorId);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return false; // prevent the default '#anchor' jump so the smooth scroll isn't fought
}

/* ── Entry point: request location ─────────── */
function requestAlertsLocation() {
    if (!navigator.geolocation) {
        showToast('Geolocation not supported. Using default.', 'warning');
        saveUserLocation(28.6139, 77.2090, 'Delhi');
        loadAlertsData(28.6139, 77.2090);
        return;
    }

    navigator.geolocation.getCurrentPosition(
        pos => {
            showToast('📍 Location detected!', 'success');
            saveUserLocation(pos.coords.latitude, pos.coords.longitude, null);
            loadAlertsData(pos.coords.latitude, pos.coords.longitude);
        },
        () => {
            showToast('Using default location (Delhi).', 'warning');
            saveUserLocation(28.6139, 77.2090, 'Delhi');
            loadAlertsData(28.6139, 77.2090);
        }, { timeout: 10000, enableHighAccuracy: true }
    );
}

/* ── On page load: reuse a location already granted this
   session (e.g. from the dashboard), otherwise pop up a
   request instead of showing a static "grant location"
   section on the page. ──────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
    const saved = getUserLocation();
    if (saved) {
        loadAlertsData(saved.lat, saved.lon);
        return;
    }
    showAppNotification({
        id: 'alertsLocationPrompt',
        icon: 'fa-location-crosshairs',
        title: 'Location Required for Alerts',
        message: 'Grant your location to get the alerts — we\'ll use it to show weather and pest warnings for your area.',
        primaryLabel: 'Grant Location',
        onPrimary: requestAlertsLocation,
        secondaryLabel: 'Use Default (Delhi)',
        onSecondary: () => {
            saveUserLocation(28.6139, 77.2090, 'Delhi');
            loadAlertsData(28.6139, 77.2090);
        },
    });
});

/* ── Load weather then fetch alerts ─────────── */
async function loadAlertsData(lat, lon) {
    // Show alerts section with loader
    const alertsSection = document.getElementById('alertsSection');
    if (alertsSection) alertsSection.style.display = '';

    try {
        // 1. Fetch weather (includes the 6-day forecast we need for
        // "Check Upcoming Risks" later, without a second network call)
        const weatherRes = await fetch(`/api/weather?lat=${lat}&lon=${lon}`);
        const weatherData = await weatherRes.json();
        currentWeather = weatherData.current;
        currentForecast = weatherData.forecast || [];

        // 2. Fetch alerts based on weather
        const alertsRes = await fetch('/api/alerts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                temp: currentWeather.temp,
                humidity: currentWeather.humidity,
                wind_speed: currentWeather.wind_speed,
                rain: currentWeather.rain || 0,
                description: currentWeather.description,
                city: currentWeather.city
            })
        });
        const alertsData = await alertsRes.json();
        allAlerts = alertsData.alerts || [];

        // Update summary bar counts
        updateSummaryCounts(allAlerts);

        // Save badge count to session
        sessionStorage.setItem('alert_count', allAlerts.length);
        updateAlertBadge(allAlerts.length);

        // Render all sections
        renderAlertsList(allAlerts);
        renderPesticideSafety(currentWeather);
        await renderHarmfulSafeCrops(currentWeather);
        renderRiskChart(currentWeather, allAlerts);

        // Show all extra sections
        ['pesticideSafetySection', 'harmfulSection', 'riskChartSection'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = '';
        });

        // Automatically fetch weekly forecast alerts in background to sync Today's weekly data with Current Alerts
        if (currentForecast && currentForecast.length > 0) {
            checkUpcomingRisks();
        }

        // Apply saved language translation immediately if non-English
        const savedLang = localStorage.getItem('agrosmart_lang') || 'en';
        if (savedLang !== 'en') {
            await loadAlertsTranslations(savedLang);
        }

    } catch (err) {
        console.error('Alerts load error:', err);
        showToast('Could not load alert data.', 'error');
        document.getElementById('alertsList').innerHTML = `
      <div style="text-align:center;padding:60px 0;color:var(--text-3)">
        <i class="fas fa-exclamation-triangle" style="font-size:2rem;margin-bottom:12px;color:var(--amber)"></i>
        <p>Could not load alerts. Please try again.</p>
        <button class="btn-secondary" style="margin-top:16px" onclick="requestAlertsLocation()">Retry</button>
      </div>`;
    }
}

/* ── Show buffering animation in weekly alerts ──── */
function showWeeklyBuffering() {
    const list = document.getElementById('alertsList');
    const isWeekly = document.getElementById('tabWeekly') && document.getElementById('tabWeekly').classList.contains('active');
    if (list && isWeekly && (!dailyAlertsData || dailyAlertsData.length === 0)) {
        list.innerHTML = `
        <div class="weekly-buffering-box">
            <div class="weekly-buffering-spinner"></div>
            <div>
                <h4 style="color:var(--text-1);font-weight:600;margin-bottom:4px">
                    <i class="fas fa-calendar-week" style="color:var(--green);margin-right:6px"></i>
                    ${_at('Loading Extended Forecast & Risk Analysis...') || 'Loading Extended Forecast & Risk Analysis...'}
                </h4>
                <p style="color:var(--text-3);font-size:0.83rem">
                    ${_at('Analyzing weather patterns, pest advisories, and crop safety for the week.') || 'Analyzing weather patterns, pest advisories, and crop safety for the week.'}
                </p>
            </div>
        </div>`;
    }
    const cropRiskGrid = document.getElementById('cropRiskGrid');
    const cropRiskSection = document.getElementById('cropRiskSection');
    if (cropRiskGrid && cropRiskSection && (!dailyAlertsData || dailyAlertsData.length === 0)) {
        cropRiskSection.style.display = '';
        cropRiskGrid.innerHTML = `
        <div class="weekly-buffering-box">
            <div class="weekly-buffering-spinner"></div>
            <div>
                <h4 style="color:var(--text-1);font-weight:600;margin-bottom:4px">
                    <i class="fas fa-seedling" style="color:#fbbf24;margin-right:6px"></i>
                    ${_at('Calculating Crop Risk Forecast...') || 'Calculating Crop Risk Forecast...'}
                </h4>
                <p style="color:var(--text-3);font-size:0.83rem">
                    ${_at('Evaluating danger percentages for recommended crops over the next 6 days.') || 'Evaluating danger percentages for recommended crops over the next 6 days.'}
                </p>
            </div>
        </div>`;
    }
}

/* ══════════════════════════════════════════════
   CHECK UPCOMING RISKS — 6-day day-wise alerts +
   crop danger % against the dashboard's recommended
   crops.
══════════════════════════════════════════════ */
async function checkUpcomingRisks() {
    if (!currentForecast || currentForecast.length === 0) {
        return;
    }

    const btn = document.getElementById('checkUpcomingBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> <span>${_at('Checking forecast...') || 'Checking forecast...'}</span>`;
    }

    if (!dailyAlertsData || dailyAlertsData.length === 0) {
        showWeeklyBuffering();
    }

    try {
        // 1. Day-wise alerts for the next 7 days (the "weekly" view is
        // intentionally capped here — the Monthly tab fetches its own
        // wider window separately in loadMonthlyAlerts(), so this cap
        // doesn't limit how many real days the Monthly calendar can show).
        const weeklyForecast = currentForecast.slice(0, 7);
        const res = await fetch('/api/alerts-forecast', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                forecast: weeklyForecast,
                today_alerts: allAlerts,
                city: currentWeather ? currentWeather.city : '',
                lat: currentWeather ? currentWeather.lat : null,
                lon: currentWeather ? currentWeather.lon : null
            })
        });
        const data = await res.json();
        dailyAlertsData = data.daily || [];

        renderDayTabs(dailyAlertsData);
        
        const isWeekly = document.getElementById('tabWeekly') && document.getElementById('tabWeekly').classList.contains('active');
        if (isWeekly) {
            selectDay(0); // Synchronize Today's alerts with Day 0 of Weekly Alerts
        }

        const dayTabsRow = document.getElementById('dayTabsRow');
        if (dayTabsRow) dayTabsRow.style.display = '';

        // 2. Crop risk vs the dashboard's recommended crops
        await loadCropRiskForecast();

        // 3. Calamity summary popup, if anything dangerous is coming
        weeklyDangerDays = (data.summary && data.summary.danger_days) || [];
        showCalamityPopupIfNeeded();
        // 4. Render Best Harvest Day and Trend Chart
        renderBestHarvestDay(dailyAlertsData);
        renderTrendChart(dailyAlertsData);

        if (document.getElementById('tabWeekly') && document.getElementById('tabWeekly').classList.contains('active')) {
            document.getElementById('dayTabsRowWrapper').style.display = 'flex';
        }

        upcomingRisksChecked = true;
        if (btn) {
            btn.innerHTML = `<i class="fas fa-check"></i> <span>${_at('Upcoming Risks Checked') || 'Upcoming Risks Checked'}</span>`;
        }
    } catch (err) {
        console.error('Check upcoming risks error:', err);
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-cloud-sun-rain"></i> <span>${_at('Check Upcoming Risks (Extended Forecast)') || 'Check Upcoming Risks (Extended Forecast)'}</span>`;
        }
    }
}

function showCalamityPopupIfNeeded() {
    if (!weeklyDangerDays || weeklyDangerDays.length === 0 || !dailyAlertsData) return;
    const isWeekly = document.getElementById('tabWeekly') && document.getElementById('tabWeekly').classList.contains('active');
    if (isWeekly && !weeklyDangerPopupShown) {
        showAppNotification({
            id: 'calamityAlertPopup',
            icon: 'fa-triangle-exclamation',
            title: 'Upcoming Weather Risk Detected',
            message: `${weeklyDangerDays.length} of the next ${dailyAlertsData.length} days show critical conditions for your crops.`,
            listItems: weeklyDangerDays.map(d => `<strong>${getDayName(d.date, false)}</strong>: ${d.titles.join(', ')}`),
            primaryLabel: 'Got it',
        });
        weeklyDangerPopupShown = true;
    }
}

/* ── Day tabs ────────────────────────────────── */
function renderDayTabs(daily) {
    const row = document.getElementById('dayTabsRow');
    if (!row) return;

    row.innerHTML = daily.map((day, i) => {
        const badgeClass = day.danger_count > 0 ? 'has-danger' : day.warning_count > 0 ? 'has-warning' : 'clear';
        const badgeText = day.danger_count > 0 ? `${day.danger_count} ⚠` : day.warning_count > 0 ? `${day.warning_count} !` : '✓';
        return `
      <button class="day-tab ${i === 0 ? 'active' : ''}" data-day-index="${i}" onclick="selectDay(${i})">
        <span class="dt-name">${getDayName(day.date, false)}</span>
        <span class="dt-icon">${getWeatherEmoji(day.icon)}</span>
        <span class="dt-badge ${badgeClass}">${badgeText}</span>
      </button>`;
    }).join('');
}

function selectDay(index) {
    if (!dailyAlertsData || !dailyAlertsData[index]) return;
    selectedDayIndex = index;

    document.querySelectorAll('.day-tab').forEach((tab, i) => {
        tab.classList.toggle('active', i === index);
    });

    allAlerts = dailyAlertsData[index].alerts || [];
    updateSummaryCounts(allAlerts);
    renderAlertsList(allAlerts);
    filterAlerts(activeFilter === 'all' ? 'all' : activeFilter);
}

/* ── Shared: dashboard's location-aware recommended crops ───
   Reused by both the Crop Risk Forecast section and the
   Harmful/Safe Crops section so they always agree with each
   other and with the dashboard, and so we only hit the AI
   recommendation endpoint once per location per session. ── */
async function getRecommendedCrops() {
    // Reuse the dashboard's crop recommendations if cached for roughly
    // this same location this session, otherwise fetch fresh.
    let crops = null;
    try {
        const cached = JSON.parse(sessionStorage.getItem('smartagro_crop_cache') || 'null');
        if (cached && cached.data && cached.data.crops &&
            Math.abs(cached.lat - currentWeather.lat) < 0.5 &&
            Math.abs(cached.lon - currentWeather.lon) < 0.5) {
            crops = cached.data.crops;
        }
    } catch (e) { /* ignore, fetch fresh below */ }

    if (!crops) {
        const cropRes = await fetch('/api/crop-recommendations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                temp: currentWeather.temp,
                humidity: currentWeather.humidity,
                rain: currentWeather.rain || 0,
                city: currentWeather.city,
                lat: currentWeather.lat,
                lon: currentWeather.lon,
            })
        });
        const cropData = await cropRes.json();
        crops = cropData.crops || [];
        try {
            sessionStorage.setItem('smartagro_crop_cache', JSON.stringify({
                lat: currentWeather.lat, lon: currentWeather.lon, data: { crops }
            }));
        } catch (e) { /* storage full or unavailable — safe to ignore */ }
    }

    return crops || [];
}

/* ── Crop risk vs recommended crops ─────────── */
async function loadCropRiskForecast() {
    try {
        const crops = await getRecommendedCrops();
        if (!crops.length) {
            renderCropRisk([]);
            return;
        }

        const riskRes = await fetch('/api/crop-risk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ crops, forecast: currentForecast.slice(0, 7) })
        });
        const riskData = await riskRes.json();
        renderCropRisk(riskData.crops || []);
    } catch (err) {
        console.error('Crop risk error:', err);
        // Never leave the "Calculating Crop Risk Forecast..." spinner
        // frozen — show an honest failure message instead.
        const grid = document.getElementById('cropRiskGrid');
        const section = document.getElementById('cropRiskSection');
        if (grid && section) {
            section.style.display = '';
            grid.innerHTML = `
            <div class="weekly-buffering-box">
                <div><i class="fas fa-triangle-exclamation" style="color:var(--amber);margin-right:6px"></i>
                ${_at('Could not load crop risk forecast right now.') || 'Could not load crop risk forecast right now.'}</div>
            </div>`;
        }
    }
}

function renderCropRisk(crops) {
    const section = document.getElementById('cropRiskSection');
    const grid = document.getElementById('cropRiskGrid');
    if (!section || !grid) return;
    section.style.display = '';

    if (!crops || crops.length === 0) {
        grid.innerHTML = `
        <div class="weekly-buffering-box">
            <div><i class="fas fa-circle-info" style="color:var(--text-3);margin-right:6px"></i>
            ${_at('No crop risk data available yet.') || 'No crop risk data available yet.'}</div>
        </div>`;
        return;
    }


    const levelColor = { High: 'var(--red)', Medium: 'var(--amber)', Low: 'var(--green)' };

    grid.innerHTML = crops.map((c, i) => `
    <div class="crop-risk-card" style="animation-delay:${i * 0.06}s">
      <div class="crk-header">
        <span class="crk-icon">${c.icon}</span>
        <span class="crk-name">${_at(c.name) || c.name}</span>
        <span class="crk-percent" style="color:${levelColor[c.risk_level]}">${c.danger_percent}%</span>
      </div>
      <span class="crk-level level-${c.risk_level.toLowerCase()}">
        <i class="fas fa-circle" style="font-size:0.4rem"></i> ${_at(c.risk_level) || c.risk_level} ${_at('Danger') || 'Danger'}
      </span>
      <div class="crk-bar">
        <div class="crk-bar-fill" style="width:${c.danger_percent}%;background:${levelColor[c.risk_level]}"></div>
      </div>
      ${c.risky_days && c.risky_days.length > 0 ? `
        <div class="crk-reasons">
          ${c.risky_days.slice(0, 3).map(rd => `
            <div class="crk-reason-day">
              <strong>${getDayName(rd.date, false)}</strong>: ${rd.reasons.join('; ')}
            </div>`).join('')}
        </div>`
      : `<div class="crk-safe-note"><i class="fas fa-check-circle"></i> ${_at('Safe to grow all 6 upcoming days') || 'Safe to grow all 6 upcoming days'}</div>`}
    </div>
  `).join('');
}

/* ── Update summary bar ─────────────────────── */
function updateSummaryCounts(alerts) {
    const danger = alerts.filter(a => a.type === 'danger').length;
    const warning = alerts.filter(a => a.type === 'warning').length;
    const info = alerts.filter(a => a.type === 'info').length;

    const setCount = (id, val) => {
        const el = document.getElementById(id);
        if (!el) return;
        let n = 0;
        const interval = setInterval(() => {
            n = Math.min(n + 1, val);
            el.textContent = n;
            if (n >= val) clearInterval(interval);
        }, 60);
    };

    setCount('dangerCount', danger);
    setCount('warningCount', warning);
    setCount('infoCount', info);
    setCount('totalCount', alerts.length);
}

/* ── Render alerts list ─────────────────────── */
function renderAlertsList(alerts) {
    const list = document.getElementById('alertsList');
    const none = document.getElementById('noAlerts');
    if (!list) return;

    if (alerts.length === 0) {
        list.innerHTML = '';
        if (none) none.style.display = 'block';
        return;
    }
    if (none) none.style.display = 'none';

    list.innerHTML = alerts.map((alert, i) => `
    <div class="alert-card ${alert.type}" 
         data-type="${alert.type}" 
         data-category="${alert.category}"
         style="animation-delay:${i * 0.07}s">
      <div class="alert-card-icon">${alert.icon}</div>
      <div class="alert-card-body">
        <div class="alert-card-top">
          <span class="alert-card-title">${_at(alert.title) || alert.title}</span>
          <span class="alert-category ${getCatClass(alert.category)}">${_at(alert.category) || alert.category}</span>
          <span class="alert-category ${getTypeClass(alert.type)}">${_at(capitalize(alert.type)) || capitalize(alert.type)}</span>
        </div>
        <div class="alert-card-msg">${_at(alert.message) || alert.message}</div>
        <div class="alert-card-action">
          <i class="fas fa-circle-right"></i>
          <span><strong>${_at('Action') || 'Action'}:</strong> ${_at(alert.action) || alert.action}</span>
        </div>
      </div>
    </div>
  `).join('');
}

function getCatClass(cat) {
    const map = {
        'Weather': 'cat-weather',
        'Disease': 'cat-disease',
        'Pest': 'cat-pest',
        'Crop Advisory': 'cat-crop',
    };
    return map[cat] || 'cat-crop';
}

function getTypeClass(type) {
    const map = { danger: 'cat-disease', warning: 'cat-pest', info: 'cat-crop' };
    return map[type] || 'cat-crop';
}

/* ── Filter alerts ──────────────────────────── */
function filterAlerts(filter) {
    activeFilter = filter;

    // Update tab styles
    document.querySelectorAll('.alert-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.textContent.trim().toLowerCase().includes(filter.toLowerCase()) ||
            (filter === 'all' && tab.textContent.trim().toLowerCase() === 'all alerts')) {
            tab.classList.add('active');
        }
    });

    const cards = document.querySelectorAll('.alert-card');
    let visibleCount = 0;

    cards.forEach(card => {
        const type = card.dataset.type;
        const category = card.dataset.category;
        let show = false;

        if (filter === 'all') show = true;
        else if (filter === 'danger' || filter === 'warning' || filter === 'info') show = type === filter;
        else show = category === filter;

        card.style.display = show ? 'flex' : 'none';
        if (show) visibleCount++;
    });

    const none = document.getElementById('noAlerts');
    if (none) none.style.display = visibleCount === 0 ? 'block' : 'none';
}


/* ── Pesticide Safety Guide ─────────────────── */
const PESTICIDE_DATA = [{
        name: 'Chlorpyriphos 20 EC',
        icon: '⚗️',
        targetPest: 'Stem borer, Aphids, Termites',
        safeDoze: '2.5 ml/L water',
        maxDose: '3 ml/L (never exceed)',
        interval: 'Every 14 days',
        waitingPeriod: '15 days before harvest',
        warning: 'Highly toxic to fish and bees. Do not spray near water bodies or during flowering.',
        ppeRequired: 'Gloves, Mask, Goggles, Full sleeve clothing'
    },
    {
        name: 'Imidacloprid 17.8 SL',
        icon: '🧪',
        targetPest: 'Whitefly, Aphids, Brown Plant Hopper',
        safeDoze: '0.3 ml/L water',
        maxDose: '0.5 ml/L (never exceed)',
        interval: 'Every 21 days max',
        waitingPeriod: '21 days before harvest',
        warning: 'Do NOT spray during bee activity (morning/evening). Highly toxic to pollinators.',
        ppeRequired: 'Gloves, Mask, Full body protection'
    },
    {
        name: 'Mancozeb 75 WP',
        icon: '🫙',
        targetPest: 'Leaf blight, Early blight, Rust, Downy mildew',
        safeDoze: '2.5 g/L water',
        maxDose: '3.5 g/L (never exceed)',
        interval: 'Every 7–10 days',
        waitingPeriod: '7 days before harvest',
        warning: 'Causes skin and eye irritation. Do not spray on edible parts 7 days before harvest.',
        ppeRequired: 'Gloves, Goggles, Dust Mask'
    },
    {
        name: 'Neem Oil 5% EC (Organic)',
        icon: '🌿',
        targetPest: 'Aphids, Whitefly, Mites, Fungal diseases',
        safeDoze: '5 ml/L water',
        maxDose: '10 ml/L (safe to exceed slightly)',
        interval: 'Every 5–7 days',
        waitingPeriod: 'No waiting period — organic',
        warning: 'Safe for humans and beneficial insects. May cause phytotoxicity in direct sunlight. Spray at dusk.',
        ppeRequired: 'Basic gloves recommended'
    },
    {
        name: 'Propiconazole 25 EC',
        icon: '⚗️',
        targetPest: 'Yellow rust, Brown rust, Sheath blight',
        safeDoze: '1 ml/L water',
        maxDose: '1.5 ml/L (never exceed)',
        interval: 'Max 2 sprays per season',
        waitingPeriod: '21 days before harvest',
        warning: 'Do not mix with alkaline pesticides. Causes groundwater contamination if overused.',
        ppeRequired: 'Full protective gear, closed shoes'
    },
    {
        name: 'Emamectin Benzoate 5 SG',
        icon: '🧫',
        targetPest: 'Fall Armyworm, Diamond back moth, Leaf miner',
        safeDoze: '0.4 g/L water',
        maxDose: '0.5 g/L (never exceed)',
        interval: 'Every 10–14 days',
        waitingPeriod: '14 days before harvest',
        warning: 'Highly toxic to aquatic organisms. Dispose empty containers safely. Do not reuse containers.',
        ppeRequired: 'Full PPE, respiratory protection'
    },
];

function renderPesticideSafety(weather) {
    const grid = document.getElementById('pesticideGrid');
    if (!grid) return;

    grid.innerHTML = PESTICIDE_DATA.map((p, i) => `
    <div class="pesticide-card" style="animation-delay:${i * 0.06}s">
      <div class="pc-header">
        <span>${p.icon}</span> ${_at(p.name) || p.name}
      </div>
      <div class="pc-body">
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-bug"></i> ${_at('Target Pest') || 'Target Pest'}</span>
          <span class="pc-item-val">${_at(p.targetPest) || p.targetPest}</span>
        </div>
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-flask"></i> ${_at('Safe Dose') || 'Safe Dose'}</span>
          <span class="pc-item-val" style="color:var(--green)">${p.safeDoze}</span>
        </div>
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-triangle-exclamation"></i> ${_at('Max Limit') || 'Max Limit'}</span>
          <span class="pc-item-val" style="color:var(--red)">${p.maxDose}</span>
        </div>
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-rotate"></i> ${_at('Interval') || 'Interval'}</span>
          <span class="pc-item-val">${_at(p.interval) || p.interval}</span>
        </div>
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-clock"></i> ${_at('Pre-Harvest') || 'Pre-Harvest'}</span>
          <span class="pc-item-val" style="color:var(--amber)">${_at(p.waitingPeriod) || p.waitingPeriod}</span>
        </div>
        <div class="pc-item">
          <span class="pc-item-label"><i class="fas fa-helmet-safety"></i> ${_at('PPE Required') || 'PPE Required'}</span>
          <span class="pc-item-val">${_at(p.ppeRequired) || p.ppeRequired}</span>
        </div>
        <div class="pc-warning">
          <i class="fas fa-circle-exclamation" style="flex-shrink:0;margin-top:1px"></i>
          <span>${_at(p.warning) || p.warning}</span>
        </div>
      </div>
    </div>
  `).join('');
}

/* ── Harmful & Safe Crops ───────────────────────────────────
   Sourced from the dashboard's location-aware AI crop picks
   (/api/crop-recommendations), checked against TODAY's real
   weather via the same rule engine used for the 6-day Crop
   Risk Forecast (/api/crop-risk) — so "safe to grow" always
   reflects this exact location's recommended crops and this
   exact location's weather, not a generic fixed crop list.
   Falls back to a generic 12-crop check only if the dashboard
   crop / weather-risk endpoints are unreachable. ──────────── */
const ALL_CROPS_DATA = [
    { name: 'Rice', icon: '🌾', minTemp: 20, maxTemp: 38, minHumidity: 70, waterNeed: 'Very High' },
    { name: 'Wheat', icon: '🌿', minTemp: 10, maxTemp: 25, minHumidity: 40, waterNeed: 'Medium' },
    { name: 'Maize', icon: '🌽', minTemp: 18, maxTemp: 35, minHumidity: 50, waterNeed: 'Medium' },
    { name: 'Cotton', icon: '☁️', minTemp: 25, maxTemp: 40, minHumidity: 40, waterNeed: 'Medium' },
    { name: 'Tomato', icon: '🍅', minTemp: 18, maxTemp: 30, minHumidity: 60, waterNeed: 'Medium' },
    { name: 'Sugarcane', icon: '🎋', minTemp: 24, maxTemp: 38, minHumidity: 75, waterNeed: 'Very High' },
    { name: 'Soybean', icon: '🫘', minTemp: 20, maxTemp: 32, minHumidity: 60, waterNeed: 'Medium' },
    { name: 'Mustard', icon: '🌻', minTemp: 10, maxTemp: 25, minHumidity: 40, waterNeed: 'Low' },
    { name: 'Potato', icon: '🥔', minTemp: 10, maxTemp: 22, minHumidity: 60, waterNeed: 'Medium' },
    { name: 'Onion', icon: '🧅', minTemp: 13, maxTemp: 28, minHumidity: 50, waterNeed: 'Medium' },
    { name: 'Chilli', icon: '🌶️', minTemp: 20, maxTemp: 35, minHumidity: 60, waterNeed: 'Medium' },
    { name: 'Groundnut', icon: '🥜', minTemp: 22, maxTemp: 36, minHumidity: 50, waterNeed: 'Medium' },
];

async function renderHarmfulSafeCrops(weather) {
    const section = document.getElementById('harmfulSection');
    const harmfulGrid = document.getElementById('harmfulGrid');
    const safeGrid = document.getElementById('safeGrid');
    if (!section || !harmfulGrid || !safeGrid) return;
    section.style.display = '';

    const temp = weather.temp;
    const humidity = weather.humidity;
    let harmful = [];
    let safe = [];
    let usedDashboardCrops = false;

    try {
        const crops = await getRecommendedCrops();
        if (crops.length) {
            // "Today" as a single day-object in the shape /api/crop-risk expects.
            // Prefer the real forecast's day-0 bucket (already has temp_max/min,
            // wind, rain) so this matches the actual location + weather exactly;
            // fall back to building it from the current snapshot.
            const todayDay = (currentForecast && currentForecast[0]) || {
                date: new Date().toISOString().slice(0, 10),
                temp_max: temp, temp_min: temp,
                humidity: humidity,
                wind_speed: weather.wind_speed || 0,
                rain: weather.rain || 0,
                description: weather.description || '',
            };

            const riskRes = await fetch('/api/crop-risk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ crops, forecast: [todayDay] })
            });
            const riskData = await riskRes.json();
            const results = riskData.crops || [];

            if (results.length) {
                usedDashboardCrops = true;
                results.forEach(c => {
                    const reasons = (c.risky_days && c.risky_days[0] && c.risky_days[0].reasons) || [];
                    if (c.danger_percent > 0) {
                        harmful.push({ name: c.name, icon: c.icon, reasons });
                    } else {
                        safe.push({ name: c.name, icon: c.icon, suitability: 100 });
                    }
                });
            }
        }
    } catch (err) {
        console.warn('[Alerts] Dashboard crop risk check failed, using fallback list:', err);
    }

    // Fallback: generic fixed crop list with basic temp/humidity thresholds,
    // only used if the dashboard-driven check above couldn't run.
    if (!usedDashboardCrops) {
        ALL_CROPS_DATA.forEach(crop => {
            const tempOk = temp >= crop.minTemp && temp <= crop.maxTemp;
            const humidityOk = humidity >= crop.minHumidity;

            if (!tempOk || !humidityOk) {
                const reasons = [];
                if (temp < crop.minTemp) reasons.push(`Too cold (min ${crop.minTemp}°C needed)`);
                if (temp > crop.maxTemp) reasons.push(`Too hot (max ${crop.maxTemp}°C tolerated)`);
                if (humidity < crop.minHumidity) reasons.push(`Humidity too low (min ${crop.minHumidity}% needed)`);
                harmful.push({ ...crop, reasons });
            } else {
                safe.push({ ...crop, suitability: Math.round(((tempOk ? 50 : 0) + (humidityOk ? 50 : 0))) });
            }
        });
    }

    harmfulGrid.innerHTML = harmful.length > 0 ?
        harmful.map(c => `
        <div class="harmful-card">
          <div class="hsc-name">
            <span style="font-size:1.5rem">${c.icon}</span> ${_at(c.name) || c.name}
            <span style="margin-left:auto;font-size:0.7rem;padding:2px 8px;background:rgba(248,113,113,0.1);color:var(--red);border-radius:50px;border:1px solid rgba(248,113,113,0.2)">⚠ ${_at('Risky') || 'Risky'}</span>
          </div>
          <div class="hsc-reason">
            ${c.reasons.map(r => `<div><i class="fas fa-xmark" style="color:var(--red);margin-right:4px"></i>${_at(r) || r}</div>`).join('')}
          </div>
        </div>`)
      .join('')
    : `<p style="color:var(--text-3);font-size:0.875rem">${_at('No harmful crops identified for current conditions.') || 'No harmful crops identified for current conditions.'}</p>`;

  safeGrid.innerHTML = safe.length > 0
    ? safe.map(c => `
        <div class="safe-card">
          <div class="hsc-name">
            <span style="font-size:1.5rem">${c.icon}</span> ${_at(c.name) || c.name}
            <span style="margin-left:auto;font-size:0.7rem;padding:2px 8px;background:rgba(74,222,128,0.1);color:var(--green);border-radius:50px;border:1px solid rgba(74,222,128,0.2)">✓ ${_at('Safe') || 'Safe'}</span>
          </div>
          <div class="hsc-reason" style="margin-top:6px">
            <div style="display:flex;align-items:center;gap:6px">
              <i class="fas fa-check-circle" style="color:var(--green)"></i>
              <span style="font-size:0.78rem;color:var(--text-2)">${_at('Suitable for') || 'Suitable for'} ${temp}°C, ${humidity}% ${_at('humidity') || 'humidity'}</span>
            </div>
            <div style="margin-top:6px;height:4px;background:var(--bg-2);border-radius:2px;overflow:hidden">
              <div style="height:100%;width:${c.suitability}%;background:linear-gradient(90deg,var(--green-dark),var(--green));border-radius:2px;transition:width 1s ease"></div>
            </div>
          </div>
        </div>`)
      .join('')
    : `<p style="color:var(--text-3);font-size:0.875rem">${_at('No fully safe crops identified — check crop calendar.') || 'No fully safe crops identified — check crop calendar.'}</p>`;
}

/* ── Risk Chart ─────────────────────────────── */
function renderRiskChart(weather, alerts) {
  const section = document.getElementById('riskChartSection');
  const canvas  = document.getElementById('riskChart');
  if (!section || !canvas) return;
  section.style.display = '';

  // Calculate risk scores
  const heatRisk    = Math.min(100, Math.max(0, ((weather.temp - 20) / 25) * 100));
  const humidRisk   = Math.min(100, Math.max(0, ((weather.humidity - 40) / 60) * 100));
  const windRisk    = Math.min(100, Math.max(0, (weather.wind_speed / 30) * 100));
  const pestRisk    = alerts.filter(a => a.category === 'Pest').length * 20;
  const diseaseRisk = alerts.filter(a => a.category === 'Disease').length * 25;
  const overallRisk = Math.round((heatRisk + humidRisk + windRisk + pestRisk + diseaseRisk) / 5);

  // Radar chart
  if (riskChartInst) riskChartInst.destroy();

  riskChartInst = new Chart(canvas, {
    type: 'radar',
    data: {
      labels: [_at('Heat Stress')||'Heat Stress', _at('Humidity Risk')||'Humidity Risk', _at('Wind Damage')||'Wind Damage', _at('Pest Risk')||'Pest Risk', _at('Disease Risk')||'Disease Risk'],
      datasets: [{
        label: _at('Current Risk Level (%)')||'Current Risk Level (%)',
        data: [
          Math.round(heatRisk),
          Math.round(humidRisk),
          Math.round(windRisk),
          Math.min(100, pestRisk),
          Math.min(100, diseaseRisk)
        ],
        backgroundColor: 'rgba(248,113,113,0.12)',
        borderColor: 'rgba(248,113,113,0.7)',
        borderWidth: 2,
        pointBackgroundColor: '#f87171',
        pointBorderColor: '#fff',
        pointBorderWidth: 2,
        pointRadius: 5,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        r: {
          min: 0, max: 100,
          beginAtZero: true,
          ticks: {
            color: 'rgba(107,140,108,0.8)',
            backdropColor: 'transparent',
            stepSize: 25,
            font: { size: 10 }
          },
          grid:         { color: 'rgba(74,222,128,0.08)' },
          angleLines:   { color: 'rgba(74,222,128,0.1)' },
          pointLabels:  { color: '#a7c4a8', font: { size: 12, weight: '600' } }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0e1510',
          borderColor: 'rgba(74,222,128,0.25)',
          borderWidth: 1,
          titleColor: '#e8f5e9',
          bodyColor: '#a7c4a8',
          callbacks: {
            label: ctx => ` ${ctx.raw}% risk`
          }
        }
      }
    }
  });

  // Risk factors list
  const factors = document.getElementById('riskFactors');
  if (!factors) return;

  const riskItems = [
    { label: _at('Heat Stress')||'Heat Stress',     value: Math.round(heatRisk),       color: '#f87171' },
    { label: _at('Humidity Risk')||'Humidity Risk', value: Math.round(humidRisk),      color: '#38bdf8' },
    { label: _at('Wind Damage')||'Wind Damage',     value: Math.round(windRisk),       color: '#94a3b8' },
    { label: _at('Pest Activity')||'Pest Activity', value: Math.min(100, pestRisk),    color: '#fbbf24' },
    { label: _at('Disease Risk')||'Disease Risk',   value: Math.min(100, diseaseRisk), color: '#f87171' },
    { label: _at('Overall Risk')||'Overall Risk',   value: overallRisk,                color: overallRisk > 60 ? '#f87171' : overallRisk > 35 ? '#fbbf24' : '#4ade80' },
  ];

  factors.innerHTML = riskItems.map(item => `
    <div class="risk-factor-item">
      <div class="rfi-label">
        <span>${item.label}</span>
        <span style="color:${item.color};font-weight:700">${item.value}%</span>
      </div>
      <div class="rfi-bar">
        <div class="rfi-fill" style="width:0%;background:${item.color}" 
             data-target="${item.value}"></div>
      </div>
    </div>
  `).join('');

  // Animate bars
  setTimeout(() => {
    document.querySelectorAll('.rfi-fill').forEach(bar => {
      bar.style.width = bar.dataset.target + '%';
    });
  }, 200);
}

/* ── Tab click handlers ─────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  // Set first tab as active
  const firstTab = document.querySelector('.alert-tab');
  if (firstTab) firstTab.classList.add('active');
});

/* ── New Features Logic ─────────────────────── */

let trendChartInst = null;

function renderBestHarvestDay(dailyData) {
    const banner = document.getElementById('bestHarvestBanner');
    const title = document.getElementById('bhTitle');
    const desc = document.getElementById('bhDesc');
    if (!banner || !dailyData.length) return;

    let bestDay = null;
    let minRisk = Infinity;

    dailyData.forEach((day, index) => {
        const riskScore = (day.danger_count * 2) + day.warning_count;
        if (riskScore < minRisk) {
            minRisk = riskScore;
            bestDay = day;
        }
    });

    if (bestDay && minRisk === 0) {
        title.textContent = `Best Harvest Day: ${getDayName(bestDay.date, true)}`;
        desc.textContent = "Perfect window. No warnings or critical weather risks detected.";
        banner.style.display = 'flex';
        banner.style.background = 'linear-gradient(135deg, rgba(74, 222, 128, 0.15), rgba(34, 197, 94, 0.05))';
        banner.style.borderColor = 'var(--green)';
        banner.querySelector('i').className = 'fas fa-check-double';
        banner.querySelector('i').style.color = 'var(--green)';
    } else if (bestDay) {
        title.textContent = `Safest Window: ${getDayName(bestDay.date, true)}`;
        desc.textContent = "Lowest risk day this week, though some minor advisories exist.";
        banner.style.display = 'flex';
        banner.style.background = 'linear-gradient(135deg, rgba(251, 191, 36, 0.15), rgba(245, 158, 11, 0.05))';
        banner.style.borderColor = 'var(--amber)';
        banner.querySelector('i').className = 'fas fa-shield-halved';
        banner.querySelector('i').style.color = 'var(--amber)';
    } else {
        banner.style.display = 'none';
    }
}

function renderTrendChart(dailyData) {
    const canvas = document.getElementById('trendChart');
    if (!canvas || !dailyData.length) return;

    const labels = dailyData.map(d => getDayName(d.date, false));
    const dataPts = dailyData.map(d => d.danger_count * 10 + d.warning_count * 5 + d.info_count * 2);

    if (trendChartInst) trendChartInst.destroy();

    trendChartInst = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Risk Trend',
                data: dataPts,
                borderColor: '#4ade80',
                backgroundColor: 'rgba(74,222,128,0.1)',
                borderWidth: 2,
                pointBackgroundColor: '#22c55e',
                pointRadius: 4,
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { display: false, min: 0 },
                x: { ticks: { color: '#94a3a0', font: { size: 10 } }, grid: { display: false } }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: '#0e1510',
                    titleColor: '#4ade80',
                    bodyColor: '#e8f5e9',
                    displayColors: false,
                    callbacks: { label: () => 'Risk Level' }
                }
            }
        }
    });
}

function shareToWhatsApp() {
    if (!dailyAlertsData || !dailyAlertsData[selectedDayIndex]) {
        showToast("No alerts to share.", "warning");
        return;
    }
    const day = dailyAlertsData[selectedDayIndex];
    const today = new Date().toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
    let msg = `*SmartAgro Alerts*\nShared on: ${today}\n\n*Forecast for: ${getDayName(day.date, true)}*\n\n`;
    
    if (day.alerts.length === 0) {
        msg += "✅ No active alerts. Safe to grow!\n";
    } else {
        day.alerts.forEach(a => {
            msg += `${a.icon} *${a.title}*\n${a.message}\nAction: _${a.action}_\n\n`;
        });
    }
    
    msg += `\nCheck SmartAgro app for more details and download:\nhttps://alphacoder7206-smartagro.hf.space/`;
    const url = `https://api.whatsapp.com/send?text=${encodeURIComponent(msg)}`;
    window.open(url, '_blank');
}

function switchPeriodTab(period) {
    // Update tab styling
    document.querySelectorAll('.period-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab' + period.charAt(0).toUpperCase() + period.slice(1)).classList.add('active');

    // Hide all views
    document.getElementById('weeklyAlertsView').style.display = 'none';
    document.getElementById('monthlyAlertsView').style.display = 'none';
    document.getElementById('seasonalAlertsView').style.display = 'none';

    // Show active view
    if (period === 'normal') {
        document.getElementById('weeklyAlertsView').style.display = 'block';
        if (document.getElementById('dayTabsRowWrapper')) {
            document.getElementById('dayTabsRowWrapper').style.display = 'none';
        }
        if (document.getElementById('waShareBtnNormal')) {
            document.getElementById('waShareBtnNormal').style.display = 'inline-flex';
        }
        if (typeof allAlerts !== 'undefined' && allAlerts && allAlerts.length > 0) {
            renderAlertsList(allAlerts);
        }
    } else if (period === 'weekly') {
        document.getElementById('weeklyAlertsView').style.display = 'block';
        if (document.getElementById('dayTabsRowWrapper')) {
            document.getElementById('dayTabsRowWrapper').style.display = 'flex';
        }
        if (document.getElementById('waShareBtnNormal')) {
            document.getElementById('waShareBtnNormal').style.display = 'none';
        }
        if (!dailyAlertsData || dailyAlertsData.length === 0) {
            showWeeklyBuffering();
            if (typeof currentForecast !== 'undefined' && currentForecast && currentForecast.length > 0) {
                checkUpcomingRisks();
            }
        } else {
            selectDay(selectedDayIndex || 0);
            showCalamityPopupIfNeeded();
        }
    } else if (period === 'monthly') {
        document.getElementById('monthlyAlertsView').style.display = 'block';
        loadMonthlyAlerts();
    } else if (period === 'seasonal') {
        document.getElementById('seasonalAlertsView').style.display = 'block';
        loadSeasonalAlerts();
    }
}

let monthlyLoaded = false;
async function loadMonthlyAlerts() {
    if (monthlyLoaded) return;
    if (!currentForecast || currentForecast.length === 0) return;

    try {
        // The weekly view above is capped to 7 days on purpose, but the
        // Monthly calendar should get real data for as many days as the
        // forecast genuinely covers (up to ~16 real days). If the real
        // forecast is longer than what the weekly fetch already covered,
        // fetch alerts for the full window here instead of reusing the
        // 7-day-capped dailyAlertsData.
        let monthlyDailyAlerts = dailyAlertsData || [];
        if (currentForecast.length > (dailyAlertsData ? dailyAlertsData.length : 0)) {
            try {
                const extRes = await fetch('/api/alerts-forecast', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        forecast: currentForecast,
                        today_alerts: allAlerts,
                        city: currentWeather ? currentWeather.city : '',
                        lat: currentWeather ? currentWeather.lat : null,
                        lon: currentWeather ? currentWeather.lon : null
                    })
                });
                const extData = await extRes.json();
                if (extData.daily && extData.daily.length) {
                    monthlyDailyAlerts = extData.daily;
                }
            } catch (extErr) {
                console.error('Monthly extended forecast error:', extErr);
                // Fall back to whatever the weekly fetch already gave us
                // rather than failing the whole calendar.
            }
        }

        const res = await fetch('/api/monthly-alerts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                forecast: currentForecast,
                daily_alerts: monthlyDailyAlerts
            })
        });
        const data = await res.json();
        const monthly = data.monthly || [];

        // Only render days with real, fetched data. Days beyond real
        // forecast coverage used to render as a separate "No data" /
        // "unavailable" placeholder card — that's honest (never fakes a
        // risk %) but clutters the calendar with empty-looking tiles for
        // the back half of the month. Simplest fix: just don't render
        // those tiles at all, so the calendar only ever shows real days.
        const availableDays = monthly.filter(d => d.data_available !== false && d.risk !== 'unavailable');

        const cal = document.getElementById('monthlyCalendar');

        if (availableDays.length === 0) {
            cal.innerHTML = `<div class="cal-empty-state" style="grid-column:1/-1;text-align:center;padding:24px;color:var(--text-3)">
                <i class="fas fa-cloud-question" style="font-size:1.6rem;display:block;margin-bottom:8px"></i>
                No forecast data available yet.
            </div>`;
            monthlyLoaded = true;
            return;
        }

        cal.innerHTML = availableDays.map(d => {
            const dateObj = new Date(d.date);
            const dayNum = dateObj.getDate();
            const dayName = dateObj.toLocaleDateString('en-US', { weekday: 'short' });

            let bgClass = 'cal-safe';
            let icon = '<i class="fas fa-check-circle" style="color:var(--green)"></i><span>Safe</span>';
            if (d.risk === 'danger') { bgClass = 'cal-danger'; icon = '<i class="fas fa-triangle-exclamation" style="color:var(--red)"></i><span style="color:var(--red)">Critical</span>'; }
            else if (d.risk === 'warning') { bgClass = 'cal-warning'; icon = '<i class="fas fa-circle-exclamation" style="color:var(--amber)"></i><span style="color:var(--amber)">Warning</span>'; }

            let alertsHtml = d.alerts.length > 0 ? d.alerts.map(a => `<li>${a.title}</li>`).join('') : '<li>Clear</li>';

            return `
            <div class="cal-day ${bgClass}" title="${d.alerts.map(a => a.title).join(', ')}">
                <div class="cal-date">${dayName} <strong>${dayNum}</strong></div>
                <div class="cal-icon">${icon}</div>
                <div style="font-size: 0.72rem; font-weight: 700; text-align: center; margin-top: 6px; color: ${d.risk === 'danger' ? 'var(--red)' : d.risk === 'warning' ? 'var(--amber)' : 'var(--green)'}">Risk: ${d.risk_pct}%</div>
                <ul class="cal-alerts-text">${alertsHtml}</ul>
            </div>`;
        }).join('');
        monthlyLoaded = true;
    } catch (err) {
        console.error("Monthly alerts error:", err);
    }
}

let seasonalLoaded = false;
async function loadSeasonalAlerts() {
    if (seasonalLoaded) return;
    if (!currentWeather) return;

    try {
        const res = await fetch('/api/seasonal-alerts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ city: currentWeather.city })
        });
        const data = await res.json();
        
        document.getElementById('seasonalTitle').textContent = `${data.season} Advisory`;
        document.getElementById('seasonalSub').textContent = `Overall advisories for ${data.city} this season`;

        const grid = document.getElementById('seasonalGrid');
        grid.innerHTML = data.alerts.map(a => {
            let color = 'var(--green)';
            if (a.type === 'danger') color = 'var(--red)';
            if (a.type === 'warning') color = 'var(--amber)';

            return `
            <div class="seasonal-card" style="border-left: 4px solid ${color}">
                <div class="sc-header">
                    <span class="sc-icon">${a.icon}</span>
                    <h3>${a.title}</h3>
                </div>
                <p class="sc-body">${a.message}</p>
            </div>`;
        }).join('');
        seasonalLoaded = true;
    } catch (err) {
        console.error("Seasonal alerts error:", err);
    }
}
