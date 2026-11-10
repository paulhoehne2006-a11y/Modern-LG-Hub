#!/usr/bin/env node
// Dev helper: builds com.custom.launcher/data/ (apps.json + icons) from design/fixtures,
// in exactly the format launcherd.py writes on the TV. Only needed for previewing on the PC.
'use strict';
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const fx = path.join(root, 'design', 'fixtures');
const out = path.join(root, 'com.custom.launcher', 'data');
const EXTRA = new Set(['com.webos.app.hdmi1', 'com.webos.app.hdmi2', 'com.webos.app.hdmi3', 'com.webos.app.hdmi4',
  'com.webos.app.livetv', 'com.palm.app.settings']);

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(path.join(out, 'icons'), { recursive: true });
const manifest = JSON.parse(fs.readFileSync(path.join(fx, 'manifest.json'), 'utf8')).apps;
const apps = [];
for (const a of manifest) {
  if (a.visible === false && !EXTRA.has(a.id)) continue;
  const e = { id: a.id, title: a.title, type: a.type, visible: a.visible, removable: a.removable,
    systemApp: a.systemApp, iconColor: a.iconColor || null };
  for (const key of ['extraLargeIcon', 'largeIcon', 'mediumLargeIcon', 'icon']) {
    const as = a.assets[key];
    if (!as || as.missing || !as.w) continue;
    const ext = path.extname(as.file) || '.png';
    fs.copyFileSync(path.join(fx, as.file), path.join(out, 'icons', a.id + ext));
    e.icon = 'data/icons/' + a.id + ext; e.iconPx = as.w;
    break;
  }
  apps.push(e);
}
fs.writeFileSync(path.join(out, 'apps.json'), JSON.stringify({ generatedAt: Math.floor(Date.now() / 1000), apps }));
const seed={netflix:3,'spotify-beehive':2.8,'com.webos.app.hdmi1':2.6,'youtube.leanback.v4':2.4,amazon:2.2,'com.disney.disneyplus-prod':2};
const now=Math.floor(Date.now()/1000);const usage={};for(const k in seed)usage[k]={n:seed[k],t:now};
fs.writeFileSync(path.join(out,'usage.json'),JSON.stringify({updatedAt:Date.now(),usage}));
console.log('wrote ' + apps.length + ' apps to ' + out);
