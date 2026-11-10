#!/usr/bin/env node
// Render a local page in headless Edge at TV size and drive it like a remote control.
// Used to preview/test the launcher on the PC (no downloads, only the Edge that ships with Windows).
// Run through tools/localshot.sh.
//
//   localshot.sh <file.html | url> [options] <step> <step> ...
//
// Steps (executed in order):
//   wait=<ms>                 pause
//   shot=<out.png>            screenshot (viewport)
//   key=<Name>                key press (ArrowUp/Down/Left/Right, Enter, Back, Red, Green, Yellow, Blue, 0-9, or a numeric keyCode)
//   keys=<A,B,C>              several key presses, 140 ms apart
//   hold=<Name>:<ms>          hold a key (keydown, auto-repeat every 90 ms, keyup) e.g. long-press OK
//   move=<x>,<y>              move the mouse pointer (Magic Remote cursor)
//   click=<x>,<y>             mouse click
//   wheel=<x>,<y>,<deltaY>    mouse wheel
//   eval=<js>                 evaluate JS in the page, print the JSON result
//   evalfile=<path.js>        same, read from file
//
// Options:
//   --dpr <n>        device pixel ratio (default 1; the TV uses 2 -> 3840x2160 screenshots)
//   --size <WxH>     viewport in CSS px (default 1920x1080)
//   --browser <exe>  browser executable (default: Edge)
//   --strict         exit with code 1 if the page logged an error or threw an exception
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const KEYS = {
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Enter: 13, Escape: 27,
  Back: 461, Red: 403, Green: 404, Yellow: 405, Blue: 406,
  Play: 415, Pause: 19, Stop: 413, Rewind: 412, FastForward: 417,
  0: 48, 1: 49, 2: 50, 3: 51, 4: 52, 5: 53, 6: 54, 7: 55, 8: 56, 9: 57
};

const args = process.argv.slice(2);
const opt = { dpr: 1, w: 1920, h: 1080, strict: false,
  browser: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' };
let target = null; const steps = [];
while (args.length) {
  const a = args.shift();
  if (a === '--dpr') opt.dpr = Number(args.shift());
  else if (a === '--size') { const [w, h] = args.shift().split('x').map(Number); opt.w = w; opt.h = h; }
  else if (a === '--browser') opt.browser = args.shift();
  else if (a === '--strict') opt.strict = true;
  else if (!target) target = a;
  else steps.push(a);
}
if (!target) { console.error('usage: localshot.sh <file.html|url> [--dpr n] [--size WxH] [--strict] step...'); process.exit(2); }
// Allow "file.html?query#hash" for local files.
let url;
if (/^[a-z][a-z0-9+.-]*:\/\//i.test(target)) url = target;
else {
  const m = /^([^?#]*)(.*)$/.exec(target);
  url = pathToFileURL(path.resolve(m[1])).href + m[2];
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const port = 9300 + Math.floor(process.pid % 600);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'localshot-'));
const child = spawn(opt.browser, [
  '--headless=new', '--remote-debugging-port=' + port, '--user-data-dir=' + profile,
  '--no-first-run', '--no-default-browser-check', '--hide-scrollbars', '--mute-audio',
  '--allow-file-access-from-files', '--disable-extensions', '--disable-features=Translate',
  '--window-size=' + opt.w + ',' + opt.h, 'about:blank'
], { stdio: 'ignore' });

let pageErrors = 0;
function cleanup(code) {
  try { child.kill(); } catch (e) { /* ignore */ }
  setTimeout(() => {
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* locked files: ignore */ }
    process.exit(code);
  }, 300);
}
process.on('uncaughtException', e => { console.error('localshot error: ' + e.message); cleanup(1); });

(async () => {
  let pages = null;
  for (let i = 0; i < 80 && !pages; i++) {
    try { pages = await (await fetch('http://127.0.0.1:' + port + '/json')).json(); }
    catch (e) { await sleep(150); }
  }
  if (!pages) throw new Error('browser did not start (' + opt.browser + ')');
  const page = pages.find(p => p.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); let loadResolve = null;
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id; pending.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    } else if (m.method === 'Page.loadEventFired' && loadResolve) { loadResolve(); loadResolve = null; }
    else if (m.method === 'Runtime.exceptionThrown') {
      pageErrors++;
      const d = m.params.exceptionDetails;
      console.error('[page exception] ' + ((d.exception && d.exception.description) || d.text) +
        (d.url ? ' @ ' + d.url + ':' + d.lineNumber : ''));
    } else if (m.method === 'Runtime.consoleAPICalled') {
      const t = m.params.type;
      const text = m.params.args.map(a => a.value !== undefined ? a.value : a.description).join(' ');
      if (t === 'error') { pageErrors++; console.error('[console.error] ' + text); }
      else if (t === 'warning') console.error('[console.warn] ' + text);
      else console.error('[console.' + t + '] ' + text);
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      pageErrors++;
      console.error('[log error] ' + m.params.entry.text + (m.params.entry.url ? ' @ ' + m.params.entry.url : ''));
    }
  };
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = () => j(new Error('websocket failed')); });

  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: opt.w, height: opt.h, deviceScaleFactor: opt.dpr, mobile: false });
  const loaded = new Promise(r => { loadResolve = r; });
  await send('Page.navigate', { url });
  await Promise.race([loaded, sleep(15000)]);

  const keyEvent = async (type, name, repeat) => {
    const code = KEYS[name] !== undefined ? KEYS[name] : Number(name);
    if (!Number.isFinite(code)) throw new Error('unknown key ' + name);
    await send('Input.dispatchKeyEvent', { type, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code,
      key: String(name), code: String(name), autoRepeat: !!repeat });
  };
  const press = async name => { await keyEvent('rawKeyDown', name); await keyEvent('keyUp', name); };

  for (const s of steps) {
    const i = s.indexOf('='); const cmd = s.slice(0, i); const val = s.slice(i + 1);
    if (cmd === 'wait') await sleep(Number(val));
    else if (cmd === 'shot') {
      const r = await send('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(path.dirname(path.resolve(val)), { recursive: true });
      fs.writeFileSync(val, Buffer.from(r.data, 'base64'));
      console.log('saved ' + val);
    } else if (cmd === 'key') { await press(val); }
    else if (cmd === 'keys') { for (const k of val.split(',')) { await press(k.trim()); await sleep(140); } }
    else if (cmd === 'hold') {
      const [name, ms] = val.split(':'); const end = Date.now() + Number(ms);
      await keyEvent('rawKeyDown', name);
      while (Date.now() < end) { await sleep(90); await keyEvent('rawKeyDown', name, true); }
      await keyEvent('keyUp', name);
    } else if (cmd === 'move' || cmd === 'click') {
      const [x, y] = val.split(',').map(Number);
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
      if (cmd === 'click') {
        await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
      }
    } else if (cmd === 'wheel') {
      const [x, y, dy] = val.split(',').map(Number);
      await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX: 0, deltaY: dy });
    } else if (cmd === 'eval' || cmd === 'evalfile') {
      const expression = cmd === 'eval' ? val : fs.readFileSync(val, 'utf8');
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) {
        pageErrors++;
        console.error('[eval exception] ' + ((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text));
      } else {
        const v = r.result.value;
        console.log(typeof v === 'string' ? v : JSON.stringify(v === undefined ? null : v, null, 2));
      }
    } else throw new Error('unknown step ' + s);
  }
  ws.close();
  if (pageErrors) console.error(pageErrors + ' page error(s)');
  cleanup(opt.strict && pageErrors ? 1 : 0);
})().catch(e => { console.error('localshot error: ' + e.message); cleanup(1); });
