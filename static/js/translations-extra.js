/* ════════════════════════════════════════════════════════════
   translations-extra.js
   Load this RIGHT AFTER translations.js (same script-tag style).

   - Fills every key that is missing, empty, or still the English
     text, for the languages listed in EXTRA below (Hindi, Bengali).
     Existing translations are never overwritten.
   - Exposes window.mergeTranslations(), which the auto-generated
     translations-generated.js uses for the other languages.
════════════════════════════════════════════════════════════ */
(function () {
    if (typeof T === 'undefined') return;   // translations.js not loaded yet
    var EN = T.en;

    EN.kw_lang_subtitle = 'Ask Kisan Helper in any of 23 languages';

    window.mergeTranslations = function (extra) {
        Object.keys(extra).forEach(function (lang) {
            if (!T[lang]) T[lang] = {};
            Object.keys(extra[lang]).forEach(function (key) {
                var cur = T[lang][key];
                if (cur === undefined || cur === '' || cur === EN[key]) {
                    T[lang][key] = extra[lang][key];
                }
            });
        });
    };

    var EXTRA = {

        hi: {
            kw_ask_any_language: 'किसी भी भाषा में पूछें',
            kw_chat_history: 'चैट इतिहास',
            kw_share_chat: 'चैट शेयर करें',
            kw_new_chat: 'नई चैट',
            kw_close: 'बंद करें',
            kw_session_only: 'केवल इस सत्र के लिए • बंद करने पर मिट जाएगा',
            kw_clear_all: 'सब मिटाएं',
            kw_back: 'वापस',
            kw_lang_subtitle: 'किसान हेल्पर से 23 भाषाओं में से किसी में भी पूछें',
            kw_lang_search_placeholder: 'भाषा खोजें (जैसे Hindi, English, বাংলা, తెలుగు)...',
            kw_voice: 'आवाज़',
            kw_type_or_speak: 'लिखें या बोलें...',
            kw_skip_continue_in: 'छोड़ें — जारी रखें: ',
            offline_title: 'आप ऑफ़लाइन हैं',
            offline_message: 'इंटरनेट कनेक्शन नहीं है। कृपया अपना नेटवर्क जांचें और फिर कोशिश करें।',
            offline_retry: 'फिर कोशिश करें',
            market_hero_badge: 'लाइव मंडी जानकारी',
            market_hero_title: 'फसल बाजार भाव',
            market_search_placeholder: 'शहर खोजें (जैसे दिल्ली, मुंबई, कोलकाता...)',
            theme_toggle_title: 'दिन/रात थीम बदलें',
            lang_search_placeholder: 'भाषा खोजें...',
            btn_install: 'ऐप इंस्टॉल करें',
            opt_top_crops: 'प्रमुख फसलें',
            opt_all_commodities: 'सभी जिंस',
            disclaimer_label: '⚠️ अस्वीकरण:',
            disclaimer_text: 'SmartAgro केवल सामान्य जानकारी देता है — यह पूर्ण निदान या सिफारिश नहीं है।',
            footer_copy: '© 2026 SmartAgro. भारत के किसानों के लिए बनाया गया।',
            nav_home_short: 'होम',
            nav_diagnose_short: 'निदान',
            nav_market_short: 'मंडी भाव',
            quick_jump_risk_overview: 'जोखिम स्तर अवलोकन',
            quick_jump_crop_risk: 'फसल जोखिम पूर्वानुमान',
            best_harvest_day: 'कटाई का सबसे अच्छा दिन',
            analyzing_safe_windows: 'सुरक्षित समय का विश्लेषण हो रहा है...',
            month_risk_outlook: 'अगले 15 दिनों का जोखिम अनुमान',
            month_risk_outlook_sub: 'आने वाले दिनों का असली पूर्वानुमान रुझान',
            analyzing_30day_trends: '30 दिनों के रुझान का विश्लेषण हो रहा है...',
            seasonal_alerts_title: 'मौसमी अलर्ट',
            seasonal_alerts_sub: 'वर्तमान मौसम के लिए सामान्य सलाह',
            generating_seasonal_advisory: 'मौसमी सलाह तैयार हो रही है...',
            awaiting_location: 'लोकेशन की प्रतीक्षा है...',
            quick_jump_satellite: 'सैटेलाइट दृश्य और वनस्पति स्वास्थ्य',
            section_map: 'सैटेलाइट दृश्य और वनस्पति स्वास्थ्य',
            section_map_sub: 'आपकी लोकेशन का लाइव पर्यावरण संदर्भ',
            quick_jump_crop_recs: 'फसल सिफारिशें',
            quick_jump_advisory_calendar: 'फसल सलाह कैलेंडर',
            btn_price_graph: 'भाव ग्राफ',
            btn_comparison_table: 'तुलना तालिका',
            quick_jump_risk: 'जोखिम में फसलें',
            quick_jump_safe: 'उगाने के लिए सुरक्षित',
            btn_check_upcoming: 'आने वाले जोखिम जांचें (6 दिन का पूर्वानुमान)',
            crop_risk_title: 'फसल जोखिम पूर्वानुमान',
            crop_risk_sub: 'आपकी सुझाई गई फसलें अगले 6 दिनों में कैसी रहेंगी',
            btn_notifications: 'सूचनाएं'
        },

        bn: {
            step_upload: 'ছবি আপলোড করুন বা তুলুন',
            step_click: 'ফসল বিশ্লেষণে ক্লিক করুন',
            step_result: 'তাৎক্ষণিক AI নির্ণয় পান',
            kw_ask_any_language: 'যেকোনো ভাষায় জিজ্ঞাসা করুন',
            kw_chat_history: 'চ্যাট ইতিহাস',
            kw_share_chat: 'চ্যাট শেয়ার করুন',
            kw_new_chat: 'নতুন চ্যাট',
            kw_close: 'বন্ধ করুন',
            kw_session_only: 'শুধু এই সেশনের জন্য • বন্ধ করলে মুছে যাবে',
            kw_clear_all: 'সব মুছুন',
            kw_back: 'ফিরে যান',
            kw_lang_subtitle: 'কিষান হেল্পারকে ২৩টি ভাষার যেকোনোটিতে জিজ্ঞাসা করুন',
            kw_lang_search_placeholder: 'ভাষা খুঁজুন (যেমন Hindi, English, বাংলা, తెలుగు)...',
            kw_voice: 'ভয়েস',
            kw_type_or_speak: 'লিখুন বা বলুন...',
            kw_skip_continue_in: 'এড়িয়ে যান — চালিয়ে যান: ',
            offline_title: 'আপনি অফলাইনে আছেন',
            offline_message: 'ইন্টারনেট সংযোগ নেই। অনুগ্রহ করে নেটওয়ার্ক পরীক্ষা করে আবার চেষ্টা করুন।',
            offline_retry: 'আবার চেষ্টা করুন',
            market_hero_badge: 'লাইভ মান্ডি তথ্য',
            market_hero_title: 'ফসলের বাজার মূল্য',
            market_search_placeholder: 'শহর খুঁজুন (যেমন দিল্লি, মুম্বাই, কলকাতা...)',
            theme_toggle_title: 'দিন/রাত থিম বদলান',
            lang_search_placeholder: 'ভাষা খুঁজুন...',
            btn_install: 'অ্যাপ ইনস্টল করুন',
            opt_top_crops: 'প্রধান ফসল',
            opt_all_commodities: 'সব পণ্য',
            disclaimer_label: '⚠️ দাবিত্যাগ:',
            disclaimer_text: 'SmartAgro শুধু সাধারণ তথ্য দেয় — এটি পূর্ণাঙ্গ রোগ নির্ণয় বা সুপারিশ নয়।',
            footer_copy: '© 2026 SmartAgro. ভারতের কৃষকদের জন্য তৈরি।',
            nav_home_short: 'হোম',
            nav_diagnose_short: 'নির্ণয়',
            nav_market_short: 'মান্ডি মূল্য',
            quick_jump_risk_overview: 'ঝুঁকি স্তরের পর্যালোচনা',
            quick_jump_crop_risk: 'ফসলের ঝুঁকির পূর্বাভাস',
            best_harvest_day: 'ফসল কাটার সেরা দিন',
            analyzing_safe_windows: 'নিরাপদ সময় বিশ্লেষণ করা হচ্ছে...',
            month_risk_outlook: 'পরবর্তী ১৫ দিনের ঝুঁকির পূর্বাভাস',
            month_risk_outlook_sub: 'আগামী দিনগুলোর প্রকৃত পূর্বাভাসের প্রবণতা',
            analyzing_30day_trends: '৩০ দিনের প্রবণতা বিশ্লেষণ করা হচ্ছে...',
            seasonal_alerts_title: 'মৌসুমি সতর্কতা',
            seasonal_alerts_sub: 'বর্তমান মৌসুমের জন্য সাধারণ পরামর্শ',
            generating_seasonal_advisory: 'মৌসুমি পরামর্শ তৈরি হচ্ছে...',
            awaiting_location: 'অবস্থানের অপেক্ষায়...',
            quick_jump_satellite: 'স্যাটেলাইট দৃশ্য ও উদ্ভিদের স্বাস্থ্য',
            section_map: 'স্যাটেলাইট দৃশ্য ও উদ্ভিদের স্বাস্থ্য',
            section_map_sub: 'আপনার অবস্থানের লাইভ পরিবেশগত তথ্য',
            quick_jump_crop_recs: 'ফসলের সুপারিশ',
            quick_jump_advisory_calendar: 'ফসল পরামর্শ ক্যালেন্ডার',
            btn_price_graph: 'মূল্য গ্রাফ',
            btn_comparison_table: 'তুলনা সারণি',
            quick_jump_risk: 'ঝুঁকিতে থাকা ফসল',
            quick_jump_safe: 'চাষের জন্য নিরাপদ',
            btn_check_upcoming: 'আসন্ন ঝুঁকি দেখুন (৬ দিনের পূর্বাভাস)',
            crop_risk_title: 'ফসলের ঝুঁকির পূর্বাভাস',
            crop_risk_sub: 'আপনার প্রস্তাবিত ফসল আগামী ৬ দিনে কেমন থাকবে',
            btn_notifications: 'বিজ্ঞপ্তি'
        }
    };

    window.mergeTranslations(EXTRA);
})();