#!/usr/bin/env node
// Minimal Chrome DevTools Protocol client for the TV's remote web inspector.
// Run it through tools/cdp.sh (adds the Node flag that enables WebSocket on Node 21).
//
//   cdp.sh pages                              list inspectable pages
//   cdp.sh eval '<js expression>'             evaluate in the page (promises awaited), print result as JSON
//   cdp.sh evalfile path/to/snippet.js        same, expression read from a file (no shell quoting trouble)
//   cdp.sh key ArrowDown ArrowRight Enter     dispatch real key events (keydown + keyup) into the page
//   cdp.sh shot out.png                       screenshot of the page itself (not the whole compositor)
//   cdp.sh reload                             reload the page
//
// Options (before the command):
//   --app <appId>       page to target (default com.custom.launcher)
//   --host <host:port>  inspector address (default 192.168.178.60:9998; use 127.0.0.1:<port> for local Edge)
//   --timeout <ms>      overall timeout (default 30000)
//   --console           also print console.* output produced while the command runs
'use strict';
const fs = require('fs');

const argv = process.argv.slice(2);
const opt = { app: 'com.custom.launcher', host: '192.168.178.60:9998', timeout: 30000, console: false };
while (argv[0] && argv[0].startsWith('--')) {
  const o = argv.shift();
  if (o === '--console') opt.console = true;
  else if (o === '--app') opt.app = argv.shift();
  else if (o === '--host') opt.host = argv.shift();
  else if (o === '--timeout') opt.timeout = Number(argv.shift());
  else { console.error('unknown option ' + o); process.exit(2); }
}
const cmd = argv.shift();

const KEYS = {
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Enter: 13, Escape: 27,
  Back: 461, Red: 403, Green: 404, Yellow: 405, Blue: 406,
  Play: 415, Pause: 19, Stop: 413, Rewind: 412, FastForward: 417,
  0: 48, 1: 49, 2: 50, 3: 51, 4: 52, 5: 53, 6: 54, 7: 55, 8: 56, 9: 57
};

function fail(msg) { console.error(msg); process.exit(1); }

(async () => {
  const timer = setTimeout(() => fail('timeout after ' + opt.timeout + ' ms'), opt.timeout);
  let pages;
  try {
    pages = await (await fetch('http://' + opt.host + '/json')).json();
  } catch (e) { fail('cannot reach inspector at ' + opt.host + ': ' + e.message); }

  if (cmd === 'pages') {
    for (const p of pages) console.log([p.description || '-', p.title, p.url].join(' | '));
    process.exit(0);
  }
  const page = pages.find(p => p.description === opt.app) ||
               pages.find(p => (p.url || '').includes(opt.app)) ||
               (opt.app === '*' ? pages.find(p => p.type === 'page') : null);
  if (!page) fail('no inspectable page for ' + opt.app + ' (is the app running?). Pages: ' +
                  pages.map(p => p.description || p.url).join(', '));

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); const logs = [];
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id; pending.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    } else if (m.method === 'Runtime.consoleAPICalled') {
      logs.push('[' + m.params.type + '] ' + m.params.args.map(a => a.value !== undefined ? a.value : a.description).join(' '));
    }
  };
  ws.onerror = e => fail('websocket error: ' + (e.message || 'unknown'));
  await new Promise(r => { ws.onopen = r; });

  const finish = code => {
    if (opt.console && logs.length) console.log('--- console ---\n' + logs.join('\n'));
    clearTimeout(timer); ws.close(); process.exit(code);
  };

  try {
    if (opt.console) await send('Runtime.enable');
    if (cmd === 'eval' || cmd === 'evalfile') {
      const expression = cmd === 'eval' ? argv.join(' ') : fs.readFileSync(argv[0], 'utf8');
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
      if (r.exceptionDetails) {
        const d = r.exceptionDetails;
        console.error('EXCEPTION: ' + ((d.exception && (d.exception.description || d.exception.value)) || d.text));
        finish(1);
      }
      const v = r.result.value;
      console.log(typeof v === 'string' ? v : JSON.stringify(v === undefined ? null : v, null, 2));
    } else if (cmd === 'key') {
      for (const name of argv) {
        const code = KEYS[name] !== undefined ? KEYS[name] : Number(name);
        if (!Number.isFinite(code)) fail('unknown key ' + name);
        const base = { windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, key: String(name), code: String(name) };
        await send('Input.dispatchKeyEvent', Object.assign({ type: 'rawKeyDown' }, base));
        await send('Input.dispatchKeyEvent', Object.assign({ type: 'keyUp' }, base));
        await new Promise(r => setTimeout(r, 120));
      }
      console.log('sent: ' + argv.join(' '));
    } else if (cmd === 'shot') {
      const r = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(argv[0], Buffer.from(r.data, 'base64'));
      console.log('saved ' + argv[0]);
    } else if (cmd === 'reload') {
      await send('Page.reload', { ignoreCache: true });
      console.log('reloaded');
    } else {
      fail('usage: cdp.sh [--app id] [--host h:p] [--timeout ms] [--console] pages|eval|evalfile|key|shot|reload ...');
    }
    finish(0);
  } catch (e) { console.error('ERROR: ' + e.message); finish(1); }
})();
