/* ── State ──────────────────────────────────── */
let allMarketData = {};
let currentSearchedMarkets = null;
let allLocations = [];
let marketChart = null;
let activeChartType = 'line';
let activeFilter = 'all';
let currentChartCity = 'Delhi';

let _translations = {};
const _translationCache = {};
let _translationInProgress = false;

function setTranslations(data) {
    _translations = data || {};
}

function _t(key) {
    if (!key) return '';
    return _translations[key] || key;
}

/** Translate a crop name */
function tCrop(name) {
    return _translations[name] || name;
}

/** Translate a demand label */
function tDemand(demand) {
    return _translations[demand] || demand;
}

/* ══════════════════════════════════════════════
   LANGUAGE DISPLAY NAMES (for overlay label)
══════════════════════════════════════════════ */
const MARKET_LANG_DISPLAY_NAMES = {
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
    sat: 'ᱥᱟᱱᱛᱟᱴ',
    ks: 'کڲشُر',
    ne: 'नेपाली',
    sd: 'سنڈی',
    kok: 'कोंकणी',
    mni: 'মৈতৈলোন্',
    bodo: 'बड़ो',
    doi: 'डोगरी',
    sa: 'संस्कृत',
    en: 'English',
};

/* ══════════════════════════════════════════════
   TRANSLATE OVERLAY
══════════════════════════════════════════════ */
function ensureMarketTranslateOverlayStyles() {
    if (document.getElementById('marketTranslateOverlayStyle')) return;
    const style = document.createElement('style');
    style.id = 'marketTranslateOverlayStyle';
    style.textContent = `
    .market-translate-overlay {
        position: fixed; inset: 0; z-index: 9999;
        display: flex; align-items: center; justify-content: center;
        background: rgba(10, 16, 12, 0.55);
        backdrop-filter: blur(3px);
        opacity: 0; pointer-events: none;
        transition: opacity 0.2s ease;
    }
    .market-translate-overlay.visible { opacity: 1; pointer-events: all; }
    .market-translate-box {
        background: var(--bg-1, #102013);
        border: 1px solid var(--green, #4ade80);
        border-radius: 16px;
        padding: 28px 32px;
        max-width: 320px;
        text-align: center;
        box-shadow: 0 10px 40px rgba(0,0,0,0.35);
        animation: marketTransPopIn 0.25s ease;
    }
    @keyframes marketTransPopIn {
        from { transform: scale(0.92); opacity: 0; }
        to   { transform: scale(1);    opacity: 1; }
    }
    .market-translate-spinner {
        width: 38px; height: 38px; margin: 0 auto 14px;
        border: 3px solid rgba(74, 222, 128, 0.25);
        border-top-color: var(--green, #4ade80);
        border-radius: 50%;
        animation: marketTransSpin 0.8s linear infinite;
    }
    @keyframes marketTransSpin { to { transform: rotate(360deg); } }
    .market-translate-title {
        color: var(--text-1, #f1f5f1);
        font-weight: 600; font-size: 0.95rem; margin-bottom: 6px;
    }
    .market-translate-sub {
        color: var(--text-3, #94a3a0);
        font-size: 0.78rem; line-height: 1.4;
    }
    .market-translate-dots span {
        display: inline-block; opacity: 0.3;
        animation: marketTransDot 1.2s infinite;
    }
    .market-translate-dots span:nth-child(2) { animation-delay: 0.2s; }
    .market-translate-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes marketTransDot { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
    `;
    document.head.appendChild(style);
}

function showMarketTranslateOverlay(langCode) {
    if (document.getElementById('txTimerOverlay')?.classList.contains('visible')) return;
    ensureMarketTranslateOverlayStyles();
    let overlay = document.getElementById('marketTranslateOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'marketTranslateOverlay';
        overlay.className = 'market-translate-overlay';
        document.body.appendChild(overlay);
    }
    const name = MARKET_LANG_DISPLAY_NAMES[langCode] || langCode.toUpperCase();
    overlay.innerHTML = `
      <div class="market-translate-box" style="position:relative">
        <button onclick="document.getElementById('marketTranslateOverlay').classList.remove('visible')" style="position:absolute;top:8px;right:10px;background:rgba(255,255,255,.1);border:none;color:#fff;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:1.1rem;display:flex;align-items:center;justify-content:center;transition:background .2s" onmouseover="this.style.background='rgba(248,113,113,.4)'" onmouseout="this.style.background='rgba(255,255,255,.1)'" title="Cancel">&times;</button>
        <div class="market-translate-spinner"></div>
        <div class="market-translate-title">Translating to ${name}<span class="market-translate-dots"><span>.</span><span>.</span><span>.</span></span></div>
        <div class="market-translate-sub">First-time translation can take a few seconds. It'll be instant after this.</div>
      </div>`;
    requestAnimationFrame(() => overlay.classList.add('visible'));
}

function hideMarketTranslateOverlay() {
    const overlay = document.getElementById('marketTranslateOverlay');
    if (overlay) overlay.classList.remove('visible');
}

async function loadTranslations(lang) {
    if (!lang || lang === 'en') {
        setTranslations({});
        reRenderMarket();
        return;
    }
    if (_translationInProgress) return;
    _translationInProgress = true;

    if (_translationCache[lang]) {
        setTranslations(_translationCache[lang]);
        reRenderMarket();
        _translationInProgress = false;
        return;
    }

    const isGlobal = document.getElementById('txTimerOverlay')?.classList.contains('visible');
    if (!isGlobal) showMarketTranslateOverlay(lang);

    try {
        const res = await fetch('/api/translate-market', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const tx = data.translations || {};
        _translationCache[lang] = tx; // cache for instant future switches
        setTranslations(tx);
        console.log(`[Market] Translations loaded for ${data.lang_name || lang}: ${Object.keys(_translations).length} terms`);
    } catch (err) {
        console.warn('[Market] Translation load failed, using English:', err);
        setTranslations({});
    }

    // Re-render fully, THEN lift overlay so there's zero gap
    reRenderMarket();
    hideMarketTranslateOverlay();
    _translationInProgress = false;
}

/* ══════════════════════════════════════════════
   INIT
══════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
    loadAllMarkets();
    setupSearchEnterKey();
});

/* ══════════════════════════════════════════════
   DATA LOADING
══════════════════════════════════════════════ */
async function loadAllMarkets() {
    try {
        const res = await fetch('/api/market');
        const data = await res.json();

        allMarketData = data.markets || {};
        allLocations = data.locations || Object.keys(allMarketData);
        const emptyCities = data.empty_cities || [];

        if (!res.ok || data.error) {
            throw new Error(data.error || `HTTP ${res.status}`);
        }

        const liveCount = data.live_count || 0;
        console.log(`[Market] Live commodities: ${liveCount} | Cities with no live data: ${emptyCities.length}`);

        updateDataSourceBadge(liveCount, emptyCities);

        hideLoading();
        populateCityDropdown();

        const firstCityWithData = allLocations.find(c => (allMarketData[c] || []).length > 0);
        const firstCity = firstCityWithData || allLocations[0] || '';
        currentChartCity = firstCity;
        const sel = document.getElementById('chartCitySelect');
        if (sel) sel.value = firstCity;

        // Apply saved language BEFORE first render so data shows translated right away
        const savedLang = localStorage.getItem('agrosmart_lang') || 'en';
        if (savedLang !== 'en') {
            // Load translations first, then render everything in one pass
            await loadTranslations(savedLang);
        } else {
            renderMarketGrid(allMarketData);
            buildTicker(allMarketData);
            buildPriceTable(allMarketData);
            if (firstCity) buildChart(allMarketData, firstCity, 'line');
        }

    } catch (err) {
        // Live fetch genuinely failed — show that honestly instead of
        // filling the screen with numbers that aren't real.
        console.warn('[Market] Live fetch failed:', err);
        showMarketLoadError();
    }
}

function showMarketLoadError() {
    hideLoading();
    allMarketData = {};
    allLocations = [];

    const grid = document.getElementById('marketCitiesGrid');
    const none = document.getElementById('noResults');
    if (grid) grid.style.display = 'none';
    if (none) {
        none.style.display = '';
        none.innerHTML = `
            <div style="text-align:center;padding:60px 0;color:var(--text-2)">
                <i class="fas fa-triangle-exclamation" style="font-size:1.6rem;color:#f87171"></i>
                <p style="margin-top:12px">Couldn't load live market data right now. Please try again shortly.</p>
            </div>`;
    }

    const badge = document.getElementById('dataSourceBadge');
    if (badge) {
        badge.innerHTML = `<i class="fas fa-circle" style="color:#f87171;font-size:0.5rem"></i> Live data unavailable`;
        badge.style.color = '#f87171';
    }

    const subtitle = document.getElementById('marketSubtitle');
    if (subtitle) subtitle.textContent = 'Live market data unavailable';

    if (typeof showToast === 'function') {
        showToast('⚠️ Could not reach the live market feed. Please try again.', 'warning');
    }
}

function updateDataSourceBadge(liveCount, emptyCities) {
    const badge = document.getElementById('dataSourceBadge');
    if (!badge) return;
    const emptyCount = (emptyCities || []).length;
    if (liveCount > 0) {
        const suffix = emptyCount > 0 ? ` · no live data for ${emptyCount} location(s)` : '';
        badge.innerHTML = `<i class="fas fa-circle" style="color:#4ade80;font-size:0.5rem"></i> ${liveCount} live prices (Agmarknet)${suffix}`;
        badge.style.color = '#4ade80';
    } else {
        badge.innerHTML = `<i class="fas fa-circle" style="color:#f87171;font-size:0.5rem"></i> No live market data available right now`;
        badge.style.color = '#f87171';
    }
}

function hideLoading() {
    const loader = document.getElementById('marketLoading');
    const grid = document.getElementById('marketCitiesGrid');
    if (loader) loader.style.display = 'none';
    if (grid) grid.style.display = '';
}

/* ══════════════════════════════════════════════
   CITY DROPDOWN
══════════════════════════════════════════════ */
function populateCityDropdown() {
    const sel = document.getElementById('chartCitySelect');
    if (!sel) return;
    sel.innerHTML = allLocations
        .map(city => `<option value="${city}">${city}</option>`)
        .join('');
}

/* ══════════════════════════════════════════════
   RENDER MARKET CITY CARDS
══════════════════════════════════════════════ */
function renderMarketGrid(markets) {
    const grid = document.getElementById('marketCitiesGrid');
    const none = document.getElementById('noResults');
    if (!grid) return;

    const entries = Object.entries(markets);
    if (entries.length === 0) {
        grid.style.display = 'none';
        if (none) none.style.display = '';
        return;
    }
    if (none) none.style.display = 'none';
    grid.style.display = '';

    const cropLabel = _t('Crop') || 'Crop';
    const priceLabel = _t('Price') || 'Price';
    const changeLabel = _t('Change') || 'Change';
    const demandLabel = _t('Demand') || 'Demand';
    const cropsLabel = _t('crops') || 'crops';

    let hasVisible = false;

    grid.innerHTML = entries.map(([city, crops], cityIdx) => {
                if (crops.length === 0) {
                    // No live Agmarknet data for this city's state right now —
                    // say so plainly rather than hiding the city or inventing prices.
                    hasVisible = true;
                    return `
        <div class="city-card" style="animation-delay:${cityIdx * 0.05}s">
            <div class="city-card-header">
                <div class="city-name">
                    <i class="fas fa-location-dot"></i> ${city}
                </div>
                <span class="city-count">0 crops</span>
            </div>
            <div style="padding:24px 8px;text-align:center;color:var(--text-2);font-size:0.85rem">
                <i class="fas fa-circle-exclamation" style="color:#fbbf24"></i>
                No live market data available for this location right now.
            </div>
        </div>`;
                }

                let filtered = crops;
                if (activeFilter === 'Very High') {
                    filtered = crops.filter(c => c.demand === 'Very High');
                } else if (activeFilter === 'rising') {
                    filtered = crops.filter(c => c.change > 0);
                } else if (activeFilter === 'falling') {
                    filtered = crops.filter(c => c.change < 0);
                }
                if (filtered.length === 0) return '';
                hasVisible = true;

                const renderRow = (crop) => {
                    const isUp   = crop.change >= 0;
                    const pctAbs = Math.abs(crop.change).toFixed(1);
                    return `
                    <div class="crop-row">
                        <div class="cr-name" data-crop-key="${crop.crop_key || crop.crop}" title="${tCrop(crop.crop)}">${tCrop(crop.crop)}</div>
                        <div class="cr-price-wrap">
                            <div class="cr-price">₹${crop.price.toLocaleString('en-IN')}</div>
                            <div class="cr-unit" data-translate-market="quintal">${_t('quintal') || crop.unit}</div>
                        </div>
                        <div class="cr-change ${isUp ? 'up' : 'down'}">
                            <i class="fas fa-arrow-${isUp ? 'up' : 'down'}"></i>
                            ${pctAbs}%
                        </div>
                        <div class="cr-demand demand-${getDemandClass(crop.demand)}" data-demand-key="${crop.demand}">
                            ${tDemand(crop.demand)}
                        </div>
                    </div>`;
                };

                // Every crop renders immediately (no "show more" click needed);
                // the card itself stays a fixed height and .crop-rows-wrapper
                // scrolls internally when a city has more crops than fit. Keeps
                // the whole grid visually even regardless of how many crops any
                // one city has, and everything is one scroll away instead of
                // hidden behind a toggle.
                return `
        <div class="city-card" style="animation-delay:${cityIdx * 0.05}s">
            <div class="city-card-header" onclick="toggleCityCard(this)">
                <div class="city-name">
                    <i class="fas fa-location-dot"></i> ${city}
                </div>
                <div class="city-header-right">
                    <span class="city-count">${filtered.length} ${cropsLabel}</span>
                    <i class="fas fa-chevron-down city-chevron"></i>
                </div>
            </div>
            <div class="crop-rows-wrapper">
                <div class="crop-rows">
                    <div class="crop-row-header">
                        <span>${cropLabel}</span>
                        <span>${priceLabel}</span>
                        <span>${changeLabel}</span>
                        <span class="cr-demand-hdr">${demandLabel}</span>
                    </div>
                    ${filtered.map(renderRow).join('')}
                </div>
            </div>
        </div>`;
    }).join('');

    if (!hasVisible) {
        grid.style.display = 'none';
        if (none) none.style.display = '';
    }

    setTimeout(() => {
        if (typeof observeAnimations === 'function') observeAnimations();
    }, 100);
}

// Cards start expanded (scrollable) by default — clicking the header
// collapses/re-expands a card, useful on mobile where screen space is
// tight and a farmer only wants to check a couple of specific cities.
window.toggleCityCard = function(header) {
    const card = header.closest('.city-card');
    if (!card) return;
    const wrapper = card.querySelector('.crop-rows-wrapper');
    const chevron = header.querySelector('.city-chevron');
    if (!wrapper) return;

    const collapsed = card.classList.toggle('collapsed');
    if (chevron) chevron.classList.toggle('rotated', !collapsed);
    wrapper.style.display = collapsed ? 'none' : '';
};

function getDemandClass(demand) {
    const map = {
        'Very High': 'very-high',
        'High':      'high',
        'Medium':    'medium',
        'Low':       'low',
    };
    return map[demand] || 'medium';
}

/* ══════════════════════════════════════════════
   SEARCH
══════════════════════════════════════════════ */
async function searchLocation() {
    const input    = document.getElementById('locationSearch');
    const clearBtn = document.getElementById('clearSearchBtn');
    if (!input) return;

    const query = input.value.trim();
    if (!query) { clearSearch(); return; }
    if (clearBtn) clearBtn.style.display = 'flex';

    const grid     = document.getElementById('marketCitiesGrid');
    const subtitle = document.getElementById('marketSubtitle');

    if (grid) grid.innerHTML = `
        <div style="grid-column:1/-1;text-align:center;padding:60px 0;">
            <div class="loading-spinner"></div>
            <p style="color:var(--text-2);margin-top:12px;font-size:0.9rem">
                ${_t('Searching') || 'Searching'} <strong style="color:var(--green)">"${query}"</strong>…
            </p>
        </div>`;
    if (subtitle) subtitle.textContent = `${_t('Searching') || 'Searching'} "${query}"…`;

    const q       = query.toLowerCase();
    const matched = Object.fromEntries(
        Object.entries(allMarketData).filter(([city]) => city.toLowerCase().includes(q))
    );

    if (Object.keys(matched).length > 0) {
        currentSearchedMarkets = matched;
        renderMarketGrid(currentSearchedMarkets);
        buildPriceTable(currentSearchedMarkets);
        const firstCity = Object.keys(matched)[0];
        currentChartCity = firstCity;
        buildChart(currentSearchedMarkets, firstCity, activeChartType);
        if (subtitle) subtitle.textContent = `${_t('Search') || 'Results'}: "${query}"`;
        if (typeof showToast === 'function')
            showToast(`📍 ${Object.keys(matched).length} market(s) found for "${query}"`, 'success');
        return;
    }

    // City not in local cache — try API
    try {
        const res  = await fetch(`/api/market?location=${encodeURIComponent(query)}`);
        const data = await res.json();

        if (!data.markets || Object.keys(data.markets).length === 0) {
            currentSearchedMarkets = null;
            if (grid) grid.style.display = 'none';
            const none = document.getElementById('noResults');
            if (none) none.style.display = '';
        } else {
            currentSearchedMarkets = data.markets;
            renderMarketGrid(currentSearchedMarkets);
            buildPriceTable(currentSearchedMarkets);
            const firstCity = data.locations[0];
            currentChartCity = firstCity;
            buildChart(currentSearchedMarkets, firstCity, activeChartType);
            if (subtitle) subtitle.textContent = `${_t('Search') || 'Showing'}: "${query}"`;
            if (typeof showToast === 'function')
                showToast(`📍 Showing ${firstCity} market data`, 'success');
        }
    } catch {
        // API unreachable for this search — say so plainly instead of
        // inventing a fake entry for the searched city.
        currentSearchedMarkets = null;
        if (grid) grid.style.display = 'none';
        const none = document.getElementById('noResults');
        if (none) {
            none.style.display = '';
            none.innerHTML = `
                <div style="text-align:center;padding:60px 0;color:var(--text-2)">
                    <i class="fas fa-triangle-exclamation" style="font-size:1.6rem;color:#f87171"></i>
                    <p style="margin-top:12px">Couldn't reach the live market feed to search for "${query}". Please try again.</p>
                </div>`;
        }
        if (subtitle) subtitle.textContent = `Search failed for "${query}"`;
        if (typeof showToast === 'function')
            showToast(`⚠️ Couldn't reach the live market feed. Please try again.`, 'warning');
    }
}

function clearSearch() {
    const input    = document.getElementById('locationSearch');
    const clearBtn = document.getElementById('clearSearchBtn');
    const subtitle = document.getElementById('marketSubtitle');
    const none     = document.getElementById('noResults');
    const grid     = document.getElementById('marketCitiesGrid');

    currentSearchedMarkets = null;
    if (input)    input.value            = '';
    if (clearBtn) clearBtn.style.display = 'none';
    if (subtitle) subtitle.textContent   = _t('Showing all major Indian markets') || 'Showing all major Indian markets';
    if (none)     none.style.display     = 'none';

    if (grid) grid.innerHTML = `
        <div class="skeleton skeleton-market"></div>
        <div class="skeleton skeleton-market"></div>
        <div class="skeleton skeleton-market"></div>
        <div class="skeleton skeleton-market"></div>`;

    renderMarketGrid(allMarketData);
    buildPriceTable(allMarketData);
    buildChart(allMarketData, currentChartCity, activeChartType);
}

function setupSearchEnterKey() {
    const input = document.getElementById('locationSearch');
    if (!input) return;
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') searchLocation();
    });
    input.addEventListener('input', () => {
        const clearBtn = document.getElementById('clearSearchBtn');
        if (clearBtn) clearBtn.style.display = input.value ? 'flex' : 'none';
    });
}

/* ══════════════════════════════════════════════
   FILTER CHIPS
══════════════════════════════════════════════ */
function filterDemand(type, el) {
    activeFilter = type;
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    if (el) el.classList.add('active');
    const targetMarkets = currentSearchedMarkets || allMarketData;
    renderMarketGrid(targetMarkets);
}

/* ══════════════════════════════════════════════
   LIVE PRICE TICKER
══════════════════════════════════════════════ */
function buildTicker(markets) {
    const content = document.getElementById('tickerContent');
    if (!content) return;

    const cityEntries = Object.entries(markets);
    if (cityEntries.length === 0) return;

    // Pick top crops from each city in round-robin order so the ticker showcases all Indian cities continuously
    const items = [];
    const maxCropsPerCity = Math.max(...cityEntries.map(([, crops]) => crops.length));

    for (let cIdx = 0; cIdx < Math.min(6, maxCropsPerCity); cIdx++) {
        cityEntries.forEach(([city, crops]) => {
            if (crops[cIdx]) {
                const crop = crops[cIdx];
                const isUp  = crop.change >= 0;
                const sign  = isUp ? '▲' : '▼';
                const color = isUp ? '#4ade80' : '#f87171';
                items.push(
                    `<span style="margin:0 20px;display:inline-flex;align-items:center;gap:6px">
                        <strong style="color:#e8f5e9">${tCrop(crop.crop)}</strong>
                        <span style="color:var(--text-3)">(${city})</span>
                        <strong style="color:var(--amber)"> ₹${crop.price.toLocaleString('en-IN')}</strong>
                        <span style="color:${color};font-size:0.7rem"> ${sign}${Math.abs(crop.change).toFixed(1)}%</span>
                        <span class="cr-demand demand-${getDemandClass(crop.demand)}" style="font-size:0.65rem;padding:1px 6px;border-radius:50px">${tDemand(crop.demand)}</span>
                    </span>`
                );
            }
        });
    }

    const block = items.join(' <span style="color:var(--border-2);margin:0 6px">•</span> ');
    content.innerHTML = `<span class="ticker-inner">${block}</span><span class="ticker-inner">${block}</span>`;
}

/* ══════════════════════════════════════════════
   CHART CONTROLS
══════════════════════════════════════════════ */
function switchChart(type) {
    activeChartType = type;
    document.querySelectorAll('.chart-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.getAttribute('onclick') && tab.getAttribute('onclick').includes(`'${type}'`)) {
            tab.classList.add('active');
        }
    });
    buildChart(allMarketData, currentChartCity, type);
}

function updateChart() {
    const sel = document.getElementById('chartCitySelect');
    if (sel) currentChartCity = sel.value;
    buildChart(allMarketData, currentChartCity, activeChartType);
}

function buildChart(markets, city, type) {
    const canvas = document.getElementById('marketChart');
    if (!canvas) return;

    const cityData = markets[city] || Object.values(markets)[0] || [];
    if (marketChart) { marketChart.destroy(); marketChart = null; }

    if (type === 'line')       buildLineChart(canvas, cityData, city);
    else if (type === 'bar')   buildBarChart(canvas, cityData, city);
    else if (type === 'radar') buildRadarChart(canvas, cityData, city);
}

function interpolateHistory(history, targetLen) {
    if (!history || history.length === 0) return new Array(targetLen).fill(0);
    if (history.length >= targetLen) return history.slice(0, targetLen);

    const result = [];
    const n      = history.length;

    for (let i = 0; i < targetLen; i++) {
        const t    = (i / (targetLen - 1)) * (n - 1);
        const lo   = Math.floor(t);
        const hi   = Math.min(lo + 1, n - 1);
        const frac = t - lo;
        result.push(Math.round(history[lo] * (1 - frac) + history[hi] * frac));
    }
    return result;
}

/* ──────────────────────────────────────────────
   LINE CHART
────────────────────────────────────────────── */
function buildLineChart(canvas, cityData, city) {
    const innerWrap = document.getElementById('chartInnerWrap');
    if (innerWrap) innerWrap.style.width = '100%';

    const scopeSelect = document.getElementById('chartCropScope');
    if (scopeSelect) scopeSelect.style.display = 'inline-block';

    const scope = scopeSelect ? scopeSelect.value : 'top';

    const labels = [];
    for (let i = 29; i >= 0; i--) {
        const d = new Date();
        d.setDate(d.getDate() - i);
        labels.push(d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }));
    }

    const palette = [
        '#4ade80','#fbbf24','#2dd4bf','#a78bfa',
        '#f87171','#38bdf8','#fb923c','#e879f9',
        '#84cc16','#f43f5e','#06b6d4','#8b5cf6',
        '#ec4899','#10b981','#f59e0b','#3b82f6',
        '#ef4444','#22c55e','#d946ef','#0ea5e9',
        '#f97316','#14b8a6','#8b5cf6','#eab308',
        '#6366f1','#db2777',
    ];

    const demandScore = { 'Very High': 4, 'High': 3, 'Medium': 2, 'Low': 1 };
    const sorted = [...cityData].sort((a, b) =>
        (demandScore[b.demand] || 0) - (demandScore[a.demand] || 0) ||
        Math.abs(b.change) - Math.abs(a.change)
    );

    // If scope === 'all', show all commodities; otherwise show top 7 major crops
    const targetCrops = scope === 'all' ? sorted : sorted.slice(0, 7);

    const datasets = targetCrops.map((crop, idx) => {
        const history30 = interpolateHistory(crop.history || [], 30);
        const color     = palette[idx % palette.length];
        return {
            label:                     tCrop(crop.crop),
            data:                      history30,
            borderColor:               color,
            backgroundColor:           color + '15',
            borderWidth:               idx === 0 ? 3 : 2,
            tension:                   0.35,
            fill:                      idx === 0,
            pointRadius:               targetCrops.length > 20 ? 0 : 2,
            pointHoverRadius:          6,
            pointHoverBackgroundColor: color,
        };
    });

    marketChart = new Chart(canvas, {
        type: 'line',
        data: { labels, datasets },
        options: {
            ...getBaseChartOptions(
                `${city} — ${_t('30-Day Price Trend')} (${targetCrops.length} ${_t('crops') || 'crops'}, ₹/${_t('quintal') || 'quintal'})`
            ),
            scales: {
                x: {
                    grid:   { color: 'rgba(74,222,128,0.05)', drawBorder: false },
                    ticks:  { color: '#6b8c6c', font: { size: 10 }, maxTicksLimit: 8 },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
                y: {
                    grid:   { color: 'rgba(74,222,128,0.06)', drawBorder: false },
                    ticks:  {
                        color:    '#6b8c6c',
                        callback: v => '₹' + v.toLocaleString('en-IN'),
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
            },
        },
    });
}

/* ──────────────────────────────────────────────
   BAR CHART
────────────────────────────────────────────── */
function buildBarChart(canvas, cityData, city) {
    const scopeSelect = document.getElementById('chartCropScope');
    if (scopeSelect) scopeSelect.style.display = 'none';

    const displayData = cityData; // Show ALL crops in the market!
    const innerWrap   = document.getElementById('chartInnerWrap');

    // Set dynamic width for scrollbar if > 15 crops (approx 48px per bar)
    const requiredWidth = Math.max(100, displayData.length * 48);
    if (innerWrap) {
        innerWrap.style.width = displayData.length > 15 ? `${requiredWidth}px` : '100%';
    }

    const colors = displayData.map(c =>
        c.change >= 3  ? 'rgba(74,222,128,0.90)'  :
        c.change >= 1  ? 'rgba(74,222,128,0.55)'  :
        c.change > 0   ? 'rgba(74,222,128,0.35)'  :
        c.change > -1  ? 'rgba(251,191,36,0.70)'  :
        c.change > -3  ? 'rgba(251,191,36,0.50)'  :
                         'rgba(248,113,113,0.80)'
    );
    const borderColors = colors.map(c => c.replace(/[\d.]+\)$/, '1)'));

    marketChart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels:   displayData.map(c => tCrop(c.crop)),
            datasets: [{
                label:           `${_t('Price') || 'Price'} (₹/${_t('quintal') || 'quintal'})`,
                data:            displayData.map(c => c.price),
                backgroundColor: colors,
                borderColor:     borderColors,
                borderWidth:     1,
                borderRadius:    6,
                borderSkipped:   false,
            }],
        },
        options: {
            ...getBaseChartOptions(
                `${city} — ${_t('Price Comparison')} (${displayData.length} ${_t('crops') || 'crops'}, ₹/${_t('quintal') || 'quintal'})`
            ),
            scales: {
                x: {
                    grid:   { color: 'rgba(74,222,128,0.05)' },
                    ticks:  {
                        color:       '#a7c4a8',
                        font:        { size: 9 },
                        maxRotation: 55,
                        minRotation: 30,
                        autoSkip:    false, // Display ALL crop labels!
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
                y: {
                    grid:   { color: 'rgba(74,222,128,0.06)' },
                    ticks:  {
                        color:    '#6b8c6c',
                        callback: v => '₹' + v.toLocaleString('en-IN'),
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
            },
            plugins: {
                ...getBaseChartOptions('').plugins,
                tooltip: {
                    backgroundColor: '#0e1510',
                    borderColor:     'rgba(74,222,128,0.25)',
                    borderWidth:     1,
                    titleColor:      '#e8f5e9',
                    bodyColor:       '#a7c4a8',
                    padding:         10,
                    callbacks: {
                        label: ctx => {
                            const crop = displayData[ctx.dataIndex];
                            const sign = crop.change >= 0 ? '▲' : '▼';
                            return [
                                ` ₹${ctx.raw.toLocaleString('en-IN')}/${_t('quintal') || 'quintal'}`,
                                ` ${sign} ${Math.abs(crop.change).toFixed(1)}%  |  ${tDemand(crop.demand)} ${_t('Demand') || 'demand'}`,
                            ];
                        },
                    },
                },
            },
        },
    });
}

/* ──────────────────────────────────────────────
   RADAR CHART
────────────────────────────────────────────── */
function buildRadarChart(canvas, cityData, city) {
    const innerWrap = document.getElementById('chartInnerWrap');
    if (innerWrap) innerWrap.style.width = '100%';

    const scopeSelect = document.getElementById('chartCropScope');
    if (scopeSelect) scopeSelect.style.display = 'none';

    const demandScore = { 'Very High': 100, 'High': 75, 'Medium': 50, 'Low': 25 };
    const display     = cityData.slice(0, 12);

    marketChart = new Chart(canvas, {
        type: 'radar',
        data: {
            labels: display.map(c => tCrop(c.crop)),
            datasets: [
                {
                    label:                _t('Demand Intensity') || 'Demand Intensity',
                    data:                 display.map(c => demandScore[c.demand] || 50),
                    backgroundColor:      'rgba(251,191,36,0.12)',
                    borderColor:          'rgba(251,191,36,0.75)',
                    borderWidth:          2,
                    pointBackgroundColor: '#fbbf24',
                    pointBorderColor:     '#fff',
                    pointBorderWidth:     2,
                    pointRadius:          5,
                },
                {
                    label:                _t('Price Momentum') || 'Price Momentum',
                    data:                 display.map(c => Math.min(100, Math.max(0, (c.change + 10) * 5))),
                    backgroundColor:      'rgba(74,222,128,0.10)',
                    borderColor:          'rgba(74,222,128,0.65)',
                    borderWidth:          2,
                    pointBackgroundColor: '#4ade80',
                    pointBorderColor:     '#fff',
                    pointBorderWidth:     2,
                    pointRadius:          4,
                },
            ],
        },
        options: {
            responsive:          true,
            maintainAspectRatio: false,
            scales: {
                r: {
                    min: 0, max: 100,
                    ticks: {
                        color:         'rgba(107,140,108,0.7)',
                        backdropColor: 'transparent',
                        stepSize:      25,
                        font:          { size: 10 },
                    },
                    grid:        { color: 'rgba(74,222,128,0.08)' },
                    angleLines:  { color: 'rgba(74,222,128,0.10)' },
                    pointLabels: { color: '#a7c4a8', font: { size: 10, weight: '600' } },
                },
            },
            plugins: {
                legend: {
                    display: true,
                    labels:  { color: '#a7c4a8', font: { size: 11 }, usePointStyle: true },
                },
                title: {
                    display: true,
                    text:    `${city} — ${_t('Demand Map') || 'Demand Map'}`,
                    color:   '#a7c4a8',
                    font:    { size: 13, weight: '600' },
                    padding: { bottom: 10 },
                },
                tooltip: {
                    backgroundColor: '#0e1510',
                    borderColor:     'rgba(74,222,128,0.25)',
                    borderWidth:     1,
                    titleColor:      '#e8f5e9',
                    bodyColor:       '#a7c4a8',
                },
            },
            animation: { duration: 700, easing: 'easeOutQuart' },
        },
    });
}

/* ──────────────────────────────────────────────
   SHARED BASE CHART OPTIONS
────────────────────────────────────────────── */
function getBaseChartOptions(titleText) {
    return {
        responsive:          true,
        maintainAspectRatio: false,
        interaction:         { mode: 'index', intersect: false },
        plugins: {
            legend: {
                display:  true,
                position: 'bottom',
                labels: {
                    color:           '#a7c4a8',
                    font:            { size: 11 },
                    usePointStyle:   true,
                    pointStyleWidth: 10,
                    boxHeight:       8,
                    padding:         12,
                },
            },
            title: {
                display: !!titleText,
                text:    titleText,
                color:   '#a7c4a8',
                font:    { size: 13, weight: '600' },
                padding: { bottom: 10 },
            },
            tooltip: {
                backgroundColor: '#0e1510',
                borderColor:     'rgba(74,222,128,0.25)',
                borderWidth:     1,
                titleColor:      '#e8f5e9',
                bodyColor:       '#a7c4a8',
                padding:         10,
                callbacks: {
                    label: ctx => ` ${ctx.dataset.label}: ₹${ctx.raw.toLocaleString('en-IN')}`,
                },
            },
        },
        animation: { duration: 700, easing: 'easeOutQuart' },
    };
}

/* ══════════════════════════════════════════════
   PRICE COMPARISON TABLE
══════════════════════════════════════════════ */
function buildPriceTable(markets) {
    const tbody = document.getElementById('priceTableBody');
    if (!tbody) return;

    const cities   = Object.keys(markets);
    const cropCounts = {};
    const cropPrices = {};
    Object.values(markets).forEach(crops => crops.forEach(c => {
        cropCounts[c.crop] = (cropCounts[c.crop] || 0) + 1;
        if (!cropPrices[c.crop]) cropPrices[c.crop] = c.price;
    }));

    // Sort crops by city coverage descending (most widespread major crops first), then name
    const allCrops = Object.keys(cropCounts).sort((a, b) =>
        (cropCounts[b] - cropCounts[a]) || a.localeCompare(b)
    );

    const lookup = {};
    Object.entries(markets).forEach(([city, crops]) => {
        lookup[city] = {};
        crops.forEach(c => { lookup[city][c.crop] = c; });
    });

    const displayCities = cities.slice(0, 10);

    tbody.innerHTML = allCrops.map(cropName => {
        const cells = displayCities.map(city => {
            const item = lookup[city]?.[cropName];
            if (!item) {
                // No live price for this crop in this city — show a plain
                // dash rather than an invented reference number.
                return `<td class="not-available"><span class="no-data-tag">—</span></td>`;
            }

            const color  = item.change >= 2  ? '#4ade80' :
                           item.change <= -2 ? '#f87171' : 'var(--text)';
            const arrow  = item.change >= 0.5  ? '▲' :
                           item.change <= -0.5 ? '▼' : '–';
            const dClass = getDemandClass(item.demand);
            return `
            <td>
                <div style="color:${color};font-weight:700">
                    ₹${item.price.toLocaleString('en-IN')}
                    <span style="font-size:0.62rem;opacity:0.7"> ${arrow}</span>
                </div>
                <div class="cr-demand demand-${dClass}" style="display:inline-flex;font-size:0.58rem;padding:1px 5px;margin-top:2px">
                    ${tDemand(item.demand)}
                </div>
            </td>`;
        });

        return `<tr>
            <td><strong>${tCrop(cropName)}</strong></td>
            ${cells.join('')}
        </tr>`;
    }).join('');

    const thead = document.querySelector('.price-table thead tr');
    if (thead) {
        thead.innerHTML =
            `<th>${_t('Crop') || 'Crop'}</th>` +
            displayCities.map(c => `<th>${c}</th>`).join('');
    }
}

function reRenderMarket() {
    if (Object.keys(allMarketData).length === 0) return;

    const subtitle = document.getElementById('marketSubtitle');
    const search   = document.getElementById('locationSearch');
    if (subtitle && search && !search.value) {
        subtitle.textContent = _t('Showing all major Indian markets') || 'Showing all major Indian markets';
    }

    const targetMarkets = currentSearchedMarkets || allMarketData;
    renderMarketGrid(targetMarkets);
    buildTicker(allMarketData);
    buildPriceTable(targetMarkets);
    buildChart(targetMarkets, currentChartCity, activeChartType);
}

document.addEventListener('langChanged', (e) => {
    const lang = e.detail?.lang || 'en';
    loadTranslations(lang);
});

/* ══════════════════════════════════════════════
   QUICK JUMP SCROLL
══════════════════════════════════════════════ */
window.scrollToMarketSection = function(sectionId) {
    const el = document.getElementById(sectionId);
    if (!el) return;
    
    const navBar = document.getElementById('navbar');
    const searchBar = document.querySelector('.search-bar-section');
    const navHeight = navBar ? navBar.offsetHeight : 60;
    const searchHeight = searchBar ? searchBar.offsetHeight : 50;
    
    const offset = navHeight + searchHeight + 15;
    const bodyRect = document.body.getBoundingClientRect().top;
    const elementRect = el.getBoundingClientRect().top;
    const elementPosition = elementRect - bodyRect;
    const offsetPosition = elementPosition - offset;

    window.scrollTo({
        top: Math.max(0, offsetPosition),
        behavior: 'smooth'
    });
};