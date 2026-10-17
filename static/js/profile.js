/* =========================================================================
   profile.js — SmartAgro User Profile Manager
   -------------------------------------------------------------------------
   Lightweight, non-intrusive farmer profile stored in localStorage.
   Never blocks the user on first visit.
   Accessible via the navbar profile badge anytime.
   ========================================================================= */

window.SmartAgroProfile = (function () {

  var PROFILE_KEY = 'smartagro_user_profile';

  function getProfile() {
    try {
      var raw = localStorage.getItem(PROFILE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function saveProfile(profile) {
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(profile)); } catch (e) {}
  }

  function renderBadge() {
    var badge = document.getElementById('userProfileBadge');
    if (!badge) return;
    var profile = getProfile();
    if (!profile || !profile.name) {
      // Unobtrusive guest badge — clickable, doesn't block
      badge.innerHTML =
        '<div class="upb-avatar" title="Farmer Profile" id="userProfileAvatarBtn" style="background:rgba(255,255,255,0.08);color:var(--text-2,#a7c4a8)"><i class="fas fa-user"></i></div>'
        + '<span class="upb-name" id="userProfileNameSpan" style="color:var(--text-3,#6b8c6c)">Profile</span>';
      badge.style.display = 'flex';
    } else {
      var initials = profile.name.trim().split(' ').map(function(w) { return w[0]; }).slice(0, 2).join('').toUpperCase() || 'FA';
      badge.innerHTML =
        '<div class="upb-avatar" title="' + profile.name + '" id="userProfileAvatarBtn">' + initials + '</div>'
        + '<span class="upb-name" id="userProfileNameSpan">' + profile.name.split(' ')[0] + '</span>';
      badge.style.display = 'flex';
    }

    var avatarBtn = document.getElementById('userProfileAvatarBtn');
    if (avatarBtn) avatarBtn.onclick = function() { handleProfileClick(); };
    var nameSpan = document.getElementById('userProfileNameSpan');
    if (nameSpan) nameSpan.onclick = function() { handleProfileClick(); };
  }

  function handleProfileClick() {
    var profile = getProfile();
    if (profile && profile.name) {
      showProfileCard();
    } else {
      showOnboardingModal(false);
    }
  }

  function showProfileCard() {
    var profile = getProfile();
    if (!profile) return;
    var existing = document.getElementById('profileCardPopup');
    if (existing) { existing.remove(); return; }

    var popup = document.createElement('div');
    popup.id = 'profileCardPopup';
    popup.className = 'profile-card-popup';
    var alreadyInstalled = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone === true;
    var initials = profile.name ? profile.name.trim().split(' ').map(function(w){return w[0];}).slice(0,2).join('').toUpperCase() : '?';

    popup.innerHTML =
      '<div class="pcp-avatar">' + initials + '</div>'
      + '<div class="pcp-name">' + (profile.name || 'Farmer') + '</div>'
      + (profile.mobile ? '<div class="pcp-detail"><i class="fas fa-phone"></i> +91 ' + profile.mobile + '</div>' : '')
      + (profile.age    ? '<div class="pcp-detail"><i class="fas fa-cake-candles"></i> Age: ' + profile.age + '</div>' : '')
      + (profile.state  ? '<div class="pcp-detail"><i class="fas fa-location-dot"></i> ' + profile.state + '</div>' : '')
      + '<div class="pcp-actions-row">'
      + '<button class="pcp-edit-btn" id="pcpEditBtn"><i class="fas fa-pen"></i> Edit</button>'
      + '<button class="pcp-settings-btn" id="pcpSettingsBtn"><i class="fas fa-gear"></i> Settings</button>'
      + '</div>'
      + '<div class="pcp-actions-row" style="margin-top:6px">'
      + (alreadyInstalled ? '' : '<button class="pcp-install-btn" id="pcpInstallBtn"><i class="fas fa-download"></i> Install App</button>')
      + '<button class="pcp-logout-btn" id="pcpLogoutBtn"><i class="fas fa-right-from-bracket"></i> Reset</button>'
      + '</div>';

    document.body.appendChild(popup);

    var badge = document.getElementById('userProfileBadge');
    if (badge) {
      var rect = badge.getBoundingClientRect();
      popup.style.top  = (rect.bottom + 8) + 'px';
      popup.style.right = Math.max(12, (window.innerWidth - rect.right)) + 'px';
    }

    document.getElementById('pcpEditBtn').addEventListener('click', function() {
      popup.remove();
      showOnboardingModal(true);
    });

    var settingsBtnInPopup = document.getElementById('pcpSettingsBtn');
    if (settingsBtnInPopup) {
      settingsBtnInPopup.addEventListener('click', function() {
        popup.remove();
        if (window.SmartAgroSettings && typeof window.SmartAgroSettings.openModal === 'function') {
          window.SmartAgroSettings.openModal();
        }
      });
    }

    var installBtnInPopup = document.getElementById('pcpInstallBtn');
    if (installBtnInPopup) {
      installBtnInPopup.addEventListener('click', function() {
        popup.remove();
        var installBtn = document.getElementById('installBtn');
        if (installBtn) installBtn.click();
      });
    }

    var logoutBtnInPopup = document.getElementById('pcpLogoutBtn');
    if (logoutBtnInPopup) {
      logoutBtnInPopup.addEventListener('click', function() {
        if (!confirm('Are you sure you want to clear your saved profile details?')) return;
        popup.remove();
        try { localStorage.removeItem(PROFILE_KEY); } catch(e) {}
        renderBadge();
        if (typeof showToast === 'function') showToast('Profile cleared', 'info');
      });
    }

    // Close on outside click
    setTimeout(function() {
      document.addEventListener('click', function handler(e) {
        if (!popup.contains(e.target) && e.target.id !== 'userProfileAvatarBtn' && e.target.id !== 'userProfileNameSpan') {
          popup.remove();
          document.removeEventListener('click', handler);
        }
      });
    }, 100);
  }

  function showOnboardingModal(isEdit) {
    isEdit = isEdit || false;
    var existingProfile = getProfile() || {};
    var existing = document.getElementById('onboardingModal');
    if (existing) existing.remove();

    var modal = document.createElement('div');
    modal.id = 'onboardingModal';
    modal.className = 'onboarding-overlay';
    modal.innerHTML =
      '<div class="onboarding-panel" style="position:relative;">'
      + '<button class="ob-btn-close" id="obCloseBtn" title="Close" aria-label="Close" style="position:absolute;top:16px;right:16px;background:transparent;border:none;color:var(--text-3,#888);font-size:1.2rem;cursor:pointer;padding:4px 8px;border-radius:6px;"><i class="fas fa-times"></i></button>'
      + '<div class="onboarding-header">'
      + '<div class="onboarding-logo"><i class="fas fa-seedling"></i></div>'
      + '<h2 class="onboarding-title">' + (isEdit ? 'Edit <span>Profile</span>' : 'Farmer <span>Profile</span>') + '</h2>'
      + '<p class="onboarding-sub">' + (isEdit ? 'Update your farming preferences' : 'Personalize your farming advisory (optional)') + '</p>'
      + '</div>'
      + '<div class="ob-step" id="obStep1">'
      + '<div class="ob-field">'
      + '<label class="ob-label"><i class="fas fa-user"></i> Full Name <span style="font-size:0.75rem;color:var(--text-3,#888);font-weight:normal;">(optional)</span></label>'
      + '<input type="text" id="obName" class="ob-input" placeholder="e.g. Ramesh Kumar" maxlength="60" value="' + (existingProfile.name || '') + '">'
      + '</div>'
      + '<div class="ob-field">'
      + '<label class="ob-label"><i class="fas fa-phone"></i> Mobile Number <span class="ob-optional">(optional)</span></label>'
      + '<div class="ob-phone-wrap"><span class="ob-phone-prefix">+91</span><input type="tel" id="obMobile" class="ob-input ob-phone-input" placeholder="10-digit number" maxlength="10" value="' + (existingProfile.mobile || '') + '"></div>'
      + '</div>'
      + '<div class="ob-field">'
      + '<label class="ob-label"><i class="fas fa-map-pin"></i> State / Region <span class="ob-optional">(optional)</span></label>'
      + '<input type="text" id="obState" class="ob-input" placeholder="e.g. Maharashtra, Punjab, UP" value="' + (existingProfile.state || '') + '">'
      + '</div>'
      + '<div class="ob-error" id="obStep1Error" style="display:none"></div>'
      + '<div class="ob-btns-row" style="margin-top:20px;">'
      + '<button class="ob-btn-skip" id="obCancelBtn"><i class="fas fa-times"></i> Dismiss</button>'
      + '<button class="ob-btn-submit" id="obSaveBtn"><i class="fas fa-check"></i> ' + (isEdit ? 'Save Changes' : 'Save Profile') + '</button>'
      + '</div>'
      + '</div>'
      + '</div>';

    document.body.appendChild(modal);
    requestAnimationFrame(function() { modal.classList.add('visible'); });

    function closeModal() {
      modal.classList.remove('visible');
      setTimeout(function() { modal.remove(); }, 300);
    }

    var closeBtn = document.getElementById('obCloseBtn');
    if (closeBtn) closeBtn.onclick = closeModal;
    var cancelBtn = document.getElementById('obCancelBtn');
    if (cancelBtn) cancelBtn.onclick = closeModal;

    modal.addEventListener('click', function(e) {
      if (e.target === modal) closeModal();
    });

    document.getElementById('obSaveBtn').addEventListener('click', function() {
      var name = document.getElementById('obName').value.trim();
      var mobile = document.getElementById('obMobile').value.trim();
      var state = document.getElementById('obState').value.trim();
      var err = document.getElementById('obStep1Error');

      if (mobile && !/^\d{10}$/.test(mobile)) {
        err.textContent = 'Please enter a valid 10-digit mobile number, or leave it blank.';
        err.style.display = '';
        return;
      }

      err.style.display = 'none';
      var profile = {
        name: name || (mobile ? 'Farmer' : ''),
        mobile: mobile || null,
        state: state || null,
        country: 'India',
        updatedAt: Date.now()
      };

      if (profile.name) {
        saveProfile(profile);
        if (typeof showToast === 'function') showToast('🌾 Profile updated successfully!', 'success');
      } else {
        try { localStorage.removeItem(PROFILE_KEY); } catch(e) {}
      }

      closeModal();
      renderBadge();
    });
  }

  function initProfile() {
    renderBadge();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initProfile);
  } else {
    initProfile();
  }

  return { getProfile: getProfile, renderBadge: renderBadge, showOnboardingModal: showOnboardingModal, showProfileCard: showProfileCard };

})();
