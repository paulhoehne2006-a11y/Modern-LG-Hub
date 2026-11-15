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
  const SAM = 'luna://com.webos.applicationManager/';
  const HB_EXEC = 'luna://org.webosbrew.hbchannel.service/exec';
  const BACKEND = 'http://127.0.0.1:8080';
  const IS_TV = typeof window.PalmServiceBridge !== 'undefined';
  const APP_DIR = decodeURIComponent(location.pathname).replace(/\/[^/]*$/, '');
  const STORE_KEY = 'launcher.v3';
  const SMART_CACHE_KEY = 'launcher.smart';
  const HALF_LIFE = 45 * 86400;    // usage counts fade by half every 45 days
  const FAVS = 7;                  // cards in the first row (last used + favourites)
  const DEFAULT_ROOM = 'Arbeitszimmer';
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
    mainRoom: ''                   // smart home room shown device by device ('' = Arbeitszimmer)
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
    play: '<path d="M7 4.5v15l13-7.5z"/>',
    // weather
    sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    night: '<path d="M19 14.2A7.5 7.5 0 1 1 9.8 5a6 6 0 0 0 9.2 9.2z"/>',
    partly: '<path d="M8.5 3.2v1.2M3.8 8H5M5.2 4.7l.8.8M11.8 4.7l-.8.8"/><path d="M5.6 11.4a3.6 3.6 0 1 1 6.6-3"/><path d="M17.3 20H9.6a3.9 3.9 0 1 1 1-7.7 4.9 4.9 0 0 1 9.3 1.9A3 3 0 0 1 17.3 20z"/>',
    cloud: '<path d="M17.5 19H7.5a5 5 0 1 1 1.2-9.86A6 6 0 0 1 20 11.5 3.75 3.75 0 0 1 17.5 19z"/>',
    fog: '<path d="M4 8.5h16M2.5 12.5h19M5 16.5h14"/>',
    rain: CLOUD_HI + '<path d="M8.5 17.5l-1 2.5M12.5 17.5l-1 2.5M16.5 17.5l-1 2.5"/>',
    snow: CLOUD_HI + '<path d="M8 18.5v.01M12 18v.01M16 18.5v.01M10 21v.01M14 21v.01" stroke-width="2.6"/>',
    storm: CLOUD_HI + '<path d="M12.8 15.5 10.6 19h3l-2 3"/>'
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
  function api(path, body, ms) {
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
  const state = { apps: [], generatedAt: 0, devices: null, usage: {}, usageStamp: 0, smart: loadSmartCache() };
  let items = new Map();          // id -> item (apps, inputs, utilities)
  let recentId = null;            // the app/input used last
  let favIds = [];                // item ids of the first row
  let rows = [];                  // [{key, title, type: 'cards'|'tiles', ids, keys}]
  let tileIds = [];
  let tiles = new Map();          // id -> smart home tile item
  let smartSummary = '';

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
      map.set(d.appId, { id: d.appId, kind: 'input', title, sub, connected: !!d.connected, glyph: title === 'PC' ? 'pc' : 'hdmi' });
    }
    map.set(LIVE_TV, { id: LIVE_TV, kind: 'input', title: 'Live TV', sub: 'TV-Empfang', connected: false, glyph: 'tv', noDot: true });
    for (const a of state.apps) {
      if (map.has(a.id) || a.id === APP_ID || a.id === SETTINGS) continue;
      if (a.visible === false || a.type === 'stub') continue;   // hidden system apps, store placeholders
      map.set(a.id, { id: a.id, kind: 'app', title: shortTitle(a.title || a.id), fullTitle: a.title || a.id,
        icon: a.icon ? a.icon + '?v=' + (a.iconV || 0) : null, iconColor: a.iconColor || null });
    }
    map.set(WETTER_APP, { id: WETTER_APP, kind: 'util', title: 'WetterOnline', sub: 'Wetter & Radar', glyph: 'weather' });
    map.set(MORE, { id: MORE, kind: 'util', title: 'Mehr', sub: 'Apps verwalten', glyph: 'more' });
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
      if (Array.isArray(c) && c.length) return c.map(d => Object.assign({}, d, { online: null }));   // state is old: shown as "…" until fresh
    } catch (e) { /* ignore */ }
    return null;
  }

  const pending = new Map();      // mac -> {state, brightness, until}: optimistic state while a command runs

  function smartDevices() {
    const list = (state.smart && state.smart.length) ? state.smart : SMART_FALLBACK;
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

  function deviceState(d) {
    if (d.online === false) return 'Nicht erreichbar';
    if (d.online == null && !pending.has(d.id)) return '…';
    if (d.state !== 'on') return 'Aus';
    return d.type === 'light' && typeof d.brightness === 'number' ? 'An · ' + d.brightness + ' %' : 'An';
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
      const lit = on.find(x => x.color);
      map.set('shr:' + room, { id: 'shr:' + room, kind: 'room', room, devs: inRoom, title: room,
        state: !known ? '…' : (on.length ? on.length + ' von ' + inRoom.length + ' an' : 'Alles aus'),
        on: on.length > 0, offline: false, lamp: lit ? lit.color : null, icon: 'room' });
    }
    map.set(DISPLAY_TILE, { id: DISPLAY_TILE, kind: 'display', title: 'Display aus', state: 'Musik-Modus', on: false, icon: 'moon' });
    tiles = map;
    tileIds = [...map.keys()];
    const mainDevs = devs.filter(x => x.room === main);
    const onCount = mainDevs.filter(x => x.state === 'on' && x.online !== false).length;
    smartSummary = main ? main + ' · ' + (onCount ? onCount + (onCount === 1 ? ' Gerät' : ' Geräte') + ' an' : 'alles aus') : '';
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
        re.sub.textContent = smartSummary;
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
    setFocus(key, { instant: !focusId, keepScroll: true });
  }

  function renderTiles() {
    const before = tileIds.join();
    buildTiles();
    const smart = rows.find(r => r.key === 'smart');
    const re = rowEls.get('smart');
    if (!smart || !re) return;
    smart.ids = tileIds; smart.keys = tileIds;
    place(re.track, tileIds, k => tiles.get(k), createTile, updateTile);
    re.sub.textContent = smartSummary;
    for (const k of [...els.keys()]) if ((k.startsWith('shd:') || k.startsWith('shr:')) && !tiles.has(k)) els.delete(k);
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
  const PUSH = { cards: 30, tiles: 16 };

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
      return { top: re ? re.section.offsetTop : 0, xs, ws, maxScroll: r.type === 'cards' ? Math.max(0, trackW - (VIEW_W - 2 * SIDE)) : 0 };
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
    if (opts.instant || !bbKey) { showBillboard(key, false); scheduleGlow(true); }
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
    if (pos.row.type === 'cards' && g) {
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

  function billboardInfo(key) {
    const okLong = (ok, long) => [['OK', ok]].concat(long ? [['Lang drücken', long]] : []);
    if (key === 'hero:weather') {
      const w = lastWeather, cur = w && w.current, d = w && w.daily;
      const [, desc] = (cur && WMO_MAP[cur.weather_code]) || ['', ''];
      let line = 'Wetterdaten werden geladen …';
      if (d && d.temperature_2m_max) line = 'Heute ' + Math.round(d.temperature_2m_min[0]) + '° bis ' + Math.round(d.temperature_2m_max[0]) + '°, ' +
        (d.precipitation_probability_max[0] || 0) + ' % Regenrisiko';
      return { kicker: 'Wetter · Musterstadt', title: cur ? Math.round(cur.temperature_2m) + '° · ' + desc : 'Wetter', desc: line,
        keys: okLong('WetterOnline öffnen'), icon: $('w-icon').dataset.icon || 'cloud', logo: 'icon weather', glow: 'rgb(60,150,255)' };
    }
    if (key === 'hero:gemini') {
      return { kicker: 'KI-Assistent', title: 'Gemini', desc: 'Filmtipps, Wetter, Wissen – und TV und Licht per Sprache steuern',
        keys: [['OK', 'Öffnen'], ['🎙', 'Mikro-Taste']], iconHtml: GEMINI_ICON, logo: 'icon gemini', glow: 'rgb(139,92,246)' };
    }
    if (key === 'hero:audio') {
      return { kicker: 'Audioausgabe', title: $('audio-title').textContent, desc: $('audio-sub').textContent,
        keys: okLong('Ton-Einstellungen'), icon: $('audio-icon').dataset.icon || 'speaker', logo: 'icon audio', glow: 'rgb(56,189,248)' };
    }
    const t = tiles.get(key);
    if (t) {
      const lamp = lampOf(t);
      if (t.kind === 'display') {
        return { kicker: 'Denon Musik-Modus', title: 'Display aus', desc: 'Nur das OLED-Panel geht aus, der Ton läuft weiter',
          keys: okLong('Ausschalten'), icon: 'moon', logo: 'icon round display', glow: 'rgb(168,85,247)' };
      }
      if (t.kind === 'room') {
        return { kicker: 'Raum · ' + t.devs.length + ' Geräte', title: t.title, desc: t.state,
          keys: okLong(t.on ? 'Alles ausschalten' : 'Alles einschalten', 'Einzelne Geräte'),
          icon: 'room', logo: 'icon round' + (t.on ? ' on' : ''), lamp, glow: t.on ? lamp : 'rgb(70,78,96)' };
      }
      const light = t.dev.type === 'light', caps = t.dev.capabilities || [];
      const tune = caps.includes('color') || caps.includes('white') ? 'Farbe & Helligkeit' : 'Helligkeit';
      return { kicker: t.dev.room + ' · ' + (light ? 'Licht' : 'Steckdose'), title: t.title, desc: t.state,
        keys: okLong(t.dev.state === 'on' ? 'Ausschalten' : 'Einschalten', light ? tune : 'Optionen'),
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
    const desc = APP_DESC[it.id] || (it.kind === 'input' ? (it.connected ? 'Verbunden · ' : 'Nicht verbunden · ') + (it.id.replace(/\D/g, '') ? 'HDMI ' + it.id.replace(/\D/g, '') : '') : '');
    const info = { kicker, title: it.title, desc, keys: okLong(it.kind === 'input' ? 'Umschalten' : 'Öffnen', 'Optionen'),
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
    const keys = $('bb-keys');
    keys.textContent = '';
    for (const [k, label] of info.keys || []) {
      const s = h('span', 'bb-key');
      s.appendChild(h('b', '', k));
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
    } else {
      img.style.display = 'none';
      const html = info.iconHtml || svg(info.icon);
      if (ic.dataset.html !== html) { ic.innerHTML = html; ic.dataset.html = html; }
    }
    scheduleGlow(true);   // colour light changes together with the text (it used to wait 650 ms: felt late)
  }

  // The colour light is painted into static backgrounds (#page, #fixed, #fixed-fade), never into a layer
  // that moves. Repainting it costs the TV a frame or two, so it changes only once per billboard swap
  // (when the text is faded out), not on every key press.
  const GLOW_REST_MS = 650;
  let glowTimer = 0;
  function scheduleGlow(now) {
    clearTimeout(glowTimer);
    if (now) { applyGlow(); return; }
    glowTimer = setTimeout(applyGlow, GLOW_REST_MS);
  }
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

  let smartRefreshTimer = 0;
  async function smartSet(devs, on, brightness, tileId, tone) {   // tone: { color: "#rrggbb" } or { kelvin }
    if (!devs.length) return;
    const until = Date.now() + 5000;
    for (const d of devs) pending.set(d.id, { state: on ? 'on' : 'off', brightness: brightness != null ? brightness : null, until });
    renderTiles();
    if (tileId) popTile(tileId);
    if (mode === 'menu' && menuRefresh) menuRefresh();
    const results = await Promise.all(devs.map(d => {
      if (!IS_TV) return Promise.resolve({ status: 'ok' });
      const body = { id: d.id, state: on ? 'on' : 'off' };
      if (brightness != null) body.brightness = brightness;
      if (tone) Object.assign(body, tone);
      return api('/api/smarthome/set', body).catch(() => ({ status: 'error', error: 'Smart-Home-Server nicht erreichbar' }));
    }));
    const failed = devs.filter((d, i) => !results[i] || results[i].status !== 'ok');
    for (const d of devs) {
      if (failed.includes(d)) pending.delete(d.id);
      else if (state.smart) {   // keep the new state in the model until the next poll confirms it
        const m = state.smart.find(x => x.id === d.id);
        if (m) { m.state = on ? 'on' : 'off'; if (brightness != null) m.brightness = brightness; if (tone) { m.color = tone.color || kelvinHex(tone.kelvin); m.kelvin = tone.kelvin || null; } m.online = true; }
      }
    }
    if (tone && failed.length < devs.length) {   // show the new colour at once, not with the next poll
      renderTiles();
      if (mode === 'menu' && menuRefresh) menuRefresh();
    }
    if (failed.length) {
      const err = results.find(r => r && r.status !== 'ok');
      toast((failed.length === 1 ? failed[0].name : failed.length + ' Geräte') + ': ' + ((err && err.error) || 'nicht erreichbar'), 3600);
      renderTiles();
      if (tileId) popTile(tileId, 'fail');
      if (mode === 'menu' && menuRefresh) menuRefresh();
    }
    clearTimeout(smartRefreshTimer);
    smartRefreshTimer = setTimeout(fetchSmartDevices, 1500);
  }

  function activateTile(t) {
    if (t.kind === 'display') { screenOff(); return; }
    if (t.kind === 'dev') {
      if (t.offline) { toast(t.title + ' ist gerade nicht erreichbar'); popTile(t.id, 'fail'); fetchSmartDevices(); return; }
      smartSet([t.dev], t.dev.state !== 'on', null, t.id);
      return;
    }
    // room: anything on -> all off, otherwise all on
    const reachable = t.devs.filter(d => d.online !== false);
    smartSet(reachable, !t.on, null, t.id);
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
    if (!IS_TV) { renderTiles(); return; }
    try {
      const devs = await api('/api/smarthome/devices', undefined, 8000);
      if (!Array.isArray(devs) || !devs.length) return;
      const now = Date.now();
      for (const [id, p] of pending) {        // a fresh reading that agrees ends the optimistic phase
        const d = devs.find(x => x.id === id);
        if (p.until < now || (d && d.state === p.state)) pending.delete(id);
      }
      const changed = JSON.stringify(devs) !== JSON.stringify(state.smart);
      state.smart = devs;
      if (changed) {
        try { localStorage.setItem(SMART_CACHE_KEY, JSON.stringify(devs)); } catch (e) { /* ignore */ }
        renderTiles();
        if (mode === 'menu' && menuRefresh) menuRefresh();
      }
    } catch (e) { /* backend down: keep showing what we have */ }
  }

  /* ------------------------------------------------------------------ */
  /* Menu                                                                */
  /* ------------------------------------------------------------------ */
  const menuEl = $('menu'), menuList = $('menu-list');
  const MENU_ROWS = 8, MENU_ROW_H = 62;
  let menu = null;              // { entries, index, back }
  let menuArmed = true;         // OK only counts after the key that opened the menu was released
  let menuRefresh = null;       // rebuilds the open menu in place (live smart home state)
  let menuCursor = null;

  function openMenu(def) {
    const reopen = mode === 'menu' && menu;
    menu = { entries: def.entries, index: Math.min(def.index || 0, def.entries.length - 1), back: def.back || null };
    menuRefresh = def.refresh || null;
    $('menu-title').textContent = def.title;
    $('menu-sub').textContent = def.sub || '';
    const icon = $('menu-icon');
    icon.style.backgroundImage = def.icon ? 'url("' + def.icon + '")' : '';
    icon.style.setProperty('--c', def.color || '#2c2d31');
    icon.style.display = def.icon ? '' : 'none';
    $('menu-hint').textContent = def.hint || '';
    menuList.textContent = '';
    menuCursor = h('div', '');
    menuCursor.id = 'menu-cursor';
    menuList.appendChild(menuCursor);
    def.entries.forEach((entry, i) => {
      const row = h('div', 'mi' + (entry.danger ? ' danger' : '') + (entry.on ? ' on' : ''));
      row.setAttribute('role', 'menuitem');
      const label = h('span', '', entry.label);
      if (entry.swatch) {
        const sw = h('i', 'mi-swatch');
        sw.style.setProperty('--sw', entry.swatch);
        label.insertBefore(sw, label.firstChild);
      }
      row.appendChild(label);
      if (entry.hint) row.appendChild(h('span', 'mi-hint', entry.hint));
      row.addEventListener('mouseenter', () => menuFocus(i));
      row.addEventListener('click', () => { menuFocus(i); menuSelect(); });
      menuList.appendChild(row);
    });
    menuEl.hidden = false;
    mode = 'menu';
    menuArmed = reopen ? menuArmed : !okDown;
    menuFocus(menu.index, !reopen);
    requestAnimationFrame(() => { if (menu) document.body.classList.add('menu-open'); });
  }

  function menuFocus(i, instant) {
    if (!menu) return;
    menu.index = Math.max(0, Math.min(menu.entries.length - 1, i));
    const rows = menuList.querySelectorAll('.mi');
    rows.forEach((row, j) => row.classList.toggle('focused', j === menu.index));
    if (menuCursor) {
      if (instant) menuCursor.style.transition = 'none';
      menuCursor.style.transform = 'translateY(' + (menu.index * MENU_ROW_H) + 'px)';
      if (instant) { void menuCursor.offsetWidth; menuCursor.style.transition = ''; }
    }
    const top = Math.max(0, Math.min(menu.index - Math.floor(MENU_ROWS / 2), menu.entries.length - MENU_ROWS));
    menuList.style.transform = 'translateY(' + (-top * MENU_ROW_H) + 'px)';
  }

  function menuSelect() {
    const entry = menu && menu.entries[menu.index];
    if (entry && entry.action) entry.action();
  }

  function closeMenu() {
    menu = null;
    menuRefresh = null;
    if (picker) { clearTimeout(picker.sendTimer); if (picker.dirty) pickerSend(); picker = null; }
    if (mode === 'menu' || mode === 'picker') mode = 'home';
    flushRender();
    document.body.classList.remove('menu-open');
    setTimeout(() => { if (!menu) menuEl.hidden = true; }, 320);
  }

  function menuBack() {
    const back = menu && menu.back;
    if (back) back(); else closeMenu();
  }

  function openCardMenu(id) {
    if (id === 'hero:weather' || id === WETTER_APP) {
      openMenu({
        title: 'Wetter', sub: 'Musterstadt',
        entries: [
          { label: 'WetterOnline öffnen', hint: '14 Tage & Radar', action: () => { closeMenu(); openWeatherApp(); } },
          { label: 'Wetter aktualisieren', action: () => { closeMenu(); fetchWeather(true); } }
        ]
      });
      return;
    }
    if (id === 'hero:gemini') { openGeminiModal(); return; }
    if (id === 'hero:audio') {
      openMenu({
        title: 'Audioausgabe', sub: $('audio-title').textContent,
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
      icon: it.icon || null, color: el ? el.style.getPropertyValue('--c') : '', entries });
  }

  function openTileMenu(id, index) {
    const t = tiles.get(id);
    if (!t) { closeMenu(); return; }
    const again = () => openTileMenu(id, menu ? menu.index : 0);
    if (t.kind === 'display') {
      openMenu({
        title: 'Display aus', sub: 'Musik-Modus für den Denon',
        hint: 'Schaltet nur das OLED-Panel aus, der Ton läuft weiter. Jede Taste schaltet es wieder ein.',
        entries: [{ label: 'Display jetzt ausschalten', action: () => { closeMenu(); screenOff(); } }]
      });
      return;
    }
    if (t.kind === 'dev') {
      const d = t.dev;
      const entries = [{ label: d.state === 'on' ? 'Ausschalten' : 'Einschalten', action: () => { activateTile(tiles.get(id) || t); } }];
      const caps = d.capabilities || [];
      if (d.type === 'light' && (caps.includes('color') || caps.includes('white'))) {
        entries.push({ label: 'Farbe', hint: (TONES.find(x => toneIs(d, x)) || {}).label || '', action: () => openToneMenu(id, () => openTileMenu(id, 1)) });
      }
      if (d.type === 'light') {
        for (const b of [10, 25, 50, 75, 100]) {
          const cur = d.state === 'on' && typeof d.brightness === 'number' && Math.abs(d.brightness - b) < 5;
          entries.push({ label: 'Helligkeit ' + b + ' %', hint: cur ? 'aktuell' : '', on: cur,
            action: () => smartSet([d], true, b, id) });
        }
      }
      entries.push({ label: 'Hauptraum wechseln', hint: t.dev.room, action: () => openRoomPicker(again) });
      openMenu({ title: d.name, sub: d.room + ' · ' + deviceState(d), entries, index, refresh: again });
      return;
    }
    // room
    const entries = [
      { label: 'Alle einschalten', action: () => smartSet(t.devs.filter(d => d.online !== false), true, null, id) },
      { label: 'Alle ausschalten', action: () => smartSet(t.devs.filter(d => d.online !== false), false, null, id) }
    ];
    for (const d of t.devs) {
      entries.push({ label: d.name, hint: deviceState(d), on: d.state === 'on' && d.online !== false,
        action: () => { if (d.online === false) toast(d.name + ' ist gerade nicht erreichbar'); else smartSet([d], d.state !== 'on', null, id); } });
    }
    entries.push({ label: 'Als Hauptraum zeigen', hint: 'Geräte einzeln', action: () => { setMainRoom(t.room); closeMenu(); } });
    openMenu({ title: t.room, sub: t.state, entries, index, refresh: again, hint: 'OK schaltet das Gerät um.' });
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
    const entries = [{
      label: caps.includes('color') ? 'Eigene Farbe …' : 'Eigener Weißton …', hint: caps.includes('color') ? 'alle Farben' : 'stufenlos',
      swatch: 'conic-gradient(#f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)',
      action: () => openPicker(id, () => openToneMenu(id, back, 0))
    }];
    for (const x of tones) {
      const cur = toneIs(d, x);
      entries.push({ label: x.label, hint: cur ? 'aktuell' : (x.hint || ''), on: cur, swatch: x.color || kelvinHex(x.kelvin),
        action: () => smartSet([d], true, null, id, x.kelvin ? { kelvin: x.kelvin } : { color: x.color }) });
    }
    openMenu({
      title: 'Farbe', sub: d.name + ' · ' + deviceState(d),
      entries, index: index || 0, refresh: again, back,
      hint: 'OK stellt die Farbe ein, das Menü bleibt offen. Links/Zurück geht eine Ebene zurück.'
    });
  }

  /* Colour picker: every colour (hue + saturation) and every white tone, live on the lamp.
     Up/down picks a slider, left/right moves it (held keys move faster), OK or Back closes. */
  let picker = null;
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
    const rows = [];
    if (caps.includes('color')) rows.push('hue', 'sat');
    if (caps.includes('white')) rows.push('kelvin');
    if (!rows.length) { closeMenu(); return; }
    const hsv = (!d.kelvin && hexHsv(d.color)) || { h: 30, s: 1 };
    picker = {
      id, d, back, rows, row: d.kelvin && rows.includes('kelvin') ? rows.indexOf('kelvin') : 0,
      h: Math.round(hsv.h), s: Math.round(hsv.s * 100) / 100,
      k: Math.max(KELVIN_MIN, Math.min(KELVIN_MAX, Math.round((d.kelvin || 2700) / 100) * 100)),
      white: !!d.kelvin || !caps.includes('color'), sendTimer: 0, els: {}
    };
    openMenu({ title: rows[0] === 'hue' ? 'Eigene Farbe' : 'Eigener Weißton', sub: d.name, entries: [],
      hint: '▲▼ Regler wählen · ◀▶ verstellen (halten = schneller) · OK fertig' });
    mode = 'picker';
    menuCursor.style.display = 'none';
    const box = h('div', 'pk');
    const prev = h('div', 'pk-preview');
    box.appendChild(prev);
    picker.els.preview = prev;
    const names = { hue: 'Farbton', sat: 'Sättigung', kelvin: 'Weißton' };
    picker.els.rows = rows.map(r => {
      const row = h('div', 'pk-row');
      const head = h('div', 'pk-head');
      head.appendChild(h('span', 'pk-label', names[r]));
      const val = h('span', 'pk-val');
      head.appendChild(val);
      const bar = h('div', 'pk-bar ' + r);
      const knob = h('b', '');
      bar.appendChild(knob);
      row.appendChild(head); row.appendChild(bar);
      box.appendChild(row);
      return { r, row, val, bar, knob };
    });
    menuList.appendChild(box);
    pickerPaint();
  }

  function pickerPaint() {
    const p = picker;
    if (!p) return;
    const color = p.white ? kelvinHex(p.k) : hsvHex(p.h, p.s);
    p.els.preview.style.setProperty('--c', color);
    for (const [i, x] of p.els.rows.entries()) {
      x.row.classList.toggle('focused', i === p.row);
      x.row.classList.toggle('active', x.r === 'kelvin' ? p.white : !p.white);
      let pos, text;
      if (x.r === 'hue') { pos = p.h / 360; text = p.h + '°'; }
      else if (x.r === 'sat') { pos = p.s; text = Math.round(p.s * 100) + ' %'; x.bar.style.setProperty('--full', hsvHex(p.h, 1)); }
      else { pos = (p.k - KELVIN_MIN) / (KELVIN_MAX - KELVIN_MIN); text = p.k + ' K'; }
      x.knob.style.setProperty('--p', pos.toFixed(4));
      x.val.textContent = text;
    }
  }

  function pickerKey(k, e) {
    const p = picker;
    if (!p) { closeMenu(); return; }
    if (k === KEY.UP || k === KEY.DOWN) {
      p.row = Math.max(0, Math.min(p.rows.length - 1, p.row + (k === KEY.UP ? -1 : 1)));
      pickerPaint();
      return;
    }
    if (k === KEY.LEFT || k === KEY.RIGHT) {
      const now = Date.now(), dir = k === KEY.LEFT ? -1 : 1;
      const fast = e.repeat || now - (p.lastKey || 0) < 260 ? 3 : 1;   // held key, or the phone app repeating
      p.lastKey = now;
      p.dirty = true;
      const r = p.rows[p.row];
      if (r === 'hue') { p.h = ((p.h + dir * 5 * fast) % 360 + 360) % 360; p.white = false; }
      else if (r === 'sat') { p.s = Math.max(0, Math.min(1, Math.round((p.s + dir * 0.05 * fast) * 100) / 100)); p.white = false; }
      else { p.k = Math.max(KELVIN_MIN, Math.min(KELVIN_MAX, p.k + dir * 100 * fast)); p.white = true; }
      pickerPaint();
      clearTimeout(p.sendTimer);   // the lamp follows once the value rests a moment (no request per key)
      p.sendTimer = setTimeout(pickerSend, 160);
      return;
    }
    if (k === KEY.OK && (e.repeat || okBlocked)) return;
    // OK, Back, Esc: done (the lamp already shows the colour)
    clearTimeout(p.sendTimer);
    if (p.dirty) pickerSend();
    const back = p.back;
    picker = null;
    if (k === KEY.OK) { okBlocked = true; closeMenu(); } else if (back) back(); else closeMenu();
  }

  function pickerSend() {
    const p = picker;
    if (!p) return;
    p.dirty = false;
    smartSet([p.d], true, null, null, p.white ? { kelvin: p.k } : { color: hsvHex(p.h, p.s) });
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
    openMenu({
      title: 'Hauptraum', sub: 'Seine Geräte erscheinen einzeln',
      entries: rooms.map(r => ({ label: r, hint: r === main ? 'aktuell' : devs.filter(d => d.room === r).length + ' Geräte',
        on: r === main, action: () => { setMainRoom(r); closeMenu(); } })),
      index: Math.max(0, rooms.indexOf(main)),
      back: back || (() => openMoreMenu(0))
    });
  }

  function openMoreMenu(index) {
    const hidden = hiddenItems();
    openMenu({
      title: 'Mehr', index,
      entries: [
        { label: 'Smart Home: Hauptraum', hint: mainRoom(smartDevices()), action: () => openRoomPicker() },
        { label: 'Ausgeblendete Apps', hint: String(hidden.length), action: () => openHiddenMenu(0) },
        { label: 'TV-Einstellungen', action: () => { closeMenu(); launchId(SETTINGS, 'Einstellungen'); } },
        { label: 'App-Liste aktualisieren', action: () => { closeMenu(); refreshData(true).then(() => toast('App-Liste aktualisiert')); } },
        { label: 'LG-Startbildschirm öffnen', action: () => { closeMenu(); openLgHome(); } },
        { label: 'Auf Standard zurücksetzen', danger: true, action: openResetMenu }
      ],
      hint: 'Tipp: OK gedrückt halten öffnet das Menü jeder Kachel.'
    });
  }

  function openHiddenMenu(index) {
    const hidden = hiddenItems().sort((a, b) => a.title.localeCompare(b.title, 'de', { sensitivity: 'base' }));
    const entries = hidden.length
      ? hidden.map(it => ({ label: it.title, hint: 'einblenden', action: () => { setHidden(it.id, false); openHiddenMenu(menu ? menu.index : 0); } }))
      : [{ label: 'Keine ausgeblendeten Apps', action: () => openMoreMenu(1) }];
    openMenu({ title: 'Ausgeblendete Apps', entries, index, back: () => openMoreMenu(1), hint: hidden.length ? 'OK blendet die App wieder ein.' : '' });
  }

  function openResetMenu() {
    openMenu({
      title: 'Zurücksetzen?', sub: 'Favoriten, Ausblendungen, Hauptraum', back: () => openMoreMenu(5), index: 1,
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
    wake();
    if (k === KEY.INFO) {
      if (e.repeat) return;
      if (mode === 'menu' || mode === 'picker') closeMenu();
      else if (mode === 'home' && !okDown && focusId) { homeStartUntil = 0; openCardMenu(focusId); }
      return;
    }
    if (mode === 'picker') { pickerKey(k, e); return; }
    if (mode === 'menu') {
      if (k === KEY.UP) menuFocus(menu.index - 1);
      else if (k === KEY.DOWN) menuFocus(menu.index + 1);
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

  $('scrim').addEventListener('click', () => { if (mode === 'menu' || mode === 'picker') closeMenu(); });

  let lastWheel = 0;
  document.addEventListener('wheel', e => {
    wake();
    const now = Date.now();
    if (now - lastWheel < 200 || !e.deltaY) return;
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
      else if (elapsed > IDLE_MS) document.body.classList.add('idle');
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
      const data = await getJSON('data/apps.json?t=' + Date.now());
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
    if (mode === 'menu' || mode === 'picker') closeMenu();
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

  if (!IS_TV) {
    // PC preview only: the TV has these fonts built in, and there is no backend
    const st = document.createElement('style');
    const f = (w, file, fam) => '@font-face{font-family:"' + (fam || 'LG Smart UI') + '";font-weight:' + w +
      ';src:url("../design/fixtures/fonts/' + file + '")}';
    st.textContent = f(300, 'LG_Smart_UI-Light.ttf') + f(400, 'LG_Smart_UI-Regular.ttf') +
      f(600, 'LG_Smart_UI-SemiBold.ttf') + f(700, 'LG_Smart_UI-Bold.ttf');
    document.head.appendChild(st);
    state.devices = [
      { id: 'HDMI_1', label: 'HDMI 1', appId: 'com.webos.app.hdmi1', connected: true },
      { id: 'HDMI_2', label: 'HDMI 2', appId: 'com.webos.app.hdmi2', connected: false },
      { id: 'HDMI_3', label: 'HDMI 3', appId: 'com.webos.app.hdmi3', connected: false },
      { id: 'HDMI_4', label: 'HDMI 4', appId: 'com.webos.app.hdmi4', connected: false }
    ];
    state.smart = SMART_FALLBACK.map((d, i) => Object.assign({}, d, { online: true,
      state: i === 1 || i === 2 ? 'on' : 'off', brightness: d.type === 'light' ? 70 : null, color: i === 1 ? '#ffa657' : (d.type === 'light' ? '#ffc18e' : null) }));
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
  async function fetchWeather(manual) {
    if (!IS_TV) {
      updateWeatherUI({ current: { temperature_2m: 4.3, apparent_temperature: 1, weather_code: 1 }, daily: { precipitation_probability_max: [10] } });
      return;
    }
    try {
      const data = await api('/api/weather', undefined, 8000);
      if (data && data.current) { lastWeather = data; updateWeatherUI(data); if (manual) toast('Wetter aktualisiert'); }
      else if (manual) toast('Wetter gerade nicht verfügbar');
    } catch (e) { if (manual) toast('Wetter gerade nicht verfügbar'); }
  }

  function updateWeatherUI(data) {
    const cur = data.current;
    if (!cur) return;
    const hour = new Date().getHours();
    let [icon, desc] = WMO_MAP[cur.weather_code] || ['cloud', 'Bewölkt'];
    if ((hour >= 20 || hour < 6) && (icon === 'sun' || icon === 'partly')) icon = cur.weather_code === 0 ? 'night' : 'cloud';
    const iconEl = $('w-icon');
    if (iconEl.dataset.icon !== icon) { iconEl.dataset.icon = icon; iconEl.innerHTML = svg(icon); }
    $('w-temp').textContent = Math.round(cur.temperature_2m) + '° Musterstadt';
    const rain = data.daily && data.daily.precipitation_probability_max && data.daily.precipitation_probability_max[0];
    $('w-sub').textContent = desc + (rain >= 30 ? ' · ' + rain + ' % Regen' : '');
    layoutDirty = true;
  }

  /* ------------------------------------------------------------------ */
  /* Audio output & WetterOnline                                         */
  /* ------------------------------------------------------------------ */
  function openWeatherApp() {
    if (IS_TV) {
      const el = focusId && itemIdOf(focusId) === WETTER_APP ? els.get(focusId) : null;
      launchId('com.webos.app.browser', 'WetterOnline', el, { target: 'https://www.wetteronline.de/wetter/musterstadt' });
    } else {
      window.open('https://www.wetteronline.de/wetter/musterstadt', '_blank');
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
    if (!w || !w.current) return 'Aktuelle Wetterdaten sind gerade nicht verfügbar.';
    const desc = (WMO_MAP[w.current.weather_code] || ['', 'unbekannt'])[1];
    let s = 'Wetter jetzt: ' + Math.round(w.current.temperature_2m) + ' °C (gefühlt ' +
      Math.round(w.current.apparent_temperature) + ' °C), ' + desc + '.';
    const d = w.daily;
    if (d && d.temperature_2m_max) {
      const day = (i, n) => ' ' + n + ': ' + Math.round(d.temperature_2m_min[i]) + ' bis ' + Math.round(d.temperature_2m_max[i]) +
        ' °C, ' + ((WMO_MAP[d.weather_code[i]] || ['', ''])[1]) + ', Regenrisiko ' + (d.precipitation_probability_max[i] || 0) + ' %.';
      s += day(0, 'Heute');
      if (d.temperature_2m_max.length > 1) s += day(1, 'Morgen');
    }
    return s;
  }

  const timeOfDay = () => new Date().toLocaleString('de-DE', { weekday: 'long', hour: '2-digit', minute: '2-digit' });

  // Topic rows. prompt() runs on click, so it uses the current apps / weather / time.
  const GM_TOPICS = [
    { label: 'Film-Tipps', items: [
      { ico: '🌙', text: 'Was schaue ich heute?', prompt: () => 'Es ist ' + timeOfDay() + '. Empfiehl mir 3 Filme oder Serien für heute, die es auf meinen Diensten gibt: ' + streamingApps() + '. Nenne bei jedem den Dienst.' },
      { ico: '🆕', text: 'Neue Serien', prompt: () => 'Welche neuen oder gerade beliebten Serien lohnen sich auf ' + streamingApps() + '? 3 Tipps mit Dienst und je einem Satz Begründung.' },
      { ico: '🍿', text: 'Spannender Film', prompt: () => 'Empfiehl mir 3 spannende Filme (Thriller oder Action) auf ' + streamingApps() + '. Nenne jeweils den Dienst.' },
      { ico: '😄', text: 'Was Leichtes', prompt: () => 'Ich will abschalten: 3 lustige oder entspannte Filme oder Serien auf ' + streamingApps() + ', mit Dienst.' },
      { ico: '🌍', text: 'Dokus', prompt: () => 'Empfiehl mir 3 sehenswerte Dokumentationen auf ' + streamingApps() + ', mit Dienst.' }
    ] },
    { label: 'Wetter & Tag', items: [
      { ico: '🧥', text: 'Was ziehe ich an?', prompt: () => weatherContext() + ' Was soll ich heute anziehen? Kurz und konkret.' },
      { ico: '🚶', text: 'Lohnt sich rausgehen?', prompt: () => weatherContext() + ' Es ist ' + timeOfDay() + '. Lohnt sich jetzt ein Spaziergang oder eher später? Kurz.' },
      { ico: '📅', text: 'Heute & morgen', prompt: () => weatherContext() + ' Fasse das Wetter für heute und morgen kurz und anschaulich zusammen.' },
      { ico: '✨', text: 'Idee für den Abend', prompt: () => weatherContext() + ' Es ist ' + timeOfDay() + '. Schlag mir 3 Ideen für den Abend vor, passend zum Wetter.' }
    ] },
    { label: 'Wissen & Rezepte', items: [
      { ico: '🎲', text: 'Fakt des Tages', prompt: () => 'Erzähle mir einen verblüffenden, wenig bekannten Fakt. Kurz und spannend erklärt.' },
      { ico: '🍳', text: 'Schnelles Rezept', prompt: () => 'Schlage ein raffiniertes Abendessen vor, das in unter 20 Minuten fertig ist. Zutaten und Schritte kurz.' },
      { ico: '💡', text: 'Erklär mir was', prompt: () => 'Erkläre mir ein spannendes Thema aus Wissenschaft oder Technik in 5 Sätzen, so dass es jeder versteht.' },
      { ico: '🧠', text: 'Quizfrage', prompt: () => 'Stell mir eine knifflige Quizfrage mit 3 Antwortmöglichkeiten (A, B, C). Verrate die Lösung noch nicht.' }
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
    b.innerHTML = (ico ? '<span class="gm-ico">' + ico + '</span>' : '') + '<span>' + esc(text) + '</span>';
    b.addEventListener('click', onClick);
    return b;
  }

  function row(label, chips) {
    const r = document.createElement('div');
    r.className = 'gm-row';
    const l = document.createElement('div');
    l.className = 'gm-row-label';
    l.textContent = label || '';
    r.appendChild(l);
    chips.forEach(c => r.appendChild(c));
    return r;
  }

  function showTopicChips() {
    geminiChipsEl.innerHTML = '';
    for (const t of GM_TOPICS) {
      geminiChipsEl.appendChild(row(t.label, t.items.map(it => chip(it.ico, it.text, '', () => askGemini(it.prompt(), it.text)))));
    }
  }

  function showFollowUps(suggestions) {
    geminiChipsEl.innerHTML = '';
    const sug = (suggestions || []).map(s => chip('↳', s, 'gm-suggest', () => askGemini(s, s)));
    if (sug.length) geminiChipsEl.appendChild(row('Nachfragen', sug));
    geminiChipsEl.appendChild(row('', [
      chip('🔄', 'Andere Vorschläge', 'gm-ghost', () => askGemini('Bitte gib mir andere, neue Vorschläge dazu.', 'Andere Vorschläge')),
      chip('✦', 'Neues Thema', 'gm-ghost', resetGeminiChat)
    ]));
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
    geminiChatEl.innerHTML = '<div class="gm-hello"><h1>Hallo! Worauf hast du Lust?</h1>' +
      '<p>Wähle ein Thema – danach schlage ich dir passende Nachfragen vor.</p></div>';
    showTopicChips();
    focusFirstChip();
  }

  function openGeminiModal() {
    if (geminiModalOpen) return;
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
  }

  function toggleGeminiModal() {
    if (geminiModalOpen) closeGeminiModal(); else openGeminiModal();
  }

  async function askGemini(promptText, label) {
    if (!promptText || gmBusy) return;
    gmBusy = true;
    const req = ++gmReq;
    if (!gmHistory.length) geminiChatEl.innerHTML = '';
    const q = document.createElement('div');
    q.className = 'gm-q';
    q.textContent = label || promptText;
    geminiChatEl.appendChild(q);
    const thinking = document.createElement('div');
    thinking.className = 'gm-thinking';
    thinking.innerHTML = '<span></span><span></span><span></span>';
    geminiChatEl.appendChild(thinking);
    q.scrollIntoView({ block: 'start' });
    gmFocusables().forEach(b => { b.disabled = true; });

    let data = null, failed = false;
    try {
      const res = await fetch('http://127.0.0.1:8080/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: promptText, history: gmHistory })
      });
      data = await res.json();
    } catch (e) {
      failed = true;
    }
    if (req !== gmReq) return;                 // popup closed or reset meanwhile
    gmBusy = false;
    thinking.remove();

    const a = document.createElement('div');
    a.className = 'gm-a' + (failed ? ' gm-err' : '');
    if (failed) {
      a.textContent = 'Der Gemini-Dienst auf dem TV ist gerade nicht erreichbar.';
    } else {
      a.innerHTML = renderAnswer(data.text || 'Keine Antwort erhalten.');
      if (Array.isArray(data.actions) && data.actions.length) {
        const acts = document.createElement('div');
        acts.className = 'gm-acts';
        data.actions.forEach(x => {
          const t = document.createElement('span');
          t.className = 'gm-act';
          t.textContent = '⚡ ' + x.tool;
          acts.appendChild(t);
        });
        a.appendChild(acts);
      }
      gmHistory.push({ role: 'user', text: promptText }, { role: 'model', text: data.text || '' });
      if (Array.isArray(data.actions) && data.actions.some(x => x.tool && (x.tool.includes('smart') || x.tool.includes('screen')))) {
        setTimeout(fetchSmartDevices, 1200);
      }
    }
    geminiChatEl.appendChild(a);
    q.scrollIntoView({ block: 'start' });
    showFollowUps(failed ? [] : data.suggestions);
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
    if (mode === 'menu' || mode === 'picker') closeMenu();
    if (mode === 'move') endMove();
    if (p.gemini === 'toggle') toggleGeminiModal(); else openGeminiModal();
    return true;
  }

  async function start() {
    activateApp();
    tick();
    $('g-icon').innerHTML = GEMINI_ICON;
    for (const [id, el] of PILLS) { el.dataset.id = id; el.dataset.key = id; els.set(id, el); bindPointer(el); }
    initGeminiUI();
    initSoundOutput();
    fetchWeather();
    fetchSmartDevices();

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
    setInterval(() => { if (!document.hidden) fetchSmartDevices(); }, 20000);
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
    get menu() { return menu ? menu.entries.map(e => e.label) : null; }
  };

  start();
})();
