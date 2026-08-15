/* ── Navbar scroll effect ───────────────────── */
const navbar = document.getElementById('navbar');
window.addEventListener('scroll', () => {
    if (window.scrollY > 40) {
        navbar.classList.add('scrolled');
    } else {
        navbar.classList.remove('scrolled');
    }
}, { passive: true });

/* ── Hamburger ──────────────────────────────── */
const hamburger = document.getElementById('hamburger');
const navLinks = document.getElementById('navLinks');
if (hamburger && navLinks) {
    hamburger.addEventListener('click', () => {
        hamburger.classList.toggle('open');
        navLinks.classList.toggle('open');
    });
    // Close on nav item click (mobile)
    navLinks.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            hamburger.classList.remove('open');
            navLinks.classList.remove('open');
        });
    });
}

/* ── Toast notification ─────────────────────── */
let toastTimer = null;

function showToast(msg, type = 'success', duration = 3500) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.className = `toast show ${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.classList.remove('show');
    }, duration);
}

/* ── Shared weather state ───────────────────── */
window.weatherData = null;

/* ══════════════════════════════════════════════
   SHARED LOCATION PERSISTENCE
   Saved in sessionStorage so it survives navigating
   between pages, but clears when the browser tab/app
   is closed — "temporary for as long as the app runs".
══════════════════════════════════════════════ */
const LOCATION_STORAGE_KEY = 'smartagro_location';

function saveUserLocation(lat, lon, city) {
    try {
        const existing = getUserLocation() || {};
        const payload = {
            lat: lat != null ? lat : existing.lat,
            lon: lon != null ? lon : existing.lon,
            city: city != null ? city : existing.city,
            ts: Date.now(),
        };
        sessionStorage.setItem(LOCATION_STORAGE_KEY, JSON.stringify(payload));
        localStorage.setItem(LOCATION_STORAGE_KEY, JSON.stringify(payload));
    } catch (e) {
        console.warn('Could not save location:', e);
    }
}

function getUserLocation() {
    try {
        const raw = localStorage.getItem(LOCATION_STORAGE_KEY) || sessionStorage.getItem(LOCATION_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (typeof parsed.lat !== 'number' || typeof parsed.lon !== 'number') return null;
        return parsed;
    } catch (e) {
        return null;
    }
}

/* ══════════════════════════════════════════════
   SHARED NOTIFICATION / PROMPT POPUP
   Generic reusable modal for things like "grant your
   location" prompts and calamity/risk summaries.
══════════════════════════════════════════════ */
function ensureAppNotificationStyles() {
    if (document.getElementById('appNotificationStyle')) return;
    const style = document.createElement('style');
    style.id = 'appNotificationStyle';
    style.textContent = `
    .app-notif-overlay {
        position: fixed; inset: 0; z-index: 10000;
        display: flex; align-items: center; justify-content: center;
        background: rgba(10, 16, 12, 0.6);
        backdrop-filter: blur(3px);
        opacity: 0; pointer-events: none;
        transition: opacity 0.2s ease;
        padding: 20px;
    }
    .app-notif-overlay.visible { opacity: 1; pointer-events: all; }
    .app-notif-box {
        background: var(--bg-1, #102013);
        border: 1px solid var(--green, #4ade80);
        border-radius: 16px;
        padding: 30px 28px;
        max-width: 380px;
        width: 100%;
        text-align: center;
        box-shadow: 0 10px 40px rgba(0,0,0,0.4);
        animation: appNotifPopIn 0.25s ease;
    }
    @keyframes appNotifPopIn { from { transform: scale(0.92); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    .app-notif-icon {
        width: 60px; height: 60px; margin: 0 auto 16px;
        border-radius: 50%;
        background: rgba(74,222,128,0.1);
        border: 1px solid rgba(74,222,128,0.25);
        display: flex; align-items: center; justify-content: center;
        font-size: 1.6rem; color: var(--green, #4ade80);
    }
    .app-notif-title {
        color: var(--text-1, #f1f5f1);
        font-weight: 700; font-size: 1.05rem; margin-bottom: 8px;
        font-family: 'Syne', sans-serif;
    }
    .app-notif-msg {
        color: var(--text-3, #94a3a0);
        font-size: 0.85rem; line-height: 1.5; margin-bottom: 22px;
    }
    .app-notif-list {
        text-align: left; margin: 0 0 20px; padding: 0; list-style: none;
        display: flex; flex-direction: column; gap: 6px;
    }
    .app-notif-list li {
        font-size: 0.8rem; color: var(--text-2, #c8d6c9);
        padding: 8px 12px; background: rgba(248,113,113,0.06);
        border: 1px solid rgba(248,113,113,0.15); border-radius: 8px;
    }
    .app-notif-btns { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
    `;
    document.head.appendChild(style);
}

function showAppNotification({ icon = 'fa-location-crosshairs', title, message, listItems, primaryLabel, onPrimary, secondaryLabel, onSecondary, id } = {}) {
    ensureAppNotificationStyles();
    const overlayId = id || 'appNotificationOverlay';
    let overlay = document.getElementById(overlayId);
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = overlayId;
        overlay.className = 'app-notif-overlay';
        document.body.appendChild(overlay);
    }

    const listHtml = Array.isArray(listItems) && listItems.length ?
        `<ul class="app-notif-list">${listItems.map(i => `<li>${i}</li>`).join('')}</ul>`
        : '';

    overlay.innerHTML = `
      <div class="app-notif-box">
        <div class="app-notif-icon"><i class="fas ${icon}"></i></div>
        <div class="app-notif-title">${title || ''}</div>
        <div class="app-notif-msg">${message || ''}</div>
        ${listHtml}
        <div class="app-notif-btns">
          ${primaryLabel ? `<button class="btn-primary" id="${overlayId}Primary">${primaryLabel}</button>` : ''}
          ${secondaryLabel ? `<button class="btn-secondary" id="${overlayId}Secondary">${secondaryLabel}</button>` : ''}
        </div>
      </div>`;

    requestAnimationFrame(() => overlay.classList.add('visible'));

    if (primaryLabel) {
        document.getElementById(`${overlayId}Primary`).addEventListener('click', () => {
            hideAppNotification(overlayId);
            if (typeof onPrimary === 'function') onPrimary();
        });
    }
    if (secondaryLabel) {
        document.getElementById(`${overlayId}Secondary`).addEventListener('click', () => {
            hideAppNotification(overlayId);
            if (typeof onSecondary === 'function') onSecondary();
        });
    }
}

function hideAppNotification(id = 'appNotificationOverlay') {
    const overlay = document.getElementById(id);
    if (overlay) overlay.classList.remove('visible');
}

/* API limit notifications disabled — 429s are handled silently per feature */
window.handleApiLimitNotification = function() {};
window.checkApiLimitError = function() { return false; };


/* ── Geolocation helper ─────────────────────── */
function requestLocation(callback) {
    const btn = document.getElementById('locationBtn') || document.getElementById('alertLocationBtn');
    if (btn) {
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> <span>Getting location...</span>`;
        btn.disabled = true;
    }

    if (!navigator.geolocation) {
        showToast('Geolocation is not supported by your browser.', 'error');
        if (btn) {
            btn.innerHTML = `<i class="fas fa-location-crosshairs"></i> <span>Get My Location</span>`;
            btn.disabled = false;
        }
        return;
    }

    navigator.geolocation.getCurrentPosition(
        position => {
            const { latitude, longitude } = position.coords;
            if (btn) {
                btn.innerHTML = `<i class="fas fa-check"></i> <span>Location Found</span>`;
                btn.style.background = 'linear-gradient(135deg, #166534, #22c55e)';
            }
            showToast('📍 Location detected successfully!', 'success');
            if (typeof callback === 'function') callback(latitude, longitude);
        },
        err => {
            console.error('Geolocation error:', err);
            showToast('Location access denied. Using default location.', 'warning');
            if (btn) {
                btn.innerHTML = `<i class="fas fa-location-crosshairs"></i> <span>Get My Location</span>`;
                btn.disabled = false;
            }
            // Fallback: use Delhi, India as default
            if (typeof callback === 'function') callback(28.6139, 77.2090);
            // To this:
        }, {
            timeout: 15000, // Gives the browser 15 seconds to find a position
            enableHighAccuracy: false, // Desktop browsers fail high accuracy if they lack a GPS chip
            maximumAge: 60000 // Allows utilizing a recently cached location asset
        }
    );
}

/* ── Fetch weather from backend ─────────────── */
async function fetchWeather(lat, lon) {
    try {
        const res = await fetch(`/api/weather?lat=${lat}&lon=${lon}`);
        if (!res.ok) throw new Error('Weather API error');
        const data = await res.json();
        window.weatherData = data;
        if (window.SmartAgroNotifications) {
            window.SmartAgroNotifications.checkAndTriggerAlertNotifications(data);
        }
        return data;
    } catch (err) {
        console.error('fetchWeather error:', err);
        showToast('Could not load weather data.', 'error');
        return null;
    }
}

/* ── Weather icon emoji map ─────────────────── */
function getWeatherEmoji(iconCode) {
    const map = {
        '01d': '☀️',
        '01n': '🌙',
        '02d': '⛅',
        '02n': '⛅',
        '03d': '☁️',
        '03n': '☁️',
        '04d': '☁️',
        '04n': '☁️',
        '09d': '🌧️',
        '09n': '🌧️',
        '10d': '🌦️',
        '10n': '🌧️',
        '11d': '⛈️',
        '11n': '⛈️',
        '13d': '❄️',
        '13n': '❄️',
        '50d': '🌫️',
        '50n': '🌫️',
    };
    return map[iconCode] || '🌤️';
}

/* ── Format day name ────────────────────────── */
function getDayName(dateStr, short = true) {
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const shortDays = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const d = new Date(dateStr);
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);

    let name;
    if (d.toDateString() === today.toDateString()) name = 'Today';
    else if (d.toDateString() === tomorrow.toDateString()) name = 'Tomorrow';
    else name = short ? shortDays[d.getDay()] : days[d.getDay()];

    // Translate via whichever helper is available on this page
    if (typeof _at === 'function') return _at(name) || name;
    if (typeof dt === 'function') return dt(name) || name;
    return name;
}

/* ── Capitalize ─────────────────────────────── */
function capitalize(str) {
    return str ? str.charAt(0).toUpperCase() + str.slice(1) : '';
}

/* ── Animate counter ────────────────────────── */
function animateCounter(el, target, duration = 800, suffix = '') {
    if (!el) return;
    const start = 0;
    const startTime = performance.now();

    function update(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(start + (target - start) * eased);
        el.textContent = current + suffix;
        if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
}

/* ── Intersection Observer for animations ───── */
function observeAnimations() {
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.style.animationPlayState = 'running';
                observer.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.crop-card, .forecast-card, .timeline-item, .alert-card, .city-card').forEach(el => {
        el.style.animationPlayState = 'paused';
        observer.observe(el);
    });
}

/* ── Ripple effect on buttons ───────────────── */
document.addEventListener('click', e => {
    const btn = e.target.closest('.btn-primary, .btn-secondary, .btn-analyze, .chart-tab, .alert-tab, .chip');
    if (!btn) return;
    const ripple = document.createElement('span');
    const rect = btn.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    ripple.style.cssText = `
    position:absolute; border-radius:50%;
    width:${size}px; height:${size}px;
    left:${e.clientX - rect.left - size/2}px;
    top:${e.clientY - rect.top - size/2}px;
    background:rgba(255,255,255,0.18);
    transform:scale(0); animation:ripple 0.55s linear;
    pointer-events:none;
  `;
    if (getComputedStyle(btn).position === 'static') btn.style.position = 'relative';
    btn.style.overflow = 'hidden';
    btn.appendChild(ripple);
    setTimeout(() => ripple.remove(), 600);
});

// Ripple keyframe injection
const styleTag = document.createElement('style');
styleTag.textContent = `@keyframes ripple { to { transform: scale(2.5); opacity: 0; } }`;
document.head.appendChild(styleTag);

/* ── Update alert badge in navbar ───────────── */
function updateAlertBadge(count) {
    const badge = document.getElementById('alertBadge');
    if (badge) {
        badge.textContent = count;
        badge.style.display = count > 0 ? 'inline-flex' : 'none';
    }
}

/* ── On page load: restore badge from session ── */
document.addEventListener('DOMContentLoaded', () => {
    const saved = sessionStorage.getItem('alert_count');
    if (saved) updateAlertBadge(parseInt(saved));
    observeAnimations();

    // ── Restore saved language and notify diagnose.js ──
    const savedLang = localStorage.getItem('smartagro_lang') || 'en';
    window.currentLang = savedLang;
    if (savedLang !== 'en') {
        document.dispatchEvent(new CustomEvent('langChanged', { detail: { lang: savedLang } }));
    }

    // ── Close lang dropdown on outside tap (mobile) ──
    document.addEventListener('click', e => {
        const sel = document.querySelector('.lang-selector');
        if (sel && !sel.contains(e.target)) {
            sel.classList.remove('open');
        }
    });
    /* ── Day / Night Theme Toggle ───────────────────────── */
    (function initTheme() {
        const btn = document.getElementById('themeToggle');
        const icon = document.getElementById('themeIcon');
        const saved = localStorage.getItem('smartagro_theme');

        function applyTheme(mode) {
            if (mode === 'light') {
                document.body.classList.add('light-theme');
                if (icon) {
                    icon.classList.remove('fa-moon');
                    icon.classList.add('fa-sun');
                }
            } else {
                document.body.classList.remove('light-theme');
                if (icon) {
                    icon.classList.remove('fa-sun');
                    icon.classList.add('fa-moon');
                }
            }
            localStorage.setItem('smartagro_theme', mode);
        }

        // Restore saved preference on load
        applyTheme(saved === 'light' ? 'light' : 'dark');

        if (btn) {
            btn.addEventListener('click', () => {
                const isLight = document.body.classList.contains('light-theme');
                applyTheme(isLight ? 'dark' : 'light');
            });
        }
    })();
});
/* ── PWA Service Worker Registration ───────── */
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/static/service-worker.js')
            .then(reg => console.log('SmartAgro SW registered:', reg.scope))
            .catch(err => console.error('SW registration failed:', err));
    });
}

/* ══════════════════════════════════════════════
   INSTALL APP — works on desktop & mobile,
   available from the navbar on every page.
══════════════════════════════════════════════ */
let deferredInstallPrompt = null;

function isAppStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches ||
        window.navigator.standalone === true; // iOS Safari flag
}

function isIosDevice() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
}

function ensureInstallModalStyles() {
    // Styles live in main.css (.install-modal-*); nothing to inject here,
    // this hook exists in case the page loads main.js before main.css.
}

function showInstallModal({ icon, title, steps, note }) {
    let overlay = document.getElementById('installModalOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'installModalOverlay';
        overlay.className = 'install-modal-overlay';
        document.body.appendChild(overlay);
        overlay.addEventListener('click', e => { if (e.target === overlay) hideInstallModal(); });
    }
    overlay.innerHTML = `
      <div class="install-modal-box">
        <div class="install-modal-icon"><i class="fas ${icon}"></i></div>
        <h3>${title}</h3>
        <ul class="install-modal-steps">
          ${steps.map((s, i) => `<li><span class="ims-num">${i + 1}</span><span>${s}</span></li>`).join('')}
        </ul>
        ${note ? `<p class="install-modal-note">${note}</p>` : ''}
        <button class="install-modal-close" onclick="hideInstallModal()">Got it</button>
      </div>`;
    requestAnimationFrame(() => overlay.classList.add('visible'));
}

function hideInstallModal() {
    const overlay = document.getElementById('installModalOverlay');
    if (overlay) overlay.classList.remove('visible');
}

// Chrome/Edge/Android fire this when the app is installable.
// We stash the event so it can be triggered later from our own button
// instead of the browser's own mini-infobar.
window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredInstallPrompt = e;
});

window.addEventListener('appinstalled', () => {
    deferredInstallPrompt = null;
    showToast('✅ SmartAgro installed on your device!', 'success');
    const btn = document.getElementById('installBtn');
    if (btn) btn.style.display = 'none';
});

function setupInstallButton() {
    const btn = document.getElementById('installBtn');
    if (!btn) return;

    // Already running as an installed app — nothing to offer.
    if (isAppStandalone()) {
        btn.style.display = 'none';
        return;
    }

    btn.addEventListener('click', async () => {
        // Case 1: browser has a native install prompt ready (Chrome/Edge,
        // desktop or Android).
        if (deferredInstallPrompt) {
            btn.disabled = true;
            deferredInstallPrompt.prompt();
            const { outcome } = await deferredInstallPrompt.userChoice;
            if (outcome !== 'accepted') {
                showToast('Installation cancelled.', 'warning');
            }
            deferredInstallPrompt = null;
            btn.disabled = false;
            return;
        }

        // Case 2: iOS Safari has no install prompt API — show manual steps.
        if (isIosDevice()) {
            showInstallModal({
                icon: 'fa-share-from-square',
                title: 'Install SmartAgro on iPhone/iPad',
                steps: [
                    'Tap the <strong>Share</strong> icon in Safari\'s toolbar.',
                    'Scroll down and tap <strong>Add to Home Screen</strong>.',
                    'Tap <strong>Add</strong> in the top-right corner.'
                ],
                note: 'SmartAgro will then open full-screen from your Home Screen, just like a native app.'
            });
            return;
        }

        // Case 3: Desktop/Android browser without beforeinstallprompt
        // support yet (e.g. Firefox), or the prompt hasn't fired.
        showInstallModal({
            icon: 'fa-circle-info',
            title: 'Install SmartAgro',
            steps: [
                'Open this site in <strong>Chrome</strong> or <strong>Edge</strong> for one-tap install.',
                'Or use your browser\'s menu (⋮ or Share) and look for <strong>Install App</strong> / <strong>Add to Home Screen</strong>.'
            ],
            note: 'Install support depends on your browser.'
        });
    });
}

/* ══════════════════════════════════════════════
   GLOBAL BACK TO TOP BUTTON
══════════════════════════════════════════════ */
function setupBackToTopButton() {
    let btn = document.getElementById('backToTopBtn');
    if (!btn) {
        btn = document.createElement('button');
        btn.id = 'backToTopBtn';
        btn.className = 'back-to-top-btn';
        btn.setAttribute('aria-label', 'Back to top');
        btn.setAttribute('title', 'Back to top');
        btn.innerHTML = '<i class="fas fa-chevron-up"></i>';
        document.body.appendChild(btn);
    }

    const toggleVisibility = () => {
        if (window.scrollY > 100) {
            btn.classList.add('visible');
        } else {
            btn.classList.remove('visible');
        }
    };

    window.addEventListener('scroll', toggleVisibility, { passive: true });
    toggleVisibility();

    btn.addEventListener('click', (e) => {
        e.preventDefault();
        window.scrollTo({
            top: 0,
            behavior: 'smooth'
        });
    });
}

/* ══════════════════════════════════════════════
   SMARTAGRO REAL-TIME DEVICE POP-UP NOTIFICATIONS
══════════════════════════════════════════════ */
window.SmartAgroNotifications = {
    permission: function() { return ('Notification' in window) ? Notification.permission : 'unsupported'; },

    requestPermission: async function(callback) {
        if (!('Notification' in window)) {
            showToast('Device notifications are not supported by this browser.', 'warning');
            if (typeof callback === 'function') callback(false);
            return false;
        }
        try {
            const result = await Notification.requestPermission();
            if (result === 'granted') {
                showToast('🔔 Device Pop-up Notifications enabled!', 'success');
                this.sendNotification('🌾 SmartAgro Notifications Activated', {
                    body: 'You will receive real-time pop-up alerts for severe weather & crop risks in your region.',
                    tag: 'smartagro-welcome'
                });
                this.hideBanner();
                if (typeof callback === 'function') callback(true);
                return true;
            } else if (result === 'denied') {
                showToast('Notification permission denied in browser settings.', 'warning');
                if (typeof callback === 'function') callback(false);
                return false;
            }
        } catch (err) {
            console.error('Error requesting notification permission:', err);
        }
        if (typeof callback === 'function') callback(false);
        return false;
    },

    sendNotification: function(title, options) {
        options = options || {};
        if (!('Notification' in window) || Notification.permission !== 'granted') return;

        const payload = {
            title: title || '🌾 SmartAgro Alert',
            body: options.body || 'Alert for your farming region.',
            icon: options.icon || '/static/icons/icon-192.png',
            url: options.url || '/alerts',
            tag: options.tag || ('smartagro-alert-' + Date.now())
        };

        if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
            navigator.serviceWorker.controller.postMessage({
                type: 'SHOW_NOTIFICATION',
                title: payload.title,
                body: payload.body,
                icon: payload.icon,
                url: payload.url,
                tag: payload.tag
            });
        } else if ('serviceWorker' in navigator) {
            navigator.serviceWorker.ready.then(function(reg) {
                reg.showNotification(payload.title, {
                    body: payload.body,
                    icon: payload.icon,
                    badge: '/static/icons/icon-192.png',
                    vibrate: [200, 100, 200],
                    data: { url: payload.url },
                    tag: payload.tag,
                    renotify: true
                });
            }).catch(function() {
                try { new Notification(payload.title, payload); } catch(e){}
            });
        } else {
            try { new Notification(payload.title, payload); } catch(e){}
        }
    },

    checkAndTriggerAlertNotifications: function(weather) {
        if (!weather || !weather.current) return;

        const key = 'smartagro_notified_alerts';
        let notifiedMap = {};
        try {
            notifiedMap = JSON.parse(localStorage.getItem(key) || '{}');
        } catch (e) { notifiedMap = {}; }

        const now = Date.now();
        const TWELVE_HOURS = 12 * 60 * 60 * 1000;

        const current = weather.current;
        const temp = current.temp || 25;
        const main = (current.weather && current.weather[0] && current.weather[0].main) ? current.weather[0].main.toLowerCase() : '';
        const desc = (current.weather && current.weather[0] && current.weather[0].description) ? current.weather[0].description : '';
        const humidity = current.humidity || 50;
        const wind = current.wind_speed || 0;
        const city = weather.location || 'Your Region';

        const alertsToTrigger = [];

        // Heavy Rain / Storm Alert
        if (main.includes('rain') || main.includes('thunderstorm') || desc.includes('heavy') || desc.includes('storm')) {
            alertsToTrigger.push({
                id: 'rain_' + Math.floor(now / TWELVE_HOURS),
                title: `⛈️ Severe Weather Warning - ${city}`,
                body: `Heavy rainfall/thunderstorm detected (${desc}). Protect harvested crops and ensure field drainage.`
            });
        }

        // Extreme Heatwave Alert
        if (temp >= 40) {
            alertsToTrigger.push({
                id: 'heat_' + Math.floor(now / TWELVE_HOURS),
                title: `🔥 Extreme Heatwave Alert - ${city}`,
                body: `High temperature of ${Math.round(temp)}°C recorded. Irrigate standing crops during early morning or evening.`
            });
        }

        // Frost / Extreme Cold Alert
        if (temp <= 4) {
            alertsToTrigger.push({
                id: 'frost_' + Math.floor(now / TWELVE_HOURS),
                title: `❄️ Frost & Cold Wave Warning - ${city}`,
                body: `Low temperature of ${Math.round(temp)}°C detected. Cover young crop saplings to prevent frost damage.`
            });
        }

        // High Wind Warning
        if (wind >= 30) {
            alertsToTrigger.push({
                id: 'wind_' + Math.floor(now / TWELVE_HOURS),
                title: `💨 High Wind Gust Alert - ${city}`,
                body: `Strong winds of ${Math.round(wind)} km/h detected. Secure light farm equipment and delay chemical spraying.`
            });
        }

        // High Humidity Pest Attack Warning
        if (humidity >= 85 && temp >= 24) {
            alertsToTrigger.push({
                id: 'pest_' + Math.floor(now / TWELVE_HOURS),
                title: `🐛 High Pest Risk Advisory - ${city}`,
                body: `High humidity (${humidity}%) & warm weather create ideal conditions for fungal and pest attacks. Inspect crop leaves.`
            });
        }

        const self = this;
        alertsToTrigger.forEach(function(alert) {
            if (!notifiedMap[alert.id] || (now - notifiedMap[alert.id]) > TWELVE_HOURS) {
                notifiedMap[alert.id] = now;
                self.sendNotification(alert.title, {
                    body: alert.body,
                    url: '/alerts',
                    tag: alert.id
                });
            }
        });

        try {
            localStorage.setItem(key, JSON.stringify(notifiedMap));
        } catch (e) {}
    },

    initBanner: function() {
        if (!('Notification' in window) || Notification.permission === 'granted') return;
        const dismissed = localStorage.getItem('smartagro_notif_banner_dismissed');
        if (dismissed && (Date.now() - parseInt(dismissed)) < (24 * 60 * 60 * 1000)) return;

        let banner = document.getElementById('notifPermissionBanner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'notifPermissionBanner';
            banner.className = 'notif-perm-banner';
            banner.innerHTML = `
              <div class="npb-content">
                <div class="npb-icon"><i class="fas fa-bell"></i></div>
                <div class="npb-text">
                  <strong>Get Real-Time Device Pop-Up Alerts</strong>
                  <span>Receive pop-up notifications on your phone/PC for severe weather, heavy rain, & pest risks even when SmartAgro is closed!</span>
                </div>
              </div>
              <div class="npb-actions">
                <button class="npb-btn-allow" onclick="window.SmartAgroNotifications.requestPermission()"><i class="fas fa-bell"></i> Enable Pop-up Alerts</button>
                <button class="npb-btn-close" onclick="window.SmartAgroNotifications.hideBanner()" title="Dismiss">✕</button>
              </div>`;
            document.body.prepend(banner);
        }
    },

    hideBanner: function() {
        const banner = document.getElementById('notifPermissionBanner');
        if (banner) banner.remove();
        try {
            localStorage.setItem('smartagro_notif_banner_dismissed', Date.now().toString());
        } catch(e) {}
    }
};

function initAppGlobalFeatures() {
    setupInstallButton();
    setupBackToTopButton();
    if (window.SmartAgroNotifications) {
        window.SmartAgroNotifications.initBanner();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAppGlobalFeatures);
} else {
    initAppGlobalFeatures();
}