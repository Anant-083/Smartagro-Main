/* =========================================================================
   settings.js — SmartAgro App Settings Manager
   ========================================================================= */

window.SmartAgroSettings = (function () {

  const DEFAULTS = {
    volume:         80,
    tempUnit:       'celsius',
    weightUnit:     'quintal',
    notifications:  true,
    notifFrequency: '08:00',
    theme:          'dark',
    fontSize:       'medium',
  };

  const STORAGE_KEY = 'smartagro_settings';
  let _settings = {};

  function _load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      _settings = raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS };

      const savedTheme = localStorage.getItem('smartagro_theme');
      if ((savedTheme === 'light' || savedTheme === 'dark') && _settings.theme !== 'system') {
        _settings.theme = savedTheme;
      }
    } catch (e) {
      console.warn('[Settings] Could not load saved settings:', e);
      _settings = { ...DEFAULTS };
    }
  }

  function _save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(_settings));
    } catch (e) {
      console.warn('[Settings] Could not save settings:', e);
    }
  }

  function get(key) { return _settings[key] !== undefined ? _settings[key] : DEFAULTS[key]; }

  function set(key, value) { _settings[key] = value; _save(); _apply(key, value); }

  function _apply(key, value) {
    switch (key) {
      case 'volume':         applyVolume(value);         break;
      case 'tempUnit':       applyTempUnit(value);       break;
      case 'weightUnit':     applyWeightUnit(value);     break;
      case 'notifications':  applyNotifications(value);  break;
      case 'notifFrequency': applyNotifFrequency(value); break;
      case 'theme':          applyTheme(value);          break;
      case 'fontSize':       applyFontSize(value);       break;
    }
  }

  function applyVolume(v) {
    const parsed = Number.parseInt(v, 10);
    window.sagrAudioVolume = Math.max(0, Math.min(100, Number.isNaN(parsed) ? DEFAULTS.volume : parsed));
  }

  function formatAppTemperature(celsius) {
    const value = Number(celsius);
    if (!Number.isFinite(value)) return '—';
    const useFahrenheit = get('tempUnit') === 'fahrenheit';
    const displayValue = useFahrenheit ? (value * 9 / 5) + 32 : value;
    return `${Math.round(displayValue)}°${useFahrenheit ? 'F' : 'C'}`;
  }
  window.formatAppTemperature = formatAppTemperature;

  function applyTempUnit(unit) {
    window.sagrTempUnit = unit;
    if (window.weatherData && window.weatherData.current) {
      if (typeof renderHeroCard === 'function')       renderHeroCard(window.weatherData.current);
      if (typeof renderWeatherSection === 'function') renderWeatherSection(window.weatherData.current, window.weatherData.forecast);
      if (typeof renderStatBar === 'function')        renderStatBar(window.weatherData.current);
      const seasonTemp = document.querySelector('#seasonLabel .season-temperature');
      if (seasonTemp) seasonTemp.textContent = formatAppTemperature(window.weatherData.current.temp);
    }
    window.dispatchEvent(new CustomEvent('appTempUnitChanged', { detail: { unit } }));
  }

  function applyWeightUnit(unit) {
    window.sagrWeightUnit = unit;
    if (typeof reRenderMarket === 'function') reRenderMarket();
  }

  function applyNotifications(enabled) {
    window.sagrNotificationsEnabled = Boolean(enabled) &&
      ('Notification' in window) && Notification.permission === 'granted';
    if (enabled) {
      applyNotifFrequency(get('notifFrequency'));
    } else {
      if (_notifTimeoutId) { clearTimeout(_notifTimeoutId); _notifTimeoutId = null; }
      if (_notifIntervalId) { clearInterval(_notifIntervalId); _notifIntervalId = null; }
    }
  }

  let _notifTimeoutId = null;
  let _notifIntervalId = null;
  function applyNotifFrequency(timeStr) {
    if (_notifTimeoutId)  { clearTimeout(_notifTimeoutId);  _notifTimeoutId  = null; }
    if (_notifIntervalId) { clearInterval(_notifIntervalId); _notifIntervalId = null; }
    if (!timeStr || !get('notifications')) return;
    const [hh, mm] = timeStr.split(':').map(Number);
    const now  = new Date();
    const next = new Date();
    next.setHours(hh, mm, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    const delay = next - now;
    _notifTimeoutId = setTimeout(function() {
      _triggerNotif();
      _notifIntervalId = setInterval(_triggerNotif, 24 * 60 * 60 * 1000);
    }, delay);
  }

  function _triggerNotif() {
    if (!get('notifications')) return;
    if (window.SmartAgroNotifications && window.weatherData) {
      window.SmartAgroNotifications.checkAndTriggerAlertNotifications(window.weatherData);
    } else if (window.SmartAgroNotifications) {
      window.SmartAgroNotifications.sendNotification('SmartAgro Daily Alert', {
        body: 'Check your farm conditions today!', tag: 'daily-reminder'
      });
    }
  }

  function applyTheme(mode) {
    const icon = document.getElementById('themeIcon');
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    let isDark = (mode === 'system') ? prefersDark : (mode !== 'light');

    if (isDark) {
      document.body.classList.remove('light-theme');
      if (icon) { icon.classList.remove('fa-sun'); icon.classList.add('fa-moon'); }
      try { localStorage.setItem('smartagro_theme', 'dark'); }
      catch (e) { console.warn('[Settings] Could not save theme:', e); }
    } else {
      document.body.classList.add('light-theme');
      if (icon) { icon.classList.remove('fa-moon'); icon.classList.add('fa-sun'); }
      try { localStorage.setItem('smartagro_theme', 'light'); }
      catch (e) { console.warn('[Settings] Could not save theme:', e); }
    }
    _settings.theme = mode;
    _save();
    document.querySelectorAll('.settings-theme-btn').forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.theme === mode);
    });
  }

  const FONT_SIZES = { small: '13px', medium: '15px', large: '17px', xlarge: '19px' };

  function applyFontSize(size) {
    document.documentElement.style.fontSize = FONT_SIZES[size] || FONT_SIZES.medium;
    document.documentElement.setAttribute('data-font-size', size);
  }

  function _initSystemThemeListener() {
    if (!window.matchMedia) return;
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const onThemeChange = function() {
      if (get('theme') === 'system') applyTheme('system');
    };
    if (mediaQuery.addEventListener) mediaQuery.addEventListener('change', onThemeChange);
    else if (mediaQuery.addListener) mediaQuery.addListener(onThemeChange);
  }

  function init() {
    _load();
    Object.keys(_settings).forEach(function(key) { _apply(key, _settings[key]); });
    _initSystemThemeListener();
    _injectModal();
    var btn = document.getElementById('settingsBtn');
    if (btn) btn.addEventListener('click', openModal);
  }

  function _buildModalHTML() {
    var notifChecked = ('Notification' in window && Notification.permission === 'granted' && get('notifications'));
    return '<div id="settingsModal" class="settings-modal-overlay" role="dialog" aria-modal="true" aria-label="App Settings">'
      + '<div class="settings-modal-panel">'
      + '<div class="settings-modal-header">'
      + '<div class="settings-modal-title"><i class="fas fa-sliders"></i> App Settings</div>'
      + '<button class="settings-modal-close" id="settingsModalClose" title="Close"><i class="fas fa-times"></i></button>'
      + '</div>'
      + '<div class="settings-modal-body">'

      // Volume
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-volume-up"></i> Sound &amp; Volume</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label" for="settingsVolume">Chatbot Voice Volume<span class="settings-label-sub">Controls AI assistant speaker volume</span></label>'
      + '<div class="settings-volume-wrap">'
      + '<i class="fas fa-volume-off settings-vol-icon"></i>'
      + '<input type="range" id="settingsVolume" class="settings-slider" min="0" max="100" step="5" value="' + get('volume') + '">'
      + '<i class="fas fa-volume-up settings-vol-icon"></i>'
      + '<span class="settings-volume-val" id="settingsVolumeVal">' + get('volume') + '%</span>'
      + '</div></div></div>'

      // Units
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-ruler-combined"></i> Preferred Units</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label">Market Price Unit<span class="settings-label-sub">How crop prices are displayed</span></label>'
      + '<div class="settings-radio-group" id="weightUnitGroup">'
      + '<label class="settings-radio-opt' + (get('weightUnit') === 'quintal' ? ' active' : '') + '" data-value="quintal"><input type="radio" name="weightUnit" value="quintal"' + (get('weightUnit') === 'quintal' ? ' checked' : '') + '><span class="radio-dot"></span>&#8377;/Quintal <span class="radio-sub">(100 kg)</span></label>'
      + '<label class="settings-radio-opt' + (get('weightUnit') === 'kg' ? ' active' : '') + '" data-value="kg"><input type="radio" name="weightUnit" value="kg"' + (get('weightUnit') === 'kg' ? ' checked' : '') + '><span class="radio-dot"></span>&#8377;/Kg</label>'
      + '</div></div>'
      + '<div class="settings-row">'
      + '<label class="settings-label">Temperature Unit<span class="settings-label-sub">Weather temperature display</span></label>'
      + '<div class="settings-radio-group" id="tempUnitGroup">'
      + '<label class="settings-radio-opt' + (get('tempUnit') === 'celsius' ? ' active' : '') + '" data-value="celsius"><input type="radio" name="tempUnit" value="celsius"' + (get('tempUnit') === 'celsius' ? ' checked' : '') + '><span class="radio-dot"></span>&deg;C (Celsius)</label>'
      + '<label class="settings-radio-opt' + (get('tempUnit') === 'fahrenheit' ? ' active' : '') + '" data-value="fahrenheit"><input type="radio" name="tempUnit" value="fahrenheit"' + (get('tempUnit') === 'fahrenheit' ? ' checked' : '') + '><span class="radio-dot"></span>&deg;F (Fahrenheit)</label>'
      + '</div></div></div>'

      // Notifications
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-bell"></i> Notifications</div>'
      + '<div class="settings-row settings-row-toggle">'
      + '<label class="settings-label" for="settingsNotifToggle">Push Notifications<span class="settings-label-sub">Real-time weather &amp; pest alerts</span></label>'
      + '<label class="settings-toggle-switch"><input type="checkbox" id="settingsNotifToggle"' + (notifChecked ? ' checked' : '') + '><span class="settings-toggle-track"><span class="settings-toggle-thumb"></span></span></label>'
      + '</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label" for="settingsNotifTime">Daily Alert Time<span class="settings-label-sub">When to send your daily farm report</span></label>'
      + '<input type="time" id="settingsNotifTime" class="settings-time-input" value="' + get('notifFrequency') + '">'
      + '</div></div>'

      // Theme
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-palette"></i> Theme</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label">App Theme<span class="settings-label-sub">Choose your preferred appearance</span></label>'
      + '<div class="settings-theme-group" id="themePickerGroup">'
      + '<button class="settings-theme-btn' + (get('theme') === 'dark' ? ' active' : '') + '" data-theme="dark"><i class="fas fa-moon"></i><span>Dark</span></button>'
      + '<button class="settings-theme-btn' + (get('theme') === 'light' ? ' active' : '') + '" data-theme="light"><i class="fas fa-sun"></i><span>Light</span></button>'
      + '<button class="settings-theme-btn' + (get('theme') === 'system' ? ' active' : '') + '" data-theme="system"><i class="fas fa-circle-half-stroke"></i><span>System</span></button>'
      + '</div></div></div>'

      // Font Size
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-text-height"></i> Accessibility</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label">Font Size<span class="settings-label-sub">Adjust text size across the app</span></label>'
      + '<div class="settings-font-group" id="fontSizeGroup">'
      + '<button class="settings-font-btn' + (get('fontSize') === 'small' ? ' active' : '') + '" data-size="small" style="font-size:0.8em">A</button>'
      + '<button class="settings-font-btn' + (get('fontSize') === 'medium' ? ' active' : '') + '" data-size="medium">A</button>'
      + '<button class="settings-font-btn' + (get('fontSize') === 'large' ? ' active' : '') + '" data-size="large" style="font-size:1.15em">A</button>'
      + '<button class="settings-font-btn' + (get('fontSize') === 'xlarge' ? ' active' : '') + '" data-size="xlarge" style="font-size:1.35em">A</button>'
      + '</div></div></div>'

      // Help
      + '<div class="settings-section">'
      + '<div class="settings-section-title"><i class="fas fa-circle-question"></i> Help &amp; Support</div>'
      + '<div class="settings-row">'
      + '<label class="settings-label">Help / FAQ<span class="settings-label-sub">Ask the SmartAgro AI Assistant anything</span></label>'
      + '<button class="settings-help-btn" id="settingsHelpBtn"><i class="fas fa-robot"></i> Open Kisan Helper</button>'
      + '</div></div>'

      + '</div>'
      + '<div class="settings-modal-footer">'
      + '<button class="settings-reset-btn" id="settingsResetBtn"><i class="fas fa-rotate-left"></i> Reset to Defaults</button>'
      + '<button class="settings-save-btn" id="settingsSaveBtn"><i class="fas fa-check"></i> Done</button>'
      + '</div>'
      + '</div></div>';
  }

  function _injectModal() {
    if (document.getElementById('settingsModal')) return;
    document.body.insertAdjacentHTML('beforeend', _buildModalHTML());
    _bindModalEvents();
  }

  function _bindModalEvents() {
    var closeBtn = document.getElementById('settingsModalClose');
    if (closeBtn) closeBtn.addEventListener('click', closeModal);

    var overlay = document.getElementById('settingsModal');
    if (overlay) overlay.addEventListener('click', function(e) { if (e.target === overlay) closeModal(); });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && overlay && overlay.classList.contains('visible')) closeModal();
    });

    var volSlider = document.getElementById('settingsVolume');
    var volVal    = document.getElementById('settingsVolumeVal');
    if (volSlider) volSlider.addEventListener('input', function() {
      var v = parseInt(volSlider.value);
      if (volVal) volVal.textContent = v + '%';
      set('volume', v);
    });

    document.querySelectorAll('input[name="weightUnit"]').forEach(function(radio) {
      radio.addEventListener('change', function() {
        document.querySelectorAll('#weightUnitGroup label[data-value]').forEach(function(l) {
          l.classList.toggle('active', l.dataset.value === radio.value);
        });
        set('weightUnit', radio.value);
      });
    });

    document.querySelectorAll('input[name="tempUnit"]').forEach(function(radio) {
      radio.addEventListener('change', function() {
        document.querySelectorAll('#tempUnitGroup label[data-value]').forEach(function(l) {
          l.classList.toggle('active', l.dataset.value === radio.value);
        });
        set('tempUnit', radio.value);
      });
    });

    var notifToggle = document.getElementById('settingsNotifToggle');
    if (notifToggle) notifToggle.addEventListener('change', function() {
      if (!notifToggle.checked) {
        set('notifications', notifToggle.checked);
        return;
      }

      if (!('Notification' in window) || !window.SmartAgroNotifications) {
        notifToggle.checked = false;
        set('notifications', false);
        if (typeof showToast === 'function') showToast('Device notifications are not supported by this browser.', 'warning');
        return;
      }

      if (Notification.permission === 'granted') {
        set('notifications', true);
        return;
      }

      set('notifications', true);
      notifToggle.disabled = true;
      window.SmartAgroNotifications.requestPermission(function(granted) {
        notifToggle.disabled = false;
        if (!granted) {
          notifToggle.checked = false;
          set('notifications', false);
        } else {
          applyNotifications(true);
        }
      });
    });

    var notifTime = document.getElementById('settingsNotifTime');
    if (notifTime) notifTime.addEventListener('change', function() {
      set('notifFrequency', notifTime.value);
      if (typeof showToast === 'function') showToast('Daily alert set for ' + notifTime.value, 'success');
    });

    document.querySelectorAll('.settings-theme-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        document.querySelectorAll('.settings-theme-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        applyTheme(btn.dataset.theme);
      });
    });

    document.querySelectorAll('.settings-font-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        document.querySelectorAll('.settings-font-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        set('fontSize', btn.dataset.size);
      });
    });

    var helpBtn = document.getElementById('settingsHelpBtn');
    if (helpBtn) helpBtn.addEventListener('click', function() {
      closeModal();
      setTimeout(function() { if (typeof window.toggleKisan === 'function') window.toggleKisan(); }, 200);
    });

    var saveBtn = document.getElementById('settingsSaveBtn');
    if (saveBtn) saveBtn.addEventListener('click', closeModal);

    var resetBtn = document.getElementById('settingsResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', function() {
      if (!confirm('Reset all settings to defaults?')) return;
      _settings = Object.assign({}, DEFAULTS);
      _save();
      Object.keys(_settings).forEach(function(key) { _apply(key, _settings[key]); });
      _syncModalControls();
      if (typeof showToast === 'function') showToast('Settings reset to defaults.', 'success');
    });
  }

  function _syncModalControls() {
    var volume = document.getElementById('settingsVolume');
    var volumeValue = document.getElementById('settingsVolumeVal');
    if (volume) volume.value = get('volume');
    if (volumeValue) volumeValue.textContent = get('volume') + '%';

    ['weightUnit', 'tempUnit'].forEach(function(setting) {
      document.querySelectorAll('input[name="' + setting + '"]').forEach(function(radio) {
        radio.checked = radio.value === get(setting);
        const label = radio.closest('label[data-value]');
        if (label) label.classList.toggle('active', radio.checked);
      });
    });

    var notifToggle = document.getElementById('settingsNotifToggle');
    if (notifToggle) {
      notifToggle.checked = Boolean(get('notifications')) &&
        ('Notification' in window) && Notification.permission === 'granted';
    }

    var notifTime = document.getElementById('settingsNotifTime');
    if (notifTime) notifTime.value = get('notifFrequency');

    document.querySelectorAll('.settings-theme-btn').forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.theme === get('theme'));
    });
    document.querySelectorAll('.settings-font-btn').forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.size === get('fontSize'));
    });
  }

  function openModal() {
    if (!document.getElementById('settingsModal')) _injectModal();
    _syncModalControls();
    requestAnimationFrame(function() {
      var m = document.getElementById('settingsModal');
      if (m) m.classList.add('visible');
    });
  }

  function closeModal() {
    var modal = document.getElementById('settingsModal');
    if (modal) modal.classList.remove('visible');
  }

  return { init: init, get: get, set: set, openModal: openModal, closeModal: closeModal, applyTheme: applyTheme };

})();

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function() { SmartAgroSettings.init(); });
} else {
  SmartAgroSettings.init();
}
