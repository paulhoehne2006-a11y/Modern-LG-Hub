#!/usr/bin/env python3
"""Root helper for the custom home screen (com.custom.launcher) on LG webOS.

Runs on the TV as root, started at boot by /var/lib/webosbrew/init.d/99-custom-launcher.

What it does (event driven, no polling):
  * Replaces LG's home screen: whenever com.webos.app.home is launched or comes
    to the front, the custom launcher is put in front instead.
  * Keeps <app dir>/data/apps.json and data/icons/ up to date. The launcher is a
    sandboxed web app: it may not list apps or read other apps' files itself.
  * Counts how often each app is brought to the front (also via the remote's
    Netflix button etc.) and publishes it as data/usage.json; the launcher sorts
    its app grid by it. Master copy: /var/lib/webosbrew/custom-launcher-usage.json

Commands:
  launcherd.py start [--boot]   start in the background (no-op if already running);
                                --boot also brings the launcher to the front
  launcherd.py stop             stop the background process
  launcherd.py status           show whether it is running
  launcherd.py refresh          rewrite data/apps.json + icons once and exit
  launcherd.py run [--boot]     run in the foreground (debugging)

Switches:
  touch /var/lib/webosbrew/custom-launcher.off   LG home behaves as stock again (immediately)
  rm    /var/lib/webosbrew/custom-launcher.off   custom launcher takes over again
  touch /tmp/custom-launcher.pass                let LG home through once (next 20 s)
"""
import codecs
import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import traceback

APP_ID = 'com.custom.launcher'
LG_HOME = 'com.webos.app.home'
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')
ICON_DIR = os.path.join(DATA_DIR, 'icons')
OFF_FLAG = '/var/lib/webosbrew/custom-launcher.off'
PASS_FLAG = '/tmp/custom-launcher.pass'
PASS_SECONDS = 20
LOCK_FILE = '/tmp/custom-launcher.lock'
LOG_FILE = '/tmp/custom-launcher.log'          # tmpfs: no flash wear, cleared on reboot
LOG_MAX = 256 * 1024
REFRESH_LOCK = '/tmp/custom-launcher.refresh.lock'
REPRESS_AFTER = 1.5          # a 'launch' of LG home this long after the pass = HOME pressed again
ALLOW_MAX = 600              # LG home opened on purpose is given back to the launcher after this long
WATCHDOG_SECONDS = 30        # safety net: poll the foreground app (also detects a restarted app manager)
SAM = 'luna://com.webos.applicationManager/'
SYSLOG = '/var/log/messages'
VOICE_DEBOUNCE = 0.6         # one mic press can be logged more than once
# Hidden system apps the launcher still wants artwork/metadata for.
EXTRA_IDS = ('com.webos.app.hdmi1', 'com.webos.app.hdmi2', 'com.webos.app.hdmi3',
             'com.webos.app.hdmi4', 'com.webos.app.livetv', 'com.palm.app.settings')
ICON_KEYS = ('extraLargeIcon', 'largeIcon', 'mediumLargeIcon', 'icon')   # best first
BURST = (0.0, 0.045, 0.09, 0.135, 0.18, 0.24)   # seconds after LG home's "launch" event
USAGE_PERSIST = '/var/lib/webosbrew/custom-launcher-usage.json'   # survives reinstalling the app
USAGE_HALF_LIFE = 45 * 86400        # a launch counts half as much after 45 days
USAGE_FLUSH_DELAY = 5.0             # seconds; batches writes, keeps the order fresh when returning home
BOOT_QUIET = 150                    # ignore app switches this long after a boot start (TV starts Live TV itself)
# Starting order on a fresh install, replaced by real usage within days
USAGE_SEED = (('netflix', 3.0), ('spotify-beehive', 2.8), ('com.webos.app.hdmi1', 2.6),
              ('youtube.leanback.v4', 2.4), ('amazon', 2.2), ('com.disney.disneyplus-prod', 2.0),
              ('com.apple.appletv', 1.8), ('com.webos.app.hdmi2', 1.6), ('com.limelight.webos', 1.4))
KNOWN = set()                       # app ids the launcher shows (set by write_data)

_children = []


def log(msg):
    line = '%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            with open(LOG_FILE, 'rb') as fh:
                fh.seek(-LOG_MAX // 2, os.SEEK_END)
                tail = fh.read()
            with open(LOG_FILE, 'wb') as fh:
                fh.write(tail)
        with open(LOG_FILE, 'a') as fh:
            fh.write(line)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Luna bus access. luna-send only prints replies when stdout is a terminal,
# so every call gets a pseudo terminal.
# --------------------------------------------------------------------------
def _parse_first_json(text):
    i = text.find('{')
    if i < 0:
        raise ValueError('no JSON in reply: %r' % text[:120])
    return json.JSONDecoder().raw_decode(text[i:])[0]


def luna_call(uri, params=None, timeout=20):
    """One request, one reply. Raises on timeout or unparsable reply."""
    master, slave = os.openpty()
    try:
        proc = subprocess.Popen(['luna-send', '-n', '1', uri, json.dumps(params or {})],
                                stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, close_fds=True)
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    buf = b''
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('luna call timed out: ' + uri)
            ready, _, _ = select.select([master], [], [], min(left, 0.5))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:          # EIO: the other end is closed = end of output
                    chunk = b''
                if not chunk:
                    break
                buf += chunk
            elif proc.poll() is not None:
                break
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    return _parse_first_json(buf.decode('utf-8', 'replace'))


def luna_fire(uri, params):
    """Send a request without waiting for the reply (fast path for redirects)."""
    try:
        _children.append((subprocess.Popen(['luna-send', '-n', '1', uri, json.dumps(params)],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=True), time.monotonic()))
    except OSError as e:
        log('luna_fire failed: %s' % e)


def reap_children():
    now = time.monotonic()
    keep = []
    for proc, started in _children:
        if proc.poll() is None:
            if now - started > 20:         # hung luna-send: kill it, it is collected on the next pass
                try:
                    proc.kill()
                except OSError:
                    pass
            keep.append((proc, started))
    _children[:] = keep


class Subscription:
    """A long-lived `luna-send -i` whose JSON replies are read incrementally."""

    def __init__(self, uri, params):
        self.uri, self.params = uri, params
        self.proc = None
        self.fd = None
        self.buf = ''
        self.decoder = None
        self.retry_at = 0.0
        self.backoff = 1.0

    def start(self):
        master, slave = os.openpty()
        try:
            self.proc = subprocess.Popen(['luna-send', '-i', self.uri, json.dumps(self.params)],
                                         stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                         preexec_fn=os.setsid, close_fds=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        self.buf = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def stop(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def failed(self):
        """Schedule a restart with exponential backoff (1 s .. 30 s)."""
        self.stop()
        self.retry_at = time.monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, 30.0)

    def read(self):
        """Return the list of complete JSON objects available, or None at end of stream."""
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            chunk = b''
        if not chunk:
            return None
        self.buf += self.decoder.decode(chunk)
        out = []
        dec = json.JSONDecoder()
        while True:
            i = self.buf.find('{')
            if i < 0:
                self.buf = ''
                break
            try:
                obj, end = dec.raw_decode(self.buf[i:])
            except ValueError:
                self.buf = self.buf[i:]      # incomplete object: wait for more data
                break
            self.buf = self.buf[i + end:]
            out.append(obj)
        if len(self.buf) > 262144:           # garbage that never turns into JSON: drop it
            self.buf = ''
        return out


# --------------------------------------------------------------------------
# App list + icons for the launcher
# --------------------------------------------------------------------------
def _best_icon(app):
    folder = app.get('folderPath') or ''
    for key in ICON_KEYS:
        name = app.get(key)
        if not name or not isinstance(name, str):
            continue
        if name.startswith('/'):
            path = name
        else:
            path = os.path.join(folder, name[2:] if name.startswith('./') else name)
        if os.path.isfile(path):
            return path
    return None


def write_data(apps):
    """Write data/apps.json and data/icons/. Returns True if anything changed."""
    os.makedirs(ICON_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o755)
    os.chmod(ICON_DIR, 0o755)
    entries, keep, changed = [], set(), False
    for app in apps:
        app_id = app.get('id')
        if not app_id or app_id == APP_ID:
            continue
        if app.get('visible') is False and app_id not in EXTRA_IDS:
            continue
        entry = {'id': app_id, 'title': app.get('title') or app_id, 'type': app.get('type'),
                 'visible': app.get('visible') is not False, 'removable': bool(app.get('removable')),
                 'systemApp': bool(app.get('systemApp')), 'iconColor': app.get('iconColor') or None}
        src = _best_icon(app)
        if src:
            ext = os.path.splitext(src)[1].lower() or '.png'
            name = app_id + ext
            dst = os.path.join(ICON_DIR, name)
            try:
                st = os.stat(src)
                if (not os.path.exists(dst) or os.path.getsize(dst) != st.st_size
                        or os.path.getmtime(dst) < st.st_mtime):
                    tmp = '%s.%d.tmp' % (dst, os.getpid())
                    shutil.copyfile(src, tmp)
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, dst)
                    changed = True
                entry['icon'] = 'data/icons/' + name
                entry['iconV'] = int(st.st_mtime)
                keep.add(name)
            except OSError as e:
                log('icon copy failed for %s: %s' % (app_id, e))
        entries.append(entry)
    entries.sort(key=lambda e: e['id'])
    KNOWN.clear()
    KNOWN.update(e['id'] for e in entries)
    for name in os.listdir(ICON_DIR):
        if name not in keep:
            try:
                os.remove(os.path.join(ICON_DIR, name))
                changed = True
            except OSError:
                pass
    target = os.path.join(DATA_DIR, 'apps.json')
    try:
        with open(target) as fh:
            old = json.load(fh).get('apps')
    except (OSError, ValueError):
        old = None
    if old != entries:
        tmp = '%s.%d.tmp' % (target, os.getpid())
        with open(tmp, 'w') as fh:
            json.dump({'generatedAt': int(time.time()), 'apps': entries}, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        changed = True
    return changed


_shrink_seen = False


def _stored_count():
    try:
        with open(os.path.join(DATA_DIR, 'apps.json')) as fh:
            return len(json.load(fh).get('apps') or [])
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def refresh(apps=None, timeout=40, wait=True):
    """Rewrite data/apps.json + icons. Returns True, False (failed) or 'retry' (busy / suspicious list)."""
    global _shrink_seen
    lock = None
    try:
        lock = open(REFRESH_LOCK, 'a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return 'retry'                         # another refresh is running
        if apps is None:
            reply = luna_call(SAM + 'listApps', {}, timeout=timeout)
            if not reply.get('returnValue') or not isinstance(reply.get('apps'), list):
                raise ValueError('listApps failed: %s' % reply.get('errorText'))
            apps = reply['apps']
        if not apps:
            raise ValueError('empty app list')
        new_n = sum(1 for a in apps if isinstance(a, dict) and a.get('id') and a['id'] != APP_ID
                    and not (a.get('visible') is False and a['id'] not in EXTRA_IDS))
        old_n = _stored_count()
        if old_n >= 10 and new_n < old_n * 0.6 and not _shrink_seen:
            _shrink_seen = True                    # needs a second, equally small answer before we believe it
            log('refresh: list shrank from %d to %d apps, re-checking' % (old_n, new_n))
            return 'retry'
        _shrink_seen = False
        changed = write_data(apps)
        log('refresh: %d apps%s' % (len(apps), ' (data updated)' if changed else ''))
        return True
    except Exception as e:                         # never let a refresh kill the daemon
        log('refresh failed: %r' % (e,))
        return False
    finally:
        if lock is not None:
            lock.close()


# --------------------------------------------------------------------------
# Usage statistics
# --------------------------------------------------------------------------
def _write_json_atomic(path, obj, mode=0o644):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w') as fh:
        json.dump(obj, fh)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def usage_load():
    try:
        with open(USAGE_PERSIST) as fh:
            raw = json.load(fh).get('usage')
        if isinstance(raw, dict):
            clean = {k: {'n': float(v['n']), 't': int(v['t'])} for k, v in raw.items()
                     if isinstance(v, dict) and isinstance(v.get('n'), (int, float))
                     and isinstance(v.get('t'), (int, float))}
            return clean
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    now = int(time.time())
    return {app: {'n': n, 't': now} for app, n in USAGE_SEED}


def usage_record(usage, app, now):
    prev = usage.get(app)
    n = 1.0
    if prev:
        n += prev['n'] * 0.5 ** (max(0.0, now - prev['t']) / USAGE_HALF_LIFE)
    usage[app] = {'n': round(n, 4), 't': int(now)}


def usage_flush(usage):
    payload = {'updatedAt': int(time.time() * 1000), 'usage': usage}
    for path in (USAGE_PERSIST, os.path.join(DATA_DIR, 'usage.json')):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_json_atomic(path, payload)
        except OSError as e:
            log('usage write failed (%s): %s' % (path, e))


# --------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------
class State:
    home_front = False     # LG home is the foreground app
    allow_home = False     # owner asked for LG home on purpose (pass flag)
    allow_since = 0.0
    burst = []             # times at which to (re)launch the launcher
    nag_left = 0           # safety net: retries while LG home stays in front
    nag_at = 0.0
    stop = False
    usage = None           # {app id: {'n': decayed count, 't': last use}}
    usage_dirty_at = 0.0   # when to write the statistics (0 = nothing pending)
    last_fg = None         # app that was in front before the current event
    started = 0.0
    quiet = 0.0


def paused():
    # off switch, or the app was uninstalled (then redirecting would lead nowhere)
    return os.path.exists(OFF_FLAG) or not os.path.exists(os.path.join(APP_DIR, 'appinfo.json'))


def redirect_wanted(st):
    return not paused() and not st.allow_home


def nag_due(st):
    return st.home_front and st.nag_left > 0 and not st.burst and redirect_wanted(st)


def take_pass():
    try:
        fresh = time.time() - os.path.getmtime(PASS_FLAG) < PASS_SECONDS
        os.remove(PASS_FLAG)
        return fresh
    except OSError:
        return False


def launch_launcher():
    # explicit params, so a mic button's {'gemini': ...} never lingers in PalmSystem.launchParams
    luna_fire(SAM + 'launch', {'id': APP_ID, 'params': {'reason': 'home'}})
    luna_fire(SAM + 'closeByAppId', {'id': LG_HOME})
    luna_fire('luna://com.webos.surfacemanager/closeByAppId', {'id': LG_HOME})


def on_life_event(st, ev):
    app, event = ev.get('appId'), ev.get('event')
    if not app or not event:
        return
    now, wall = time.monotonic(), time.time()
    if event == 'foreground':
        if app in KNOWN and app != st.last_fg and wall - st.started >= st.quiet and st.usage is not None:
            usage_record(st.usage, app, wall)
            st.usage_dirty_at = st.usage_dirty_at or now + USAGE_FLUSH_DELAY
        st.last_fg = app
    if app == LG_HOME:
        if event in ('launch', 'foreground'):
            if event == 'foreground':
                st.home_front = True
            if paused():
                return
            if st.allow_home:
                if event == 'launch' and now - st.allow_since > REPRESS_AFTER:
                    st.allow_home = False          # HOME pressed again while LG home is up: back to the launcher
                    log('HOME pressed on LG home: back to the launcher')
                else:
                    return
            elif take_pass():
                st.allow_home, st.allow_since = True, now
                st.burst, st.nag_left = [], 0
                log('LG home allowed once (pass flag)')
                return
            log('intercept: LG home %s -> redirecting to %s' % (event, APP_ID))
            launch_launcher()
            if event == 'launch':
                st.burst = [now + d for d in BURST if d > 0]
            else:
                st.burst = [now + 0.08, now + 0.2]
            st.nag_left, st.nag_at = 5, now + 0.8
        elif event in ('background', 'stop', 'close'):
            st.home_front = False
            st.allow_home = False
            st.burst, st.nag_left = [], 0
    elif event == 'foreground':
        st.home_front = False
        st.allow_home = False
        st.nag_left = 0
        if app == APP_ID:
            st.burst = []


class VoiceKeyWatch:
    """Follows the system log for the remote's mic button (lginput2 logs KEY_VOICE).

    LG's own voice app is stubbed on this TV, so the button would do nothing; we use it
    to open the Gemini popup in the launcher instead. `tail` exits by itself (SIGPIPE)
    on the next log line if this daemon dies, so no orphan survives for long.
    """
    def __init__(self):
        self.proc = None
        self.fd = None
        self.buf = b''
        self.retry_at = 0.0
        self.last = 0.0

    def start(self):
        if not os.path.exists(SYSLOG):
            raise OSError('%s missing' % SYSLOG)
        self.proc = subprocess.Popen(['tail', '-n', '0', '-F', SYSLOG], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        self.fd = self.proc.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.buf = b''

    def failed(self):
        self.stop()
        self.retry_at = time.monotonic() + 10

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
        self.proc, self.fd = None, None

    def read_presses(self):
        """Returns the number of mic presses seen, or None if tail ended."""
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return 0
        except OSError:
            return None
        if not chunk:
            return None
        self.buf += chunk
        lines = self.buf.split(b'\n')
        self.buf = lines.pop()[-4096:]
        return sum(1 for ln in lines if b'"button_type":"KEY_VOICE"' in ln)


def on_voice_key(st, watch):
    now = time.monotonic()
    if now - watch.last < VOICE_DEBOUNCE or paused():
        return
    watch.last = now
    mode = 'toggle' if st.last_fg == APP_ID else 'open'
    log('mic button -> gemini %s (front: %s)' % (mode, st.last_fg))
    luna_fire(SAM + 'launch', {'id': APP_ID, 'params': {'gemini': mode}})


def wait_for_sam(st, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline and not st.stop:
        try:
            if luna_call(SAM + 'getForegroundAppInfo', {}, timeout=8).get('returnValue'):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def watchdog(st, subs):
    """Cheap safety net: catches missed events and an application manager that restarted."""
    try:
        reply = luna_call(SAM + 'getForegroundAppInfo', {}, timeout=6)
        if not reply.get('returnValue'):
            raise ValueError(reply.get('errorText') or 'returnValue false')
    except Exception as e:
        log('watchdog: application manager not answering (%r), resubscribing' % (e,))
        for sub in subs:
            sub.failed()
        return
    app = reply.get('appId')
    st.home_front = (app == LG_HOME)
    if app == LG_HOME and redirect_wanted(st) and not os.path.exists(PASS_FLAG):
        log('watchdog: LG home in front, redirecting')
        launch_launcher()


def kill_stale_subscriptions():
    """luna-send children survive a kill -9 of the daemon: remove them before subscribing again."""
    wanted = (SAM + 'getAppLifeEvents', SAM + 'listApps')
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open('/proc/%s/cmdline' % name, 'rb') as fh:
                argv = fh.read().split(b'\0')
        except OSError:
            continue
        if (len(argv) > 3 and argv[0].endswith(b'luna-send') and argv[1] == b'-i'
                and argv[2].decode('utf-8', 'replace') in wanted):
            try:
                os.kill(int(name), signal.SIGKILL)
                log('killed stale subscription process %s' % name)
            except OSError:
                pass


def daemon(boot):
    lock = open(LOCK_FILE, 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                              # already running
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    kill_stale_subscriptions()

    st = State()
    st.started = time.time()
    st.quiet = BOOT_QUIET if boot else 0.0
    st.usage = usage_load()

    # SIGTERM must wake the select() at once (Python restarts interrupted calls otherwise)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)

    def on_signal(_sig, _frm):
        st.stop = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log('daemon start pid=%d boot=%s' % (os.getpid(), boot))
    if not wait_for_sam(st):
        log('application manager not reachable, continuing anyway')
    first = refresh(timeout=15, wait=False)
    usage_flush(st.usage)                  # (re)publish for the launcher, creates the file on first run
    if boot and not paused():
        launch_launcher()
        log('launcher brought to front (boot)')

    life = Subscription(SAM + 'getAppLifeEvents', {'subscribe': True})
    applist = Subscription(SAM + 'listApps', {'subscribe': True})
    subs = (life, applist)
    voice = VoiceKeyWatch()
    mono = time.monotonic()
    refresh_at = mono + 60 if boot else 0.0          # store apps can be mounted late at boot
    if first == 'retry':
        refresh_at = mono + 10 if not refresh_at else min(refresh_at, mono + 10)
    watch_at = mono + 1.0
    first_applist = True

    while not st.stop:
        try:
            now = time.monotonic()
            for sub in subs:
                if sub.fd is None and now >= sub.retry_at:
                    try:
                        sub.start()
                        watch_at = min(watch_at, now + 1.0)      # re-check the state right after (re)subscribing
                    except OSError as e:
                        log('subscribe failed (%s): %s' % (sub.uri, e))
                        sub.failed()
            if voice.fd is None and now >= voice.retry_at:
                try:
                    voice.start()
                except OSError as e:
                    log('mic button watch failed: %s' % e)
                    voice.failed()
            # timers
            while st.burst and st.burst[0] <= now:
                st.burst.pop(0)
                if redirect_wanted(st):
                    launch_launcher()
            if st.allow_home and now - st.allow_since > ALLOW_MAX:
                st.allow_home = False
                log('LG home pass expired')
                if st.home_front and not paused():
                    launch_launcher()
            if nag_due(st) and now >= st.nag_at:
                st.nag_left -= 1
                st.nag_at = now + 0.8
                launch_launcher()
            if st.usage_dirty_at and now >= st.usage_dirty_at:
                st.usage_dirty_at = 0.0
                usage_flush(st.usage)
            if refresh_at and now >= refresh_at:
                refresh_at = 0.0
                if refresh(timeout=15, wait=False) == 'retry':
                    refresh_at = time.monotonic() + 10
            if now >= watch_at:
                watch_at = now + WATCHDOG_SECONDS
                watchdog(st, subs)
            reap_children()

            now = time.monotonic()
            waits = [5.0, max(watch_at - now, 0.05)]
            if st.burst:
                waits.append(max(st.burst[0] - now, 0.0))
            if nag_due(st):
                waits.append(max(st.nag_at - now, 0.0))
            if st.allow_home:
                waits.append(max(st.allow_since + ALLOW_MAX - now, 0.0))
            if refresh_at:
                waits.append(max(refresh_at - now, 0.0))
            if st.usage_dirty_at:
                waits.append(max(st.usage_dirty_at - now, 0.0))
            for sub in subs:
                if sub.fd is None:
                    waits.append(max(sub.retry_at - now, 0.05))
            if voice.fd is None:
                waits.append(max(voice.retry_at - now, 0.05))
            fds = [s.fd for s in subs if s.fd is not None] + [wake_r]
            if voice.fd is not None:
                fds.append(voice.fd)
            try:
                ready, _, _ = select.select(fds, [], [], min(waits))
            except (OSError, ValueError):
                ready = []
            if wake_r in ready:
                try:
                    os.read(wake_r, 64)
                except OSError:
                    pass
            if voice.fd is not None and voice.fd in ready:
                presses = voice.read_presses()
                if presses is None:
                    log('mic button watch ended, restarting')
                    voice.failed()
                elif presses:
                    on_voice_key(st, voice)
            for sub in subs:
                if sub.fd is None or sub.fd not in ready:
                    continue
                objs = sub.read()
                if objs is None:
                    log('subscription ended: %s' % sub.uri)
                    sub.failed()
                    continue
                for obj in objs:
                    if obj.get('returnValue') is False:      # luna-send stays alive after an error reply
                        log('subscription error (%s): %s' % (sub.uri, obj.get('errorText')))
                        sub.failed()
                        break
                    sub.backoff = 1.0
                    if sub is life:
                        on_life_event(st, obj)
                    elif first_applist:                      # the initial reply is the current state
                        first_applist = False
                    else:                                    # an app was installed/removed/updated: refresh soon
                        refresh_at = time.monotonic() + 3
        except Exception:
            log('loop error: ' + traceback.format_exc().replace('\n', ' | ')[-700:])
            time.sleep(1)

    for sub in subs:
        sub.stop()
    voice.stop()
    if st.usage_dirty_at:
        usage_flush(st.usage)
    log('daemon stop')
    return 0


def daemonize():
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir('/')
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.closerange(3, 1024)                    # drop every inherited descriptor (pipes, locks)


def running_pid():
    try:
        with open(LOCK_FILE) as fh:
            pid = int(fh.read().strip() or '0')
        with open('/proc/%d/cmdline' % pid, 'rb') as fh:
            if b'launcherd.py' in fh.read():
                return pid
    except (OSError, ValueError):
        pass
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'status'
    boot = '--boot' in argv
    if cmd == 'start':
        if running_pid():
            print('already running (pid %d)' % running_pid())
            return 0
        daemonize()
        return daemon(boot)
    if cmd == 'run':
        return daemon(boot)
    if cmd == 'stop':
        pid = running_pid()
        if not pid:
            print('not running')
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not running_pid():
                break
            time.sleep(0.1)
        print('stopped' if not running_pid() else 'still running (pid %d)' % pid)
        return 0
    if cmd == 'refresh':
        ok = refresh()
        print('ok' if ok is True else 'failed (see %s)' % LOG_FILE)
        return 0 if ok is True else 1
    if cmd == 'status':
        pid = running_pid()
        print(('running (pid %d)' % pid) if pid else 'not running')
        if not pid:
            print('redirect: inactive (daemon not running)')
        else:
            print('redirect: %s' % ('PAUSED (%s exists)' % OFF_FLAG if paused() else 'active'))
        top = sorted(usage_load().items(), key=lambda kv: -kv[1]['n'])[:8]
        print('usage (top, undecayed): ' + ', '.join('%s=%.1f' % (k, v['n']) for k, v in top))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
