/* Custom home screen for LG webOS — com.custom.launcher
 *
 * Layout (Netflix-style, 1920x1080):
 *   fixed top    clock · weather / Gemini / audio pills, and a billboard that describes whatever is
 *                focused (app, lamp, room, …) in big type with the app's colour
 *   rows below   Zuletzt & Favoriten · Smart Home · one row per app category. The focused row slides
 *                up under the billboard; long rows slide sideways. The focused card grows and its
 *                neighbours make room.
 *
 * Data sources
 *   data/apps.json + data/icons/   written by launcherd.py (root helper; this app is sandboxed)
 *   data/usage.json                launch statistics ({id: {n: decayed count, t: last use}})
 *   luna eim/getAllInputStatus     live HDMI names + connection state (allowed for this app)
 *   http://127.0.0.1:8080          backend on the TV: smart home (WiZ/Tapo), weather, Gemini
 *   localStorage "launcher.v3"     favourites, hidden apps, main room
 */
(() => {
  'use strict';

  document.body.classList.add('booting');

  const APP_ID = 'com.custom.launcher';
  const LG_HOME = 'com.webos.app.home';
  const LIVE_TV = 'com.webos.app.livetv';
  const SETTINGS = 'com.palm.app.settings';
  const MORE = 'util:more';
  const WETTER_APP = 'app:wetteronline';
  const DISPLAY_TILE = 'sh:display';
  const AMBIENT_TILE = 'sh:spotify';   // Spotify-Licht: a WiZ lamp takes the cover colour (backend spotify_ambient.py)
  const SAM = 'luna://com.webos.applicationManager/';
  const HB_EXEC = 'luna://org.webosbrew.hbchannel.service/exec';
  const BACKEND = 'http://127.0.0.1:8080';
  const IS_TV = typeof window.PalmServiceBridge !== 'undefined';
  const APP_DIR = decodeURIComponent(location.pathname).replace(/\/[^/]*$/, '');
  const STORE_KEY = 'launcher.v3';
  const SMART_CACHE_KEY = 'launcher.smart';
  const HALF_LIFE = 45 * 86400;    // usage counts fade by half every 45 days
  const FAVS = 7;                  // cards in the first row (last used + favourites)
  const DEFAULT_ROOM = '';                 // '' = first room in devices.json
  const HOLD_MS = 600;             // long-press OK
  const IDLE_MS = 60000;           // dim after 1 min without input
  const SCREENSAVER_MS = 240000;   // pure black screensaver after 4 min without input
  const SHIFT_MS = 90000;          // pixel shift interval (OLED care)
  const VIEW_W = 1920, SIDE = 96;
  const BB_REST_MS = 300;          // billboard text + colour light follow the focus once it rests this long (cards move .4 s;
                                   // repainting the light while they still move costs frames)

  // app categories (rows); anything unknown lands in "Weitere Apps"
  const CATEGORIES = [
    { key: 'inputs', title: 'Eingänge', kinds: ['input'] },
    { key: 'streaming', title: 'Streaming', ids: ['netflix', 'amazon', 'com.disney.disneyplus-prod', 'com.apple.appletv',
      'youtube.leanback.v4', 'com.skygo.app.de.q', 'ui30', 'dazn', 'com.wow.app', 'paramountplus'] },
    { key: 'tv', title: 'TV & Mediatheken', ids: ['ard.mediathek', 'zdf', 'com.netrtl.tvnow', 'joyn', 'de.telekom.magentatv',
      'hdp.opapp', 'com.webos.app.lgchannels', 'waipu.tv', 'zattoo'] },
    { key: 'play', title: 'Musik & Gaming', ids: ['spotify-beehive', 'com.microsoft.xbox', 'com.limelight.webos',
      'com.webos.app.mediadiscovery', 'com.nvidia.geforcenow', 'com.valvesoftware.steamlink', 'tunein', 'deezer'] },
    { key: 'tools', title: 'Werkzeuge', ids: ['com.webos.app.browser', 'com.webos.app.discovery', 'org.webosbrew.hbchannel',
      'ooo.lew.lifesgoodwithoutspying', 'amazon.alexa.view', 'com.webos.app.camera', 'com.webos.app.homeconnect',
      'com.webos.app.lifeonscreen', 'com.webos.app.multiviewsettings', 'com.webos.app.sportsteamsettings', WETTER_APP], end: [MORE] },
    { key: 'other', title: 'Weitere Apps', rest: true }
  ];
  const APP_DESC = {
    netflix: 'Serien und Filme', amazon: 'Filme, Serien und Prime Originals', 'com.disney.disneyplus-prod': 'Disney, Pixar, Marvel, Star Wars',
    'com.apple.appletv': 'Apple Originals und Filme', 'youtube.leanback.v4': 'Videos, Musik und Livestreams', 'com.skygo.app.de.q': 'Sky Serien, Filme und Sport',
    ui30: 'Filme leihen und kaufen', 'ard.mediathek': 'Das Erste und die ARD-Sender', 'com.netrtl.tvnow': 'RTL+ Serien, Shows und Live-TV',
    joyn: 'Live-TV und Mediathek', 'de.telekom.magentatv': 'Fernsehen von der Telekom', 'hdp.opapp': 'Privatsender in HD',
    'com.webos.app.lgchannels': 'Kostenlose Kanäle', 'spotify-beehive': 'Musik und Podcasts', 'com.microsoft.xbox': 'Cloud Gaming mit dem Game Pass',
    'com.limelight.webos': 'PC-Spiele auf den Fernseher streamen', 'com.webos.app.mediadiscovery': 'Fotos, Videos und Musik vom USB-Stick',
    'com.webos.app.browser': 'Im Internet surfen', 'com.webos.app.discovery': 'Neue Apps aus dem LG Store', 'org.webosbrew.hbchannel': 'Homebrew-Apps für den rooted TV',
    'ooo.lew.lifesgoodwithoutspying': 'Tracking und Werbung blockiert', 'amazon.alexa.view': 'Sprachassistent von Amazon',
    [WETTER_APP]: '14-Tage-Vorhersage und Regenradar', [MORE]: 'Ausgeblendete Apps, Hauptraum, Einstellungen', [LIVE_TV]: 'Fernsehempfang'
  };

  const DEFAULTS = {
    favorites: [],                 // pinned into the first row; the rest of the order follows usage
    // LG promotional / partner apps and unused inputs. Nothing is uninstalled; "Mehr" brings them back.
    hidden: ['com.webos.app.hdmi3', 'com.webos.app.hdmi4', LIVE_TV,
      'com.webos.app.lgchannels', 'com.webos.app.lifeonscreen', 'com.webos.app.sportsteamsettings',
      'com.webos.app.homeconnect', 'com.webos.app.camera', 'com.webos.app.multiviewsettings',
      'amazon.alexa.view', 'com.lgtravel.app', 'com.twin.app.gamingportal', 'public.value'],
    order: [],
    mainRoom: ''                   // smart home room shown device by device ('' = first room)
  };
  const INPUT_ALIAS = { 'com.webos.app.hdmi1': 'PC' };   // the owner's PC is on HDMI 1
  const FALLBACK_DEVICES = [1, 2, 3, 4].map(n => ({
    id: 'HDMI_' + n, label: 'HDMI ' + n, appId: 'com.webos.app.hdmi' + n, connected: false, unknown: true
  }));

  /* line icons, 24x24 */
  const CLOUD_HI = '<path d="M17.5 14.5H7.5a4.5 4.5 0 1 1 1.1-8.87A5.5 5.5 0 0 1 19 8a3.3 3.3 0 0 1-1.5 6.5z"/>';
  const ICONS = {
    hdmi: '<path d="M2.5 8.5h19v4.2l-2.6 3.3H5.1l-2.6-3.3z"/><path d="M7 11.5h10"/>',
    pc: '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8.5 20.5h7M12 16.5v4"/>',
    tv: '<rect x="2.5" y="7" width="19" height="12.5" rx="2"/><path d="M8 2.5l4 4.5 4-4.5"/>',
    weather: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z"/><path d="M12 3v2M4.93 6.93l1.41 1.41M19.07 6.93l-1.41 1.41"/>',
    more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
    bulb: '<path d="M9.5 18h5M10.5 21h3"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.1 2.2h5c0-.9.4-1.6 1.1-2.2A6 6 0 0 0 12 3z"/>',
    plug: '<path d="M9 2.5v4.5M15 2.5v4.5"/><path d="M6 7h12v4a6 6 0 0 1-12 0z"/><path d="M12 17v4.5"/>',
    moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
    room: '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
    speaker: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4.5" height="7" rx="1.5"/><rect x="16.5" y="14" width="4.5" height="7" rx="1.5"/>',
    // weather
    sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    night: '<path d="M19 14.2A7.5 7.5 0 1 1 9.8 5a6 6 0 0 0 9.2 9.2z"/>',
    partly: '<path d="M8.5 3.2v1.2M3.8 8H5M5.2 4.7l.8.8M11.8 4.7l-.8.8"/><path d="M5.6 11.4a3.6 3.6 0 1 1 6.6-3"/><path d="M17.3 20H9.6a3.9 3.9 0 1 1 1-7.7 4.9 4.9 0 0 1 9.3 1.9A3 3 0 0 1 17.3 20z"/>',
    cloud: '<path d="M17.5 19H7.5a5 5 0 1 1 1.2-9.86A6 6 0 0 1 20 11.5 3.75 3.75 0 0 1 17.5 19z"/>',
    fog: '<path d="M4 8.5h16M2.5 12.5h19M5 16.5h14"/>',
    rain: CLOUD_HI + '<path d="M8.5 17.5l-1 2.5M12.5 17.5l-1 2.5M16.5 17.5l-1 2.5"/>',
    snow: CLOUD_HI + '<path d="M8 18.5v.01M12 18v.01M16 18.5v.01M10 21v.01M14 21v.01" stroke-width="2.6"/>',
    storm: CLOUD_HI + '<path d="M12.8 15.5 10.6 19h3l-2 3"/>',
    // menus and Gemini (same 24px line style; no emoji anywhere in the UI)
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    chevron: '<path d="M9.5 5.5 16 12l-6.5 6.5"/>',
    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    film: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7.5 4.5v15M16.5 4.5v15M3 9h4.5M3 15h4.5M16.5 9H21M16.5 15H21"/>',
    spark: '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9-1.9 5.1-1.9-5.1L5 10.5l5.1-1.9z"/><path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
    smile: '<circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0M9 9.5v.01M15 9.5v.01" stroke-width="2.4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    shirt: '<path d="M8.5 3.5 3 6.5l2 4.5 2-1V20.5h10V10l2 1 2-4.5-5.5-3a3.5 3.5 0 0 1-7 0z"/>',
    walk: '<circle cx="13" cy="4.5" r="1.8"/><path d="M10 21l2.5-6 2.5 2.5V21M8.5 11.5l2.5-3.5 3 1 2 3 2.5 1M11 8l-1 6"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    dice: '<rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/><path d="M8.5 8.5v.01M15.5 8.5v.01M12 12v.01M8.5 15.5v.01M15.5 15.5v.01" stroke-width="2.6"/>',
    pan: '<circle cx="10" cy="13" r="6.5"/><path d="M16 11.5l5.5-3"/><path d="M8 11.5a2.5 2.5 0 0 1 2.5-2" />',
    brain: '<path d="M9 4.5a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 .5V6a2.5 2.5 0 0 0-3-1.5zM15 4.5a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 .5"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4v5h-5"/>',
    reply: '<path d="M5 5v6.5a3 3 0 0 0 3 3h11"/><path d="M15 10.5l4 4-4 4"/>',
    news: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 10h6M7 14h10"/>',
    music: '<path d="M9 18V5.5l11-2.5V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  };
  const GEMINI_ICON = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="gmg" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#5b8cff"/><stop offset=".55" stop-color="#a855f7"/><stop offset="1" stop-color="#f472b6"/></linearGradient></defs>' +
    '<path d="M12 2.5c.6 4.6 4 8.9 9.5 9.5-5.5.6-8.9 4.9-9.5 9.5-.6-4.6-4-8.9-9.5-9.5 5.5-.6 8.9-4.9 9.5-9.5z" fill="url(#gmg)" stroke="none"/></svg>';
  const svg = (name, cls) => '<svg class="i' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true">' + (ICONS[name] || ICONS.more) + '</svg>';

  const $ = id => document.getElementById(id);
  const scrollerEl = $('scroller');
  const weatherEl = $('weather'), geminiBadgeEl = $('gemini-badge'), audioBadgeEl = $('audio-badge');
  const PILLS = [['hero:weather', weatherEl], ['hero:gemini', geminiBadgeEl], ['hero:audio', audioBadgeEl]];
  const bbEl = $('bb-content'), bbLogo = $('bb-logo'), glowEls = ['glow-top', 'fixed-fade', 'glow-low'].map($);   // the three small static areas painting the colour light (never #page: all cards would restyle)

  /* ------------------------------------------------------------------ */
  /* Luna bus                                                            */
  /* ------------------------------------------------------------------ */
  const bridges = new Set();   // keep references so pending calls are not garbage collected

  function luna(uri, params) {
    return new Promise((resolve, reject) => {
      if (!IS_TV) { reject(new Error('not on a TV')); return; }
      const bridge = new window.PalmServiceBridge();
      bridges.add(bridge);
      const timer = setTimeout(() => {
        bridges.delete(bridge);
        try { bridge.cancel(); } catch (e) { /* ignore */ }
        reject(new Error('timeout: ' + uri));
      }, 12000);
      bridge.onservicecallback = raw => {
        clearTimeout(timer);
        bridges.delete(bridge);
        let reply;
        try { reply = JSON.parse(raw); } catch (e) { reject(new Error('bad reply from ' + uri)); return; }
        if (reply.returnValue === false) reject(Object.assign(new Error(reply.errorText || 'luna error'), { reply }));
        else resolve(reply);
      };
      bridge.call(uri, JSON.stringify(params || {}));
    });
  }

  function lunaSubscribe(uri, params, onMessage) {
    if (!IS_TV) return;
    const bridge = new window.PalmServiceBridge();
    bridges.add(bridge);
    bridge.onservicecallback = raw => {
      let msg;
      try { msg = JSON.parse(raw); } catch (e) { return; }
      onMessage(msg);
    };
    bridge.call(uri, JSON.stringify(Object.assign({ subscribe: true }, params || {})));
  }

  const rootExec = command => luna(HB_EXEC, { command });

  // backend call with a timeout; resolves to the parsed JSON (also for HTTP errors with a JSON body)
  let previewApi = null, previewNoApps = false;   // PC preview only (see the end of the file)
  function api(path, body, ms) {
    if (!IS_TV && previewApi) return previewApi(path, body, ms);
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), ms || 6000) : 0;
    const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    if (ctrl) opts.signal = ctrl.signal;
    return fetch(BACKEND + path, opts).then(r => r.json()).finally(() => clearTimeout(timer));
  }

  /* ------------------------------------------------------------------ */
  /* Preferences                                                         */
  /* ------------------------------------------------------------------ */
  const clone = o => JSON.parse(JSON.stringify(o));

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(STORE_KEY));
      if (p && Array.isArray(p.favorites) && Array.isArray(p.hidden) && Array.isArray(p.order)) {
        if (typeof p.mainRoom !== 'string') p.mainRoom = '';
        delete p.smartDevices;     // old 4-tile picker (before 3.5)
        return p;
      }
    } catch (e) { /* fall through */ }
    return clone(DEFAULTS);
  }
  function savePrefs() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(prefs)); } catch (e) { /* storage full or blocked */ }
  }
  let prefs = loadPrefs();

  /* ------------------------------------------------------------------ */
  /* Model                                                               */
  /* ------------------------------------------------------------------ */
  const state = { apps: [], generatedAt: 0, devices: null, usage: {}, usageStamp: 0, smart: loadSmartCache(), ambient: null };
  let items = new Map();          // id -> item (apps, inputs, utilities)
  let recentId = null;            // the app/input used last
  let favIds = [];                // item ids of the first row
  let rows = [];                  // [{key, title, type: 'cards'|'tiles', ids, keys}]
  let tileIds = [];
  let tiles = new Map();          // id -> smart home tile item
  let smartSummary = '', smartWarn = false;

  // "Spotify – Musik und Podcasts" -> "Spotify" (store titles carry slogans)
  function shortTitle(t) {
    const m = /^(.{3,}?)\s+[–—-]\s+|^(.{3,}?):\s+/.exec(t);
    return m ? (m[1] || m[2]) : t;
  }

  // focus keys: cards are "<row>|<item id>" (an app can sit in two rows), tiles and pills use their own id
  const itemIdOf = key => { const i = String(key).indexOf('|'); return i < 0 ? key : key.slice(i + 1); };

  function buildItems() {
    const map = new Map();
    for (const d of (state.devices || FALLBACK_DEVICES)) {
      if (!d.appId || !/^com\.webos\.app\.hdmi\d/.test(d.appId)) continue;
      const port = String(d.id || '').replace('_', ' ') || d.label;
      const isDefaultLabel = !d.label || /^HDMI[ _-]?\d+$/i.test(d.label);
      let title = d.label || port, sub = port;
      if (isDefaultLabel && INPUT_ALIAS[d.appId]) title = INPUT_ALIAS[d.appId];
      else if (isDefaultLabel) sub = d.unknown ? 'Eingang' : (d.connected ? 'Verbunden' : 'Nicht verbunden');
      map.set(d.appId, { id: d.appId, kind: 'input', title, sub, connected: !!d.connected, unknown: !!d.unknown, glyph: title === 'PC' ? 'pc' : 'hdmi' });
    }
    map.set(LIVE_TV, { id: LIVE_TV, kind: 'input', title: 'Live TV', sub: 'TV-Empfang', connected: false, glyph: 'tv', noDot: true });
    for (const a of state.apps) {
      if (map.has(a.id) || a.id === APP_ID || a.id === SETTINGS) continue;
      if (a.visible === false || a.type === 'stub') continue;   // hidden system apps, store placeholders
      map.set(a.id, { id: a.id, kind: 'app', title: shortTitle(a.title || a.id), fullTitle: a.title || a.id,
        icon: a.icon ? a.icon + '?v=' + (a.iconV || 0) : null, iconColor: a.iconColor || null });
    }
    map.set(WETTER_APP, { id: WETTER_APP, kind: 'util', title: 'WetterOnline', sub: 'Wetter & Radar', glyph: 'weather' });
    map.set(MORE, { id: MORE, kind: 'util', title: 'Mehr', sub: 'Apps & Einstellungen', glyph: 'more' });
    return map;
  }

  // usage score: launches counted by the root helper, fading with a 45-day half-life
  function score(id) {
    const u = state.usage[id];
    if (!u || typeof u.n !== 'number' || typeof u.t !== 'number') return 0;
    return u.n * Math.pow(0.5, Math.max(0, Date.now() / 1000 - u.t) / HALF_LIFE);
  }
  const lastUse = id => { const u = state.usage[id]; return u && typeof u.t === 'number' ? u.t : 0; };

  function byUsage(a, b) {
    const d = score(b.id) - score(a.id);
    if (Math.abs(d) > 1e-6) return d;
    return a.title.localeCompare(b.title, 'de', { sensitivity: 'base' });
  }

  // first row: last used, then pinned, then the most used; below: one row per category
  function buildRows() {
    const hidden = new Set(prefs.hidden);
    const visible = [...items.values()].filter(it => !hidden.has(it.id));
    const launchable = visible.filter(it => it.kind !== 'util');
    recentId = null;
    let best = 0;
    for (const it of launchable) { const t = lastUse(it.id); if (t > best) { best = t; recentId = it.id; } }
    const pinned = prefs.favorites.filter((id, i, arr) => items.has(id) && !hidden.has(id) && items.get(id).kind !== 'util' && arr.indexOf(id) === i);
    const usage = launchable.slice().sort(byUsage).map(it => it.id);
    favIds = [];
    for (const id of [recentId, ...pinned, ...usage]) if (id && !favIds.includes(id) && favIds.length < FAVS) favIds.push(id);

    rows = [{ key: 'favs', title: 'Zuletzt & Favoriten', type: 'cards', ids: favIds }, { key: 'smart', title: 'Smart Home', type: 'tiles', ids: [] }];
    const placed = new Set();
    for (const cat of CATEGORIES) {
      let members;
      if (cat.rest) members = visible.filter(it => it.kind === 'app' && !placed.has(it.id));
      else members = visible.filter(it => (cat.kinds && cat.kinds.includes(it.kind)) || (cat.ids && cat.ids.includes(it.id)));
      members = members.filter(it => !(cat.end || []).includes(it.id)).sort((a, b) => {
        if (a.kind === 'util' || b.kind === 'util') return (a.kind === 'util') - (b.kind === 'util');
        return byUsage(a, b);
      });
      members.forEach(it => placed.add(it.id));
      const ids = members.map(it => it.id).concat((cat.end || []).filter(id => items.has(id)));
      if (ids.length) rows.push({ key: cat.key, title: cat.title, type: 'cards', ids });
    }
    for (const r of rows) r.keys = r.ids.map(id => r.key + '|' + id);
  }

  const hiddenItems = () => prefs.hidden.filter(id => items.has(id) && !prefs.favorites.includes(id)).map(id => items.get(id));

  /* ------------------------------------------------------------------ */
  /* Smart home model                                                    */
  /* ------------------------------------------------------------------ */
  // What the row shows before the backend answered for the first time (ids are MACs, see devices.json).
  const SMART_FALLBACK = [
    ['0a0000000001', 'Deckenlampe', 'Arbeitszimmer', 'light'], ['0a0000000002', 'Fernseher Bar', 'Arbeitszimmer', 'light'],
    ['0a0000000003', 'Bett', 'Arbeitszimmer', 'plug'], ['0a0000000004', 'Kühlschrank', 'Arbeitszimmer', 'plug'],
    ['0a0000000005', 'Licht', 'Wohnzimmer', 'light'], ['0a0000000009', 'Essen', 'Esszimmer', 'light'],
    ['0a000000000a', 'Licht', 'Büro', 'light']
  ].map(([id, name, room, type]) => ({ id, name, room, type, state: 'off', online: null }));

  function loadSmartCache() {
    try {
      const c = JSON.parse(localStorage.getItem(SMART_CACHE_KEY));
      if (Array.isArray(c)) return normDevices(c).map(d => Object.assign(d, { online: null }));   // state is old: shown as "…" until fresh
    } catch (e) { /* ignore */ }
    return null;
  }
  // only entries the UI can work with (anything odd from the backend is dropped instead of breaking a render)
  function normDevices(list) {
    return list.filter(d => d && typeof d === 'object' && typeof d.id === 'string' && d.id).map(d => Object.assign({}, d, {
      name: typeof d.name === 'string' && d.name.trim() ? d.name : d.id,
      room: typeof d.room === 'string' && d.room.trim() ? d.room : 'Weitere Geräte',
      capabilities: Array.isArray(d.capabilities) ? d.capabilities : []
    }));
  }

  const pending = new Map();      // mac -> {state, brightness, until}: optimistic state while a command runs
  // backend health for the smart home row: 'wait' (no answer yet), 'ok', 'down' (the last 2+ polls failed)
  let smartFails = 0, smartFresh = false;
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);

  // null = no answer and no cache yet (show the built-in list), [] = the backend has no devices
  function smartDevices() {
    const list = state.smart ? state.smart : SMART_FALLBACK;
    const now = Date.now();
    return list.map(d => {
      const p = pending.get(d.id);
      if (!p || p.until < now) return d;
      return Object.assign({}, d, { state: p.state, brightness: p.brightness != null ? p.brightness : d.brightness, online: true });
    });
  }

  function roomsOf(devs) {
    const rooms = [];
    for (const d of devs) if (d.room && !rooms.includes(d.room)) rooms.push(d.room);
    return rooms;
  }

  function mainRoom(devs) {
    const rooms = roomsOf(devs);
    if (prefs.mainRoom && rooms.includes(prefs.mainRoom)) return prefs.mainRoom;
    return rooms.includes(DEFAULT_ROOM) ? DEFAULT_ROOM : (rooms[0] || '');
  }

  // state of unknown freshness: "…" while waiting for the first answer, "–" while the backend is down
  const unknownState = () => (smartFails >= 2 ? '–' : '…');

  function deviceState(d) {
    if (d.online === false) return 'Nicht erreichbar';
    if (d.online == null && !pending.has(d.id)) return unknownState();
    if (d.state !== 'on') return 'Aus';
    return d.type === 'light' && typeof d.brightness === 'number' ? 'An · ' + Math.round(d.brightness) + ' %' : 'An';
  }

  // human name of a lamp's colour: "Warmweiß", "2900 K", "Rot", "Eigene Farbe"
  function toneName(d) {
    if (!d || d.state !== 'on') return '';
    const preset = TONES.find(x => toneIs(d, x));
    if (preset) return preset.label;
    if (d.kelvin) return Math.round(d.kelvin / 100) * 100 + ' K';
    return typeof d.color === 'string' && /^#[0-9a-f]{6}$/i.test(d.color) ? 'Eigene Farbe' : '';
  }

  function buildTiles() {
    const devs = smartDevices();
    const main = mainRoom(devs);
    const map = new Map();
    for (const d of devs.filter(x => x.room === main)) {
      map.set('shd:' + d.id, { id: 'shd:' + d.id, kind: 'dev', dev: d, title: d.name, state: deviceState(d),
        on: d.state === 'on' && d.online !== false, offline: d.online === false, lamp: d.color || null,
        icon: d.type === 'plug' ? 'plug' : 'bulb' });
    }
    for (const room of roomsOf(devs).filter(r => r !== main)) {
      const inRoom = devs.filter(x => x.room === room);
      const on = inRoom.filter(x => x.state === 'on' && x.online !== false);
      const known = inRoom.some(x => x.online != null || pending.has(x.id));
      const reachable = inRoom.filter(x => x.online !== false);
      const lit = on.find(x => x.color);
      let st = !known ? unknownState() : (on.length ? on.length + ' von ' + inRoom.length + ' an' : 'Alles aus');
      if (known && !reachable.length) st = 'Nicht erreichbar';
      map.set('shr:' + room, { id: 'shr:' + room, kind: 'room', room, devs: inRoom, title: room, state: st,
        on: on.length > 0, offline: known && !reachable.length, lamp: lit ? lit.color : null, icon: 'room' });
    }
    if (state.ambient) map.set(AMBIENT_TILE, ambientTile(state.ambient));
    map.set(DISPLAY_TILE, { id: DISPLAY_TILE, kind: 'display', title: 'Display aus', state: 'Musik-Modus', on: false, icon: 'moon' });
    tiles = map;
    tileIds = [...map.keys()];
    const mainDevs = devs.filter(x => x.room === main);
    const onCount = mainDevs.filter(x => x.state === 'on' && x.online !== false).length;
    smartWarn = false;
    if (smartFails >= 2 && !smartFresh) { smartSummary = 'Smart-Home-Server nicht erreichbar'; smartWarn = true; }
    else if (smartFails >= 2) { smartSummary = 'Keine Verbindung · Stand von vorhin'; smartWarn = true; }
    else if (!devs.length) smartSummary = 'Keine Geräte eingerichtet';
    else if (!smartFresh && !pending.size) smartSummary = main;   // cached or built-in list: nothing known yet
    else smartSummary = main ? main + ' · ' + (onCount ? plural(onCount, 'Gerät', 'Geräte') + ' an' : 'alles aus') : '';
  }

  /* ------------------------------------------------------------------ */
  /* Icon analysis: decide how an icon sits on its card                   */
  /*   bleed  uniform opaque edge -> card takes the icon's own colour      */
  /*   glyph  transparent icon    -> icon on a brand-coloured card         */
  /*   badge  busy opaque edge    -> icon as a rounded tile on a dark card */
  /* plus an accent colour (most saturated part of the artwork) for light */
  /* ------------------------------------------------------------------ */
  const analysisCache = new Map();
  const lum = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  const sat = ([r, g, b]) => (Math.max(r, g, b) - Math.min(r, g, b)) / 255;
  const rgb = c => 'rgb(' + c.map(Math.round).join(',') + ')';

  function parseColor(str) {
    if (!str) return null;
    const m = /^#?([0-9a-f]{6}|[0-9a-f]{3})$/i.exec(String(str).trim());
    if (!m) return null;
    let h = m[1];
    if (h.length === 3) h = h.split('').map(ch => ch + ch).join('');
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
  }

  // bounding box (fractions of the icon) of the pixels that belong to the artwork
  function contentBox(px, N, test) {
    let x0 = N, y0 = N, x1 = -1, y1 = -1;
    for (let y = 0; y < N; y++) {
      for (let x = 0; x < N; x++) {
        const i = (y * N + x) * 4;
        if (test(px[i], px[i + 1], px[i + 2], px[i + 3])) {
          if (x < x0) x0 = x; if (x > x1) x1 = x;
          if (y < y0) y0 = y; if (y > y1) y1 = y;
        }
      }
    }
    return x1 < 0 ? { cw: 1, ch: 1 } : { cw: (x1 - x0 + 1) / N, ch: (y1 - y0 + 1) / N };
  }
  // percent of the card height the (square) icon image should get so its artwork looks equally big everywhere
  function artScale(box) {
    const H = Math.min(0.46 / Math.max(box.ch, 0.05), 1.0 / Math.max(box.cw, 0.05), 0.95);
    return Math.round(Math.max(H, 0.45) * 100);
  }

  // saturation-weighted mean colour of the pixels that pass `test` (null if the artwork is grey)
  function accentOf(px, test) {
    let w = 0; const acc = [0, 0, 0];
    for (let i = 0; i < px.length; i += 4) {
      if (px[i + 3] < 128 || !test(px[i], px[i + 1], px[i + 2])) continue;
      const c = [px[i], px[i + 1], px[i + 2]];
      const s = sat(c), l = lum(c);
      if (l < 0.08 || l > 0.95) continue;
      const k = s * s;
      w += k; acc[0] += c[0] * k; acc[1] += c[1] * k; acc[2] += c[2] * k;
    }
    if (w < 3) return null;
    const c = acc.map(v => v / w);
    return sat(c) > 0.22 ? c : null;
  }

  function analyze(img, iconColor) {
    const brand = parseColor(iconColor);
    const fallback = { mode: 'badge', color: brand ? brand.map(v => v * 0.5) : [31, 32, 35], accent: brand };
    try {
      const N = 48;
      const cv = document.createElement('canvas');
      cv.width = N; cv.height = N;
      const ctx = cv.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, N, N);
      const px = ctx.getImageData(0, 0, N, N).data;
      const at = (x, y) => { const i = (y * N + x) * 4; return [px[i], px[i + 1], px[i + 2], px[i + 3]]; };
      const edge = [];
      for (let i = 0; i < N; i++) edge.push(at(i, 0), at(i, N - 1), at(0, i), at(N - 1, i));
      const opaqueEdge = edge.filter(p => p[3] > 200);
      const brandSat = brand && sat(brand) > 0.25 ? brand : null;
      if (opaqueEdge.length < edge.length * 0.5) {
        // transparent icon: pick a card colour that contrasts with the glyph
        let n = 0, sum = 0;
        for (let i = 0; i < px.length; i += 4) if (px[i + 3] > 128) { n++; sum += lum([px[i], px[i + 1], px[i + 2]]); }
        const glyphLight = n ? sum / n > 0.55 : true;
        let color = brand || [38, 40, 44];
        if (glyphLight && lum(color) > 0.7) color = [38, 40, 44];
        if (!glyphLight && lum(color) < 0.3) color = [233, 233, 238];
        const accent = accentOf(px, () => true) || brandSat || (sat(color) > 0.25 ? color : null);
        return { mode: 'glyph', color, accent, art: artScale(contentBox(px, N, (r, g, b, a) => a > 64)) };
      }
      const mean = [0, 1, 2].map(k => opaqueEdge.reduce((s, p) => s + p[k], 0) / opaqueEdge.length);
      const dist = opaqueEdge.map(p => Math.max(Math.abs(p[0] - mean[0]), Math.abs(p[1] - mean[1]), Math.abs(p[2] - mean[2])))
        .sort((a, b) => a - b);
      const differs = (r, g, b) => Math.max(Math.abs(r - mean[0]), Math.abs(g - mean[1]), Math.abs(b - mean[2])) > 40;
      if (dist[Math.floor(dist.length * 0.9)] < 26) {
        const box = contentBox(px, N, (r, g, b, a) => a > 128 && differs(r, g, b));
        const accent = accentOf(px, differs) || (sat(mean) > 0.25 ? mean : null) || brandSat;
        return { mode: 'bleed', color: mean, accent, art: artScale(box) };
      }
      let n = 0; const avg = [0, 0, 0];
      for (let i = 0; i < px.length; i += 4) if (px[i + 3] > 128) { n++; avg[0] += px[i]; avg[1] += px[i + 1]; avg[2] += px[i + 2]; }
      const accent = accentOf(px, () => true) || brandSat;
      return { mode: 'badge', color: avg.map(v => (v / Math.max(n, 1)) * 0.42), accent };
    } catch (e) {
      return fallback;      // canvas not readable: keep a safe look
    }
  }

  // light colour for ambient glow / hero tint: the accent, brightened a little; grey icons get a neutral blue-grey
  function lightOf(look) {
    const a = look && look.accent;
    if (!a) return [96, 108, 132];
    const m = Math.max(a[0], a[1], a[2]) || 1;
    const k = Math.min(1.6, 235 / m);
    return a.map(v => Math.min(255, v * k));
  }

  function applyLook(el, look) {
    el.classList.remove('bleed', 'glyph', 'badge');
    el.classList.add(look.mode);
    el.style.setProperty('--c', rgb(look.color));
    if (look.art) el.style.setProperty('--art', look.art + '%'); else el.style.removeProperty('--art');
    const light = lightOf(look);
    el.dataset.amb = rgb(light);
    el.style.setProperty('--tint', 'rgba(' + light.map(Math.round).join(',') + ',.42)');
    el.style.setProperty('--tint-deep', rgb(light.map(v => v * 0.16)));
  }

  /* ------------------------------------------------------------------ */
  /* Cards and rows                                                      */
  /* ------------------------------------------------------------------ */
  const els = new Map();   // focus key -> element

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // pointer (Magic Remote) and long-press handling shared by all focusable elements
  let pointerHold = 0, pointerLong = false;
  function bindPointer(el) {
    const key = () => el.dataset.key;
    el.addEventListener('mouseenter', () => { if (mode === 'home') setFocus(key(), { fromPointer: true }); });
    el.addEventListener('mousedown', () => {
      clearTimeout(pointerHold);
      pointerLong = false;
      pointerHold = setTimeout(() => { pointerLong = true; if (mode === 'home') openCardMenu(key()); }, HOLD_MS);
    });
    el.addEventListener('mouseup', () => clearTimeout(pointerHold));
    el.addEventListener('mouseleave', () => clearTimeout(pointerHold));
    el.addEventListener('click', () => {
      if (pointerLong) { pointerLong = false; return; }
      if (mode === 'home') activate(key());
      else if (mode === 'move') endMove();         // the pointer has no arrow keys: a click finishes moving
    });
  }

  function faceFor(it) {
    const face = h('div', 'face');
    face.innerHTML = '<span class="face-icon">' + svg(it.glyph) + '</span>';
    const text = h('div', 'face-text');
    text.appendChild(h('div', 'face-title'));
    text.appendChild(h('div', 'face-sub'));
    face.appendChild(text);
    return face;
  }

  function createCard(it) {
    const el = h('div', 'card ' + it.kind + (it.id === WETTER_APP ? ' weather' : ''));
    el.setAttribute('role', 'button');
    const plate = h('div', 'plate');
    el.appendChild(plate);
    if (it.kind === 'app') {
      const img = document.createElement('img');
      img.className = 'art';
      img.alt = '';
      img.draggable = false;
      img.decoding = 'async';
      plate.appendChild(img);
    } else {
      plate.appendChild(faceFor(it));
      el.dataset.amb = it.id === WETTER_APP ? 'rgb(60,150,255)' : (it.glyph === 'pc' ? 'rgb(70,140,255)' : 'rgb(110,120,140)');
    }
    bindPointer(el);
    return el;
  }

  // load an app icon into `img` and give `el` the matching look
  function loadIcon(el, img, it, onFail) {
    if (el.dataset.icon === String(it.icon)) return;
    el.dataset.icon = String(it.icon);
    const cached = it.icon && analysisCache.get(it.icon);
    const brand = parseColor(it.iconColor);
    applyLook(el, cached || { mode: 'badge', color: brand ? brand.map(v => v * 0.5) : [31, 32, 35], accent: brand });
    if (!it.icon) { img.style.display = 'none'; onFail(); return; }
    img.onload = () => {
      if (el.dataset.icon !== String(it.icon)) return;
      let look = analysisCache.get(it.icon);
      if (!look) { look = analyze(img, it.iconColor); analysisCache.set(it.icon, look); }
      applyLook(el, look);
      if (els.get(focusId) === el && bbKey === focusId) showBillboard(focusId, false);   // late icon: refresh the colours
    };
    img.onerror = () => { if (el.dataset.icon === String(it.icon)) { img.style.display = 'none'; onFail(); } };
    img.style.display = '';
    img.src = it.icon;
  }

  function updateCard(el, it) {
    el.setAttribute('aria-label', it.title);
    if (it.kind === 'app') {
      loadIcon(el, el.querySelector('.art'), it, () => letterFace(el, it));
    } else {
      el.querySelector('.face-title').textContent = it.title;
      const sub = el.querySelector('.face-sub');
      sub.textContent = '';
      if (it.kind === 'input' && !it.noDot) sub.appendChild(h('i', 'dot' + (it.connected ? ' on' : '')));
      sub.appendChild(document.createTextNode(it.sub || ''));
    }
  }

  function letterFace(el, it) {     // app without a usable icon: show its name on the card
    const plate = el.querySelector('.plate');
    if (plate.querySelector('.face')) return;
    const face = h('div', 'face');
    const text = h('div', 'face-text');
    text.appendChild(h('div', 'face-title', it.title));
    face.appendChild(text);
    plate.appendChild(face);
  }

  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return 'gerade eben';
    if (s < 3600) return 'vor ' + Math.round(s / 60) + ' Min.';
    if (s < 86400) return 'vor ' + Math.round(s / 3600) + ' Std.';
    if (s < 2 * 86400) return 'gestern';
    return 'vor ' + Math.round(s / 86400) + ' Tagen';
  }

  /* smart home tiles */
  function createTile(t) {
    const el = h('div', 'card tile ' + t.kind);
    el.setAttribute('role', 'button');
    el.innerHTML = '<div class="plate"><div class="glow"></div><div class="t-face"><span class="t-icon"></span>' +
      '<div class="t-meta"><div class="t-name"></div><div class="t-state"></div></div></div></div>';
    bindPointer(el);
    return el;
  }

  function lampOf(t) {
    return t.lamp || (t.kind === 'dev' && t.dev.type === 'plug' ? '#7ee0a1' : '#ffcf8a');
  }

  function updateTile(el, t) {
    el.classList.toggle('on', !!t.on);
    el.classList.toggle('offline', !!t.offline);
    const iconEl = el.querySelector('.t-icon');
    if (iconEl.dataset.icon !== t.icon) { iconEl.dataset.icon = t.icon; iconEl.innerHTML = svg(t.icon); }
    el.querySelector('.t-name').textContent = t.title;
    el.querySelector('.t-state').textContent = t.state;
    el.setAttribute('aria-label', t.title + ', ' + t.state);
    const lamp = lampOf(t);
    el.style.setProperty('--lamp', lamp);
    el.dataset.amb = t.kind === 'display' ? 'rgb(168,85,247)' : (t.on ? lamp : 'rgb(70,78,96)');
  }

  // put elements for `keys` into `container` in order, reusing existing ones
  function place(container, keys, get, create, update) {
    keys.forEach((key, i) => {
      const it = get(key);
      let el = els.get(key);
      if (el && el.dataset.kind !== it.kind) { el.remove(); els.delete(key); el = null; }
      if (!el) { el = create(it); el.dataset.kind = it.kind; el.dataset.key = key; els.set(key, el); }
      update(el, it);
      if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
    });
    while (container.children.length > keys.length) container.lastElementChild.remove();
  }

  const rowEls = new Map();   // row key -> {section, title, sub, track}
  function rowEl(r, i) {
    let re = rowEls.get(r.key);
    if (!re) {
      const section = h('section', 'row ' + r.type + ' reveal');
      section.dataset.row = r.key;
      const head = h('div', 'row-head');
      const title = h('span', 'row-title');
      const sub = h('span', 'row-sub');
      head.appendChild(title); head.appendChild(sub);
      const track = h('div', 'track');
      section.appendChild(head); section.appendChild(track);
      re = { section, title, sub, track, scrollX: 0 };
      rowEls.set(r.key, re);
    }
    re.section.style.setProperty('--d', Math.min(i, 4) * 70 + 70 + 'ms');
    re.title.textContent = r.title;
    return re;
  }

  // re-ranking while a menu is open or a card is being moved would shuffle things under the user's hand
  let renderPending = false;
  function requestRender() { if (mode !== 'home') renderPending = true; else render(); }
  function flushRender() { if (renderPending && mode === 'home') { renderPending = false; render(); } }

  function render() {
    items = buildItems();
    buildRows();
    buildTiles();
    const smart = rows.find(r => r.key === 'smart');
    smart.ids = tileIds; smart.keys = tileIds;
    rows.forEach((r, i) => {
      const re = rowEl(r, i);
      if (scrollerEl.children[i] !== re.section) scrollerEl.insertBefore(re.section, scrollerEl.children[i] || null);
      if (r.type === 'tiles') {
        place(re.track, r.keys, k => tiles.get(k), createTile, updateTile);
        setSmartSub(re);
      } else {
        place(re.track, r.keys, k => items.get(itemIdOf(k)), createCard, updateCard);
        re.sub.textContent = r.key === 'favs' ? '' : String(r.ids.length);
      }
    });
    while (scrollerEl.children.length > rows.length) {
      const gone = scrollerEl.lastElementChild;
      rowEls.delete(gone.dataset.row);
      gone.remove();
    }
    const live = new Set(PILLS.map(p => p[0]));
    for (const r of rows) for (const k of r.keys) live.add(k);
    for (const k of [...els.keys()]) if (!live.has(k)) els.delete(k);
    layoutDirty = true;
    let key = focusId;
    // right after Home the focus belongs on the first card, even when the app list arrives a moment
    // later and re-sorts the row (the old first app would otherwise drag the focus to its new place)
    if (homeStartUntil > Date.now()) key = firstId();
    else if (!key || !els.has(key)) key = (key && findKey(itemIdOf(key))) || firstId();
    setFocus(key, { instant: !focusId });
  }

  function setSmartSub(re) {
    re.sub.textContent = smartSummary;
    re.sub.classList.toggle('warn', smartWarn);
  }

  function renderTiles() {
    const before = tileIds.join();
    buildTiles();
    const smart = rows.find(r => r.key === 'smart');
    const re = rowEls.get('smart');
    if (!smart || !re) return;
    smart.ids = tileIds; smart.keys = tileIds;
    place(re.track, tileIds, k => tiles.get(k), createTile, updateTile);
    setSmartSub(re);
    for (const k of [...els.keys()]) if ((k.startsWith('shd:') || k.startsWith('shr:') || k === AMBIENT_TILE) && !tiles.has(k)) els.delete(k);
    if (tileIds.join() !== before) layoutDirty = true;
    if (focusId && !els.has(focusId)) setFocus(tileIds[0] || firstId());
    else if (focusId && tiles.has(focusId) && bbKey === focusId) showBillboard(focusId, false);
  }

  // first key showing item `id` (the same app can be in two rows)
  function findKey(id) {
    for (const r of rows) { const i = r.ids.indexOf(id); if (i >= 0) return r.keys[i]; }
    return null;
  }
  const firstId = () => (rows[0] && rows[0].keys[0]) || (rows[1] && rows[1].keys[0]) || PILLS[0][0];

  /* ------------------------------------------------------------------ */
  /* Focus, navigation, sliding rows                                     */
  /* ------------------------------------------------------------------ */
  let focusId = null;            // focus key
  let homeStartUntil = 0;        // after Home: keep the focus on the first card while the row re-sorts
  let mode = 'home';             // 'home' | 'menu' | 'move'
  let layoutDirty = true;
  let geom = [];                 // per navigable row: {top, xs, ws, maxScroll}
  let scrollY = 0;
  let pushedRow = null;          // row element whose cards are currently pushed aside
  const PUSH = { cards: 26, tiles: 14 };

  // navigable rows: the pills on top, then the card/tile rows
  const navRows = () => [{ key: 'pills', type: 'pills', keys: PILLS.map(p => p[0]) }, ...rows];

  function measure() {
    if (!layoutDirty) return;
    layoutDirty = false;
    geom = navRows().map(r => {
      if (r.type === 'pills') {
        const xs = [], ws = [];
        for (const [, el] of PILLS) { const b = el.getBoundingClientRect(); xs.push(b.left); ws.push(b.width); }
        return { top: 0, xs, ws, maxScroll: 0 };
      }
      const re = rowEls.get(r.key);
      const kids = re ? [...re.track.children] : [];
      const xs = kids.map(k => k.offsetLeft), ws = kids.map(k => k.offsetWidth);
      const last = kids[kids.length - 1];
      const trackW = last ? last.offsetLeft + last.offsetWidth - SIDE : 0;   // offsetLeft includes the track padding (SIDE)
      const over = trackW - (VIEW_W - 2 * SIDE);   // tiles slide too when there are many devices (flex rounding: ignore < 4 px)
      return { top: re ? re.section.offsetTop : 0, xs, ws, maxScroll: over > 4 ? over : 0 };
    });
  }

  function posOf(key) {
    const nr = navRows();
    for (let r = 0; r < nr.length; r++) { const c = nr[r].keys.indexOf(key); if (c >= 0) return { r, c, row: nr[r] }; }
    return null;
  }

  // horizontal centre of a card as it appears on screen (row scroll included)
  function visualX(r, c) {
    const g = geom[r];
    if (!g || !g.xs.length) return VIEW_W / 2;
    c = Math.max(0, Math.min(c, g.xs.length - 1));
    const re = r > 0 ? rowEls.get(navRows()[r].key) : null;
    return g.xs[c] + g.ws[c] / 2 - (re ? re.scrollX : 0);
  }

  function setFocus(key, opts) {
    const el = els.get(key);
    if (!el) return;
    opts = opts || {};
    if (opts.fromPointer) homeStartUntil = 0;
    measure();
    const prev = focusId && els.get(focusId);
    if (prev && prev !== el) prev.classList.remove('focused', 'pressed');
    focusId = key;
    el.classList.add('focused');
    const pos = posOf(key);
    if (pos) {
      layoutRow(pos, opts.instant);
      scrollToRow(pos.r, opts.instant);
    }
    if (opts.instant || !bbKey) { showBillboard(key, false); applyGlow(); }
    else scheduleBillboard(key);   // the colour light changes together with the billboard text (fillBillboard)
  }

  // the focused card grows; its neighbours move aside; long rows slide so the focus stays left
  function layoutRow(pos, instant) {
    const re = pos.r > 0 ? rowEls.get(pos.row.key) : null;
    const track = re ? re.track : null;
    if (pushedRow && pushedRow !== track) {
      for (const k of pushedRow.children) if (k.dataset.dx && k.dataset.dx !== '0') { k.style.setProperty('--dx', '0px'); k.dataset.dx = '0'; }
    }
    pushedRow = track;
    if (!track) return;
    const push = PUSH[pos.row.type] || 0;
    [...track.children].forEach((k, i) => {
      const dx = String(i < pos.c ? -push : i > pos.c ? push : 0);
      if (k.dataset.dx !== dx) { k.style.setProperty('--dx', dx + 'px'); k.dataset.dx = dx; }
    });
    const g = geom[pos.r];
    if (g) {
      const x = Math.max(0, Math.min(g.maxScroll, g.xs[pos.c] - SIDE));
      if (x !== re.scrollX || instant) {
        re.scrollX = x;
        if (instant) track.style.transition = 'none';
        track.style.transform = x ? 'translate3d(' + (-x) + 'px,0,0)' : '';
        if (instant) { void track.offsetWidth; track.style.transition = ''; }
      }
    }
  }

  // the focused row sits right under the billboard; rows above slide away beneath it
  let currentRow = -1;
  function scrollToRow(r, instant) {
    currentRow = r;
    const y = r <= 1 || !geom[r] || !geom[1] ? 0 : geom[r].top - geom[1].top;
    if (y === scrollY && !instant) return;
    scrollY = y;
    if (instant) scrollerEl.style.transition = 'none';
    scrollerEl.style.transform = y ? 'translate3d(0,' + (-y) + 'px,0)' : '';
    if (instant) { void scrollerEl.offsetHeight; scrollerEl.style.transition = ''; }
  }

  function bump(el, dir) {
    const cls = 'bump-' + dir[0];
    el.classList.remove('bump-l', 'bump-r', 'bump-u', 'bump-d');
    void el.offsetWidth;
    el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), 280);
  }

  function move(dir) {
    homeStartUntil = 0;
    measure();
    const pos = posOf(focusId);
    if (!pos) { setFocus(firstId()); return; }
    const nr = navRows();
    let target = null;
    if (dir === 'left' || dir === 'right') {
      target = pos.row.keys[pos.c + (dir === 'left' ? -1 : 1)] || null;
    } else {
      let r = pos.r + (dir === 'up' ? -1 : 1);
      while (r >= 0 && r < nr.length && !nr[r].keys.length) r += dir === 'up' ? -1 : 1;
      if (r >= 0 && r < nr.length && pos.row.type === 'pills') {
        target = nr[r].keys[0];   // down from the top bar always starts at the first card, left
      } else if (r >= 0 && r < nr.length) {
        const x = visualX(pos.r, pos.c);
        let best = 0, bd = Infinity;
        nr[r].keys.forEach((k, c) => { const d = Math.abs(visualX(r, c) - x); if (d < bd) { bd = d; best = c; } });
        target = nr[r].keys[best];
      }
    }
    if (target) setFocus(target);
    else if (els.get(focusId)) bump(els.get(focusId), dir);
  }

  /* ------------------------------------------------------------------ */
  /* Billboard: describes the focused card in big type                   */
  /* ------------------------------------------------------------------ */
  let bbKey = null, bbTimer = 0, bbSwapTimer = 0;
  const billboardEl = $('billboard');

  function scheduleBillboard(key) {
    clearTimeout(bbTimer);
    if (key === bbKey) return;
    bbTimer = setTimeout(() => showBillboard(key, true), BB_REST_MS);
  }

  function isMuted(color) {
    const m = /^rgb\((\d+),(\d+),(\d+)\)$/.exec(String(color).replace(/\s/g, ''));
    return !!m && Math.max(+m[1], +m[2], +m[3]) - Math.min(+m[1], +m[2], +m[3]) < 40;
  }

  const WEEKDAY = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
  const num = v => typeof v === 'number' && isFinite(v);
  const at = (arr, i) => (Array.isArray(arr) && num(arr[i]) ? arr[i] : null);

  // weather lines for the billboard (any field may be missing: open-meteo answers vary, the backend may send {error})
  function weatherInfo() {
    const w = lastWeather, cur = w && w.current, d = (w && w.daily) || {};
    const info = { kicker: placeName() ? 'Wetter · ' + placeName() : 'Wetter', title: 'Wetter', desc: weatherFailed ? 'Wetterdaten gerade nicht verfügbar' : 'Wetterdaten werden geladen …', meta: [] };
    if (!cur || !num(cur.temperature_2m)) return info;
    const [, desc] = WMO_MAP[cur.weather_code] || ['', 'Bewölkt'];
    info.title = Math.round(cur.temperature_2m) + '° · ' + desc;
    const lo = at(d.temperature_2m_min, 0), hi = at(d.temperature_2m_max, 0), rain = at(d.precipitation_probability_max, 0);
    const parts = [];
    if (lo != null && hi != null) parts.push('Heute ' + Math.round(lo) + '° bis ' + Math.round(hi) + '°');
    if (rain != null) parts.push(Math.round(rain) + ' % Regenrisiko');
    if (num(cur.apparent_temperature) && Math.abs(cur.apparent_temperature - cur.temperature_2m) >= 2) parts.push('gefühlt ' + Math.round(cur.apparent_temperature) + '°');
    info.desc = parts.join(' · ');
    const days = Array.isArray(d.time) ? d.time : [];
    for (let i = 1; i <= 3; i++) {
      const dl = at(d.temperature_2m_min, i), dh = at(d.temperature_2m_max, i);
      if (dl == null || dh == null) break;
      const date = days[i] ? new Date(days[i] + 'T12:00:00') : new Date(Date.now() + i * 86400000);
      const name = i === 1 ? 'Morgen' : (WEEKDAY[date.getDay()] || '');
      info.meta.push({ icon: (WMO_MAP[at(d.weather_code, i)] || ['cloud'])[0], text: name + '  ' + Math.round(dl) + '° / ' + Math.round(dh) + '°' });
    }
    if (weatherFailed) info.meta.push({ warn: true, text: 'Stand von vorhin' });
    return info;
  }

  function billboardInfo(key) {
    const OK = 'OK', HOLD = { text: 'OK', hold: true };
    const okLong = (ok, long) => [[OK, ok]].concat(long ? [[HOLD, long]] : []);
    if (key === 'hero:weather') {
      return Object.assign(weatherInfo(), { keys: okLong('WetterOnline öffnen'), icon: $('w-icon').dataset.icon || 'cloud',
        logo: 'icon weather', glow: 'rgb(60,150,255)' });
    }
    if (key === 'hero:gemini') {
      return { kicker: 'KI-Assistent', title: 'Gemini', desc: 'Filmtipps, Wetter, Wissen – und TV und Licht per Sprache steuern',
        keys: [[OK, 'Öffnen'], [{ icon: 'mic' }, 'Mikro-Taste']], iconHtml: GEMINI_ICON, logo: 'icon gemini', glow: 'rgb(139,92,246)' };
    }
    if (key === 'hero:audio') {
      return { kicker: 'Audioausgabe', title: $('audio-title').textContent, desc: $('audio-sub').textContent,
        keys: okLong('Ton-Einstellungen'), icon: $('audio-icon').dataset.icon || 'speaker', logo: 'icon audio', glow: 'rgb(56,189,248)' };
    }
    const t = tiles.get(key);
    if (t) {
      const lamp = lampOf(t);
      if (t.kind === 'display') {
        return { kicker: 'Musik-Modus', title: 'Display aus', desc: 'Nur das OLED-Panel geht aus, der Ton läuft weiter',
          keys: okLong('Ausschalten'), icon: 'moon', logo: 'icon round display', glow: 'rgb(168,85,247)' };
      }
      if (t.kind === 'spotify') return ambientBillboard(t);
      if (t.kind === 'room') {
        const meta = t.devs.slice(0, 5).map(d => ({ dot: d.online === false ? 'off' : d.state === 'on' ? (d.color || lampOf({ kind: 'dev', dev: d })) : '',
          text: d.name + (d.online === false ? ' · nicht erreichbar' : '') }));
        if (t.devs.length > 5) meta.push({ text: '+' + (t.devs.length - 5) });
        return { kicker: 'Raum · ' + plural(t.devs.length, 'Gerät', 'Geräte'), title: t.title, desc: t.state, meta,
          keys: t.offline ? okLong('Erneut prüfen', 'Einzelne Geräte') : okLong(t.on ? 'Alle ausschalten' : 'Alle einschalten', 'Einzelne Geräte'),
          icon: 'room', logo: 'icon round' + (t.on ? ' on' : '') + (t.offline ? ' offline' : ''), lamp, glow: t.on ? lamp : 'rgb(70,78,96)' };
      }
      const d = t.dev, light = d.type === 'light', caps = d.capabilities || [];
      const tune = caps.includes('color') || caps.includes('white') ? 'Farbe & Helligkeit' : 'Helligkeit';
      const meta = [];
      let desc = t.state;
      if (t.offline) { desc = 'Nicht erreichbar'; meta.push({ warn: true, text: 'Strom und WLAN prüfen' }); }
      else if (t.state === '…' || t.state === '–') desc = t.state === '–' ? 'Zustand unbekannt' : 'Zustand wird geladen …';
      else if (d.state === 'on') {
        desc = 'Eingeschaltet';
        if (light && num(d.brightness)) meta.push({ level: d.brightness / 100, text: Math.round(d.brightness) + ' %' });
        const tone = light ? toneName(d) : '';
        if (tone) meta.push({ swatch: d.kelvin ? kelvinHex(d.kelvin) : d.color, text: tone });
      } else desc = 'Ausgeschaltet';
      return { kicker: d.room + ' · ' + (light ? 'Licht' : 'Steckdose'), title: t.title, desc, meta,
        keys: okLong(t.offline ? 'Erneut prüfen' : (t.state === '…' || t.state === '–') ? 'Ein/Aus' : d.state === 'on' ? 'Ausschalten' : 'Einschalten', light ? tune : 'Optionen'),
        icon: t.icon, logo: 'icon round' + (t.on ? ' on' : '') + (t.offline ? ' offline' : ''), lamp, glow: t.on ? lamp : 'rgb(70,78,96)' };
    }
    const it = items.get(itemIdOf(key));
    if (!it) return null;
    const el = els.get(key);
    const t0 = lastUse(it.id);
    const pinned = prefs.favorites.includes(it.id);
    const pos = posOf(key);
    let kicker = it.kind === 'input' ? 'Eingang' : it.kind === 'util' ? 'Werkzeug' : (pos && pos.row.key !== 'favs' ? pos.row.title : 'App');
    if (it.id === recentId && t0) kicker = 'Zuletzt geöffnet · ' + ago(t0);
    else if (pinned) kicker = 'Angeheftet' + (t0 ? ' · zuletzt ' + ago(t0) : '');
    else if (t0) kicker = kicker + ' · zuletzt ' + ago(t0);
    const port = it.id.replace(/\D/g, '');
    let desc = APP_DESC[it.id] || '';
    const meta = [];
    if (it.kind === 'input' && !it.noDot) {
      desc = desc || (it.unknown ? 'Eingang' : it.connected ? 'Gerät verbunden' : 'Kein Gerät verbunden');
      meta.push({ dot: it.connected ? '#34c759' : '', text: port ? 'HDMI ' + port : 'Eingang' });
    }
    if (it.kind === 'app' && it.fullTitle && it.fullTitle !== it.title && !APP_DESC[it.id]) desc = it.fullTitle;
    const info = { kicker, title: it.title, desc, meta, keys: okLong(it.kind === 'input' ? 'Umschalten' : 'Öffnen', 'Optionen'),
      glow: (el && el.dataset.amb) || 'rgb(110,120,140)' };
    if (it.kind === 'app' && it.icon && el) {
      const mode = ['bleed', 'glyph', 'badge'].find(c => el.classList.contains(c)) || 'badge';
      info.img = it.icon; info.logo = 'app ' + mode;
      info.c = el.style.getPropertyValue('--c'); info.art = el.style.getPropertyValue('--art');
    } else {
      info.icon = it.glyph || 'more'; info.logo = 'icon glyph' + (it.id === WETTER_APP ? ' weather' : '');
    }
    return info;
  }

  function fillBillboard(info) {
    $('bb-kicker').textContent = info.kicker || '';
    $('bb-title').textContent = info.title || '';
    $('bb-desc').textContent = info.desc || '';
    const meta = $('bb-meta');
    meta.textContent = '';
    for (const m of info.meta || []) {
      const c = h('span', 'bb-chip' + (m.warn ? ' warn' : ''));
      if (m.icon) c.insertAdjacentHTML('beforeend', '<span class="bb-chip-ico">' + svg(m.icon) + '</span>');
      if (m.dot !== undefined) {
        const dot = h('i', 'bb-dot' + (m.dot === 'off' ? ' bad' : m.dot ? ' on' : ''));
        if (m.dot && m.dot !== 'off') dot.style.setProperty('--dc', m.dot);
        c.appendChild(dot);
      }
      if (m.swatch) { const sw = h('i', 'bb-swatch'); sw.style.setProperty('--dc', m.swatch); c.appendChild(sw); }
      if (m.level != null) {
        const bar = h('i', 'bb-level');
        const fill = h('b', '');
        fill.style.setProperty('--p', Math.max(0.04, Math.min(1, m.level)).toFixed(3));
        bar.appendChild(fill);
        c.appendChild(bar);
      }
      c.appendChild(document.createTextNode(m.text || ''));
      meta.appendChild(c);
    }
    const keys = $('bb-keys');
    keys.textContent = '';
    for (const [k, label] of info.keys || []) {
      const s = h('span', 'bb-key');
      const cap = h('b', typeof k === 'object' && k.hold ? 'hold' : '');
      if (typeof k === 'object' && k.icon) { cap.className = 'icon'; cap.innerHTML = svg(k.icon); }
      else cap.textContent = typeof k === 'object' ? k.text : k;
      s.appendChild(cap);
      s.appendChild(document.createTextNode(label));
      keys.appendChild(s);
    }
    bbLogo.className = info.logo || '';
    bbLogo.style.setProperty('--c', info.c || '');
    bbLogo.style.setProperty('--art', info.art || '72%');
    bbLogo.style.setProperty('--lamp', info.lamp || '');
    const img = bbLogo.querySelector('img'), ic = bbLogo.querySelector('.bb-icon');
    if (info.img) {
      if (img.getAttribute('src') !== info.img) img.src = info.img;
      img.style.display = '';
      ic.innerHTML = '';
      ic.dataset.html = '';
    } else {
      img.style.display = 'none';
      const html = info.iconHtml || svg(info.icon);
      if (ic.dataset.html !== html) { ic.innerHTML = html; ic.dataset.html = html; }
    }
    applyGlow();   // colour light changes together with the text (it used to wait 650 ms: felt late)
  }

  // The colour light is painted into three small static areas (#glow-top, #fixed-fade, #glow-low), never into
  // a layer that moves and never via #page. Repainting it costs the TV a frame or two, so it changes only once
  // per billboard swap (together with the text, BB_REST_MS after the last key), not on every key press.
  function applyGlow() {
    const info = bbKey && billboardInfo(bbKey);
    if (!info) return;
    const c = toRgb(info.glow), a = isMuted(info.glow) ? 0.22 : 0.55;
    const g1 = 'rgba(' + c + ',' + a + ')', g2 = 'rgba(' + c + ',' + (a * 0.38).toFixed(3) + ')';
    if (glowEls[0].style.getPropertyValue('--g1') !== g1) {   // only repaint if the colour really changed
      for (const el of glowEls) { el.style.setProperty('--g1', g1); el.style.setProperty('--g2', g2); }
    }
  }

  // 'rgb(1,2,3)' or '#rrggbb' -> '1,2,3'
  function toRgb(color) {
    const s = String(color || '').replace(/\s/g, '');
    let m = /^rgba?\((\d+),(\d+),(\d+)/.exec(s);
    if (m) return m[1] + ',' + m[2] + ',' + m[3];
    const p = parseColor(s);
    return p ? p.join(',') : '110,120,140';
  }

  function showBillboard(key, animate) {
    const info = billboardInfo(key);
    if (!info) return;
    clearTimeout(bbTimer);
    bbKey = key;
    if (!animate) { clearTimeout(bbSwapTimer); billboardEl.classList.remove('out'); fillBillboard(info); return; }
    billboardEl.classList.add('out');
    clearTimeout(bbSwapTimer);
    bbSwapTimer = setTimeout(() => {
      if (bbKey !== key) return;
      fillBillboard(billboardInfo(key) || info);
      billboardEl.classList.remove('out');
    }, 140);
  }

  /* ------------------------------------------------------------------ */
  /* Launching                                                           */
  /* ------------------------------------------------------------------ */
  let leaveTimer = 0;

  function restoreView() {
    clearTimeout(leaveTimer);
    document.body.classList.remove('leaving');
    for (const el of document.querySelectorAll('.card.launching')) el.classList.remove('launching');
  }

  function launchId(id, title, el, params) {
    if (el) el.classList.add('launching');
    document.body.classList.add('leaving');
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(restoreView, 7000);      // nothing took over: come back
    if (!IS_TV) { setTimeout(() => { restoreView(); toast('Starte ' + title); }, 700); return; }
    luna(SAM + 'launch', params ? { id, params } : { id }).catch(err => {
      restoreView();
      toast('„' + title + '“ lässt sich nicht starten' + (err && err.message ? ' (' + err.message + ')' : ''));
    });
  }

  function activate(key) {
    if (key === 'hero:weather') { openWeatherApp(); return; }
    if (key === 'hero:gemini') { openGeminiModal(); return; }
    if (key === 'hero:audio') { openAudioSettings(); return; }
    if (tiles.has(key)) { activateTile(tiles.get(key)); return; }
    launchItem(itemIdOf(key), els.get(key));
  }

  function launchItem(id, el) {
    const it = items.get(id);
    if (!it) return;
    if (it.kind === 'util') { if (it.id === WETTER_APP) openWeatherApp(); else openMoreMenu(); return; }
    launchId(it.id, it.title, el);
  }

  /* ------------------------------------------------------------------ */
  /* Smart home actions (optimistic: the tile switches at once)          */
  /* ------------------------------------------------------------------ */
  function popTile(id, cls) {
    const el = els.get(id);
    if (!el) return;
    el.classList.remove('pop', 'fail');
    void el.offsetWidth;
    el.classList.add(cls || 'pop');
    setTimeout(() => el.classList.remove(cls || 'pop'), 460);
  }

  // Commands and polls can overlap (a poll started before a command answers after it; two quick presses
  // send two commands). smartEpoch changes with every command, and a poll that started under an older epoch
  // is dropped, so it can never paint a state from before the command. cmdSeq marks the newest command per
  // device: only that one may write the model or clear the optimistic state.
  let smartRefreshTimer = 0, smartEpoch = 0, pollSeq = 0, pollApplied = 0;
  const cmdSeq = new Map();       // mac -> number of the newest command

  function refreshMenu() { if (mode === 'menu' && menuRefresh) menuRefresh(); }

  async function smartSet(devs, on, brightness, tileId, tone) {   // tone: { color: "#rrggbb" } or { kelvin }
    if (!devs.length) return [];
    smartEpoch++;
    const until = Date.now() + 5000;
    const seqs = devs.map(d => {
      const n = (cmdSeq.get(d.id) || 0) + 1;
      cmdSeq.set(d.id, n);
      pending.set(d.id, { state: on ? 'on' : 'off', brightness: brightness != null ? brightness : null, until, seq: n });
      return n;
    });
    renderTiles();
    if (tileId) popTile(tileId);
    refreshMenu();
    const results = await Promise.all(devs.map(d => {
      const body = { id: d.id, state: on ? 'on' : 'off' };
      if (brightness != null) body.brightness = brightness;
      if (tone) Object.assign(body, tone);
      return api('/api/smarthome/set', body).catch(() => ({ status: 'error', error: 'Smart-Home-Server nicht erreichbar' }));
    }));
    smartEpoch++;
    const failed = devs.filter((d, i) => !results[i] || results[i].status !== 'ok');
    let changed = false;
    devs.forEach((d, i) => {
      if (cmdSeq.get(d.id) !== seqs[i]) return;          // a newer command for this device owns its state
      if (failed.includes(d)) { pending.delete(d.id); return; }
      const m = state.smart && state.smart.find(x => x.id === d.id);   // keep the new state until the next poll confirms it
      if (m) {
        m.state = on ? 'on' : 'off'; m.online = true;
        if (brightness != null) m.brightness = brightness;
        if (tone) { m.color = tone.color || kelvinHex(tone.kelvin); m.kelvin = tone.kelvin || null; }
        changed = true;
      }
    });
    if (failed.length) {
      const err = results.find(r => r && r.status !== 'ok');
      toast((failed.length === 1 ? failed[0].name : plural(failed.length, 'Gerät', 'Geräte')) + ': ' + ((err && err.error) || 'nicht erreichbar'), 3600);
      if (tileId) popTile(tileId, 'fail');
    }
    if (changed || failed.length) { renderTiles(); refreshMenu(); }   // e.g. the new colour at once, not with the next poll
    clearTimeout(smartRefreshTimer);
    smartRefreshTimer = setTimeout(fetchSmartDevices, 1500);
    return failed;
  }

  function activateTile(t) {
    if (t.kind === 'display') { screenOff(); return; }
    if (t.kind === 'spotify') { setAmbientMode(state.ambient && state.ambient.mode !== 'off' ? 'off' : ambientOnMode()); popTile(t.id); return; }
    if (t.kind === 'dev') {
      if (t.offline) { toast(t.title + ' ist gerade nicht erreichbar'); popTile(t.id, 'fail'); fetchSmartDevices(); return; }
      smartSet([t.dev], t.dev.state !== 'on', null, t.id);
      return;
    }
    roomSet(t, !t.on);   // room: anything on -> all off, otherwise all on
  }

  function screenOff() {
    toast('Display wird ausgeschaltet – der Ton läuft weiter');
    setTimeout(() => {
      // Backend first; fall back to a direct luna call if it is down, slow or reports failure.
      api('/api/quick', { action: 'screen_off' }, 4000).then(data => {
        if (!data || data.success !== true) throw new Error('screen_off failed');
      }).catch(() => {
        if (IS_TV) luna('luna://com.webos.service.tvpower/power/turnOffScreen', {}).catch(() => toast('Display ließ sich nicht ausschalten'));
      });
    }, 600);
  }

  async function fetchSmartDevices() {
    const seq = ++pollSeq, epoch = smartEpoch;
    let devs;
    try {
      devs = await api('/api/smarthome/devices', undefined, 8000);
      if (!Array.isArray(devs)) throw new Error('bad reply');
    } catch (e) {                               // backend down: keep showing what we have, say so after 2 misses
      if (seq < pollApplied) return;
      smartFails++;
      if (smartFails === 2) { renderTiles(); refreshMenu(); }
      return;
    }
    if (seq < pollApplied || epoch !== smartEpoch) return;   // an answer from before a newer poll or a command
    pollApplied = seq;
    devs = normDevices(devs);
    const wasDown = smartFails >= 2;
    smartFails = 0;
    smartFresh = true;
    const now = Date.now();
    for (const [id, p] of pending) {        // a fresh reading that agrees ends the optimistic phase
      const d = devs.find(x => x.id === id);
      if (p.until < now || (d && d.state === p.state && (p.brightness == null || d.brightness == null || Math.abs(d.brightness - p.brightness) < 2))) pending.delete(id);
    }
    const changed = JSON.stringify(devs) !== JSON.stringify(state.smart);
    state.smart = devs;
    if (changed || wasDown) {
      if (changed) { try { localStorage.setItem(SMART_CACHE_KEY, JSON.stringify(devs)); } catch (e) { /* ignore */ } }
      renderTiles();
      refreshMenu();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Menu                                                                */
  /* ------------------------------------------------------------------ */
  const menuEl = $('menu'), menuList = $('menu-list'), menuIcon = $('menu-icon');
  // Every menu is one list. Entries:
  //   action  { label, hint, action, danger, on (current choice: check mark), sub (opens a submenu: chevron), swatch }
  //   toggle  { label, hint, toggle: true|false, action }                       (a switch on the right)
  //   slider  { label, slider: { min, max, step, wrap, bar }, value(), text(v), hint(), change(v), full() }
  //           ◀▶ move it (held/fast presses: 3 steps), OK = action or close, ◀ never leaves the menu
  // Row heights are constants, so scrolling and the cursor never read layout.
  const MENU_CLIP_H = 496, ROW_H = 62, SLIDER_H = 104;
  let menu = null;              // { entries, index, back, onClose, rows, tops }
  let menuArmed = true;         // OK only counts after the key that opened the menu was released
  let menuRefresh = null;       // rebuilds the open menu in place (live smart home state)
  let menuCursor = null;

  function menuHead(def) {
    $('menu-title').textContent = def.title;
    $('menu-sub').textContent = def.sub || '';
    menuIcon.className = '';
    menuIcon.style.backgroundImage = def.icon ? 'url("' + def.icon + '")' : '';
    menuIcon.style.setProperty('--c', def.color || '');
    if (def.icon) { menuIcon.textContent = ''; menuIcon.classList.add('img'); }
    else if (def.glyph) {
      menuIcon.innerHTML = def.glyphHtml || svg(def.glyph);
      menuIcon.classList.add('glyph');
      if (def.round) menuIcon.classList.add('round');
      if (def.lit) menuIcon.classList.add('lit');
    } else if (def.swatch) { menuIcon.textContent = ''; menuIcon.classList.add('swatch', 'round'); }
    else menuIcon.classList.add('none');
  }

  function menuRow(entry, i) {
    const row = h('div', 'mi' + (entry.danger ? ' danger' : '') + (entry.on ? ' on' : '') + (entry.slider ? ' slider' : ''));
    row.setAttribute('role', entry.slider ? 'slider' : 'menuitem');
    const top = h('div', 'mi-line');
    const label = h('span', 'mi-label', entry.label);
    if (entry.swatch) {
      const sw = h('i', 'mi-swatch');
      sw.style.setProperty('--sw', entry.swatch);
      label.insertBefore(sw, label.firstChild);
    }
    top.appendChild(label);
    const right = h('span', 'mi-right');
    const hint = h('span', 'mi-hint', entry.slider ? '' : (entry.hint || ''));
    right.appendChild(hint);
    if (entry.on) right.insertAdjacentHTML('beforeend', '<span class="mi-mark">' + svg('check') + '</span>');
    if (entry.sub) right.insertAdjacentHTML('beforeend', '<span class="mi-mark sub">' + svg('chevron') + '</span>');
    if (typeof entry.toggle === 'boolean') right.appendChild(h('i', 'mi-switch' + (entry.toggle ? ' on' : '')));
    top.appendChild(right);
    row.appendChild(top);
    const parts = { row, hint };
    if (entry.slider) {
      const bar = h('div', 'mi-bar ' + (entry.slider.bar || 'level'));
      const fill = h('i', 'mi-fill');
      const knobBox = h('span', 'mi-knob');
      knobBox.appendChild(h('b', ''));
      bar.appendChild(fill);
      bar.appendChild(knobBox);
      row.appendChild(bar);
      Object.assign(parts, { bar, fill, knobBox });
      bindSliderPointer(bar, i);
    }
    row.addEventListener('mouseenter', () => menuFocus(i));
    row.addEventListener('click', e => {
      if (entry.slider && parts.bar.contains(e.target)) return;   // a click on the bar sets the value (bindSliderPointer)
      menuFocus(i); menuSelect();
    });
    return parts;
  }

  function openMenu(def) {
    const reopen = (mode === 'menu') && menu;
    if (menu && menu.onClose && menu.onClose !== def.onClose) menu.onClose();   // e.g. the colour picker sends its last value
    menu = { entries: def.entries, index: Math.max(0, Math.min(def.index || 0, def.entries.length - 1)),
      back: def.back || null, onClose: def.onClose || null, rows: [], tops: [] };
    menuRefresh = def.refresh || null;
    menuHead(def);
    $('menu-hint').textContent = def.hint || '';
    menuList.textContent = '';
    menuCursor = h('div', '');
    menuCursor.id = 'menu-cursor';
    menuList.appendChild(menuCursor);
    let y = 0;
    def.entries.forEach((entry, i) => {
      const parts = menuRow(entry, i);
      menu.rows.push(parts);
      menu.tops.push(y);
      y += entry.slider ? SLIDER_H : ROW_H;
      menuList.appendChild(parts.row);
      if (entry.slider) paintSlider(i);
    });
    menu.height = y;
    menuEl.hidden = false;
    mode = 'menu';
    menuArmed = reopen ? menuArmed : !okDown;
    menuFocus(menu.index, !reopen);
    requestAnimationFrame(() => { if (menu) document.body.classList.add('menu-open'); });
  }

  function menuFocus(i, instant) {
    if (!menu || !menu.entries.length) return;
    menu.index = Math.max(0, Math.min(menu.entries.length - 1, i));
    menu.rows.forEach((p, j) => p.row.classList.toggle('focused', j === menu.index));
    const top = menu.tops[menu.index], hgt = (menu.entries[menu.index].slider ? SLIDER_H : ROW_H) - 4;
    if (menuCursor) {
      if (instant) menuCursor.style.transition = 'none';
      menuCursor.style.transform = 'translateY(' + top + 'px)';
      menuCursor.style.height = hgt + 'px';
      if (instant) { void menuCursor.offsetWidth; menuCursor.style.transition = ''; }
    }
    // keep the focused row near the middle of the clip
    const scroll = Math.max(0, Math.min(menu.height - 4 - MENU_CLIP_H, top - (MENU_CLIP_H - hgt) / 2));
    menuList.style.transform = scroll > 0 ? 'translateY(' + (-Math.round(scroll)) + 'px)' : '';
  }

  function menuSelect() {
    const entry = menu && menu.entries[menu.index];
    if (!entry) return;
    if (entry.action) entry.action();
    else if (entry.slider) closeMenu();          // OK on a slider: done
  }

  function closeMenu() {
    const done = menu && menu.onClose;
    menu = null;
    menuRefresh = null;
    if (done) done();
    if (mode === 'menu') mode = 'home';
    flushRender();
    document.body.classList.remove('menu-open');
    setTimeout(() => { if (!menu) menuEl.hidden = true; }, 320);
  }

  function menuBack() {
    const back = menu && menu.back;
    if (back) back(); else closeMenu();
  }

  /* sliders */
  function sliderValue(entry) {
    const s = entry.slider, v = Number(entry.value());
    return Math.max(s.min, Math.min(s.max, isFinite(v) ? v : s.min));
  }

  function paintSlider(i) {
    const entry = menu && menu.entries[i], p = menu && menu.rows[i];
    if (!entry || !p || !p.bar) return;
    const s = entry.slider, v = sliderValue(entry);
    const f = ((v - s.min) / (s.max - s.min)).toFixed(4);
    p.bar.style.setProperty('--p', f);
    if (entry.full) p.bar.style.setProperty('--full', entry.full());
    const extra = entry.hint ? entry.hint() : '';
    p.hint.textContent = (extra ? extra + '  ·  ' : '') + entry.text(v);
  }

  let sliderLastKey = 0;
  function sliderStep(dir, e) {
    const entry = menu.entries[menu.index], s = entry.slider;
    const now = Date.now();
    const fast = e && (e.repeat || now - sliderLastKey < 260) ? 3 : 1;   // held key, or the phone app repeating
    sliderLastKey = now;
    const cur = sliderValue(entry);
    let v = cur + dir * s.step * fast;
    if (s.wrap) v = ((v - s.min) % (s.max - s.min + s.step) + (s.max - s.min + s.step)) % (s.max - s.min + s.step) + s.min;
    else v = Math.max(s.min, Math.min(s.max, v));
    if (v === cur) { bump(menu.rows[menu.index].bar, dir < 0 ? 'left' : 'right'); return; }
    sliderSet(menu.index, v);
  }

  function sliderSet(i, v) {
    const entry = menu.entries[i];
    entry.change(v);
    for (let j = 0; j < menu.entries.length; j++) if (menu.entries[j].slider) paintSlider(j);   // linked sliders ("aktiv")
    if (menu.paint) menu.paint();
  }

  // Magic Remote: click or drag on a slider bar
  function bindSliderPointer(bar, i) {
    const at = e => {
      if (!menu || !menu.entries[i] || !menu.entries[i].slider) return;
      const s = menu.entries[i].slider, b = bar.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (e.clientX - b.left) / Math.max(1, b.width)));
      const v = Math.round((s.min + f * (s.max - s.min)) / s.step) * s.step;
      menuFocus(i);
      sliderSet(i, Math.max(s.min, Math.min(s.max, v)));
    };
    bar.addEventListener('mousedown', e => { e.stopPropagation(); at(e); });
    bar.addEventListener('mousemove', e => { if (e.buttons & 1) at(e); });
  }

  // Sends the newest value only: while a request runs, later values wait and only the last one goes out.
  const senders = new Map();
  function sendLatest(key, fn) {
    let s = senders.get(key);
    if (!s) { s = { busy: false, next: null }; senders.set(key, s); }
    if (s.busy) { s.next = fn; return; }
    s.busy = true;
    Promise.resolve().then(fn).catch(() => {}).then(() => {
      s.busy = false;
      const n = s.next;
      s.next = null;
      if (n) sendLatest(key, n);
    });
  }

  // Brightness while it is being changed: the slider shows this, not the (older) lamp state
  const drafts = new Map();       // mac -> {value, at}
  const draftOf = (id, field) => { const x = drafts.get(id + field); return x && Date.now() - x.at < 4000 ? x.value : null; };
  let draftTimer = 0;
  function setLevel(d, v) {
    drafts.set(d.id + 'bri', { value: v, at: Date.now() });
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => sendLatest(d.id, () => smartSet([d], true, v, null).then(failed => {
      if (failed && failed.length) drafts.delete(d.id + 'bri');
    })), 160);   // the lamp follows once the value rests a moment (no request per key)
  }

  function openCardMenu(id) {
    if (id === 'hero:weather' || id === WETTER_APP) {
      openMenu({
        title: 'Wetter', sub: placeName() || 'Vorhersage', glyph: $('w-icon').dataset.icon || 'weather',
        entries: [
          { label: 'WetterOnline öffnen', hint: 'Wetter & Radar', action: () => { closeMenu(); openWeatherApp(); } },
          { label: 'Wetter aktualisieren', action: () => { closeMenu(); fetchWeather(true); } }
        ]
      });
      return;
    }
    if (id === 'hero:gemini') { openGeminiModal(); return; }
    if (id === 'hero:audio') {
      openMenu({
        title: 'Audioausgabe', sub: $('audio-title').textContent, glyph: $('audio-icon').dataset.icon || 'speaker',
        entries: [{ label: 'Ton-Einstellungen öffnen', action: () => { closeMenu(); openAudioSettings(); } }]
      });
      return;
    }
    if (tiles.has(id)) { openTileMenu(id); return; }
    const itemId = itemIdOf(id);
    const it = items.get(itemId);
    if (!it) return;
    if (it.kind === 'util') { openMoreMenu(); return; }
    const pinned = prefs.favorites.includes(itemId);
    const entries = [{ label: it.kind === 'input' ? 'Umschalten' : 'Öffnen', action: () => { closeMenu(); activate(id); } }];
    if (pinned) entries.push({ label: 'Nicht mehr anheften', hint: 'Reihenfolge nach Nutzung', action: () => { closeMenu(); setFavorite(itemId, false); } });
    else entries.push({ label: 'Bei den Favoriten anheften', action: () => { closeMenu(); setFavorite(itemId, true); } });
    if (pinned && favIds.includes(itemId) && prefs.favorites.filter(x => favIds.includes(x)).length > 1) {
      entries.push({ label: 'Verschieben', action: () => { closeMenu(); startMove(itemId); } });
    }
    entries.push({ label: 'Ausblenden', danger: true, action: () => { closeMenu(); setHidden(itemId, true); } });
    const el = els.get(id);
    openMenu({ title: it.title, sub: it.kind === 'input' ? it.sub : (lastUse(itemId) ? 'Zuletzt ' + ago(lastUse(itemId)) : ''),
      icon: it.icon || null, glyph: it.glyph || null, color: el ? el.style.getPropertyValue('--c') : '', entries });
  }

  function tileHead(t, title, sub) {
    return { title, sub, glyph: t.icon, round: true, lit: !!t.on, color: t.on ? lampOf(t) : '' };
  }

  function openTileMenu(id, index) {
    const t = tiles.get(id);
    if (!t) { closeMenu(); return; }
    const again = () => openTileMenu(id, menu ? menu.index : 0);
    if (t.kind === 'spotify') { openAmbientMenu(index || 0); return; }
    if (t.kind === 'display') {
      openMenu(Object.assign(tileHead(t, 'Display aus', 'Musik-Modus · der Ton läuft weiter'), {
        hint: 'Nur das OLED-Panel geht aus. Jede Taste der Fernbedienung schaltet es wieder ein.',
        entries: [{ label: 'Display jetzt ausschalten', action: () => { closeMenu(); screenOff(); } }]
      }));
      return;
    }
    if (t.kind === 'dev') {
      const d = t.dev, caps = d.capabilities || [], light = d.type === 'light';
      const entries = [{ label: light ? 'Licht' : 'Steckdose', hint: t.offline ? 'nicht erreichbar' : '',
        toggle: d.state === 'on' && !t.offline, action: () => { activateTile(tiles.get(id) || t); } }];
      if (light) {
        entries.push({
          label: 'Helligkeit', slider: { min: 10, max: 100, step: 5, bar: 'level' },
          value: () => { const x = draftOf(d.id, 'bri'); return x != null ? x : (typeof d.brightness === 'number' ? d.brightness : 100); },
          text: v => Math.round(v) + ' %',
          hint: () => (d.state === 'on' ? '' : 'schaltet ein'),
          change: v => setLevel(d, v)
        });
      }
      if (light && (caps.includes('color') || caps.includes('white'))) {
        entries.push({ label: 'Farbe', hint: toneName(d), sub: true, swatch: d.state === 'on' && d.color ? d.color : null,
          action: () => openToneMenu(id, () => openTileMenu(id, entries.findIndex(x => x.label === 'Farbe'))) });
      }
      entries.push({ label: 'Hauptraum wechseln', hint: d.room, sub: true, action: () => openRoomPicker(again) });
      openMenu(Object.assign(tileHead(t, d.name, d.room + ' · ' + deviceState(d)), { entries, index, refresh: again }));
      return;
    }
    // room
    const reachable = () => t.devs.filter(d => d.online !== false);
    const entries = [
      { label: 'Alle einschalten', action: () => roomSet(t, true) },
      { label: 'Alle ausschalten', action: () => roomSet(t, false) }
    ];
    for (const d of t.devs) {
      const off = d.online === false;
      entries.push({ label: d.name, hint: deviceState(d), toggle: d.state === 'on' && !off,
        action: () => { if (off) toast(d.name + ' ist gerade nicht erreichbar'); else smartSet([d], d.state !== 'on', null, id); } });
    }
    entries.push({ label: 'Als Hauptraum zeigen', hint: 'Geräte einzeln', action: () => { setMainRoom(t.room); closeMenu(); } });
    openMenu(Object.assign(tileHead(t, t.room, t.state + (reachable().length < t.devs.length ? ' · ' + (t.devs.length - reachable().length) + ' nicht erreichbar' : '')),
      { entries, index, refresh: again }));
  }

  function roomSet(t, on) {
    const reachable = t.devs.filter(d => d.online !== false);
    if (!reachable.length) { toast(t.room + ': kein Gerät erreichbar'); popTile(t.id, 'fail'); fetchSmartDevices(); return; }
    smartSet(reachable, on, null, t.id);
  }

  // colours and white tones for WiZ lights (white needs caps "white", colours need "color")
  const TONES = [
    { label: 'Warmweiß', hint: '2700 K', kelvin: 2700 },
    { label: 'Neutralweiß', hint: '4000 K', kelvin: 4000 },
    { label: 'Tageslicht', hint: '6500 K', kelvin: 6500 },
    { label: 'Rot', color: '#ff0000' },
    { label: 'Orange', color: '#ff6000' },
    { label: 'Gelb', color: '#ffc000' },
    { label: 'Grün', color: '#00ff20' },
    { label: 'Türkis', color: '#00ffc0' },
    { label: 'Blau', color: '#0030ff' },
    { label: 'Lila', color: '#8000ff' },
    { label: 'Pink', color: '#ff0080' }
  ];
  function kelvinHex(k) {   // rough colour of a white tone, only for the tile until the next poll
    if (!k) return null;
    const t = Math.max(0, Math.min(1, (k - 2200) / 4300));
    const c = (a, b) => Math.round(a + (b - a) * t).toString(16).padStart(2, '0');
    return '#' + c(255, 255) + c(160, 245) + c(60, 235);
  }
  function toneIs(d, x) {
    if (d.state !== 'on') return false;
    if (x.kelvin) return !!d.kelvin && Math.abs(d.kelvin - x.kelvin) < 150;
    return !d.kelvin && typeof d.color === 'string' && d.color.toLowerCase() === x.color;
  }

  function openToneMenu(id, back, index) {
    const t = tiles.get(id);
    if (!t || t.kind !== 'dev') { closeMenu(); return; }
    const d = t.dev, caps = d.capabilities || [];
    const tones = TONES.filter(x => caps.includes(x.kelvin ? 'white' : 'color'));
    const again = () => openToneMenu(id, back, menu ? menu.index : 0);
    const custom = !!toneName(d) && !TONES.some(x => toneIs(d, x));
    const entries = [{
      label: caps.includes('color') ? 'Eigene Farbe' : 'Eigener Weißton', hint: custom ? (d.kelvin ? toneName(d) : '') : (caps.includes('color') ? 'alle Farben' : 'stufenlos'),
      on: custom, sub: true, swatch: caps.includes('color') ? 'conic-gradient(#f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)' : 'linear-gradient(90deg, #ffb46b, #fff, #dfe9ff)',
      action: () => openPicker(id, () => openToneMenu(id, back, 0))
    }];
    for (const x of tones) {
      entries.push({ label: x.label, hint: x.hint || '', on: toneIs(d, x), swatch: x.color || kelvinHex(x.kelvin),
        action: () => smartSet([d], true, null, id, x.kelvin ? { kelvin: x.kelvin } : { color: x.color }) });
    }
    openMenu(Object.assign(tileHead(t, 'Farbe', d.name + ' · ' + deviceState(d)), {
      entries, index: index || 0, refresh: again, back,
      hint: 'OK stellt die Farbe ein, das Menü bleibt offen.'
    }));
  }

  /* Colour picker: every colour (hue + saturation) and every white tone, live on the lamp. Three sliders in
     the same menu: ▲▼ picks one, ◀▶ moves it (held keys move faster), OK closes, Back returns to the colours.
     The slider moved last is "aktiv" and decides whether the lamp gets a colour or a white tone. */
  const KELVIN_MIN = 2200, KELVIN_MAX = 6500;

  function hsvHex(h, s) {   // value is always full: the lamp's brightness is set separately
    const f = n => { const k = (n + h / 60) % 6; return 1 - s * Math.max(0, Math.min(k, 4 - k, 1)); };
    return '#' + [f(5), f(3), f(1)].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
  }
  function hexHsv(hex) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    if (!m) return null;
    const [r, g, b] = [m[1], m[2], m[3]].map(x => parseInt(x, 16) / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), c = max - min;
    let h = 0;
    if (c) h = max === r ? 60 * (((g - b) / c) % 6) : max === g ? 60 * ((b - r) / c + 2) : 60 * ((r - g) / c + 4);
    return { h: (h + 360) % 360, s: max ? c / max : 0 };
  }

  function openPicker(id, back) {
    const t = tiles.get(id);
    if (!t || t.kind !== 'dev') { closeMenu(); return; }
    const d = t.dev, caps = d.capabilities || [];
    const color = caps.includes('color'), white = caps.includes('white');
    if (!color && !white) { closeMenu(); return; }
    const hsv = (!d.kelvin && hexHsv(d.color)) || { h: 30, s: 1 };
    const p = {
      h: Math.round(hsv.h / 5) * 5 % 360, s: Math.round(hsv.s * 20) / 20,
      k: Math.max(KELVIN_MIN, Math.min(KELVIN_MAX, Math.round((d.kelvin || 2700) / 100) * 100)),
      white: !!d.kelvin || !color, dirty: false, timer: 0
    };
    const shown = () => (p.white ? kelvinHex(p.k) : hsvHex(p.h, p.s));
    const send = () => {
      clearTimeout(p.timer);
      if (!p.dirty) return;
      p.dirty = false;
      const tone = p.white ? { kelvin: p.k } : { color: hsvHex(p.h, p.s) };
      sendLatest(d.id, () => smartSet([d], true, null, null, tone));
    };
    const changed = () => { p.dirty = true; clearTimeout(p.timer); p.timer = setTimeout(send, 160); };
    const active = on => (on ? 'aktiv' : '');
    const entries = [];
    if (color) {
      entries.push({ label: 'Farbton', slider: { min: 0, max: 355, step: 5, wrap: true, bar: 'hue' },
        value: () => p.h, text: v => Math.round(v) + '°', hint: () => active(!p.white),
        change: v => { p.h = v; p.white = false; changed(); } });
      entries.push({ label: 'Sättigung', slider: { min: 0, max: 100, step: 5, bar: 'sat' },
        value: () => Math.round(p.s * 100), text: v => Math.round(v) + ' %', hint: () => active(!p.white), full: () => hsvHex(p.h, 1),
        change: v => { p.s = v / 100; p.white = false; changed(); } });
    }
    if (white) {
      entries.push({ label: 'Weißton', slider: { min: KELVIN_MIN, max: KELVIN_MAX, step: 100, bar: 'kelvin' },
        value: () => p.k, text: v => Math.round(v) + ' K', hint: () => active(p.white),
        change: v => { p.k = v; p.white = true; changed(); } });
    }
    openMenu({ title: color ? 'Eigene Farbe' : 'Eigener Weißton', sub: d.name, swatch: true, entries, back, onClose: send,
      index: p.white && white ? entries.length - 1 : 0,
      hint: '▲▼ Regler wählen  ·  ◀▶ verstellen (halten = schneller)  ·  OK fertig' });
    menu.paint = () => menuIcon.style.setProperty('--c', shown());
    menu.paint();
  }

  /* ------------------------------------------------------------------ */
  /* Spotify-Licht: the backend colours a WiZ lamp with the cover of the */
  /* song that plays. The launcher only shows the state and changes the  */
  /* settings (mode, calibration); the backend does all the work.        */
  /* ------------------------------------------------------------------ */
  const AMBIENT_MODES = [
    { mode: 'off', label: 'Aus', hint: 'Lampe bleibt, wie sie ist' },
    { mode: 'spotify', label: 'Spotify', hint: 'Farbe des Songs, der auf dem TV läuft' },
    { mode: 'ambilight', label: 'Ambilight', hint: 'Spotify, sonst die Farben vom Bildschirm' }
  ];
  const AMBIENT_SAMPLE = '#f07628';   // slider colours before any colour was seen
  const ambientDraft = {};            // calibration values being changed: key -> {value, at}
  let ambientTimer = 0, ambientModeSeq = 0;

  const ambientModeLabel = m => (AMBIENT_MODES.find(x => x.mode === m) || AMBIENT_MODES[1]).label;
  // mode OK switches on: the last one used (older versions saved 'tv' / 'all')
  const ambientOnMode = () => (AMBIENT_MODES.some(x => x.mode === prefs.ambientMode && x.mode !== 'off') ? prefs.ambientMode : 'spotify');
  const ambientLit = a => (a.state === 'active' || a.state === 'preview') && /^#[0-9a-f]{6}$/i.test(a.sent || '');
  const ambientTrouble = a => a.mode !== 'off' && a.state === 'error';

  function ambientStateText(a) {
    if (a.mode === 'off') return 'Aus';
    const s = { active: 'aktiv', preview: 'Vorschau', idle: 'wartet', lamp_off: 'Lampe aus', override: 'pausiert',
      error: 'Problem', off: 'Aus', starting: '…' }[a.state] || '…';
    return ambientModeLabel(a.mode) + ' · ' + s;
  }

  function ambientTile(a) {
    return { id: AMBIENT_TILE, kind: 'spotify', title: 'Spotify-Licht', state: ambientStateText(a), on: a.mode !== 'off',
      offline: ambientTrouble(a), lamp: ambientLit(a) ? a.sent : '#1ed760', icon: 'music' };
  }

  // one line saying what the lamp does right now (the backend's message is more specific when there is one)
  function ambientSentence(a) {
    if (a.mode === 'off') return 'Ausgeschaltet – die Lampe bleibt, wie sie ist';
    if (a.state === 'active') return a.source === 'screen' ? 'Die Lampe zeigt die Farben vom Bildschirm' : 'Die Lampe zeigt die Farbe des Songs';
    if (a.state === 'preview') return 'Vorschau der Kalibrierung';
    if (a.message) return a.message;
    return a.mode === 'ambilight' ? 'Wartet auf Musik oder ein Bild' : 'Wartet auf Musik vom Fernseher';
  }

  function ambientBillboard(t) {
    const a = state.ambient || {};
    const meta = [];
    if (a.track && a.track.name) meta.push({ swatch: ambientLit(a) ? a.sent : null, text: a.track.name + (a.track.artist ? ' – ' + a.track.artist : '') });
    if (a.lamp && a.lamp.name) meta.push({ icon: 'bulb', text: a.lamp.name });
    if (ambientTrouble(a)) meta.push({ warn: true, text: ambientStateText(a) });
    return { kicker: 'Smart Home · ' + (a.mode === 'off' ? 'Aus' : ambientModeLabel(a.mode)), title: 'Spotify-Licht', desc: ambientSentence(a), meta,
      keys: [['OK', a.mode === 'off' ? 'Einschalten (' + ambientModeLabel(ambientOnMode()) + ')' : 'Ausschalten'],
        [{ text: 'OK', hold: true }, 'Modus & Kalibrierung']],
      icon: 'music', logo: 'icon round' + (t.on ? ' on' : '') + (t.offline ? ' offline' : ''), lamp: t.lamp,
      glow: t.on ? t.lamp : 'rgb(70,78,96)' };
  }

  // poll fast only while someone looks at it
  const ambientHot = () => focusId === AMBIENT_TILE || (mode === 'menu' && !!menu && !!menu.ambient);

  async function fetchAmbient() {
    const seq = ambientModeSeq;
    let a;
    try { a = await api('/api/spotify/ambient', undefined, 5000); } catch (e) { return; }   // backend down: the smart home row says so
    if (!a || typeof a.mode !== 'string' || seq !== ambientModeSeq) return;   // older backend without the feature: no tile
    applyAmbient(a);
  }

  function applyAmbient(a) {
    const before = JSON.stringify(state.ambient);
    state.ambient = a;
    if (JSON.stringify(a) === before) return;
    renderTiles();
    if (mode === 'menu' && menu && menu.ambient) {   // the open menu: header only (rebuilding it would move the sliders under the user's hand)
      $('menu-sub').textContent = ambientMenuSub();
      if (menu.paint) menu.paint();
    }
  }

  function setAmbientMode(m) {
    if (!state.ambient) return;
    if (m !== 'off') { prefs.ambientMode = m; savePrefs(); }
    const seq = ++ambientModeSeq;                // a poll that started before this must not undo it
    state.ambient = Object.assign({}, state.ambient, { mode: m });
    renderTiles();
    api('/api/spotify/ambient/mode', { mode: m }).then(a => {
      if (seq !== ambientModeSeq) return;
      if (a && a.success) applyAmbient(a); else throw new Error((a && a.error) || 'failed');
    }).catch(() => {
      toast('Spotify-Licht: Einstellung nicht gespeichert');
      popTile(AMBIENT_TILE, 'fail');
      ambientModeSeq++;
      fetchAmbient();
    });
  }

  function ambientCal(key) {
    const d = ambientDraft[key];
    if (d && Date.now() - d.at < 4000) return d.value;
    const c = state.ambient && state.ambient.calibration;
    return c && typeof c[key] === 'number' ? c[key] : { saturation: 1.4, brightness: 35, hue_shift: 0 }[key];
  }

  // slider moved: the lamp follows once the value rests a moment (no request per key press)
  function setAmbientCal(key, value) {
    ambientDraft[key] = { value, at: Date.now() };
    clearTimeout(ambientTimer);
    ambientTimer = setTimeout(() => {
      const body = { saturation: ambientCal('saturation'), brightness: ambientCal('brightness'), hue_shift: ambientCal('hue_shift'), preview: true };
      sendLatest('ambient', () => api('/api/spotify/ambient/calibration', body).then(a => {
        if (a && a.success) applyAmbient(a); else toast('Kalibrierung nicht gespeichert');
      }).catch(() => toast('Smart-Home-Server nicht erreichbar')));
    }, 160);
  }

  // the cover colour the sliders work on, and the same after hue shift + saturation (as the backend does it)
  function ambientBase() {
    const a = state.ambient || {};
    return /^#[0-9a-f]{6}$/i.test(a.color || '') ? a.color : AMBIENT_SAMPLE;
  }
  function ambientShifted(shift, satFactor) {
    const hs = hexHsv(ambientBase()) || { h: 25, s: 0.85 };
    return hsvHex(((hs.h + shift) % 360 + 360) % 360, Math.max(0, Math.min(1, hs.s * satFactor)));
  }

  function ambientMenuSub() {
    const a = state.ambient || {};
    return (a.lamp && a.lamp.name ? a.lamp.name + ' · ' : '') + ambientStateText(a);
  }

  function openAmbientMenu(index) {
    if (!state.ambient) { closeMenu(); return; }
    const again = () => openAmbientMenu(menu ? menu.index : 0);
    const entries = AMBIENT_MODES.map(x => ({ label: x.label, hint: x.hint, on: state.ambient.mode === x.mode,
      action: () => { setAmbientMode(x.mode); again(); } }));
    entries.push({
      label: 'Sättigung', slider: { min: 0, max: 300, step: 10, bar: 'sat' },
      value: () => Math.round(ambientCal('saturation') * 100), text: v => Math.round(v) + ' %',
      full: () => ambientShifted(ambientCal('hue_shift'), 1), change: v => setAmbientCal('saturation', v / 100)
    });
    entries.push({
      label: 'Helligkeit', slider: { min: 10, max: 100, step: 5, bar: 'level' },
      value: () => ambientCal('brightness'), text: v => Math.round(v) + ' %', change: v => setAmbientCal('brightness', v)
    });
    entries.push({
      label: 'Farbton', slider: { min: -180, max: 180, step: 5, bar: 'shift' },
      value: () => ambientCal('hue_shift'), text: v => (v > 0 ? '+' : '') + Math.round(v) + '°',
      full: () => 'linear-gradient(90deg,' + [-180, -120, -60, 0, 60, 120, 180].map(d => ambientShifted(d, Math.max(ambientCal('saturation'), 0.6))).join(',') + ')',
      change: v => setAmbientCal('hue_shift', v)
    });
    openMenu({ title: 'Spotify-Licht', sub: ambientMenuSub(), swatch: true, entries, index,
      hint: 'Die Regler wirken sofort auf die Lampe, auch ohne Musik (10 s Vorschau).' });
    menu.ambient = true;
    menu.paint = () => menuIcon.style.setProperty('--c', ambientShifted(ambientCal('hue_shift'), ambientCal('saturation')));
    menu.paint();
  }

  function setMainRoom(room) {
    prefs.mainRoom = room === DEFAULT_ROOM ? '' : room;
    savePrefs();
    renderTiles();
    toast('Smart Home zeigt jetzt ' + room);
  }

  function openRoomPicker(back) {
    const devs = smartDevices();
    const main = mainRoom(devs);
    const rooms = roomsOf(devs);
    const goBack = back || (() => openMoreMenu(0));
    const entries = rooms.length
      ? rooms.map(r => ({ label: r, hint: plural(devs.filter(d => d.room === r).length, 'Gerät', 'Geräte'),
        on: r === main, action: () => { setMainRoom(r); closeMenu(); } }))
      : [{ label: 'Keine Smart-Home-Geräte', hint: 'zurück', action: goBack }];
    openMenu({
      title: 'Hauptraum', sub: 'Seine Geräte erscheinen einzeln', glyph: 'room', round: true,
      entries, index: Math.max(0, rooms.indexOf(main)), back: goBack
    });
  }

  function openMoreMenu(index) {
    const hidden = hiddenItems();
    openMenu({
      title: 'Mehr', sub: 'Apps & Einstellungen', glyph: 'more', index,
      entries: [
        { label: 'Smart Home: Hauptraum', hint: mainRoom(smartDevices()) || '–', sub: true, action: () => openRoomPicker() },
        { label: 'Ausgeblendete Apps', hint: String(hidden.length), sub: true, action: () => openHiddenMenu(0) },
        { label: 'TV-Einstellungen', action: () => { closeMenu(); launchId(SETTINGS, 'Einstellungen'); } },
        { label: 'App-Liste aktualisieren', action: () => { closeMenu(); refreshData(true).then(() => toast('App-Liste aktualisiert')); } },
        { label: 'LG-Startbildschirm öffnen', action: () => { closeMenu(); openLgHome(); } },
        { label: 'Auf Standard zurücksetzen', danger: true, sub: true, action: openResetMenu }
      ],
      hint: 'Tipp: OK gedrückt halten öffnet das Menü jeder Kachel.'
    });
  }

  function openHiddenMenu(index) {
    const hidden = hiddenItems().sort((a, b) => a.title.localeCompare(b.title, 'de', { sensitivity: 'base' }));
    const entries = hidden.length
      ? hidden.map(it => ({ label: it.title, hint: 'einblenden', action: () => { setHidden(it.id, false); openHiddenMenu(menu ? menu.index : 0); } }))
      : [{ label: 'Keine ausgeblendeten Apps', hint: 'zurück', action: () => openMoreMenu(1) }];
    openMenu({ title: 'Ausgeblendete Apps', sub: hidden.length ? plural(hidden.length, 'App', 'Apps') : '', glyph: 'more',
      entries, index, back: () => openMoreMenu(1), hint: hidden.length ? 'OK blendet die App wieder ein.' : '' });
  }

  function openResetMenu() {
    openMenu({
      title: 'Zurücksetzen?', sub: 'Favoriten, Ausblendungen und Hauptraum', glyph: 'refresh', back: () => openMoreMenu(5), index: 1,
      entries: [
        { label: 'Ja, alles zurücksetzen', danger: true, action: () => { prefs = clone(DEFAULTS); savePrefs(); closeMenu(); focusId = null; render(); toast('Zurückgesetzt'); } },
        { label: 'Abbrechen', action: () => openMoreMenu(5) }
      ]
    });
  }

  function openLgHome() {
    if (!IS_TV) { toast('Nur auf dem TV verfügbar'); return; }
    // tell the root helper to let LG's home screen through once, then open it
    rootExec('touch /tmp/custom-launcher.pass')
      .then(() => luna(SAM + 'launch', { id: LG_HOME }))
      .catch(() => toast('LG-Startbildschirm lässt sich nicht öffnen'));
  }

  /* ------------------------------------------------------------------ */
  /* Favourites, hiding, moving                                          */
  /* ------------------------------------------------------------------ */
  function setFavorite(id, on) {
    prefs.favorites = prefs.favorites.filter(x => x !== id);
    if (on) {
      if (prefs.favorites.length >= FAVS) prefs.favorites = prefs.favorites.slice(0, FAVS - 1);
      prefs.favorites.push(id);
      prefs.hidden = prefs.hidden.filter(x => x !== id);
    }
    savePrefs();
    if (on) focusId = 'favs|' + id;              // follow the card into the first row
    render();
    toast(on ? 'Bei den Favoriten angeheftet' : 'Nicht mehr angeheftet – Reihenfolge folgt der Nutzung');
  }

  function setHidden(id, on) {
    const it = items.get(id);
    prefs.hidden = prefs.hidden.filter(x => x !== id);
    if (on) {
      prefs.hidden.push(id);
      prefs.favorites = prefs.favorites.filter(x => x !== id);
      // keep the focus near where the card was (same row, next card)
      const pos = posOf(focusId);
      if (pos && itemIdOf(focusId) === id) {
        const rest = pos.row.keys.filter(k => itemIdOf(k) !== id);
        focusId = rest[Math.min(pos.c, rest.length - 1)] || null;
      }
    }
    savePrefs();
    render();
    if (it) toast(on ? '„' + it.title + '“ ausgeblendet – unter „Mehr“ wieder einblendbar' : '„' + it.title + '“ eingeblendet');
  }

  function startMove(id) {
    const key = 'favs|' + id;
    const el = els.get(key);
    if (!el) return;
    setFocus(key);
    mode = 'move';
    el.classList.add('moving');
    document.body.classList.add('moving');
    toast('Pfeiltasten verschieben · OK fertig', 4000);
  }

  function endMove() {
    mode = 'home';
    flushRender();
    document.body.classList.remove('moving');
    for (const el of document.querySelectorAll('.card.moving')) el.classList.remove('moving');
  }

  function moveCard(dir) {
    const key = focusId, id = itemIdOf(key);
    const list = prefs.favorites.filter((x, i, arr) => favIds.includes(x) && arr.indexOf(x) === i);
    const from = list.indexOf(id);
    const to = from + ({ left: -1, right: 1, up: 0, down: 0 })[dir];
    if (from < 0 || to < 0 || to >= list.length || to === from) { bump(els.get(key), dir); return; }
    list.splice(to, 0, list.splice(from, 1)[0]);
    prefs.favorites = list.concat(prefs.favorites.filter(x => !list.includes(x)));
    savePrefs();
    render();
    const el = els.get(key);
    if (el) el.classList.add('moving');
  }

  /* ------------------------------------------------------------------ */
  /* Keys and pointer                                                    */
  /* ------------------------------------------------------------------ */
  // INFO: "long OK" from the phone app (the network input can only press+release, never hold)
  const KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, OK: 13, BACK: 461, ESC: 27, INFO: 457 };
  const DIRS = { 37: 'left', 38: 'up', 39: 'right', 40: 'down' };
  let okDown = false, okLong = false, okTimer = 0;
  let okBlocked = false;         // OK is still held from an action that just finished: ignore until released

  function pressed(on) {
    const el = els.get(focusId);
    if (el) el.classList.toggle('pressed', on);
  }

  document.addEventListener('keydown', e => {
    const k = e.keyCode;
    wake();                        // any key counts as activity (also inside Gemini, also colour keys)
    if (geminiModalOpen) {
      if (k === KEY.BACK || k === KEY.ESC) { e.preventDefault(); closeGeminiModal(); return; }
      if (k in DIRS) { e.preventDefault(); geminiKey(k); return; }
      if (k === KEY.OK) {
        if (e.repeat) { e.preventDefault(); return; }
        if (document.activeElement && typeof document.activeElement.click === 'function') {
          e.preventDefault();
          document.activeElement.click();
        }
      }
      return;
    }
    if (!(k in DIRS) && k !== KEY.OK && k !== KEY.BACK && k !== KEY.ESC && k !== KEY.INFO) return;
    e.preventDefault();
    if (k === KEY.INFO) {
      if (e.repeat) return;
      if (mode === 'menu') closeMenu();
      else if (mode === 'home' && !okDown && focusId) { homeStartUntil = 0; openCardMenu(focusId); }
      return;
    }
    if (mode === 'menu') {
      const entry = menu && menu.entries[menu.index];
      if (k === KEY.UP) menuFocus(menu.index - 1);
      else if (k === KEY.DOWN) menuFocus(menu.index + 1);
      else if ((k === KEY.LEFT || k === KEY.RIGHT) && entry && entry.slider) sliderStep(k === KEY.LEFT ? -1 : 1, e);
      else if (k === KEY.OK) { if (menuArmed && !okBlocked && !e.repeat) { okBlocked = true; menuSelect(); } }
      else if (k === KEY.BACK || k === KEY.ESC || k === KEY.LEFT) menuBack();
      return;
    }
    if (mode === 'move') {
      if (k in DIRS) moveCard(DIRS[k]);
      else if (k !== KEY.OK) endMove();
      else if (!e.repeat && !okBlocked) { okBlocked = true; endMove(); }
      return;
    }
    if (k in DIRS) { if (okDown) return; move(DIRS[k]); return; }
    if (k === KEY.OK) {
      if (okDown || okBlocked || e.repeat) return;
      okDown = true; okLong = false;
      pressed(true);
      clearTimeout(okTimer);
      okTimer = setTimeout(() => { if (okDown && mode === 'home') { okLong = true; pressed(false); openCardMenu(focusId); } }, HOLD_MS);
      return;
    }
    // Back: to the top (this is the home screen, there is nothing further back)
    const first = firstId();
    if (first && focusId !== first) setFocus(first);
  });

  document.addEventListener('keyup', e => {
    if (e.keyCode !== KEY.OK) return;
    if (geminiModalOpen) return;
    e.preventDefault();
    menuArmed = true;
    okBlocked = false;
    if (!okDown) return;
    okDown = false;
    clearTimeout(okTimer);
    pressed(false);
    if (!okLong && mode === 'home') activate(focusId);
  });

  $('scrim').addEventListener('click', () => { if (mode === 'menu') closeMenu(); });

  let lastWheel = 0;
  document.addEventListener('wheel', e => {
    wake();
    const now = Date.now();
    if (now - lastWheel < 200 || !e.deltaY || geminiModalOpen) return;   // Gemini: the wheel scrolls the answer natively
    lastWheel = now;
    if (mode === 'menu') menuFocus(menu.index + (e.deltaY > 0 ? 1 : -1));
    else if (mode === 'home') move(e.deltaY > 0 ? 'down' : 'up');
  }, { passive: true });

  document.addEventListener('mousemove', () => wake(), { passive: true });

  /* ------------------------------------------------------------------ */
  /* Toast, clock, OLED care                                             */
  /* ------------------------------------------------------------------ */
  const toastEl = $('toast');
  let toastTimer = 0;
  function toast(text, ms) {
    toastEl.textContent = text;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), ms || 2600);
  }

  const two = n => (n < 10 ? '0' : '') + n;
  const dateFmt = new Intl.DateTimeFormat('de-DE', { weekday: 'long', day: 'numeric', month: 'long' });
  let shownMinute = -1;
  function tick() {
    const d = new Date();
    if (d.getMinutes() === shownMinute) return;
    shownMinute = d.getMinutes();
    $('hh').textContent = two(d.getHours());
    $('mm').textContent = two(d.getMinutes());
    $('date').textContent = dateFmt.format(d);
    if (bbKey && !tiles.has(bbKey)) showBillboard(bbKey, false);   // keeps "zuletzt vor …" current
  }

  let lastInput = Date.now();
  function wake() {
    lastInput = Date.now();
    document.body.classList.remove('idle', 'screensaver');
  }
  setInterval(() => {
    tick();
    const elapsed = Date.now() - lastInput;
    if (!document.hidden) {
      if (elapsed > SCREENSAVER_MS) document.body.classList.add('screensaver');
      else if (elapsed > (geminiModalOpen ? IDLE_MS * 3 : IDLE_MS)) document.body.classList.add('idle');   // reading an answer takes a while
    }
  }, 1000);
  setInterval(() => {       // pixel shift: spread wear of static elements
    const r = () => Math.round(Math.random() * 10 - 5);
    $('shift').style.transform = 'translate(' + r() + 'px,' + r() + 'px)';
  }, SHIFT_MS);

  /* ------------------------------------------------------------------ */
  /* Data loading and lifecycle                                          */
  /* ------------------------------------------------------------------ */
  function getJSON(url) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.onload = () => { try { resolve(JSON.parse(xhr.responseText)); } catch (e) { reject(e); } };
      xhr.onerror = () => reject(new Error('cannot load ' + url));
      xhr.open('GET', url);
      xhr.send();
    });
  }

  async function loadApps() {
    let changed = false;
    try {
      const data = previewNoApps ? {} : await getJSON('data/apps.json?t=' + Date.now());
      if (Array.isArray(data.apps)) {
        changed = data.generatedAt !== state.generatedAt || !state.apps.length;
        state.apps = data.apps;
        state.generatedAt = data.generatedAt;
      }
    } catch (e) { /* keep what we have */ }
    try {
      const data = await getJSON('data/usage.json?t=' + Date.now());
      if (data && data.usage && typeof data.usage === 'object') {
        if (data.updatedAt !== state.usageStamp) changed = true;
        state.usageStamp = data.updatedAt;
        state.usage = data.usage;
      }
    } catch (e) { /* no usage data yet: order falls back to alphabetical */ }
    return changed;
  }

  async function refreshData(force) {
    if (IS_TV && force) {
      // ask the root helper for a fresh app list (it also runs on its own; this is the fast path)
      try { await rootExec('python3 "' + APP_DIR + '/launcherd.py" refresh'); } catch (e) { /* helper keeps data fresh anyway */ }
    }
    if (await loadApps()) requestRender();
  }

  function activateApp() {
    try {
      if (window.PalmSystem && typeof window.PalmSystem.activate === 'function') window.PalmSystem.activate();
    } catch (_) { /* ignore */ }
  }

  function resetKeys() {            // a key-up can get lost while the app is hidden or loses focus
    okDown = false; okLong = false; okBlocked = false; menuArmed = true;
    clearTimeout(okTimer);
    for (const el of document.querySelectorAll('.pressed')) el.classList.remove('pressed');
  }

  // visibilitychange, focus and webOSRelaunch often fire together for one return to the app:
  // run the (network-heavy) refresh only once per burst.
  let lastShown = 0;
  function onShown() {
    const now = Date.now();
    if (now - lastShown < 1000) return;
    lastShown = now;
    resetKeys();
    restoreView();
    wake();
    tick();
    refreshData(false);
    fetchSmartDevices();
    fetchAmbient();
  }

  // A relaunch means "Home was pressed" (launcherd redirects LG home here): start fresh at the top.
  // A plain return from an app (Back) keeps the focused card.
  let lastRelaunch = 0;
  function onRelaunch(e) {
    // With handlesRelaunch the app has to raise its own window, on EVERY relaunch: LG home's
    // late card can push the launcher back right after the first one, and launcherd's follow-up
    // launches are then the only way back. (Debouncing this left the TV with no app in front.)
    activateApp();
    // the focus watchdog relaunched us: only take the focus, keep the view (launchParams can be stale, hence the time window)
    if (launchParams(e).refocus && refocusUntil > Date.now()) return;
    const now = Date.now();
    if (now - lastRelaunch < 300) return;   // fired on both document and window
    lastRelaunch = now;
    onShown();
    if (handleGeminiParam(launchParams(e))) return;   // mic button: open/close Gemini, keep the page as it is
    if (mode === 'menu') closeMenu();
    if (geminiModalOpen) closeGeminiModal();
    if (mode === 'move') endMove();
    const first = firstId();
    if (first) setFocus(first);
    homeStartUntil = Date.now() + 5000;   // until the user moves
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) resetKeys(); else onShown(); });
  window.addEventListener('blur', resetKeys);
  window.addEventListener('focus', onShown);
  document.addEventListener('webOSRelaunch', onRelaunch);
  window.addEventListener('webOSRelaunch', onRelaunch);

  // Focus watchdog. Seen on the TV: the launcher was on screen but did not have the key focus,
  // so the first key press only gave it focus and did nothing else ("press twice").
  // While the launcher is visible without focus and no overlay (quick settings, voice) took it
  // via a blur, ask the compositor for the focus again. activate() does not steal the focus
  // from an open overlay (checked with quick settings). Every step is kept in a small log
  // (localStorage "launcher.focuslog", also window.__launcher.focusLog) to find the trigger.
  const FOCUS_LOG_KEY = 'launcher.focuslog';
  let focusLog = [];
  try { focusLog = JSON.parse(localStorage.getItem(FOCUS_LOG_KEY)) || []; } catch (e) { focusLog = []; }
  function flog(what) {
    focusLog.push(new Date().toISOString().slice(5, 19) + ' ' + what);
    if (focusLog.length > 60) focusLog = focusLog.slice(-60);
    try { localStorage.setItem(FOCUS_LOG_KEY, JSON.stringify(focusLog)); } catch (e) { /* ignore */ }
  }
  let lastBlurAt = 0, lostSince = 0, lastShowAt = Date.now();
  window.addEventListener('blur', () => { lastBlurAt = Date.now(); flog('blur vis=' + document.visibilityState); });
  window.addEventListener('focus', () => {
    // gained without a show/relaunch just before it: most likely a key press that was swallowed
    const quiet = Date.now() - Math.max(lastShowAt, lastRelaunch) > 1500;
    flog('focus t=' + Math.round(performance.now()) + (quiet && !document.hidden ? ' (by key?)' : ''));
  });
  document.addEventListener('visibilitychange', () => { lastShowAt = Date.now(); flog('vis ' + document.visibilityState); });
  flog('start hasFocus=' + document.hasFocus() + ' t=' + Math.round(performance.now()));
  window.addEventListener('load', () => flog('load t=' + Math.round(performance.now()) + ' hasFocus=' + document.hasFocus()));
  // Seen on the TV (15.11., several minutes): activate() every 4 s did not bring the focus back,
  // only a key press did. So each episode is logged once with what the TV showed at that moment,
  // then two ways back are tried: activate() after 4 s, a relaunch of ourselves after 10 s (results logged).
  // A relaunch of ourselves does not take the focus from an open overlay either (checked).
  let lostStep = 0, refocusUntil = 0;
  function focusContext() {
    const ws = window.webOSSystem || window.PalmSystem;
    const parts = ['act=' + (ws ? ws.isActivated : '?'), 'blur ' + (lastBlurAt ? Math.round((Date.now() - lastBlurAt) / 1000) + 's ago' : 'never')];
    luna(SAM + 'getForegroundAppInfo', { extraInfo: true }).then(r => {
      const fg = (r && r.foregroundAppInfo || []).map(a => a.appId + '/' + (a.windowType || '').replace('_WEBOS_WINDOW_TYPE_', '').toLowerCase());
      flog('  context ' + parts.join(' ') + ' fg=' + fg.join(','));
    }).catch(() => flog('  context ' + parts.join(' ')));
  }
  setInterval(() => {
    if (!IS_TV || document.hidden || document.hasFocus()) {
      if (lostStep > 0) flog('focus back after step ' + lostStep);
      lostSince = 0; lostStep = 0;
      return;
    }
    const now = Date.now();
    if (!lostSince) { lostSince = now; return; }
    // an overlay that blurred us is left alone; only a focus that never came back on its own
    if (now - lastBlurAt < 60000) return;
    const waited = now - lostSince;
    const check = label => setTimeout(() => flog('watchdog ' + label + ' -> hasFocus=' + document.hasFocus()), 600);
    if (lostStep === 0 && waited >= 4000) {
      lostStep = 1;
      flog('watchdog: visible without focus');
      focusContext();
      activateApp();
      check('activate');
    } else if (lostStep === 1 && waited >= 10000) {
      // (deactivate()+activate() was tried here: deactivate sends the app to the background for minutes)
      lostStep = 2;   // same path as the Home key, which is known to bring the focus; keeps the view as it is
      refocusUntil = Date.now() + 3000;
      luna(SAM + 'launch', { id: 'com.custom.launcher', params: { refocus: true } }).catch(() => {});
      check('relaunch');
    }
  }, 2000);

  /* ------------------------------------------------------------------ */
  /* PC preview only (never runs on the TV): fonts and a fake backend    */
  /* ------------------------------------------------------------------ */
  // The fake backend answers the same endpoints as gemini-tv-controller/server.py, so the preview runs the
  // real code paths (polls, optimistic commands, failures). Query flags for testing unhappy states:
  //   backend=offline|slow   smart=none|offline|mixed   smartdelay=<ms>   weather=error   gemini=error|slow
  //   apps=none (no data/apps.json)   long=1 (very long names)
  if (!IS_TV) {
    // LG's fonts are not in the repo (licence). The TV has them built in; on the PC the preview uses
    // "LG Smart UI" only if it is installed locally, otherwise the fallback fonts from style.css.
    state.devices = [
      { id: 'HDMI_1', label: 'HDMI 1', appId: 'com.webos.app.hdmi1', connected: true },
      { id: 'HDMI_2', label: 'HDMI 2', appId: 'com.webos.app.hdmi2', connected: false },
      { id: 'HDMI_3', label: 'HDMI 3', appId: 'com.webos.app.hdmi3', connected: false },
      { id: 'HDMI_4', label: 'HDMI 4', appId: 'com.webos.app.hdmi4', connected: false }
    ];
    const q = {};
    location.search.replace(/^\?/, '').split('&').forEach(kv => { if (kv) { const [k, v] = kv.split('='); q[decodeURIComponent(k)] = decodeURIComponent(v || '1'); } });
    const long = q.long === '1';
    let fake = SMART_FALLBACK.map((d, i) => Object.assign({}, d, {
      name: long && i === 0 ? 'Stehlampe neben dem großen Sofa am Fenster' : d.name,
      room: long && i === 4 ? 'Wintergarten und Terrasse im Erdgeschoss' : d.room,
      online: q.smart === 'offline' ? false : !(q.smart === 'mixed' && (i === 0 || i === 6)),
      state: i === 1 || i === 2 ? 'on' : 'off', brightness: d.type === 'light' ? 70 : null, kelvin: null,
      color: i === 1 ? '#ffa657' : (d.type === 'light' ? '#ffc18e' : null),
      capabilities: d.type === 'light' ? (i === 0 ? ['brightness', 'white'] : ['brightness', 'color', 'white']) : []
    }));
    if (q.smart === 'none') fake = [];
    const calls = [];
    window.__previewCalls = calls;
    // spotify=off|error|none (none: an older backend without the feature)
    const fakeAmbient = { mode: q.spotify === 'off' ? 'off' : 'spotify', state: q.spotify === 'off' ? 'off' : (q.spotify === 'error' ? 'error' : 'active'),
      message: q.spotify === 'error' ? 'Spotify-App nicht lesbar' : '', calibration: { saturation: 1.4, brightness: 35, hue_shift: 0 },
      lamp: { id: '0a0000000002', name: 'Arbeitszimmer Fernseher Bar' }, source: 'spotify',
      track: { name: 'Super Rich Kids', artist: 'Frank Ocean', cover: '' },
      color: '#f07628', sent: '#ff5a0a', active: true, override: false };
    const wait = ms => new Promise(r => setTimeout(r, ms));
    const day = i => new Date(Date.now() + i * 86400000).toISOString().slice(0, 10);
    const WEATHER = { place: 'Musterstadt', current: { temperature_2m: 4.3, apparent_temperature: 1.2, weather_code: 2 },
      daily: { time: [0, 1, 2, 3, 4].map(day), weather_code: [2, 61, 3, 0, 71], temperature_2m_max: [7.1, 6, 8.4, 9, 3],
        temperature_2m_min: [0.6, 2, 3.1, 1, -2], precipitation_probability_max: [10, 80, 35, 5, 60] } };
    previewApi = async (path, body, ms) => {
      calls.push(path + (body ? ' ' + JSON.stringify(body) : ''));
      const limit = ms || 6000;
      const answer = async (delay, fn) => {
        if (delay > limit) { await wait(limit); throw Object.assign(new Error('aborted'), { name: 'AbortError' }); }
        await wait(delay);
        return fn();
      };
      if (q.backend === 'offline') { await wait(30); throw new TypeError('Failed to fetch'); }
      const slow = q.backend === 'slow' ? 9000 : 0;
      if (path === '/api/smarthome/devices') return answer(slow || Number(q.smartdelay || 250), () => JSON.parse(JSON.stringify(fake)));
      if (path === '/api/smarthome/set') {
        return answer(slow || 220, () => {
          const d = fake.find(x => x.id === body.id);
          if (!d) return { status: 'error', error: 'Unbekanntes Gerät' };
          if (d.online === false) return { status: 'error', error: d.name + ' nicht erreichbar (timeout)' };
          d.state = body.state === 'on' ? 'on' : 'off';
          if (body.brightness != null) d.brightness = body.brightness;
          if (body.color) { d.color = body.color; d.kelvin = null; }
          if (body.kelvin) { d.kelvin = body.kelvin; d.color = kelvinHex(body.kelvin); }
          return { status: 'ok', state: d.state };
        });
      }
      if (path.startsWith('/api/spotify/ambient')) {
        return answer(slow || 120, () => {
          if (q.spotify === 'none') return { error: 'Nicht gefunden' };
          if (path.endsWith('/mode')) fakeAmbient.mode = body.mode;
          if (path.endsWith('/calibration')) {
            for (const k of ['saturation', 'brightness', 'hue_shift']) if (typeof body[k] === 'number') fakeAmbient.calibration[k] = body[k];
            const hs = hexHsv(fakeAmbient.color);
            fakeAmbient.sent = hsvHex(((hs.h + fakeAmbient.calibration.hue_shift) % 360 + 360) % 360, Math.min(1, hs.s * fakeAmbient.calibration.saturation));
          }
          fakeAmbient.state = fakeAmbient.mode === 'off' ? 'off' : (q.spotify === 'error' ? 'error' : 'active');
          return Object.assign(JSON.parse(JSON.stringify(fakeAmbient)), path === '/api/spotify/ambient' ? {} : { success: true });
        });
      }
      if (path === '/api/weather') return answer(slow || 120, () => (q.weather === 'error' ? { error: 'timed out' } : WEATHER));
      if (path === '/api/quick') return answer(slow || 100, () => ({ success: true, action: body.action }));
      if (path === '/api/command') {
        if (q.gemini === 'error') { await wait(300); throw new TypeError('Failed to fetch'); }
        return answer(q.gemini === 'slow' ? 60000 : 900, () => ({
          text: 'Hier sind drei Tipps für heute Abend:\n\n- **Dark** (Netflix) – verschachtelte Zeitreise-Serie\n- **Severance** (Apple TV) – Büro-Thriller\n' +
            '- **The Bear** (Disney+) – Küchen-Drama in hohem Tempo\n\nIch habe außerdem das Licht gedimmt.',
          actions: [{ tool: 'control_smart_device', args: { device_name: 'Fernseher Bar', brightness: 30 }, result: { success: true } }],
          suggestions: ['Mehr wie Dark', 'Etwas Lustiges', 'Nur Filme']
        }));
      }
      return answer(50, () => ({ error: 'Not found' }));
    };
    if (q.apps === 'none') previewNoApps = true;
  }

  /* ------------------------------------------------------------------ */
  /* Weather                                                             */
  /* ------------------------------------------------------------------ */
  // WMO weather code -> [icon, description]
  const WMO_MAP = {
    0: ['sun', 'Klarer Himmel'], 1: ['partly', 'Überwiegend klar'], 2: ['partly', 'Teilweise bewölkt'], 3: ['cloud', 'Bedeckt'],
    45: ['fog', 'Nebel'], 48: ['fog', 'Raureif-Nebel'],
    51: ['rain', 'Leichter Niesel'], 53: ['rain', 'Mäßiger Niesel'], 55: ['rain', 'Dichter Niesel'],
    56: ['rain', 'Gefrierender Niesel'], 57: ['rain', 'Gefrierender Niesel'],
    61: ['rain', 'Leichter Regen'], 63: ['rain', 'Mäßiger Regen'], 65: ['rain', 'Starker Regen'],
    66: ['rain', 'Gefrierender Regen'], 67: ['rain', 'Gefrierender Regen'],
    71: ['snow', 'Leichter Schneefall'], 73: ['snow', 'Schneefall'], 75: ['snow', 'Starker Schneefall'], 77: ['snow', 'Schneegriesel'],
    80: ['rain', 'Leichte Schauer'], 81: ['rain', 'Regenschauer'], 82: ['storm', 'Starke Schauer'],
    85: ['snow', 'Schneeschauer'], 86: ['snow', 'Starke Schneeschauer'],
    95: ['storm', 'Gewitter'], 96: ['storm', 'Gewitter mit Hagel'], 99: ['storm', 'Gewitter mit Hagel']
  };

  let lastWeather = null;          // raw forecast, also used as context for Gemini
  let weatherFailed = false;       // the last update failed (backend down or open-meteo error)
  async function fetchWeather(manual) {
    let data = null;
    try { data = await api('/api/weather', undefined, 8000); } catch (e) { data = null; }
    const ok = !!(data && data.current && num(data.current.temperature_2m));
    weatherFailed = !ok;
    if (ok) lastWeather = data;
    updateWeatherUI(lastWeather);
    if (manual) toast(ok ? 'Wetter aktualisiert' : 'Wetter gerade nicht verfügbar');
    if (bbKey === 'hero:weather') showBillboard(bbKey, false);
  }

  // Place name comes from the backend (WEATHER_PLACE in its .env), never from this file.
  function placeName() {
    const p = lastWeather && lastWeather.place;
    return typeof p === 'string' ? p.trim().slice(0, 40) : '';
  }

  function wetterOnlineUrl() {
    const slug = placeName().toLowerCase().replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue')
      .replace(/ß/g, 'ss').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    return 'https://www.wetteronline.de/' + (slug ? 'wetter/' + slug : '');
  }

  function updateWeatherUI(data) {
    const cur = data && data.current;
    const iconEl = $('w-icon');
    let icon = 'cloud', title = '–°', sub = weatherFailed ? 'nicht verfügbar' : (placeName() || 'Wetter');
    if (cur && num(cur.temperature_2m)) {
      const hour = new Date().getHours();
      let desc;
      [icon, desc] = WMO_MAP[cur.weather_code] || ['cloud', 'Bewölkt'];
      if ((hour >= 20 || hour < 6) && (icon === 'sun' || icon === 'partly')) icon = cur.weather_code === 0 ? 'night' : 'cloud';
      title = Math.round(cur.temperature_2m) + '°';
      const rain = at(data.daily && data.daily.precipitation_probability_max, 0);
      sub = desc + (rain >= 30 ? ' · ' + Math.round(rain) + ' % Regen' : '');
    }
    if (iconEl.dataset.icon !== icon) { iconEl.dataset.icon = icon; iconEl.innerHTML = svg(icon); }
    weatherEl.classList.toggle('stale', weatherFailed);
    $('w-temp').textContent = title;
    $('w-sub').textContent = sub;
    layoutDirty = true;
  }

  /* ------------------------------------------------------------------ */
  /* Audio output & WetterOnline                                         */
  /* ------------------------------------------------------------------ */
  function openWeatherApp() {
    if (IS_TV) {
      const el = focusId && itemIdOf(focusId) === WETTER_APP ? els.get(focusId) : null;
      launchId('com.webos.app.browser', 'WetterOnline', el, { target: wetterOnlineUrl() });
    } else {
      window.open(wetterOnlineUrl(), '_blank');
    }
  }

  function openAudioSettings() {
    if (IS_TV) {
      luna(SAM + 'launch', { id: SETTINGS, params: { target: 'SoundOut' } })
        .catch(() => toast('Ton-Einstellungen konnten nicht geöffnet werden'));
    } else {
      toast('Audioausgabe: Denon AVR');
    }
  }

  const SOUND_OUT = {
    external_optical: ['speaker', 'Denon AVR', 'Optisch', ''],
    lgSoundSync: ['speaker', 'Denon AVR', 'Optisch · Sound Sync', ''],   // optical with LG's control protocol
    external_arc: ['speaker', 'HDMI eARC', 'Digital', ''],
    tv_speaker: ['speaker', 'TV-Lautsprecher', 'Intern', 'tv-speaker'],
    tv_speaker_and_external_optical: ['speaker', 'TV + Denon', 'Optisch + intern', '']
  };
  function updateSoundOutputUI(out) {
    let [icon, title, sub, cls] = SOUND_OUT[out] || ['speaker', 'Audio', out || 'Ausgabe', ''];
    if (!SOUND_OUT[out] && out && /bt|bluetooth/.test(out)) { icon = 'headphones'; title = 'Bluetooth'; sub = 'Drahtlos'; }
    const iconEl = $('audio-icon');
    if (iconEl.dataset.icon !== icon) { iconEl.dataset.icon = icon; iconEl.innerHTML = svg(icon); }
    $('audio-title').textContent = title;
    $('audio-sub').textContent = sub;
    audioBadgeEl.className = 'pill audio' + (cls ? ' ' + cls : '') + (focusId === 'hero:audio' ? ' focused' : '');
    layoutDirty = true;
  }

  function initSoundOutput() {
    if (!IS_TV) { updateSoundOutputUI('external_optical'); return; }
    luna('luna://com.webos.service.audio/getSoundOutput', {}).then(res => {
      if (res && res.soundOutput) updateSoundOutputUI(res.soundOutput);
    }).catch(() => {});
    lunaSubscribe('luna://com.webos.service.audio/getSoundOutput', {}, msg => {
      if (msg && msg.soundOutput) updateSoundOutputUI(msg.soundOutput);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Gemini assistant: remote only (tiles + follow-up suggestions)      */
  /* ------------------------------------------------------------------ */
  let geminiModalOpen = false;
  let gmHistory = [];          // [{role: 'user'|'model', text}] - kept until the popup closes
  let gmBusy = false;
  let gmReq = 0;               // ignores answers that arrive after the popup was closed
  const geminiModalEl = $('gemini-modal');
  const geminiChatEl = $('gemini-chat');
  const geminiChipsEl = $('gemini-chips');
  const geminiCloseBtn = $('gemini-close-btn');
  const geminiBackdropEl = $('gemini-backdrop');

  const STREAMING = ['netflix', 'amazon', 'com.disney.disneyplus-prod', 'com.apple.appletv', 'com.skygo.app.de.q',
    'joyn', 'com.netrtl.tvnow', 'de.telekom.magentatv', 'ard.mediathek', 'youtube.leanback.v4'];

  function streamingApps() {
    const names = state.apps.filter(a => STREAMING.includes(a.id)).map(a => a.title);
    return names.length ? names.join(', ') : 'Netflix, Prime Video, Disney+, Apple TV, YouTube';
  }

  function weatherContext() {
    const w = lastWeather;
    if (!w || !w.current || !num(w.current.temperature_2m)) return 'Aktuelle Wetterdaten sind gerade nicht verfügbar.';
    const desc = (WMO_MAP[w.current.weather_code] || ['', 'unbekannt'])[1];
    let s = 'Wetter jetzt: ' + Math.round(w.current.temperature_2m) + ' °C' +
      (num(w.current.apparent_temperature) ? ' (gefühlt ' + Math.round(w.current.apparent_temperature) + ' °C)' : '') + ', ' + desc + '.';
    const d = w.daily || {};
    const day = (i, n) => {
      const lo = at(d.temperature_2m_min, i), hi = at(d.temperature_2m_max, i);
      if (lo == null || hi == null) return '';
      const rain = at(d.precipitation_probability_max, i);
      return ' ' + n + ': ' + Math.round(lo) + ' bis ' + Math.round(hi) + ' °C, ' + ((WMO_MAP[at(d.weather_code, i)] || ['', ''])[1]) +
        (rain != null ? ', Regenrisiko ' + Math.round(rain) + ' %' : '') + '.';
    };
    s += day(0, 'Heute') + day(1, 'Morgen');
    if (weatherFailed) s += ' (Stand von vorhin.)';
    return s;
  }

  const timeOfDay = () => new Date().toLocaleString('de-DE', { weekday: 'long', hour: '2-digit', minute: '2-digit' });

  // Topic rows. prompt() runs on click, so it uses the current apps / weather / time.
  const GM_TOPICS = [
    { label: 'Film-Tipps', icon: 'film', items: [
      { ico: 'moon', text: 'Was schaue ich heute?', prompt: () => 'Es ist ' + timeOfDay() + '. Empfiehl mir 3 Filme oder Serien für heute, die es auf meinen Diensten gibt: ' + streamingApps() + '. Nenne bei jedem den Dienst.' },
      { ico: 'news', text: 'Neue Serien', prompt: () => 'Welche neuen oder gerade beliebten Serien lohnen sich auf ' + streamingApps() + '? 3 Tipps mit Dienst und je einem Satz Begründung.' },
      { ico: 'film', text: 'Spannender Film', prompt: () => 'Empfiehl mir 3 spannende Filme (Thriller oder Action) auf ' + streamingApps() + '. Nenne jeweils den Dienst.' },
      { ico: 'smile', text: 'Was Leichtes', prompt: () => 'Ich will abschalten: 3 lustige oder entspannte Filme oder Serien auf ' + streamingApps() + ', mit Dienst.' },
      { ico: 'globe', text: 'Dokus', prompt: () => 'Empfiehl mir 3 sehenswerte Dokumentationen auf ' + streamingApps() + ', mit Dienst.' }
    ] },
    { label: 'Wetter & Tag', icon: 'partly', items: [
      { ico: 'shirt', text: 'Was ziehe ich an?', prompt: () => weatherContext() + ' Was soll ich heute anziehen? Kurz und konkret.' },
      { ico: 'walk', text: 'Lohnt sich rausgehen?', prompt: () => weatherContext() + ' Es ist ' + timeOfDay() + '. Lohnt sich jetzt ein Spaziergang oder eher später? Kurz.' },
      { ico: 'calendar', text: 'Heute & morgen', prompt: () => weatherContext() + ' Fasse das Wetter für heute und morgen kurz und anschaulich zusammen.' },
      { ico: 'spark', text: 'Idee für den Abend', prompt: () => weatherContext() + ' Es ist ' + timeOfDay() + '. Schlag mir 3 Ideen für den Abend vor, passend zum Wetter.' }
    ] },
    { label: 'Wissen & Rezepte', icon: 'bulb', items: [
      { ico: 'dice', text: 'Fakt des Tages', prompt: () => 'Erzähle mir einen verblüffenden, wenig bekannten Fakt. Kurz und spannend erklärt.' },
      { ico: 'pan', text: 'Schnelles Rezept', prompt: () => 'Schlage ein raffiniertes Abendessen vor, das in unter 20 Minuten fertig ist. Zutaten und Schritte kurz.' },
      { ico: 'bulb', text: 'Erklär mir was', prompt: () => 'Erkläre mir ein spannendes Thema aus Wissenschaft oder Technik in 5 Sätzen, so dass es jeder versteht.' },
      { ico: 'brain', text: 'Quizfrage', prompt: () => 'Stell mir eine knifflige Quizfrage mit 3 Antwortmöglichkeiten (A, B, C). Verrate die Lösung noch nicht.' }
    ] }
  ];

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Minimal, safe Markdown for answers: escape first, then **bold**, *italic*, headings, lists, paragraphs.
  function renderAnswer(text) {
    const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    let html = '', list = false, para = [];
    const flushPara = () => { if (para.length) { html += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; } };
    const flushList = () => { if (list) { html += '</ul>'; list = false; } };
    for (const raw of String(text || '').split('\n')) {
      const line = raw.trim();
      const indented = /^\s{2,}/.test(raw);
      const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      const head = line.match(/^#{1,4}\s+(.*)$/);
      if (!line) { flushPara(); flushList(); continue; }
      if (head) { flushPara(); flushList(); html += '<h3>' + inline(head[1]) + '</h3>'; continue; }
      if (bullet) { flushPara(); if (!list) { html += '<ul>'; list = true; } html += (indented ? '<li class="gm-sub">' : '<li>') + inline(bullet[1]) + '</li>'; continue; }
      flushList(); para.push(line);
    }
    flushPara(); flushList();
    return html;
  }

  function chip(ico, text, cls, onClick) {
    const b = document.createElement('button');
    b.className = 'gm-chip' + (cls ? ' ' + cls : '');
    b.tabIndex = -1;
    b.innerHTML = (ico ? '<span class="gm-ico">' + svg(ico) + '</span>' : '') + '<span class="gm-chip-text">' + esc(text) + '</span>';
    b.addEventListener('click', onClick);
    return b;
  }

  function row(label, chips, icon) {
    const r = document.createElement('div');
    r.className = 'gm-row';
    const l = document.createElement('div');
    l.className = 'gm-row-label';
    if (icon) l.innerHTML = '<span class="gm-row-ico">' + svg(icon) + '</span>';
    l.appendChild(document.createTextNode(label || ''));
    r.appendChild(l);
    chips.forEach(c => r.appendChild(c));
    return r;
  }

  function showTopicChips() {
    geminiChipsEl.innerHTML = '';
    for (const t of GM_TOPICS) {
      geminiChipsEl.appendChild(row(t.label, t.items.map(it => chip(it.ico, it.text, '', () => askGemini(it.prompt(), it.text))), t.icon));
    }
  }

  // after an answer: follow-up questions; after a failure: try the same question again
  function showFollowUps(suggestions, retry) {
    geminiChipsEl.innerHTML = '';
    const sug = (suggestions || []).filter(x => typeof x === 'string' && x.trim()).slice(0, 4)
      .map(x => chip('reply', x, 'gm-suggest', () => askGemini(x, x)));
    if (sug.length) geminiChipsEl.appendChild(row('Nachfragen', sug, 'reply'));
    const more = [];
    if (retry) more.push(chip('refresh', 'Erneut versuchen', 'gm-suggest', () => askGemini(retry.prompt, retry.label, true)));
    else if (gmHistory.length) more.push(chip('refresh', 'Andere Vorschläge', 'gm-ghost', () => askGemini('Bitte gib mir andere, neue Vorschläge dazu.', 'Andere Vorschläge')));
    more.push(chip('spark', 'Neues Thema', 'gm-ghost', resetGeminiChat));
    geminiChipsEl.appendChild(row('', more));
  }

  function gmFocusables() {
    return [...geminiChipsEl.querySelectorAll('.gm-chip')].filter(b => !b.disabled);
  }

  function focusFirstChip() {
    const first = gmFocusables()[0];
    if (first) first.focus();
  }

  function resetGeminiChat() {
    gmReq++;
    gmHistory = [];
    gmBusy = false;
    geminiModalEl.classList.add('gm-start');
    geminiChatEl.innerHTML = '<div class="gm-hello"><span class="gm-hello-mark">' + GEMINI_ICON + '</span>' +
      '<h1>Worauf hast du Lust?</h1>' +
      '<p>Wähle ein Thema – danach schlage ich passende Nachfragen vor.</p></div>';
    showTopicChips();
    focusFirstChip();
  }

  function openGeminiModal() {
    if (geminiModalOpen) return;
    // a long OK on the pill opens Gemini while OK is still down; its key-up goes to Gemini, so the
    // hub's OK state must not wait for it (it stayed "held" and swallowed every arrow key afterwards)
    okDown = false; okLong = false; clearTimeout(okTimer); pressed(false);
    geminiModalOpen = true;
    geminiModalEl.style.display = 'flex';
    resetGeminiChat();
    requestAnimationFrame(() => geminiModalEl.classList.add('gm-in'));
  }

  function closeGeminiModal() {
    if (!geminiModalOpen) return;
    geminiModalOpen = false;
    gmReq++;                                   // drop answers still on their way
    gmHistory = [];
    gmBusy = false;
    geminiModalEl.classList.remove('gm-in');
    geminiModalEl.style.display = 'none';
    if (document.activeElement && document.activeElement !== document.body && geminiModalEl.contains(document.activeElement)) document.activeElement.blur();
  }

  function toggleGeminiModal() {
    if (geminiModalOpen) closeGeminiModal(); else openGeminiModal();
  }

  // what Gemini did, in words (the backend reports tool names)
  const GM_TOOLS = {
    launch_app: a => (a.app_name ? String(a.app_name) + ' geöffnet' : 'App geöffnet'),
    switch_input: a => 'Eingang ' + (a.input_name ? String(a.input_name).toUpperCase() : '') + ' gewählt',
    volume_up: () => 'Lauter', volume_down: () => 'Leiser', toggle_mute: () => 'Stumm umgeschaltet',
    screen_off: () => 'Display aus', screen_on: () => 'Display an', screensaver: () => 'Bildschirmschoner',
    media_control: a => 'Wiedergabe: ' + (a.action || ''), get_tv_status: () => 'TV-Status gelesen', show_toast: () => 'Hinweis gezeigt',
    control_smart_device: a => (a.device_name || 'Gerät') + ' ' + (typeof a.brightness === 'number' ? Math.round(a.brightness) + ' %' : (a.state === 'on' ? 'an' : a.state === 'off' ? 'aus' : 'geschaltet'))
  };
  function actionLabel(x) {
    const f = GM_TOOLS[x && x.tool];
    try { return f ? f(x.args || {}) : String((x && x.tool) || 'Aktion'); } catch (e) { return String(x.tool); }
  }
  const actionFailed = r => !!r && typeof r === 'object' && (r.success === false || r.status === 'error' || (!!r.error && r.success !== true));

  async function askGemini(promptText, label, isRetry) {
    if (!promptText || gmBusy) return;
    gmBusy = true;
    const req = ++gmReq;
    geminiModalEl.classList.remove('gm-start');
    if (!gmHistory.length && !isRetry) geminiChatEl.innerHTML = '';
    if (isRetry) { const last = geminiChatEl.lastElementChild; if (last && last.classList.contains('gm-err')) last.remove(); }
    let q = null;
    if (!isRetry) {
      q = document.createElement('div');
      q.className = 'gm-q';
      q.textContent = label || promptText;
      geminiChatEl.appendChild(q);
    }
    const thinking = document.createElement('div');
    thinking.className = 'gm-thinking';
    thinking.innerHTML = '<span></span><span></span><span></span>';
    geminiChatEl.appendChild(thinking);
    if (q) q.scrollIntoView({ block: 'start' });
    gmFocusables().forEach(b => { b.disabled = true; });

    let data = null, failed = '';
    try {
      // a bounded wait: a hanging backend used to leave the dots spinning and every chip disabled for good
      data = await api('/api/command', { prompt: promptText, history: gmHistory }, 45000);
      if (!data || typeof data !== 'object') failed = 'Keine Antwort erhalten.';
      else if (typeof data.text !== 'string' && data.error) failed = String(data.error);
    } catch (e) {
      failed = e && e.name === 'AbortError' ? 'Gemini hat zu lange gebraucht. Versuch es noch einmal.' : 'Der Gemini-Dienst auf dem TV ist gerade nicht erreichbar.';
    }
    if (req !== gmReq) return;                 // popup closed or reset meanwhile
    gmBusy = false;
    thinking.remove();

    const a = document.createElement('div');
    a.className = 'gm-a' + (failed ? ' gm-err' : '');
    a.innerHTML = '<span class="gm-a-mark">' + (failed ? svg('close') : GEMINI_ICON) + '</span>';
    const body = document.createElement('div');
    body.className = 'gm-a-body';
    a.appendChild(body);
    if (failed) {
      body.textContent = failed;
    } else {
      body.innerHTML = renderAnswer(data.text || 'Keine Antwort erhalten.');
      const acts = Array.isArray(data.actions) ? data.actions.filter(x => x && x.tool) : [];
      if (acts.length) {
        const box = document.createElement('div');
        box.className = 'gm-acts';
        acts.forEach(x => {
          const bad = actionFailed(x.result);
          const t = document.createElement('span');
          t.className = 'gm-act' + (bad ? ' bad' : '');
          t.innerHTML = svg(bad ? 'close' : 'check');
          t.appendChild(document.createTextNode(actionLabel(x)));
          box.appendChild(t);
        });
        body.appendChild(box);
      }
      gmHistory.push({ role: 'user', text: promptText }, { role: 'model', text: data.text || '' });
      if (acts.some(x => /smart|screen/.test(String(x.tool)))) setTimeout(fetchSmartDevices, 1200);
    }
    geminiChatEl.appendChild(a);
    (q || a).scrollIntoView({ block: 'start' });
    showFollowUps(failed ? [] : data.suggestions, failed ? { prompt: promptText, label } : null);
    focusFirstChip();
  }

  // D-pad inside the popup: left/right within a row, up/down to the nearest chip of the next row;
  // beyond the top/bottom row, up/down scrolls the answer instead.
  function geminiKey(k) {
    const chips = gmFocusables();
    if (!chips.length) {
      if (k === KEY.UP) geminiChatEl.scrollTop -= 260;
      if (k === KEY.DOWN) geminiChatEl.scrollTop += 260;
      return;
    }
    const cur = document.activeElement;
    if (!chips.includes(cur)) { chips[0].focus(); return; }
    const rows = [...geminiChipsEl.querySelectorAll('.gm-row')]
      .map(r => [...r.querySelectorAll('.gm-chip')].filter(b => !b.disabled)).filter(r => r.length);
    const ri = rows.findIndex(r => r.includes(cur));
    const ci = rows[ri].indexOf(cur);
    if (k === KEY.LEFT && ci > 0) rows[ri][ci - 1].focus();
    else if (k === KEY.RIGHT && ci < rows[ri].length - 1) rows[ri][ci + 1].focus();
    else if (k === KEY.UP || k === KEY.DOWN) {
      const ti = ri + (k === KEY.UP ? -1 : 1);
      if (ti < 0) { geminiChatEl.scrollTop -= 260; return; }
      if (ti >= rows.length) { geminiChatEl.scrollTop += 260; return; }
      const x = cur.getBoundingClientRect().left + cur.offsetWidth / 2;
      let best = rows[ti][0], bd = Infinity;
      for (const b of rows[ti]) {
        const d = Math.abs(b.getBoundingClientRect().left + b.offsetWidth / 2 - x);
        if (d < bd) { bd = d; best = b; }
      }
      best.focus();
    }
  }

  function initGeminiUI() {
    if (geminiCloseBtn) geminiCloseBtn.addEventListener('click', closeGeminiModal);
    if (geminiBackdropEl) geminiBackdropEl.addEventListener('click', closeGeminiModal);
  }

  // Launch parameters: launcherd.py starts us with {gemini: 'open'|'toggle'} for the mic button
  // and with {reason: 'home'} for Home, so old parameters never linger.
  function launchParams(e) {
    let p = e && e.detail;
    if (!p || typeof p !== 'object') {
      try { p = JSON.parse((window.PalmSystem && window.PalmSystem.launchParams) || '{}'); } catch (_) { p = {}; }
    }
    return p || {};
  }

  function handleGeminiParam(p) {
    if (!p || !p.gemini) return false;
    if (mode === 'menu') closeMenu();
    if (mode === 'move') endMove();
    if (p.gemini === 'toggle') toggleGeminiModal(); else openGeminiModal();
    return true;
  }

  async function start() {
    activateApp();
    tick();
    $('g-icon').innerHTML = GEMINI_ICON;
    $('gm-spark').innerHTML = GEMINI_ICON;
    $('gm-mic').innerHTML = svg('mic');
    geminiCloseBtn.innerHTML = svg('close');
    for (const [id, el] of PILLS) { el.dataset.id = id; el.dataset.key = id; els.set(id, el); bindPointer(el); }
    initGeminiUI();
    initSoundOutput();
    fetchWeather();
    fetchSmartDevices();
    fetchAmbient();

    try {
      await loadApps();
      render();
    } catch (e) {
      console.error('start failed', e);
    } finally {
      // two frames: the first paint shows the hidden state, then the sections rise in
      requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.remove('booting')));
    }
    handleGeminiParam(launchParams(null));    // first start via the mic button (app was not running)
    lunaSubscribe('luna://com.webos.service.eim/getAllInputStatus', {}, msg => {
      if (Array.isArray(msg.devices)) { state.devices = msg.devices; requestRender(); }
    });
    refreshData(false);
    setInterval(() => { if (!document.hidden) loadApps().then(changed => { if (changed) requestRender(); }); }, 60000);
    setInterval(() => { if (!document.hidden) { fetchSmartDevices(); fetchAmbient(); } }, 20000);
    setInterval(() => { if (!document.hidden && ambientHot()) fetchAmbient(); }, 4000);   // tile focused or its menu open
    setInterval(() => { if (!document.hidden) fetchWeather(); }, 600000);
    window.addEventListener('resize', () => { layoutDirty = true; });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { layoutDirty = true; });
  }

  // whatever goes wrong, never leave the screen faded out or blank
  const unstick = () => document.body.classList.remove('booting', 'leaving');
  window.addEventListener('error', unstick);
  window.addEventListener('unhandledrejection', unstick);

  // for tests and debugging from the web inspector
  window.__launcher = {
    get focus() { return focusId; }, get focusLog() { return focusLog.slice(); }, get geom() { return JSON.stringify(geom.map(g => ({ top: g.top, xs: g.xs.map(Math.round).join(","), max: g.maxScroll }))); }, get scrollXs() { return [...rowEls].map(([k, re]) => k + "=" + re.scrollX).join(" "); }, get mode() { return mode; }, get recent() { return recentId; },
    get rows() { return rows.map(r => r.key + ': ' + r.ids.join(', ')); }, get favs() { return favIds.slice(); },
    get tiles() { return tileIds.slice(); }, get scrollY() { return scrollY; }, get billboard() { return bbKey; },
    setUsage(u) { state.usage = u; render(); }, get prefs() { return clone(prefs); },
    get menu() { return menu ? menu.entries.map(e => e.label) : null; },
    get menuIndex() { return menu ? menu.index : -1; }, get menuTitle() { return menu ? $('menu-title').textContent : null; },
    get gemini() { return geminiModalOpen; }, get okDown() { return okDown; }
  };

  start();
})();
