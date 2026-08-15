/* ═══════════════════════════════════════════════════════════════
   market_translate.js — SmartAgro
   NOTE: Translation is now fully handled by market.js
   (loadTranslations → cache → reRenderMarket → overlay).
   This file is kept for backward-compatibility only —
   any external code that calls window.tMarket / window.tDemand
   / window.initMarketTranslation / window.reTranslateMarket
   will continue to work correctly via the stubs below.
═══════════════════════════════════════════════════════════════ */

(function() {
    'use strict';

    window.tMarket = function(key) {
        if (typeof tCrop === 'function') return tCrop(key);
        return key;
    };

    window.tDemand = function(key) {
        if (typeof tDemand === 'function') return tDemand(key);
        return key;
    };

    window.applyMarketTranslations = function() {};
    window.initMarketTranslation = function() {};
    window.reTranslateMarket = function() {};

})();