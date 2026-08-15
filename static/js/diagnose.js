let currentImageBase64 = null;
let remedyChartInst = null;
let cameraStream = null;

// Stores the raw (English) diagnosis result so we can re-translate on lang change
let _lastDiagnosisResult = null;

function getLang() {
    return (window.currentLang || 'en').toLowerCase().trim();
}

function tr(english) {
    if (!english) return '';
    const lang = getLang();
    if (lang === 'en') return english;
    const map = window.diagnoseTx || {};
    return map[english.trim()] || english;
}

/* ══════════════════════════════════════════════
   LANGUAGE DISPLAY NAMES (for overlay label)
══════════════════════════════════════════════ */
const DIAGNOSE_LANG_DISPLAY_NAMES = {
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

/* ══════════════════════════════════════════════
   TRANSLATE OVERLAY — same pattern as dashboard
══════════════════════════════════════════════ */
function ensureDiagnoseTranslateOverlayStyles() {
    if (document.getElementById('diagnoseTranslateOverlayStyle')) return;
    const style = document.createElement('style');
    style.id = 'diagnoseTranslateOverlayStyle';
    style.textContent = `
    .diagnose-translate-overlay {
        position: fixed; inset: 0; z-index: 9999;
        display: flex; align-items: center; justify-content: center;
        background: rgba(10, 16, 12, 0.55);
        backdrop-filter: blur(3px);
        opacity: 0; pointer-events: none;
        transition: opacity 0.2s ease;
    }
    .diagnose-translate-overlay.visible { opacity: 1; pointer-events: all; }
    .diagnose-translate-box {
        background: var(--bg-1, #102013);
        border: 1px solid var(--green, #4ade80);
        border-radius: 16px;
        padding: 28px 32px;
        max-width: 320px;
        text-align: center;
        box-shadow: 0 10px 40px rgba(0,0,0,0.35);
        animation: diagnoseTransPopIn 0.25s ease;
    }
    @keyframes diagnoseTransPopIn {
        from { transform: scale(0.92); opacity: 0; }
        to   { transform: scale(1);    opacity: 1; }
    }
    .diagnose-translate-spinner {
        width: 38px; height: 38px; margin: 0 auto 14px;
        border: 3px solid rgba(74, 222, 128, 0.25);
        border-top-color: var(--green, #4ade80);
        border-radius: 50%;
        animation: diagnoseTransSpin 0.8s linear infinite;
    }
    @keyframes diagnoseTransSpin { to { transform: rotate(360deg); } }
    .diagnose-translate-title {
        color: var(--text-1, #f1f5f1);
        font-weight: 600; font-size: 0.95rem; margin-bottom: 6px;
    }
    .diagnose-translate-sub {
        color: var(--text-3, #94a3a0);
        font-size: 0.78rem; line-height: 1.4;
    }
    .diagnose-translate-dots span {
        display: inline-block; opacity: 0.3;
        animation: diagnoseTransDot 1.2s infinite;
    }
    .diagnose-translate-dots span:nth-child(2) { animation-delay: 0.2s; }
    .diagnose-translate-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes diagnoseTransDot { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
    `;
    document.head.appendChild(style);
}

function showDiagnoseTranslateOverlay(langCode) {
    if (document.getElementById('txTimerOverlay')?.classList.contains('visible')) return;
    ensureDiagnoseTranslateOverlayStyles();
    let overlay = document.getElementById('diagnoseTranslateOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'diagnoseTranslateOverlay';
        overlay.className = 'diagnose-translate-overlay';
        document.body.appendChild(overlay);
    }
    const name = DIAGNOSE_LANG_DISPLAY_NAMES[langCode] || langCode.toUpperCase();
    overlay.innerHTML = `
      <div class="diagnose-translate-box" style="position:relative">
        <button onclick="document.getElementById('diagnoseTranslateOverlay').classList.remove('visible')" style="position:absolute;top:8px;right:10px;background:rgba(255,255,255,.1);border:none;color:#fff;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:1.1rem;display:flex;align-items:center;justify-content:center;transition:background .2s" onmouseover="this.style.background='rgba(248,113,113,.4)'" onmouseout="this.style.background='rgba(255,255,255,.1)'" title="Cancel">&times;</button>
        <div class="diagnose-translate-spinner"></div>
        <div class="diagnose-translate-title">Translating to ${name}<span class="diagnose-translate-dots"><span>.</span><span>.</span><span>.</span></span></div>
        <div class="diagnose-translate-sub">First-time translation can take a few seconds. It'll be instant after this.</div>
      </div>`;
    requestAnimationFrame(() => overlay.classList.add('visible'));
}

function hideDiagnoseTranslateOverlay() {
    const overlay = document.getElementById('diagnoseTranslateOverlay');
    if (overlay) overlay.classList.remove('visible');
}

/* ══════════════════════════════════════════════
   LOAD DIAGNOSE-PAGE TRANSLATIONS FROM SERVER
   Called by the language-switcher in main.js
   (or whenever language changes).
   Exposes window.diagnoseTx for tr().
══════════════════════════════════════════════ */
async function loadDiagnoseTranslations(lang) {
    lang = (lang || getLang()).toLowerCase().trim();
    if (lang === 'en') {
        window.diagnoseTx = {};
        applyDiagnoseStaticTranslations();
        return;
    }

    const isGlobal = document.getElementById('txTimerOverlay')?.classList.contains('visible');
    if (!isGlobal) showDiagnoseTranslateOverlay(lang);

    try {
        const res = await fetch('/api/translate-diagnose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        window.diagnoseTx = data.translations || {};
    } catch (err) {
        console.warn('[DiagnoseTranslate] fetch failed:', err);
        window.diagnoseTx = {};
    }

    applyDiagnoseStaticTranslations();
    if (_lastDiagnosisResult) {
        await renderDiagnosisResults(_lastDiagnosisResult);
    } else {
        resetResultsPanel();
    }
    if (!isGlobal) hideDiagnoseTranslateOverlay();
}

/**
 * Apply translated strings to all static elements on the /diagnose page
 * (upload zone copy, tips, placeholder, button labels, section headers, etc.)
 */
function applyDiagnoseStaticTranslations() {
    // Helper: update text if element exists
    const setText = (sel, key) => {
        const el = document.querySelector(sel);
        if (el) el.textContent = tr(key);
    };
    const setHTML = (sel, key) => {
        const el = document.querySelector(sel);
        if (el) el.innerHTML = tr(key);
    };
    const setPlaceholder = (sel, key) => {
        const el = document.querySelector(sel);
        if (el) el.placeholder = tr(key);
    };

    // ── Upload zone ──
    setText('.upload-title', 'Drop your crop image here');
    setText('.upload-subtitle', 'Supports JPG, PNG, WEBP — max 10 MB');
    setText('.btn-upload-text', 'Upload Photo');
    setText('.btn-camera-text', 'Take Photo');
    setText('#imageReadyText', 'Image ready for analysis');

    // ── Analyze button (when visible) ──
    const analyzeBtn = document.getElementById('analyzeBtn');
    if (analyzeBtn && !analyzeBtn.disabled) {
        const icon = '<i class="fas fa-wand-magic-sparkles"></i> ';
        const shine = '<div class="btn-shine"></div>';
        analyzeBtn.innerHTML = icon + tr('Analyze Crop') + shine;
    }

    // ── Tips card ──
    setText('.photo-tips-title', 'Photo Tips for Best Results');
    const tipEls = document.querySelectorAll('.tip-item');
    const tipKeys = [
        'Focus on the most visibly affected area',
        'Use natural daylight — avoid harsh shadows',
        'Include both healthy and affected parts if possible',
        'Keep the camera steady and close (30–50 cm)'
    ];
    tipEls.forEach((el, i) => { if (tipKeys[i]) el.textContent = tr(tipKeys[i]); });

    // ── How It Works section ──
    setText('.how-it-works-title', 'How It Works');
    const howSteps = document.querySelectorAll('.how-step');
    if (howSteps.length >= 3) {
        const steps = [
            { title: 'Capture or Upload', desc: 'Take a clear photo of the affected crop leaf, stem, or fruit' },
            { title: 'AI Analysis', desc: 'Our AI model analyzes visual patterns to identify diseases with high accuracy' },
            { title: 'Get Remedies', desc: 'Receive eco-friendly and chemical treatment plans with dosage details instantly' }
        ];
        howSteps.forEach((el, i) => {
            const t = el.querySelector('.how-step-title');
            const d = el.querySelector('.how-step-desc');
            if (t && steps[i]) t.textContent = tr(steps[i].title);
            if (d && steps[i]) d.textContent = tr(steps[i].desc);
        });
    }

    // ── Reset placeholder with translated text ──
    if (document.getElementById('resultsPlaceholder')) {
        resetResultsPanel();
    }
}

/* ══════════════════════════════════════════════
   DRAG & DROP
══════════════════════════════════════════════ */
function handleDragOver(e) {
    e.preventDefault();
    e.stopPropagation();
    document.getElementById('uploadZone').classList.add('drag-over');
}

function handleDragLeave(e) {
    document.getElementById('uploadZone').classList.remove('drag-over');
}

function handleDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    document.getElementById('uploadZone').classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith('image/')) {
        processImageFile(file);
    } else {
        showToast(tr('Please drop a valid image file (JPG, PNG, WEBP).'), 'error');
    }
}

/* ══════════════════════════════════════════════
   FILE INPUT CHANGE
══════════════════════════════════════════════ */
function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) processImageFile(file);
}

/* ══════════════════════════════════════════════
   PROCESS IMAGE FILE → read as base64 → preview
══════════════════════════════════════════════ */
function processImageFile(file) {
    if (file.size > 10 * 1024 * 1024) {
        showToast(tr('Image too large. Max 10 MB allowed.'), 'error');
        return;
    }
    const reader = new FileReader();
    reader.onload = function (ev) {
        const dataUrl = ev.target.result;
        currentImageBase64 = dataUrl.split(',')[1];
        showPreview(dataUrl);
    };
    reader.readAsDataURL(file);
}

/* ══════════════════════════════════════════════
   SHOW PREVIEW
══════════════════════════════════════════════ */
function showPreview(dataUrl) {
    const uploadZone = document.getElementById('uploadZone');
    const imagePreview = document.getElementById('imagePreview');
    const previewImg = document.getElementById('previewImg');
    const analyzeBtn = document.getElementById('analyzeBtn');
    const cameraModal = document.getElementById('cameraModal');

    if (uploadZone) uploadZone.style.display = 'none';
    if (cameraModal) cameraModal.style.display = 'none';
    if (imagePreview) {
        imagePreview.style.display = '';
        previewImg.src = dataUrl;
    }
    if (analyzeBtn) {
        analyzeBtn.style.display = '';
        // Translate the button label immediately
        analyzeBtn.innerHTML = `<i class="fas fa-wand-magic-sparkles"></i> ${tr('Analyze Crop')}<div class="btn-shine"></div>`;
    }

    // Reset right panel to translated placeholder
    resetResultsPanel();
}

/* ══════════════════════════════════════════════
   CLEAR IMAGE
══════════════════════════════════════════════ */
function clearImage() {
    currentImageBase64 = null;
    _lastDiagnosisResult = null;

    const uploadZone = document.getElementById('uploadZone');
    const imagePreview = document.getElementById('imagePreview');
    const analyzeBtn = document.getElementById('analyzeBtn');
    const fileInput = document.getElementById('fileInput');

    if (uploadZone) uploadZone.style.display = '';
    if (imagePreview) imagePreview.style.display = 'none';
    if (analyzeBtn) analyzeBtn.style.display = 'none';
    if (fileInput) fileInput.value = '';

    resetResultsPanel();
    closeCamera();
}

/* ══════════════════════════════════════════════
   RESET RIGHT PANEL — fully translated
══════════════════════════════════════════════ */
function resetResultsPanel() {
    const panel = document.getElementById('resultsPanel');
    if (!panel) return;

    const step1 = tr('Upload or capture image');
    const step2 = tr('Click Analyze Crop');
    const step3 = tr('Get instant AI diagnosis');

    panel.innerHTML = `
    <div class="results-placeholder" id="resultsPlaceholder">
      <div class="placeholder-icon"><i class="fas fa-leaf"></i></div>
      <h3>${tr('Upload a crop image to begin diagnosis')}</h3>
      <p>${tr('Our AI will identify the disease and suggest eco-friendly treatments')}</p>
      <div class="placeholder-steps">
        <div class="ps-item"><span class="ps-num">1</span> ${step1}</div>
        <div class="ps-item"><span class="ps-num">2</span> ${step2}</div>
        <div class="ps-item"><span class="ps-num">3</span> ${step3}</div>
      </div>
    </div>`;

    if (remedyChartInst) {
        remedyChartInst.destroy();
        remedyChartInst = null;
    }
}

/* ══════════════════════════════════════════════
   CAMERA
══════════════════════════════════════════════ */
async function openCamera() {
    const modal = document.getElementById('cameraModal');
    const video = document.getElementById('cameraFeed');
    const uploadZone = document.getElementById('uploadZone');

    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }
        });
        video.srcObject = cameraStream;
        if (uploadZone) uploadZone.style.display = 'none';
        if (modal) modal.style.display = '';
        showToast(tr('Camera ready — position your crop in frame.'), 'success');
    } catch (err) {
        console.error('Camera error:', err);
        showToast(tr('Camera access denied or not available.'), 'error');
    }
}

function closeCamera() {
    const modal = document.getElementById('cameraModal');
    const video = document.getElementById('cameraFeed');
    const uploadZone = document.getElementById('uploadZone');

    if (cameraStream) {
        cameraStream.getTracks().forEach(t => t.stop());
        cameraStream = null;
    }
    if (video) video.srcObject = null;
    if (modal) modal.style.display = 'none';

    if (!currentImageBase64 && uploadZone) {
        uploadZone.style.display = '';
    }
}

function capturePhoto() {
    const video = document.getElementById('cameraFeed');
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext('2d').drawImage(video, 0, 0);

    canvas.toBlob(blob => {
        const file = new File([blob], 'capture.jpg', { type: 'image/jpeg' });
        closeCamera();
        processImageFile(file);
        showToast('📸 ' + tr('Photo captured!'), 'success');
    }, 'image/jpeg', 0.92);
}

/* ══════════════════════════════════════════════
   ANALYZE IMAGE  — call Flask /api/diagnose
══════════════════════════════════════════════ */
async function analyzeImage() {
    if (!currentImageBase64) {
        showToast(tr('Please upload or capture a crop image first.'), 'warning');
        return;
    }

    const analyzeBtn = document.getElementById('analyzeBtn');
    const panel = document.getElementById('resultsPanel');

    // ── Loading state on button ──
    if (analyzeBtn) {
        analyzeBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${tr('Analyzing…')}`;
        analyzeBtn.disabled = true;
    }

    // ── Show loader in results panel ── (translated)
    if (panel) {
        const s1 = tr('Scanning image…');
        const s2 = tr('Detecting patterns…');
        const s3 = tr('Finding remedies…');
        panel.innerHTML = `
        <div class="analyzing-loader">
          <div class="ai-loading-ring"></div>
          <p style="color:var(--green);font-weight:700;font-size:1rem">${tr('AI is analyzing your crop…')}</p>
          <p style="color:var(--text-3);font-size:0.82rem;margin-top:4px">${tr('Identifying disease patterns and preparing remedies')}</p>
          <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;justify-content:center">
            ${[`🔍 ${s1}`, `🧬 ${s2}`, `🌿 ${s3}`].map((s, i) => `
              <span style="font-size:0.72rem;padding:4px 11px;background:var(--bg-3);border:1px solid var(--border);
                           border-radius:50px;color:var(--text-3);animation:fadeInUp 0.3s ease ${i * 0.15}s both">${s}</span>
            `).join('')}
          </div>
        </div>`;
    }

    try {
        const hints = extractImageHints(document.getElementById('previewImg'));

        const res = await fetch('/api/diagnose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image: currentImageBase64, hints, lang: getLang() })
        });

        let data;
        try {
            data = await res.json();
        } catch (parseErr) {
            throw new Error(`Server responded ${res.status} (no JSON body)`);
        }
        if (!res.ok || data.error) {
            throw new Error(data.error || `Server responded ${res.status}`);
        }

        // Store raw English result for re-translation on language switch
        _lastDiagnosisResult = data;

        await renderDiagnosisResults(data);
        showToast('✅ ' + tr('Diagnosis complete!'), 'success');

    } catch (err) {
        console.error('Diagnose error:', err);
        showToast(tr('Diagnosis failed. Please try again.'), 'error');
        if (panel) panel.innerHTML = `
          <div class="results-placeholder">
            <div class="placeholder-icon" style="opacity:1;color:var(--red)">
              <i class="fas fa-circle-xmark"></i>
            </div>
            <h3 style="color:var(--red)">${tr('Analysis Failed')}</h3>
            <p>${tr('Could not process the image.')}<br>${tr('Make sure your API key is set and the image is clear.')}</p>
            <button class="btn-secondary" style="margin-top:16px" onclick="analyzeImage()">
              <i class="fas fa-rotate"></i> ${tr('Try Again')}
            </button>
          </div>`;
    } finally {
        if (analyzeBtn) {
            analyzeBtn.innerHTML = `<i class="fas fa-wand-magic-sparkles"></i> ${tr('Analyze Crop')}<div class="btn-shine"></div>`;
            analyzeBtn.disabled = false;
        }
    }
}

/* ══════════════════════════════════════════════
   RENDER DIAGNOSIS RESULTS
   Fetches translated result fields from the server
   before building the HTML so every visible string
   is already in the selected language.
══════════════════════════════════════════════ */
async function renderDiagnosisResults(data) {
    const panel = document.getElementById('resultsPanel');
    if (!panel) return;

    const lang = getLang();

    // ── Fetch translated result fields ──────────────────
    // Skip if the /api/diagnose call already returned the result in the
    // selected language (data._lang matches current lang) to avoid a
    // redundant second round-trip.
    let tx = {};  // map: english_string -> translated_string
    const resultAlreadyTranslated = data._lang && data._lang === lang;
    if (lang !== 'en' && !resultAlreadyTranslated) {
        try {
            const res = await fetch('/api/translate-diagnosis-result', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lang, result: data })
            });
            if (res.ok) {
                const d = await res.json();
                tx = d.translations || {};
            }
        } catch (e) {
            console.warn('[DiagnoseResult] translation fetch failed:', e);
        }
    }

    // Helper: translate a result field string
    const tField = (val) => {
        if (!val) return val;
        const v = val.trim();
        return tx[v] || v;
    };

    // ── Build translated data object ─────────────────────
    const disease = tField(data.disease) || tr('Unknown Disease');
    const severity = tField(data.severity) || '';
    const affectedPart = tField(data.affected_part) || '';
    const cause = tField(data.cause) || '';
    const timeline = tField(data.recovery_timeline) || '';

    const ecoList = (data.eco_remedies || []).map(r => ({
        remedy: tField(r.remedy),
        method: tField(r.method),
        frequency: tField(r.frequency),
        effectiveness: r.effectiveness
    }));
    const chemList = (data.chemical_remedies || []).map(c => ({
        name: tField(c.name),
        dose: tField(c.dose),
        interval: tField(c.interval)
    }));
    const prevList = (data.prevention || []).map(tip => tField(tip));

    // ── UI label translations ───────────────────────────
    const lCause = tr('Cause');
    const lTimeline = tr('Recovery Timeline');
    const lEco = tr('Eco-Friendly Remedies');
    const lRecommended = tr('RECOMMENDED');
    const lChart = tr('Remedy Effectiveness Chart');
    const lChem = tr('Chemical Treatment Options');
    const lPrev = tr('Prevention Tips');
    const lConf = tr('Confidence');
    const lSev = tr('Severity');
    const lEffect = tr('effectiveness');
    const lDisclaimer = tr('AI-generated diagnosis for guidance only. Consult a local agronomist for critical crop decisions.');

    const sevClass = `badge-severity-${(data.severity || 'mild').toLowerCase()}`;
    const isHealthy = (data.disease || '').toLowerCase().includes('healthy');

    panel.innerHTML = `
    <div class="results-content">

      <!-- Header -->
      <div class="result-header">
        <div class="result-disease-name">
          ${isHealthy ? '✅' : '🔬'} ${disease}
        </div>
        <div class="result-meta">
          <span class="result-badge badge-confidence">
            <i class="fas fa-circle-check"></i> ${data.confidence || 0}% ${lConf}
          </span>
          ${severity ? `<span class="result-badge ${sevClass}">${severity} ${lSev}</span>` : ''}
          ${affectedPart ? `
          <span class="result-badge" style="background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.2);color:var(--amber)">
            <i class="fas fa-leaf"></i> ${affectedPart}
          </span>` : ''}
        </div>
      </div>

      <div class="result-body">

        <!-- Cause -->
        ${cause ? `
          <div class="result-section">
            <h4><i class="fas fa-circle-info"></i> ${lCause}</h4>
            <div class="result-cause">${cause}</div>
          </div>` : ''}

        <!-- Recovery Timeline -->
        ${timeline ? `
          <div class="result-section">
            <h4><i class="fas fa-clock-rotate-left"></i> ${lTimeline}</h4>
            <div class="result-timeline">
              <i class="fas fa-calendar-check"></i> ${timeline}
            </div>
          </div>` : ''}

        <!-- Eco Remedies -->
        ${ecoList.length > 0 ? `
          <div class="result-section">
            <h4>
              <i class="fas fa-leaf"></i> ${lEco}
              <span style="font-size:0.68rem;padding:2px 8px;background:rgba(74,222,128,0.1);
                           color:var(--green);border-radius:50px;border:1px solid rgba(74,222,128,0.2);
                           margin-left:4px;font-weight:700">${lRecommended}</span>
            </h4>
            <div class="eco-remedies">
              ${ecoList.map(r => `
                <div class="eco-remedy-card">
                  <div class="eco-remedy-name">🌿 ${r.remedy}</div>
                  <div class="eco-remedy-method">
                    <i class="fas fa-hand-dots" style="color:var(--teal);margin-right:4px"></i>${r.method}
                  </div>
                  <div class="eco-remedy-freq">
                    <i class="fas fa-rotate" style="color:var(--text-3);margin-right:4px"></i>${r.frequency}
                  </div>
                  <div class="eco-effectiveness">
                    <div class="eco-effectiveness-bar" style="width:0%" data-target="${r.effectiveness || 75}%"></div>
                  </div>
                  <div style="font-size:0.68rem;color:var(--text-3);margin-top:2px">${r.effectiveness || 75}% ${lEffect}</div>
                </div>`).join('')}
            </div>
          </div>` : ''}

        <!-- Remedy Effectiveness Chart -->
        ${ecoList.length > 0 ? `
          <div class="result-section">
            <h4><i class="fas fa-chart-bar"></i> ${lChart}</h4>
            <div class="remedy-chart-wrap"><canvas id="remedyChart"></canvas></div>
          </div>` : ''}

        <!-- Chemical Treatment -->
        ${chemList.length > 0 ? `
          <div class="result-section">
            <h4><i class="fas fa-flask"></i> ${lChem}</h4>
            <div class="chemical-remedies">
              ${chemList.map(c => `
                <div class="chem-item">
                  <span class="chem-name">⚗️ ${c.name}</span>
                  <span class="chem-dose">${c.dose}</span>
                  <span style="font-size:0.72rem;color:var(--text-3)">${c.interval}</span>
                </div>`).join('')}
            </div>
          </div>` : ''}

        <!-- Prevention Tips -->
        ${prevList.length > 0 ? `
          <div class="result-section">
            <h4><i class="fas fa-shield-halved"></i> ${lPrev}</h4>
            <ul class="prevention-list">
              ${prevList.map(tip => `<li>${tip}</li>`).join('')}
            </ul>
          </div>` : ''}

        <!-- Disclaimer -->
        <div style="padding:10px 14px;background:rgba(251,191,36,0.05);
                    border:1px solid rgba(251,191,36,0.15);border-radius:8px;
                    font-size:0.72rem;color:var(--text-3);line-height:1.5">
          <i class="fas fa-circle-info" style="color:var(--amber);margin-right:4px"></i>
          ${lDisclaimer}
        </div>

      </div><!-- /.result-body -->
    </div><!-- /.results-content -->`;

    // Animate effectiveness bars after paint
    setTimeout(() => {
        document.querySelectorAll('.eco-effectiveness-bar').forEach(bar => {
            bar.style.width = bar.dataset.target;
        });
    }, 300);

    // Draw chart with translated remedy names
    if (ecoList.length > 0) {
        setTimeout(() => buildRemedyChart(ecoList), 450);
    }
}

/* ══════════════════════════════════════════════
   REMEDY BAR CHART
══════════════════════════════════════════════ */
function buildRemedyChart(remedies) {
    const canvas = document.getElementById('remedyChart');
    if (!canvas) return;
    if (remedyChartInst) { remedyChartInst.destroy(); remedyChartInst = null; }

    const labels = remedies.map(r => r.remedy);
    const values = remedies.map(r => r.effectiveness || 75);
    const COLORS = ['rgba(74,222,128,0.75)', 'rgba(45,212,191,0.75)', 'rgba(34,197,94,0.75)', 'rgba(16,185,129,0.75)'];
    const effLabel = tr('effectiveness');

    remedyChartInst = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: `% ${effLabel}`,
                data: values,
                backgroundColor: COLORS.slice(0, values.length),
                borderColor: COLORS.slice(0, values.length).map(c => c.replace('0.75', '1')),
                borderWidth: 1,
                borderRadius: 6,
                borderSkipped: false
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: 'y',
            scales: {
                x: {
                    min: 0, max: 100,
                    grid: { color: 'rgba(74,222,128,0.06)' },
                    ticks: { color: '#6b8c6c', callback: v => v + '%', font: { size: 10 } },
                    border: { color: 'rgba(74,222,128,0.1)' }
                },
                y: {
                    grid: { display: false },
                    ticks: { color: '#a7c4a8', font: { size: 11 } },
                    border: { display: false }
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
                    callbacks: { label: ctx => ` ${ctx.raw}% ${effLabel}` }
                }
            },
            animation: { duration: 800, easing: 'easeOutQuart' }
        }
    });
}

/* ── Stop camera on page leave ─── */
window.addEventListener('beforeunload', () => {
    if (cameraStream) cameraStream.getTracks().forEach(t => t.stop());
});

/* ══════════════════════════════════════════════
   HOOK INTO LANGUAGE SWITCHER
   main.js / translations.js fires a custom event
   'langChanged' (or calls window.onLangChange).
   We listen here and reload translations.
══════════════════════════════════════════════ */
document.addEventListener('langChanged', async (e) => {
    const lang = e.detail?.lang || getLang();
    window.currentLang = lang;
    await loadDiagnoseTranslations(lang);
});

// Also expose as a direct hook in case main.js calls it
window.onDiagnoseLangChange = loadDiagnoseTranslations;

/* ── Init on page load ────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
    const lang = getLang();
    if (lang !== 'en') {
        loadDiagnoseTranslations(lang);
    }
});
document.addEventListener('langChanged', (e) => {
    const lang = (e.detail && e.detail.lang) || getLang();
    loadDiagnoseTranslations(lang);
});
/* ══════════════════════════════════════════════
   IMAGE COLOUR HINTS FOR GROQ
══════════════════════════════════════════════ */
function extractImageHints(imgEl) {
    if (!imgEl) return {};
    try {
        const canvas = document.createElement('canvas');
        canvas.width = 100;
        canvas.height = 100;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(imgEl, 0, 0, 100, 100);
        const data = ctx.getImageData(0, 0, 100, 100).data;

        let r = 0, g = 0, b = 0, darkPixels = 0, yellowPixels = 0, brownPixels = 0, whitePixels = 0;
        const total = data.length / 4;

        for (let i = 0; i < data.length; i += 4) {
            const pr = data[i], pg = data[i + 1], pb = data[i + 2];
            r += pr; g += pg; b += pb;
            const brightness = (pr + pg + pb) / 3;
            if (brightness < 60) darkPixels++;
            if (pr > 180 && pg > 180 && pb < 80) yellowPixels++;
            if (pr > 120 && pg < 90 && pb < 70) brownPixels++;
            if (pr > 200 && pg > 200 && pb > 200) whitePixels++;
        }

        r = Math.round(r / total);
        g = Math.round(g / total);
        b = Math.round(b / total);

        return {
            colors: `RGB(${r},${g},${b}) - dominant: ${r > g && r > b ? 'red/brown' : g > r && g > b ? 'green' : b > r && b > g ? 'blue' : 'mixed'}`,
            dark_spots: darkPixels / total > 0.15,
            yellow: yellowPixels / total > 0.10,
            brown: brownPixels / total > 0.10,
            white_powder: whitePixels / total > 0.20,
            wilting: g < 80 && r > 100,
            green_ratio: Math.round((g / (r + g + b + 1)) * 100) + '%'
        };
    } catch (e) {
        return { colors: 'unknown' };
    }
}