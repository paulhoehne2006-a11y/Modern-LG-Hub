/* Custom home screen for LG webOS — com.custom.launcher
 *
 * Data sources
 *   data/apps.json + data/icons/   written by launcherd.py (root helper; this app is sandboxed)
 *   luna eim/getAllInputStatus     live HDMI names + connection state (allowed for this app)
 *   localStorage "launcher.v2"     favourites, hidden apps, order
 */
(() => {
  'use strict';

  const APP_ID = 'com.custom.launcher';
  const LG_HOME = 'com.webos.app.home';
  const LIVE_TV = 'com.webos.app.livetv';
  const SETTINGS = 'com.palm.app.settings';
  const MORE = 'util:more';
  const SAM = 'luna://com.webos.applicationManager/';
  const HB_EXEC = 'luna://org.webosbrew.hbchannel.service/exec';
  const IS_TV = typeof window.PalmServiceBridge !== 'undefined';
  const APP_DIR = decodeURIComponent(location.pathname).replace(/\/[^/]*$/, '');
  const STORE_KEY = 'launcher.v3';
  const HALF_LIFE = 45 * 86400;     // usage counts fade by half every 45 days
  const COLS = { smarthome: 5, dock: 5, grid: 6 };
  const HOLD_MS = 600;             // long-press OK
  const IDLE_MS = 60000;           // dim after 1 min without input
  const SCREENSAVER_MS = 240000;   // pure black screensaver after 4 min without input
  const SHIFT_MS = 90000;          // pixel shift interval (90s for G4 MLA OLED care)

  const DEFAULTS = {
    favorites: [],                    // pinned to the top row; the rest of the order follows usage
    // LG promotional / partner apps and unused inputs. Nothing is uninstalled; "Mehr" brings them back.
    hidden: ['com.webos.app.hdmi3', 'com.webos.app.hdmi4', LIVE_TV,
      'com.webos.app.lgchannels', 'com.webos.app.lifeonscreen', 'com.webos.app.sportsteamsettings',
      'com.webos.app.homeconnect', 'com.webos.app.camera', 'com.webos.app.multiviewsettings',
      'amazon.alexa.view', 'com.lgtravel.app', 'com.twin.app.gamingportal', 'public.value'],
    order: [],
    smartDevices: []                  // custom ordered device IDs for the smart home dashboard row
  };
  const INPUT_ALIAS = { 'com.webos.app.hdmi1': 'PC' };   // the owner's PC is on HDMI 1
  const FALLBACK_DEVICES = [1, 2, 3, 4].map(n => ({
    id: 'HDMI_' + n, label: 'HDMI ' + n, appId: 'com.webos.app.hdmi' + n, connected: false, unknown: true
  }));

  const GLYPHS = {
    hdmi: '<path d="M2.5 8.5h19v4.2l-2.6 3.3H5.1l-2.6-3.3z"/><path d="M7 11.5h10"/>',
    pc: '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8.5 20.5h7M12 16.5v4"/>',
    tv: '<rect x="2.5" y="7" width="19" height="12.5" rx="2"/><path d="M8 2.5l4 4.5 4-4.5"/>',
    weather: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z"/><path d="M12 3v2M4.93 6.93l1.41 1.41M19.07 6.93l-1.41 1.41"/>',
    more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>'
  };

  const $ = id => document.getElementById(id);
  const pageEl = $('page'), smarthomeEl = $('smarthome'), dockEl = $('dock'), gridEl = $('grid');
  const weatherEl = $('weather'), geminiBadgeEl = $('gemini-badge'), audioBadgeEl = $('audio-badge');

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

  /* ------------------------------------------------------------------ */
  /* Preferences                                                         */
  /* ------------------------------------------------------------------ */
  const clone = o => JSON.parse(JSON.stringify(o));

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(STORE_KEY));
      if (p && Array.isArray(p.favorites) && Array.isArray(p.hidden) && Array.isArray(p.order)) {
        if (!Array.isArray(p.smartDevices)) p.smartDevices = [];
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
  const state = { apps: [], generatedAt: 0, devices: null, usage: {}, usageStamp: 0, smartDevices: [] };
  let items = new Map();      // id -> item
  let smarthomeIds = [], dockIds = [], gridIds = [];

  function getSmartDeviceList() {
    return (state.smartDevices && state.smartDevices.length) ? state.smartDevices : [
      { id: '574d6169-7fe8-4c4e-abd6-9a9b7415d843', name: 'TV-Licht', type: 'light', state: 'off' },
      { id: 'a9f65c88-c19f-4b8a-bcb2-655ba244d192', name: 'Wohnzimmer', type: 'light', state: 'off' },
      { id: '24915d98-77d2-4376-b4d8-624f881c77fa', name: 'Deckenlicht', type: 'light', state: 'off' },
      { id: 'a9700d2f-2a05-4753-aef4-817a3341eeae', name: 'Schreibtisch', type: 'plug', state: 'off' }
    ];
  }

  function getActiveSmartDeviceIds() {
    const all = getSmartDeviceList();
    if (prefs.smartDevices && Array.isArray(prefs.smartDevices) && prefs.smartDevices.length) {
      const valid = prefs.smartDevices.filter(id => all.some(d => d.id === id));
      if (valid.length) return valid.slice(0, 4);
    }
    return all.slice(0, 4).map(d => d.id);
  }

  function buildItems() {
    const map = new Map();

    // 1. Smart Home Devices
    const smartList = getSmartDeviceList();
    const activeIds = getActiveSmartDeviceIds();

    smarthomeIds = [];
    for (const devId of activeIds) {
      const s = smartList.find(d => d.id === devId);
      if (!s) continue;
      const sId = 'sh:' + s.id;
      const isOn = s.state === 'on';
      const icon = s.type === 'plug' ? '🔌' : '💡';
      map.set(sId, {
        id: sId,
        deviceId: s.id,
        kind: 'sh',
        title: s.name,
        sub: isOn ? 'AN' : 'AUS',
        iconEmoji: icon,
        state: s.state,
        isAudioMode: false
      });
      smarthomeIds.push(sId);
    }
    const audioCardId = 'sh:audio-mode';
    map.set(audioCardId, {
      id: audioCardId,
      kind: 'sh',
      title: 'Display Aus',
      sub: 'Denon Musik-Modus',
      iconEmoji: '🌙',
      state: 'off',
      isAudioMode: true
    });
    smarthomeIds.push(audioCardId);

    // 2. HDMI & TV Inputs
    for (const d of (state.devices || FALLBACK_DEVICES)) {
      if (!d.appId || !/^com\.webos\.app\.hdmi\d/.test(d.appId)) continue;
      const port = String(d.id || '').replace('_', ' ') || d.label;
      const isDefaultLabel = !d.label || /^HDMI[ _-]?\d+$/i.test(d.label);
      let title = d.label || port, sub = port;
      if (isDefaultLabel && INPUT_ALIAS[d.appId]) title = INPUT_ALIAS[d.appId];
      else if (isDefaultLabel) sub = d.unknown ? 'Eingang' : (d.connected ? 'Verbunden' : 'Nicht verbunden');
      map.set(d.appId, { id: d.appId, kind: 'input', title, sub, connected: !!d.connected,
        glyph: title === 'PC' ? 'pc' : 'hdmi' });
    }
    map.set(LIVE_TV, { id: LIVE_TV, kind: 'input', title: 'Live TV', sub: 'TV-Empfang', connected: false, glyph: 'tv', noDot: true });

    // 3. Installed Apps
    for (const a of state.apps) {
      if (map.has(a.id) || a.id === APP_ID || a.id === SETTINGS) continue;
      if (a.visible === false || a.type === 'stub') continue;   // hidden system apps, store placeholders
      map.set(a.id, { id: a.id, kind: 'app', title: a.title || a.id,
        icon: a.icon ? a.icon + '?v=' + (a.iconV || 0) : null, iconColor: a.iconColor || null });
    }
    const WETTER_APP = 'app:wetteronline';
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

  // top row: pinned items first, then the most used ones up to a full row; below: everything else by usage
  function buildLists() {
    const hidden = new Set(prefs.hidden);
    const pinned = prefs.favorites.filter((id, i, arr) =>
      items.has(id) && id !== MORE && !hidden.has(id) && arr.indexOf(id) === i);
    const pinSet = new Set(pinned);
    const rest = [...items.values()].filter(it => it.id !== MORE && it.kind !== 'sh' && !pinSet.has(it.id) && !hidden.has(it.id));
    rest.sort((a, b) => {
      const d = score(b.id) - score(a.id);
      if (Math.abs(d) > 1e-6) return d;
      if (a.kind !== b.kind) {
        if (a.kind === 'input') return -1;
        if (b.kind === 'input') return 1;
      }
      return a.title.localeCompare(b.title, 'de', { sensitivity: 'base' });
    });
    const fill = Math.max(0, COLS.dock - pinned.length);
    dockIds = pinned.concat(rest.slice(0, fill).map(it => it.id));
    gridIds = rest.slice(fill).map(it => it.id).concat(MORE);
  }

  const hiddenItems = () => prefs.hidden.filter(id => items.has(id) && !prefs.favorites.includes(id)).map(id => items.get(id));

  /* ------------------------------------------------------------------ */
  /* Icon analysis: decide how an icon sits on its card                   */
  /*   bleed  uniform opaque edge -> card takes the icon's own colour      */
  /*   glyph  transparent icon    -> icon on a brand-coloured card         */
  /*   badge  busy opaque edge    -> icon as a rounded tile on a dark card */
  /* ------------------------------------------------------------------ */
  const analysisCache = new Map();
  const lum = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
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

  function analyze(img, iconColor) {
    const brand = parseColor(iconColor);
    const fallback = { mode: 'badge', color: brand ? brand.map(v => v * 0.5) : [31, 32, 35] };
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
      if (opaqueEdge.length < edge.length * 0.5) {
        // transparent icon: pick a card colour that contrasts with the glyph
        let n = 0, sum = 0;
        for (let i = 0; i < px.length; i += 4) if (px[i + 3] > 128) { n++; sum += lum([px[i], px[i + 1], px[i + 2]]); }
        const glyphLight = n ? sum / n > 0.55 : true;
        let color = brand || [38, 40, 44];
        if (glyphLight && lum(color) > 0.7) color = [38, 40, 44];
        if (!glyphLight && lum(color) < 0.3) color = [233, 233, 238];
        return { mode: 'glyph', color, art: artScale(contentBox(px, N, (r, g, b, a) => a > 64)) };
      }
      const mean = [0, 1, 2].map(k => opaqueEdge.reduce((s, p) => s + p[k], 0) / opaqueEdge.length);
      const dist = opaqueEdge.map(p => Math.max(Math.abs(p[0] - mean[0]), Math.abs(p[1] - mean[1]), Math.abs(p[2] - mean[2])))
        .sort((a, b) => a - b);
      if (dist[Math.floor(dist.length * 0.9)] < 26) {
        const box = contentBox(px, N, (r, g, b, a) => a > 128 &&
          Math.max(Math.abs(r - mean[0]), Math.abs(g - mean[1]), Math.abs(b - mean[2])) > 40);
        return { mode: 'bleed', color: mean, art: artScale(box) };
      }
      let n = 0; const avg = [0, 0, 0];
      for (let i = 0; i < px.length; i += 4) if (px[i + 3] > 128) { n++; avg[0] += px[i]; avg[1] += px[i + 1]; avg[2] += px[i + 2]; }
      return { mode: 'badge', color: avg.map(v => (v / Math.max(n, 1)) * 0.42) };
    } catch (e) {
      return fallback;      // canvas not readable: keep a safe look
    }
  }

  function applyLook(el, look) {
    el.classList.remove('bleed', 'glyph', 'badge');
    el.classList.add(look.mode);
    const l = lum(look.color);
    el.style.setProperty('--c', rgb(look.color));
    if (look.art) el.style.setProperty('--art', look.art + '%'); else el.style.removeProperty('--art');
    el.style.setProperty('--glow', l < 0.1 ? 'rgba(255,255,255,.5)' : rgb(look.color));
    el.dataset.amb = l < 0.12 ? '#6f7180' : (l > 0.82 ? '#9c9eab' : rgb(look.color));
  }

  /* ------------------------------------------------------------------ */
  /* Cards                                                               */
  /* ------------------------------------------------------------------ */
  const els = new Map();   // id -> card element

  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function faceFor(it) {
    const face = h('div', 'face');
    face.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + (GLYPHS[it.glyph] || GLYPHS.more) + '</svg>';
    if (it.glyph === 'more') face.firstChild.style.fill = 'rgba(255,255,255,.9)';
    const text = h('div', 'face-text');
    text.appendChild(h('div', 'face-title'));
    text.appendChild(h('div', 'face-sub'));
    face.appendChild(text);
    return face;
  }

  function createCard(it) {
    const el = h('div', 'card ' + it.kind + (it.isAudioMode ? ' audio-mode' : ''));
    el.dataset.id = it.id;
    el.setAttribute('role', 'button');
    el.appendChild(h('div', 'halo'));
    const plate = h('div', 'plate');
    el.appendChild(plate);
    if (it.kind === 'app') {
      const img = document.createElement('img');
      img.className = 'art';
      img.alt = '';
      img.draggable = false;
      plate.appendChild(img);
    } else if (it.kind === 'sh') {
      const face = h('div', 'sh-face');
      face.innerHTML = '<div class="sh-icon">' + (it.iconEmoji || '💡') + '</div>' +
        '<div class="sh-meta">' +
          '<div class="sh-name">' + it.title + '</div>' +
          '<div class="sh-status"><span class="sh-dot"></span><span class="sh-status-text">' + it.sub + '</span></div>' +
        '</div>';
      plate.appendChild(face);
      el.dataset.amb = it.isAudioMode ? '#bf5af2' : (it.state === 'on' ? '#5ac8fa' : '#6f7180');
      el.style.setProperty('--glow', it.isAudioMode ? 'rgba(191,90,242,.6)' : (it.state === 'on' ? 'rgba(90,200,250,.6)' : 'rgba(255,255,255,.35)'));
    } else {
      plate.appendChild(faceFor(it));
      if (it.id === 'app:wetteronline') {
        el.dataset.amb = '#0a84ff';
        el.style.setProperty('--glow', 'rgba(10,132,255,.55)');
      } else {
        el.dataset.amb = '#6f7180';
        el.style.setProperty('--glow', 'rgba(255,255,255,.45)');
      }
    }
    plate.appendChild(h('div', 'sheen'));
    el.appendChild(h('div', 'name'));

    el.addEventListener('mouseenter', () => { if (mode === 'home') setFocus(it.id, { scroll: false }); });
    el.addEventListener('mousemove', e => tilt(el, e));
    el.addEventListener('mouseleave', () => untilt(el));
    el.addEventListener('mousedown', () => {
      clearTimeout(pointerHold);
      pointerLong = false;
      pointerHold = setTimeout(() => { pointerLong = true; if (mode === 'home') openCardMenu(it.id); }, HOLD_MS);
    });
    el.addEventListener('mouseup', () => clearTimeout(pointerHold));
    el.addEventListener('click', () => {
      if (pointerLong) { pointerLong = false; return; }
      if (mode === 'home') activate(it.id);
    });
    return el;
  }

  function updateCard(el, it) {
    el.setAttribute('aria-label', it.title);
    const nameEl = el.querySelector('.name');
    if (nameEl) nameEl.textContent = it.title;
    if (it.kind === 'app') {
      const img = el.querySelector('.art');
      if (el.dataset.icon !== String(it.icon)) {
        el.dataset.icon = String(it.icon);
        const brand = parseColor(it.iconColor);
        applyLook(el, { mode: 'badge', color: brand ? brand.map(v => v * 0.5) : [31, 32, 35] });
        if (it.icon) {
          img.onload = () => {
            const stale = el.querySelector('.plate > .face');
            if (stale) stale.remove();
            let look = analysisCache.get(it.icon);
            if (!look) { look = analyze(img, it.iconColor); analysisCache.set(it.icon, look); }
            applyLook(el, look);
            if (focusId === it.id) setAmbient(el);
          };
          img.onerror = () => { img.style.display = 'none'; letterFace(el, it); };
          img.style.display = '';
          img.src = it.icon;
        } else {
          img.style.display = 'none';
          letterFace(el, it);
        }
      }
    } else if (it.kind === 'sh') {
      el.setAttribute('aria-label', it.title + ' ' + it.sub);
      if (it.state === 'on') el.classList.add('on');
      else el.classList.remove('on');
      const shName = el.querySelector('.sh-name');
      if (shName) shName.textContent = it.title;
      const shSub = el.querySelector('.sh-status-text');
      if (shSub) shSub.textContent = it.sub;
      el.dataset.amb = it.isAudioMode ? '#bf5af2' : (it.state === 'on' ? '#5ac8fa' : '#6f7180');
      el.style.setProperty('--glow', it.isAudioMode ? 'rgba(191,90,242,.6)' : (it.state === 'on' ? 'rgba(90,200,250,.6)' : 'rgba(255,255,255,.35)'));
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
    plate.insertBefore(face, plate.querySelector('.sheen'));
  }

  function place(container, ids) {
    ids.forEach((id, i) => {
      const it = items.get(id);
      let el = els.get(id);
      if (el && !el.classList.contains(it.kind)) { el.remove(); els.delete(id); el = null; }
      if (!el) { el = createCard(it); els.set(id, el); }
      updateCard(el, it);
      if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
    });
    while (container.children.length > ids.length) container.lastElementChild.remove();
  }

  // re-ranking while a menu is open or a card is being moved would shuffle things under the user's hand
  let renderPending = false;
  function requestRender() { if (mode !== 'home') renderPending = true; else render(); }
  function flushRender() { if (renderPending && mode === 'home') { renderPending = false; render(); } }

  function render() {
    items = buildItems();
    buildLists();
    place(smarthomeEl, smarthomeIds);
    place(dockEl, dockIds);
    place(gridEl, gridIds);
    for (const id of [...els.keys()]) if (!id.startsWith('hero:') && !smarthomeIds.includes(id) && !dockIds.includes(id) && !gridIds.includes(id)) els.delete(id);
    if (!focusId || !els.has(focusId)) focusId = null;
    setFocus(focusId || dockIds[0] || smarthomeIds[0] || gridIds[0], { instant: !focusId });
  }

  /* ------------------------------------------------------------------ */
  /* Focus, ambient light, scrolling                                     */
  /* ------------------------------------------------------------------ */
  let focusId = null;
  let mode = 'home';             // 'home' | 'menu' | 'move'
  const ambLayers = [...document.querySelectorAll('.amb')];
  let ambIndex = 0, ambColor = '';

  function setAmbient(el) {
    // Ambient radial gradient disabled for pure OLED black and 60 FPS compositor speed
  }

  function setFocus(id, opts) {
    const el = els.get(id);
    if (!el) return;
    const prev = focusId && els.get(focusId);
    if (prev && prev !== el) { prev.classList.remove('focused'); untilt(prev); }
    focusId = id;
    el.classList.add('focused');
    setAmbient(el);
    if (!opts || opts.scroll !== false) scrollToFocus(opts && opts.instant);
  }

  function scrollToFocus(instant) {
    const el = els.get(focusId);
    if (!el) return;
    let y = 0;
    if (el.closest('#grid')) {
      const first = gridEl.firstElementChild;
      y = Math.max(0, el.offsetTop - (first ? first.offsetTop : 0) + 120);
    }
    if (instant) pageEl.style.transition = 'none';
    pageEl.style.transform = y ? 'translate3d(0,' + (-y) + 'px,0)' : '';
    document.body.classList.toggle('scrolled', y > 0);
    if (instant) { void pageEl.offsetHeight; pageEl.style.transition = ''; }
  }

  function rows() {
    const out = [];
    out.push([weatherEl, geminiBadgeEl, audioBadgeEl]);
    const chunk = (container, n) => {
      if (!container) return;
      const kids = [...container.children];
      for (let i = 0; i < kids.length; i += n) out.push(kids.slice(i, i + n));
    };
    chunk(smarthomeEl, COLS.smarthome);
    chunk(dockEl, COLS.dock);
    chunk(gridEl, COLS.grid);
    return out;
  }

  function bump(el, dir) {
    const cls = 'bump-' + dir[0];
    el.classList.remove('bump-l', 'bump-r', 'bump-u', 'bump-d');
    void el.offsetWidth;
    el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), 260);
  }

  function neighbour(dir) {
    const cur = els.get(focusId);
    if (!cur) return null;
    const rs = rows();
    let r = -1, c = -1;
    rs.forEach((row, i) => { const j = row.indexOf(cur); if (j >= 0) { r = i; c = j; } });
    if (r < 0) return null;
    if (dir === 'left') return rs[r][c - 1] || null;
    if (dir === 'right') return rs[r][c + 1] || null;
    const next = rs[dir === 'up' ? r - 1 : r + 1];
    if (!next || !next.length) return null;
    // Pure mathematical index mapping: zero DOM layout reflows
    const curLen = Math.max(1, rs[r].length - 1);
    const nextLen = next.length - 1;
    const targetCol = Math.min(next.length - 1, Math.max(0, Math.round((c / curLen) * nextLen)));
    return next[targetCol] || null;
  }

  function move(dir) {
    const target = neighbour(dir);
    if (target) setFocus(target.dataset.id);
    else if (els.get(focusId)) bump(els.get(focusId), dir);
  }

  /* pointer tilt (Magic Remote cursor) - throttled with rAF */
  let tiltTicking = false;
  let activeTiltEl = null;
  let tiltBounds = null;

  function tilt(el, e) {
    if (mode !== 'home' || !el.classList.contains('focused')) return;
    if (tiltTicking) return;
    tiltTicking = true;
    requestAnimationFrame(() => {
      tiltTicking = false;
      if (!el.isConnected || !el.classList.contains('focused')) return;
      const r = (activeTiltEl === el && tiltBounds) ? tiltBounds : (activeTiltEl = el, tiltBounds = el.getBoundingClientRect());
      const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
      el.classList.add('tilting');
      el.style.setProperty('--ry', (x * 7).toFixed(1) + 'deg');
      el.style.setProperty('--rx', (-y * 5).toFixed(1) + 'deg');
    });
  }
  function untilt(el) {
    activeTiltEl = null;
    tiltBounds = null;
    el.classList.remove('tilting');
    for (const p of ['--rx', '--ry', '--sx', '--sy']) el.style.removeProperty(p);
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

  function launchId(id, title, el) {
    if (el) el.classList.add('launching');
    document.body.classList.add('leaving');
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(restoreView, 7000);      // nothing took over: come back
    if (!IS_TV) { setTimeout(() => { restoreView(); toast('Starte ' + title); }, 700); return; }
    luna(SAM + 'launch', { id }).catch(err => {
      restoreView();
      toast('„' + title + '“ lässt sich nicht starten' + (err && err.message ? ' (' + err.message + ')' : ''));
    });
  }

  function activate(id) {
    if (id === 'hero:weather' || id === 'app:wetteronline') {
      openWeatherApp();
      return;
    }
    if (id === 'hero:gemini') {
      openGeminiModal();
      return;
    }
    if (id === 'hero:audio') {
      openAudioSettings();
      return;
    }
    const it = items.get(id);
    if (!it) return;
    if (it.kind === 'util') {
      if (it.id === 'app:wetteronline') openWeatherApp();
      else openMoreMenu();
      return;
    }
    if (it.kind === 'sh') {
      if (it.isAudioMode) {
        toast('Denon AVR Audio-Modus: Schalte OLED-Panel aus...');
        setTimeout(() => {
          if (IS_TV) {
            luna('luna://com.webos.service.tvpower/power/turnOffScreen', {}).catch(err => {
              toast('Fehler beim Ausschalten des Bildschirms');
            });
          }
        }, 700);
      } else {
        toggleSmartDevice(it.deviceId, it);
      }
    } else {
      launchId(it.id, it.title, els.get(id));
    }
  }

  /* ------------------------------------------------------------------ */
  /* Menu                                                                */
  /* ------------------------------------------------------------------ */
  const menuEl = $('menu'), menuList = $('menu-list');
  const MENU_ROWS = 8, MENU_ROW_H = 62;
  let menu = null;              // { entries, index, back }
  let menuArmed = true;         // OK only counts after the key that opened the menu was released

  function openMenu(def) {
    menu = { entries: def.entries, index: Math.min(def.index || 0, def.entries.length - 1), back: def.back || null };
    $('menu-title').textContent = def.title;
    const icon = $('menu-icon');
    icon.style.backgroundImage = def.icon ? 'url("' + def.icon + '")' : '';
    icon.style.setProperty('--c', def.color || '#2c2d31');
    icon.style.display = def.icon ? '' : 'none';
    $('menu-hint').textContent = def.hint || '';
    menuList.textContent = '';
    def.entries.forEach((entry, i) => {
      const row = h('div', 'mi' + (entry.danger ? ' danger' : ''));
      row.setAttribute('role', 'menuitem');
      row.appendChild(h('span', '', entry.label));
      if (entry.hint) row.appendChild(h('span', 'mi-hint', entry.hint));
      row.addEventListener('mouseenter', () => menuFocus(i));
      row.addEventListener('click', () => { menuFocus(i); menuSelect(); });
      menuList.appendChild(row);
    });
    menuEl.hidden = false;
    mode = 'menu';
    menuArmed = !okDown;
    menuFocus(menu.index);
    requestAnimationFrame(() => { if (menu) document.body.classList.add('menu-open'); });
  }

  function menuFocus(i) {
    if (!menu) return;
    menu.index = Math.max(0, Math.min(menu.entries.length - 1, i));
    [...menuList.children].forEach((row, j) => row.classList.toggle('focused', j === menu.index));
    const top = Math.max(0, Math.min(menu.index - Math.floor(MENU_ROWS / 2), menu.entries.length - MENU_ROWS));
    menuList.style.transform = 'translateY(' + (-top * MENU_ROW_H) + 'px)';
  }

  function menuSelect() {
    const entry = menu && menu.entries[menu.index];
    if (entry && entry.action) entry.action();
  }

  function closeMenu() {
    menu = null;
    if (mode === 'menu') mode = 'home';
    flushRender();
    document.body.classList.remove('menu-open');
    setTimeout(() => { if (!menu) menuEl.hidden = true; }, 300);
  }

  function menuBack() {
    const back = menu && menu.back;
    if (back) back(); else closeMenu();
  }

  function iconInfo(it) {
    const el = els.get(it.id);
    return { icon: it.icon || null, color: el ? el.style.getPropertyValue('--c') : '' };
  }

  function openCardMenu(id) {
    if (id === 'hero:weather' || id === 'app:wetteronline') {
      openMenu({
        title: 'WetterOnline',
        hint: '14-Tage-Vorhersage & RegenRadar für Musterstadt',
        entries: [
          { label: 'WetterOnline öffnen', action: () => { closeMenu(); openWeatherApp(); } },
          { label: 'Wetter aktualisieren', action: () => { closeMenu(); fetchWeather(); } }
        ]
      });
      return;
    }
    if (id === 'hero:audio') {
      openMenu({
        title: 'Audioausgabe',
        hint: 'Ausgangs-Einstellungen des Fernsehers',
        entries: [
          { label: 'Audioausgabe-Einstellungen öffnen', action: () => { closeMenu(); openAudioSettings(); } }
        ]
      });
      return;
    }
    const it = items.get(id);
    if (!it) return;
    if (it.kind === 'util') { openMoreMenu(); return; }
    if (it.kind === 'sh') {
      if (it.isAudioMode) {
        openMenu({
          title: 'Display Aus (OLED Schutz)',
          hint: 'Schaltet das Panel aus, während Toslink Digital Audio weiterläuft.',
          entries: [
            { label: 'Display sofort ausschalten', action: () => { closeMenu(); activate(id); } }
          ]
        });
      } else {
        const devId = it.deviceId;
        openMenu({
          title: it.title,
          hint: 'SmartThings Gerät (' + it.sub + ')',
          entries: [
            { label: it.state === 'on' ? 'Ausschalten' : 'Einschalten', action: () => { closeMenu(); activate(id); } },
            { label: 'Gerät auf dieser Kachel austauschen...', hint: 'Wählen', action: () => openDeviceSwapMenu(devId) },
            { label: 'Smart Home Kacheln verwalten...', action: () => openSmartHomeManager() },
            { label: 'Status aktualisieren', action: () => { closeMenu(); fetchSmartThingsDevices(); } }
          ]
        });
      }
      return;
    }
    const pinned = prefs.favorites.includes(id);
    const entries = [{ label: 'Öffnen', action: () => { closeMenu(); activate(id); } }];
    if (pinned) entries.push({ label: 'Anheftung lösen', hint: 'sortiert nach Nutzung', action: () => { closeMenu(); setFavorite(id, false); } });
    else entries.push({ label: 'Oben anheften', action: () => { closeMenu(); setFavorite(id, true); } });
    if (pinned && prefs.favorites.filter(x => items.has(x)).length > 1) entries.push({ label: 'Verschieben', action: () => { closeMenu(); startMove(id); } });
    entries.push({ label: 'Ausblenden', danger: true, action: () => { closeMenu(); setHidden(id, true); } });
    const info = iconInfo(it);
    openMenu({ title: it.title, icon: info.icon, color: info.color || '#2c3037', entries });
  }

  function openMoreMenu(index) {
    const hidden = hiddenItems();
    const smartDevCount = (state.smartDevices && state.smartDevices.length) ? state.smartDevices.length : 0;
    openMenu({
      title: 'Mehr', index,
      entries: [
        { label: 'Smart Home Geräte anpassen', hint: (smartDevCount ? smartDevCount + ' Geräte' : ''), action: () => openSmartHomeManager() },
        { label: 'Ausgeblendete Apps', hint: String(hidden.length), action: () => openHiddenMenu(0) },
        { label: 'TV-Einstellungen', action: () => { closeMenu(); launchId(SETTINGS, 'Einstellungen'); } },
        { label: 'App-Liste aktualisieren', action: () => { closeMenu(); refreshData(true).then(() => toast('App-Liste aktualisiert')); } },
        { label: 'LG-Startbildschirm öffnen', action: () => { closeMenu(); openLgHome(); } },
        { label: 'Auf Standard zurücksetzen', danger: true, action: openResetMenu }
      ],
      hint: 'Tipp: OK gedrückt halten öffnet das Menü einer Kachel.'
    });
  }

  function openDeviceSwapMenu(currentSlotDevId) {
    const all = getSmartDeviceList();
    const activeIds = getActiveSmartDeviceIds();
    const entries = all.map(dev => {
      const isCurrent = dev.id === currentSlotDevId;
      const isShown = activeIds.includes(dev.id);
      const icon = dev.type === 'plug' ? '🔌' : '💡';
      const statusText = dev.state === 'on' ? 'AN' : 'AUS';
      const hint = isCurrent ? 'Aktuell hier' : (isShown ? 'Bereits aktiv' : statusText);
      return {
        label: icon + ' ' + dev.name,
        hint,
        action: () => {
          swapSmartDeviceSlot(currentSlotDevId, dev.id);
          closeMenu();
        }
      };
    });
    openMenu({
      title: 'Kachel austauschen',
      hint: 'Wähle das Gerät für diese Kachelposition',
      back: () => openCardMenu('sh:' + currentSlotDevId),
      entries
    });
  }

  function swapSmartDeviceSlot(oldId, newId) {
    let curList = getActiveSmartDeviceIds().slice();
    const idx = curList.indexOf(oldId);
    if (idx >= 0) {
      const existingIdx = curList.indexOf(newId);
      if (existingIdx >= 0) {
        curList[existingIdx] = oldId;
      }
      curList[idx] = newId;
    } else {
      curList.push(newId);
    }
    prefs.smartDevices = curList.slice(0, 4);
    savePrefs();
    render();
    const dev = getSmartDeviceList().find(d => d.id === newId);
    toast((dev ? dev.name : 'Gerät') + ' auf Kachel platziert');
  }

  function openSmartHomeManager() {
    const all = getSmartDeviceList();
    const activeIds = getActiveSmartDeviceIds();
    const entries = all.map(dev => {
      const idx = activeIds.indexOf(dev.id);
      const isAssigned = idx >= 0;
      const icon = dev.type === 'plug' ? '🔌' : '💡';
      return {
        label: (isAssigned ? '✓ ' : '  ') + icon + ' ' + dev.name,
        hint: isAssigned ? ('Kachel ' + (idx + 1)) : (dev.state === 'on' ? 'AN' : 'AUS'),
        action: () => {
          toggleSmartDeviceAssignment(dev.id);
        }
      };
    });
    entries.push({
      label: 'Auf Standard-Kacheln zurücksetzen',
      danger: true,
      action: () => {
        prefs.smartDevices = [];
        savePrefs();
        render();
        toast('Smart Home Kacheln zurückgesetzt');
        closeMenu();
      }
    });
    openMenu({
      title: 'Smart Home Kacheln (max. 4)',
      hint: 'OK drücken zum Hinzufügen / Abwählen der Kacheln',
      back: () => openMoreMenu(0),
      entries
    });
  }

  function toggleSmartDeviceAssignment(devId) {
    let curList = getActiveSmartDeviceIds().slice();
    const idx = curList.indexOf(devId);
    if (idx >= 0) {
      if (curList.length <= 1) {
        toast('Mindestens 1 Gerät muss angezeigt werden');
        return;
      }
      curList.splice(idx, 1);
    } else {
      if (curList.length >= 4) {
        curList[3] = devId;
      } else {
        curList.push(devId);
      }
    }
    prefs.smartDevices = curList.slice(0, 4);
    savePrefs();
    render();
    openSmartHomeManager();
  }

  function openHiddenMenu(index) {
    const hidden = hiddenItems().sort((a, b) => a.title.localeCompare(b.title, 'de', { sensitivity: 'base' }));
    const entries = hidden.length
      ? hidden.map(it => ({ label: it.title, hint: 'einblenden', action: () => { setHidden(it.id, false); openHiddenMenu(menu ? menu.index : 0); } }))
      : [{ label: 'Keine ausgeblendeten Apps', action: () => openMoreMenu(0) }];
    openMenu({ title: 'Ausgeblendete Apps', entries, index, back: () => openMoreMenu(0), hint: hidden.length ? 'OK blendet die App wieder ein.' : '' });
  }

  function openResetMenu() {
    openMenu({
      title: 'Zurücksetzen?', back: () => openMoreMenu(4), index: 1,
      entries: [
        { label: 'Ja, Favoriten und Anordnung zurücksetzen', danger: true, action: () => { prefs = clone(DEFAULTS); savePrefs(); closeMenu(); focusId = null; render(); toast('Zurückgesetzt'); } },
        { label: 'Abbrechen', action: () => openMoreMenu(4) }
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
    if (on) { prefs.favorites.push(id); prefs.hidden = prefs.hidden.filter(x => x !== id); }
    savePrefs();
    focusId = id;
    render();
    toast(on ? 'Oben angeheftet' : 'Anheftung gelöst – Reihenfolge folgt wieder der Nutzung');
  }

  function setHidden(id, on) {
    const it = items.get(id);
    prefs.hidden = prefs.hidden.filter(x => x !== id);
    if (on) {
      prefs.hidden.push(id);
      prefs.favorites = prefs.favorites.filter(x => x !== id);
      // keep the focus near where the card was
      const list = dockIds.includes(id) ? dockIds : gridIds;
      const i = list.indexOf(id);
      focusId = list[i + 1] || list[i - 1] || null;
    }
    savePrefs();
    render();
    if (it) toast(on ? '„' + it.title + '“ ausgeblendet – unter „Mehr“ wieder einblendbar' : '„' + it.title + '“ eingeblendet');
  }

  function startMove(id) {
    const el = els.get(id);
    if (!el) return;
    mode = 'move';
    setFocus(id);
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
    const id = focusId;
    const list = prefs.favorites.filter((x, i, arr) => items.has(x) && x !== MORE && arr.indexOf(x) === i);
    const from = list.indexOf(id);
    const to = from + ({ left: -1, right: 1, up: 0, down: 0 })[dir];
    if (from < 0 || to < 0 || to >= list.length || to === from) { bump(els.get(id), dir); return; }
    list.splice(to, 0, list.splice(from, 1)[0]);
    prefs.favorites = list;
    savePrefs();
    render();
    els.get(id).classList.add('moving');
  }

  /* ------------------------------------------------------------------ */
  /* Keys and pointer                                                    */
  /* ------------------------------------------------------------------ */
  const KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, OK: 13, BACK: 461, ESC: 27, HOME: 36 };
  const DIRS = { 37: 'left', 38: 'up', 39: 'right', 40: 'down' };
  let okDown = false, okLong = false, okTimer = 0;
  let okBlocked = false;         // OK is still held from an action that just finished: ignore until released
  let pointerHold = 0, pointerLong = false;

  document.addEventListener('keydown', e => {
    const k = e.keyCode;
    if (geminiModalOpen) {
      if (k === KEY.BACK || k === KEY.ESC) {
        e.preventDefault();
        closeGeminiModal();
        return;
      }
      const focusables = [...geminiModalEl.querySelectorAll('button')].filter(b => b.offsetParent !== null);
      const activeIdx = focusables.indexOf(document.activeElement);
      if (k === KEY.DOWN) {
        e.preventDefault();
        const next = focusables[activeIdx < 0 ? 0 : (activeIdx + 1) % focusables.length];
        if (next) next.focus();
        return;
      }
      if (k === KEY.UP) {
        e.preventDefault();
        const prev = focusables[activeIdx < 0 ? 0 : (activeIdx - 1 + focusables.length) % focusables.length];
        if (prev) prev.focus();
        return;
      }
      if (k === KEY.OK) {
        if (document.activeElement && typeof document.activeElement.click === 'function') {
          e.preventDefault();
          document.activeElement.click();
        }
        return;
      }
      return;
    }
    if (!(k in DIRS) && k !== KEY.OK && k !== KEY.BACK && k !== KEY.ESC && k !== KEY.HOME) return;
    e.preventDefault();
    wake();
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
    if (k in DIRS) { move(DIRS[k]); return; }
    if (k === KEY.OK) {
      if (okDown || okBlocked || e.repeat) return;
      okDown = true; okLong = false;
      clearTimeout(okTimer);
      okTimer = setTimeout(() => { if (okDown && mode === 'home') { okLong = true; openCardMenu(focusId); } }, HOLD_MS);
      return;
    }
    // Back / Home: jump to the top; at the top there is nowhere further back (this is the home screen)
    const first = dockIds[0] || smarthomeIds[0] || gridIds[0];
    if (focusId !== first) setFocus(first);
  });

  document.addEventListener('keyup', e => {
    if (e.keyCode !== KEY.OK) return;
    e.preventDefault();
    menuArmed = true;
    okBlocked = false;
    if (!okDown) return;
    okDown = false;
    clearTimeout(okTimer);
    if (!okLong && mode === 'home') activate(focusId);
  });

  $('scrim').addEventListener('click', () => { if (mode === 'menu') closeMenu(); });

  let lastWheel = 0;
  document.addEventListener('wheel', e => {
    wake();
    const now = Date.now();
    if (now - lastWheel < 220 || !e.deltaY) return;
    lastWheel = now;
    if (mode === 'menu') menuFocus(menu.index + (e.deltaY > 0 ? 1 : -1));
    else if (mode === 'home') move(e.deltaY > 0 ? 'down' : 'up');
  }, { passive: true });

  document.addEventListener('mousemove', () => wake(), { passive: true });
  // Magic Remote pointer hidden -> drop any tilt
  document.addEventListener('cursorStateChange', e => {
    if (e.detail && e.detail.visibility === false) for (const el of els.values()) untilt(el);
  });

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

  let lastRefresh = 0;
  async function refreshData(force) {
    const now = Date.now();
    if (IS_TV && force) {
      lastRefresh = now;
      // ask the root helper for a fresh app list (it also runs on its own; this is the fast path)
      try { await rootExec('python3 "' + APP_DIR + '/launcherd.py" refresh'); } catch (e) { /* helper keeps data fresh anyway */ }
    }
    if (await loadApps()) requestRender();
  }

  function activateApp() {
    try {
      if (window.PalmSystem && typeof window.PalmSystem.activate === 'function') {
        window.PalmSystem.activate();
      }
    } catch (_) {}
  }

  function captureHomeKey() {
    // While this launcher is in front, the Home button must not open LG's home screen.
    try {
      if (window.PalmSystem && window.PalmSystem.setWindowProperty) {
        window.PalmSystem.setWindowProperty('_WEBOS_ACCESS_POLICY_KEYS_HOME', 'true');
      }
    } catch (e) { /* older firmware: the root helper redirects instead */ }
  }

  function resetKeys() {            // a key-up can get lost while the app is hidden or loses focus
    okDown = false; okLong = false; okBlocked = false; menuArmed = true;
    clearTimeout(okTimer);
  }

  function onShown() {
    activateApp();
    resetKeys();
    restoreView();
    wake();
    tick();
    captureHomeKey();
    refreshData(false);
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) resetKeys(); else onShown(); });
  window.addEventListener('blur', resetKeys);
  window.addEventListener('focus', onShown);
  document.addEventListener('webOSRelaunch', onShown);
  window.addEventListener('webOSRelaunch', onShown);

  if (!IS_TV) {
    // PC preview only: the TV has these fonts built in
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
  }

  /* ------------------------------------------------------------------ */
  /* SmartThings & Weather Integration                                   */
  /* ------------------------------------------------------------------ */
  const WMO_MAP = {
    0: ['☀️', 'Klarer Himmel'],
    1: ['🌤️', 'Überwiegend klar'],
    2: ['⛅', 'Teilweise bewölkt'],
    3: ['☁️', 'Bedeckt'],
    45: ['🌫️', 'Nebel'],
    48: ['🌫️', 'Raureif-Nebel'],
    51: ['🌧️', 'Leichter Niesel'],
    53: ['🌧️', 'Mäßiger Niesel'],
    55: ['🌧️', 'Dichter Niesel'],
    61: ['🌧️', 'Leichter Regen'],
    63: ['🌧️', 'Mäßiger Regen'],
    65: ['🌧️', 'Starker Regen'],
    71: ['🌨️', 'Leichter Schneefall'],
    73: ['🌨️', 'Schneefall'],
    75: ['🌨️', 'Starker Schneefall'],
    80: ['🌦️', 'Leichte Schauer'],
    81: ['🌦️', 'Regenschauer'],
    82: ['⛈️', 'Starke Schauer'],
    95: ['⛈️', 'Gewitter']
  };

  async function fetchWeather() {
    try {
      const res = await fetch('http://127.0.0.1:8080/api/weather');
      const data = await res.json();
      if (data && data.current) {
        updateWeatherUI(data);
      }
    } catch (e) {
      // ignore
    }
  }

  function updateWeatherUI(data) {
    const cur = data.current;
    if (!cur) return;
    const temp = Math.round(cur.temperature_2m);
    const code = cur.weather_code;
    const [icon, desc] = WMO_MAP[code] || ['⛅', 'Bewölkt'];
    const iconEl = $('w-icon');
    const tempEl = $('w-temp');
    const subEl = $('w-sub');
    if (iconEl) iconEl.textContent = icon;
    if (tempEl) tempEl.textContent = temp + '°C';
    if (subEl) {
      let rain = '';
      if (data.daily && data.daily.precipitation_probability_max && data.daily.precipitation_probability_max[0] > 0) {
        rain = ' · ' + data.daily.precipitation_probability_max[0] + '% Regen';
      }
      subEl.textContent = desc + rain;
    }
  }

  async function fetchSmartThingsDevices() {
    try {
      const res = await fetch('http://127.0.0.1:8080/api/smartthings/devices');
      const devs = await res.json();
      if (Array.isArray(devs) && devs.length) {
        const oldJson = JSON.stringify(state.smartDevices);
        const newJson = JSON.stringify(devs);
        if (oldJson !== newJson) {
          state.smartDevices = devs;
          requestRender();
        }
      }
    } catch (e) {
      // ignore
    }
  }

  async function toggleSmartDevice(deviceId, it) {
    try {
      const res = await fetch('http://127.0.0.1:8080/api/smartthings/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: deviceId })
      });
      const data = await res.json();
      if (data.status === 'ok') {
        it.state = data.state;
        it.sub = data.state === 'on' ? 'AN' : 'AUS';
        const cardEl = els.get(it.id);
        if (cardEl) updateCard(cardEl, it);
        toast(it.title + ': ' + (data.state === 'on' ? 'Eingeschaltet' : 'Ausgeschaltet'));
      } else {
        toast('Fehler: ' + (data.error || 'Unbekannt'));
      }
    } catch (err) {
      toast('SmartThings nicht erreichbar');
    }
  }

  /* ------------------------------------------------------------------ */
  /* Audio Output & WeatherOnline Launchers                             */
  /* ------------------------------------------------------------------ */
  function openWeatherApp() {
    toast('Öffne WetterOnline...');
    if (IS_TV) {
      luna(SAM + 'launch', {
        id: 'com.webos.app.browser',
        params: { target: 'https://www.wetteronline.de/wetter/musterstadt' }
      }).catch(err => {
        toast('WetterOnline konnte nicht geöffnet werden');
      });
    } else {
      window.open('https://www.wetteronline.de/wetter/musterstadt', '_blank');
    }
  }

  function openAudioSettings() {
    toast('Öffne Audioausgabe-Einstellungen...');
    if (IS_TV) {
      luna(SAM + 'launch', {
        id: SETTINGS,
        params: { target: 'SoundOut' }
      }).catch(err => {
        toast('Audioeinstellungen konnten nicht geöffnet werden');
      });
    } else {
      toast('Audioausgabe: Denon AVR-1803');
    }
  }

  function updateSoundOutputUI(out) {
    const iconEl = $('audio-icon');
    const titleEl = $('audio-title');
    const subEl = $('audio-sub');
    const badgeEl = $('audio-badge');
    if (!titleEl || !subEl || !badgeEl) return;

    if (out === 'external_optical') {
      if (iconEl) iconEl.textContent = '🔊';
      titleEl.textContent = 'Denon AVR-1803';
      subEl.textContent = 'Optischer Digitalausgang';
      badgeEl.className = 'glass-pill audio-pill denon-avr';
    } else if (out === 'tv_speaker') {
      if (iconEl) iconEl.textContent = '🔈';
      titleEl.textContent = 'TV-Lautsprecher';
      subEl.textContent = 'Interner TV-Lautsprecher';
      badgeEl.className = 'glass-pill audio-pill tv-speaker';
    } else if (out === 'external_arc') {
      if (iconEl) iconEl.textContent = '🔊';
      titleEl.textContent = 'HDMI eARC';
      subEl.textContent = 'Digitalausgang';
      badgeEl.className = 'glass-pill audio-pill denon-avr';
    } else if (out === 'tv_speaker_and_external_optical') {
      if (iconEl) iconEl.textContent = '🔊';
      titleEl.textContent = 'TV & Denon AVR';
      subEl.textContent = 'Optisch + TV-Lautsprecher';
      badgeEl.className = 'glass-pill audio-pill denon-avr';
    } else if (out && (out.includes('bt') || out.includes('bluetooth'))) {
      if (iconEl) iconEl.textContent = '🎧';
      titleEl.textContent = 'Bluetooth-Ton';
      subEl.textContent = 'Drahtlos verbunden';
      badgeEl.className = 'glass-pill audio-pill denon-avr';
    } else {
      if (iconEl) iconEl.textContent = '🔊';
      titleEl.textContent = 'Audioausgabe';
      subEl.textContent = out || 'Ton-Ausgang';
      badgeEl.className = 'glass-pill audio-pill';
    }
  }

  function initSoundOutput() {
    if (!IS_TV) return;
    luna('luna://com.webos.service.audio/getSoundOutput', {}).then(res => {
      if (res && res.soundOutput) updateSoundOutputUI(res.soundOutput);
    }).catch(() => {});

    lunaSubscribe('luna://com.webos.service.audio/getSoundOutput', {}, msg => {
      if (msg && msg.soundOutput) updateSoundOutputUI(msg.soundOutput);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Gemini TV AI Assistant Modal (100% Remote-Driven, No Typing)        */
  /* ------------------------------------------------------------------ */
  let geminiModalOpen = false;
  let lastPrompt = '';
  const geminiModalEl = $('gemini-modal');
  const geminiChatEl = $('gemini-chat');
  const geminiCloseBtn = $('gemini-close-btn');
  const geminiBackdropEl = $('gemini-backdrop');
  const geminiStatusEl = $('gemini-panel-status');
  const geminiRefreshBtn = $('gemini-refresh-btn');

  function openGeminiModal() {
    geminiModalOpen = true;
    if (geminiModalEl) geminiModalEl.style.display = 'flex';
    const firstTile = geminiModalEl ? geminiModalEl.querySelector('.g-tile') : null;
    if (firstTile) setTimeout(() => firstTile.focus(), 100);
  }

  function closeGeminiModal() {
    geminiModalOpen = false;
    if (geminiModalEl) geminiModalEl.style.display = 'none';
  }

  async function triggerGeminiTile(promptText, tileLabel) {
    if (!promptText) return;
    lastPrompt = promptText;

    if (geminiStatusEl) {
      geminiStatusEl.innerHTML = '<span class="welcome-sparkle-sm">✨</span> ' + (tileLabel || 'Gemini') + ' wird generiert...';
    }

    if (geminiChatEl) {
      geminiChatEl.innerHTML = '<div class="thinking-indicator"><span>✨</span><span>Gemini denkt nach und bereitet die Antwort vor...</span></div>';
    }

    try {
      const res = await fetch('http://127.0.0.1:8080/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: promptText })
      });
      const data = await res.json();

      if (geminiStatusEl) {
        geminiStatusEl.innerHTML = '✨ ' + (tileLabel || 'Antwort');
      }

      if (geminiRefreshBtn) {
        geminiRefreshBtn.style.display = 'inline-block';
      }

      if (geminiChatEl) {
        geminiChatEl.innerHTML = '';
        const card = document.createElement('div');
        card.className = 'gemini-answer-card';
        card.textContent = data.text || 'Keine Antwort erhalten.';

        if (Array.isArray(data.actions) && data.actions.length > 0) {
          const actionTag = document.createElement('div');
          actionTag.className = 'chat-action-tag';
          actionTag.textContent = '⚡ ' + data.actions.map(a => a.tool).join(', ');
          card.appendChild(actionTag);
        }

        geminiChatEl.appendChild(card);
        geminiChatEl.scrollTop = 0;
      }

      if (Array.isArray(data.actions) && data.actions.some(a => a.tool && (a.tool.includes('smart') || a.tool.includes('screen')))) {
        setTimeout(fetchSmartThingsDevices, 1200);
      }
    } catch (err) {
      if (geminiStatusEl) geminiStatusEl.textContent = '⚠️ Verbindungsfehler';
      if (geminiChatEl) {
        geminiChatEl.innerHTML = '<div style="color: #ff453a; font-size: 22px;">Verbindungsfehler zum lokalen Gemini-Dienst auf dem TV.</div>';
      }
    }
  }

  function initGeminiUI() {
    if (geminiCloseBtn) geminiCloseBtn.addEventListener('click', closeGeminiModal);
    if (geminiBackdropEl) geminiBackdropEl.addEventListener('click', closeGeminiModal);

    const sidebar = document.querySelector('.gemini-sidebar');
    if (sidebar) {
      sidebar.addEventListener('click', e => {
        const tile = e.target.closest('.g-tile');
        if (tile && tile.dataset.prompt) {
          sidebar.querySelectorAll('.g-tile').forEach(t => t.classList.remove('active'));
          tile.classList.add('active');
          const label = tile.querySelector('.g-tile-label') ? tile.querySelector('.g-tile-label').textContent : '';
          triggerGeminiTile(tile.dataset.prompt, label);
        }
      });
    }

    if (geminiRefreshBtn) {
      geminiRefreshBtn.addEventListener('click', () => {
        if (lastPrompt) {
          triggerGeminiTile(lastPrompt + ' (Bitte gib mir noch 2 ganz andere, neue Vorschläge/Alternativen dazu)', 'Neue Vorschläge');
        }
      });
    }
  }

  async function start() {
    document.body.classList.add('booting');
    activateApp();
    tick();
    captureHomeKey();
    fetchWeather();
    fetchSmartThingsDevices();
    initSoundOutput();

    weatherEl.dataset.id = 'hero:weather';
    geminiBadgeEl.dataset.id = 'hero:gemini';
    audioBadgeEl.dataset.id = 'hero:audio';
    els.set('hero:weather', weatherEl);
    els.set('hero:gemini', geminiBadgeEl);
    els.set('hero:audio', audioBadgeEl);

    weatherEl.addEventListener('mouseenter', () => { if (mode === 'home') setFocus('hero:weather', { scroll: false }); });
    weatherEl.addEventListener('mouseleave', () => untilt(weatherEl));
    weatherEl.addEventListener('click', () => { if (mode === 'home') activate('hero:weather'); });

    geminiBadgeEl.addEventListener('mouseenter', () => { if (mode === 'home') setFocus('hero:gemini', { scroll: false }); });
    geminiBadgeEl.addEventListener('mouseleave', () => untilt(geminiBadgeEl));
    geminiBadgeEl.addEventListener('click', () => { if (mode === 'home') activate('hero:gemini'); });

    audioBadgeEl.addEventListener('mouseenter', () => { if (mode === 'home') setFocus('hero:audio', { scroll: false }); });
    audioBadgeEl.addEventListener('mouseleave', () => untilt(audioBadgeEl));
    audioBadgeEl.addEventListener('click', () => { if (mode === 'home') activate('hero:audio'); });

    initGeminiUI();

    try {
      await loadApps();
      render();
    } catch (e) {
      console.error('start failed', e);
    } finally {
      requestAnimationFrame(() => document.body.classList.remove('booting'));
    }
    lunaSubscribe('luna://com.webos.service.eim/getAllInputStatus', {}, msg => {
      if (Array.isArray(msg.devices)) { state.devices = msg.devices; requestRender(); }
    });
    refreshData(false);
    setInterval(() => { if (!document.hidden) loadApps().then(changed => { if (changed) requestRender(); }); }, 60000);
    setInterval(() => { if (!document.hidden) fetchSmartThingsDevices(); }, 25000);
    setInterval(() => { if (!document.hidden) fetchWeather(); }, 600000);
  }

  // whatever goes wrong, never leave the screen faded out or blank
  const unstick = () => document.body.classList.remove('booting', 'leaving');
  window.addEventListener('error', unstick);
  window.addEventListener('unhandledrejection', unstick);

  // for tests and debugging from the web inspector
  window.__launcher = {
    get focus() { return focusId; }, get mode() { return mode; },
    get dock() { return dockIds.slice(); }, get grid() { return gridIds.slice(); },
    setUsage(u) { state.usage = u; render(); }, get prefs() { return clone(prefs); }, get menu() { return menu ? menu.entries.map(e => e.label) : null; }
  };

  start();
})();
