#!/usr/bin/env python3
"""
relay.py -- PC side of the LR-FHSS outdoor range test.

Decodes the backscatter tag live from the Airspy (pyLR-FHSS LiveReceiver),
logs every packet locally, and relays them through this GitHub repo so the
phone app (GitHub Pages) can show them anywhere with mobile data. Everything
is outbound HTTPS/SSH to GitHub, so campus firewalls do not matter.

  branch `live`   packets.json, force-pushed as a single commit (PC -> phone)
  branch `phone`  track/<run>.json, distance-only GPS track (phone -> PC)

The PC merges the phone's track into its own log, so every packet gets the
tag's distance from the start point at the moment it was on air.

    python pc/relay.py                 # live, Airspy
    python pc/relay.py --simulate      # fake packets, no radio
    python pc/relay.py --remote git@github.com:<you>/<repo>.git

Needs: the pyLR-FHSS package (`lrfhss`) with SoapySDR + SoapyAirspy for live
mode, airspy_rx (carrier tone measurement), and SSH push access to the repo.
"""
import argparse
import bisect
import csv
import datetime as dt
import glob
import json
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REMOTE = 'git@github.com:ShayanMajumder/LR-FHSS_Online-Test.git'

# ---- receiver settings (same as pylrfhss_rx.py in the tag project)
CENTER_MHZ = 864.0            # carrier tone (LilyGO CW, integer-N, best tag dGamma in EU band)
FS_CAPTURE = 3e6              # Airspy Mini: 3 or 6 MS/s
GAIN = dict(lna=14, mix=10, vga=3)   # swept 2026-11-23: best SNR without ADC clipping
CLIP_PEAK = 0.45              # Airspy float IQ: ADC full scale shows as ~0.5
SUBCARRIER_HZ = 100e3         # tag's F_OFFSET
BW_KHZ = 136.72               # EU868 DR8
HEADER_REPLICAS = 3

PUSH_MIN_S = 8                # never push live/ faster than this
HEARTBEAT_S = 45              # push even without packets, so the phone sees the PC alive
MERGE_S = 20                  # pull the phone's track this often
KEEP_PACKETS = 150            # packets kept in packets.json
BIN_M = 25                    # distance bin for the results table
GUI_PORT = 8765               # local dashboard: http://localhost:8765


def iso(t):
    return dt.datetime.fromtimestamp(t).isoformat(timespec='milliseconds')


def log(msg):
    print('%s  %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ------------------------------------------------------------------ git relay
class Relay:
    """A bare clone used only through plumbing: no working tree, no history
    growth on `live` (each push is one parentless commit)."""

    def __init__(self, remote, cache):
        self.remote, self.dir = remote, cache
        if not os.path.exists(os.path.join(cache, 'HEAD')):
            os.makedirs(cache, exist_ok=True)
            self.git('init', '--bare', '-q')
            self.git('remote', 'add', 'origin', remote)
        else:
            self.git('remote', 'set-url', 'origin', remote)
        self.pushes = 0
        self._ensure_phone_branch()

    def git(self, *args, data=None, check=True):
        r = subprocess.run(['git', '--git-dir', self.dir] + list(args), input=data,
                           capture_output=True, timeout=60)
        if check and r.returncode:
            raise RuntimeError('git %s: %s' % (args[0], r.stderr.decode(errors='replace').strip()))
        return r.stdout.decode(errors='replace').strip()

    def _commit(self, files, msg):
        """files: {path: bytes} (flat or one level of dirs) -> commit sha."""
        entries = {}
        for path, data in files.items():
            blob = self.git('hash-object', '-w', '--stdin', data=data)
            parts = path.split('/')
            entries.setdefault(parts[0] if len(parts) > 1 else '', []).append((parts[-1], blob))
        top = ['100644 blob %s\t%s' % (b, n) for n, b in entries.pop('', [])]
        for d, items in entries.items():
            sub = self.git('mktree', data=('\n'.join('100644 blob %s\t%s' % (b, n) for n, b in items) + '\n').encode())
            top.append('040000 tree %s\t%s' % (sub, d))
        tree = self.git('mktree', data=('\n'.join(top) + '\n').encode())
        env_msg = msg.encode()
        return self.git('commit-tree', tree, data=env_msg)

    def _ensure_phone_branch(self):
        out = self.git('ls-remote', '--heads', 'origin', 'phone')
        if out:
            return
        readme = (b'# phone branch\n\nThe range-test app uploads track/<run>.json here: distance-only '
                  b'GPS tracks ([epoch_ms, dist_m, bearing_deg, acc_m]), no coordinates.\n')
        c = self._commit({'README.md': readme}, 'create phone branch')
        self.git('push', '-q', 'origin', '%s:refs/heads/phone' % c)
        log('created branch phone on %s' % self.remote)

    def push_live(self, payload):
        data = json.dumps(payload, separators=(',', ':')).encode()
        c = self._commit({'packets.json': data,
                          'README.md': b'# live branch\n\nForce-pushed by pc/relay.py: latest decoded packets.\n'},
                         'live %s' % iso(payload['updated']))
        self.git('push', '-q', '-f', 'origin', '%s:refs/heads/live' % c)
        self.pushes += 1
        if self.pushes % 200 == 0:
            self.git('gc', '-q', '--prune=now', check=False)

    def phone_tracks(self):
        """{name: parsed json} for every track/*.json on the phone branch."""
        self.git('fetch', '-q', 'origin', '+refs/heads/phone:refs/remotes/origin/phone')
        names = self.git('ls-tree', '--name-only', 'refs/remotes/origin/phone', 'track/', check=False).split()
        out = {}
        for n in names:
            if n.endswith('.json'):
                try:
                    out[n] = json.loads(self.git('show', 'refs/remotes/origin/phone:' + n))
                except (ValueError, RuntimeError):
                    pass
        return out


# ------------------------------------------------------------------ session
class Session:
    def __init__(self, logdir, period):
        self.lock = threading.Lock()
        self.run = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.dir = os.path.join(logdir, self.run)
        os.makedirs(self.dir, exist_ok=True)
        self.period = period
        self.t0 = time.time()
        self.packets = []
        self.rx = {}
        self.changed = threading.Event()
        self.track = []          # merged phone fixes: (t_s, dist, brg, acc, phone_run)
        f = open(os.path.join(self.dir, 'packets.csv'), 'w', newline='')
        self._pk = (f, csv.writer(f))
        self._pk[1].writerow(['id', 'air_time', 'air_time_unix', 'decoded_time', 'payload_hex',
                              'temp_c', 'snr_db'])
        f.flush()

    def add(self, payload, air_t, snr):
        temp = None
        if len(payload) == 2:
            v = int.from_bytes(payload, 'big', signed=True)
            temp = None if v == -32768 else v/100
        snr = None if snr is None or snr != snr else round(float(snr), 1)
        with self.lock:
            p = dict(id=len(self.packets) + 1, t=round(air_t, 3), hex=payload.hex(), temp=temp, snr=snr)
            self.packets.append(p)
            f, w = self._pk
            w.writerow([p['id'], iso(air_t), '%.3f' % air_t, iso(time.time()), p['hex'],
                        '' if temp is None else '%.2f' % temp, '' if snr is None else snr])
            f.flush()
        self.changed.set()
        d = self.dist_at(air_t)
        log('[pkt %d] %s  SNR %s  %s%s' % (p['id'], p['hex'], snr,
                                           '' if temp is None else '%.2f C' % temp,
                                           '' if d is None else '  ~%.0f m (phone)' % d[0]))
        return p

    def payload(self):
        with self.lock:
            return dict(run=self.run, updated=time.time(), period_s=self.period, rx=dict(self.rx),
                        stats=dict(received=len(self.packets), uptime_s=round(time.time() - self.t0),
                                   started=iso(self.t0)),
                        packets=self.packets[-KEEP_PACKETS:])

    # --- merge with the phone's distance track
    def set_track(self, tracks):
        fx = []
        for name, tr in tracks.items():
            for f in tr.get('fixes', []):
                t = f[0]/1000.0
                if t >= self.t0 - 600:
                    fx.append((t, f[1], f[2], f[3], tr.get('run', name)))
        fx.sort()
        uniq = []                                   # chunked uploads can overlap
        for f in fx:
            if not uniq or f[0] != uniq[-1][0]:
                uniq.append(f)
        marks = {}
        for name, tr in tracks.items():
            for m in tr.get('marks', []):
                if m[0]/1000.0 >= self.t0 - 600:
                    marks[m[0]] = (m[0]/1000.0, m[1], m[2], tr.get('run', name))
        with self.lock:
            self.track = uniq
            self.marks = sorted(marks.values())
            self.merged_at = time.time()
        self.write_merged()
        self.write_results()

    def dist_at(self, t, max_gap=15.0):
        tr = self.track
        if not tr:
            return None
        i = bisect.bisect_left(tr, (t,))
        a = tr[i - 1] if i > 0 else None
        b = tr[i] if i < len(tr) else None
        if a and b and b[0] - a[0] < max_gap and a[4] == b[4]:
            w = (t - a[0])/(b[0] - a[0]) if b[0] > a[0] else 0
            return (a[1] + w*(b[1] - a[1]), b[2] if w > .5 else a[2], max(a[3], b[3]), a[4])
        n = min((x for x in (a, b) if x), key=lambda x: abs(x[0] - t))
        return n[1:] if abs(n[0] - t) <= max_gap else None

    def write_merged(self):
        with self.lock:
            pk = list(self.packets)
        rows = []
        for p in pk:
            d = self.dist_at(p['t'])
            rows.append([p['id'], iso(p['t']), p['t'], p['hex'], p['temp'], p['snr'],
                         '' if d is None else '%.1f' % d[0], '' if d is None else d[1],
                         '' if d is None else d[2], '' if d is None else d[3]])
        with open(os.path.join(self.dir, 'packets_with_distance.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['id', 'air_time', 'air_time_unix', 'payload_hex', 'temp_c', 'snr_db',
                        'dist_m', 'bearing_deg', 'gps_acc_m', 'phone_run'])
            w.writerows(rows)
        with open(os.path.join(self.dir, 'phone_track.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'time_unix', 'dist_m', 'bearing_deg', 'acc_m', 'phone_run'])
            for t, d, b, a, r in self.track:
                w.writerow([iso(t), '%.3f' % t, d, b, a, r])
        with open(os.path.join(self.dir, 'marks.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'time_unix', 'dist_m', 'note', 'phone_run'])
            for t, d, n, r in getattr(self, 'marks', []):
                w.writerow([iso(t), '%.3f' % t, d, n, r])

    # --- range results: packet delivery and SNR per distance bin
    def results(self):
        with self.lock:
            tr, pk = list(self.track), list(self.packets)
        bins = {}
        b = lambda d: bins.setdefault(int(d // BIN_M), dict(secs=0.0, rx=0, snrs=[]))
        for (t0, d0, *_), (t1, *_r) in zip(tr, tr[1:]):          # time spent at each distance
            if t0 >= self.t0:
                b(d0)['secs'] += min(t1 - t0, self.period)
        rows, with_d = [], []
        for p in pk:
            d = self.dist_at(p['t'])
            if d is not None:
                with_d.append((p, d[0]))
                bb = b(d[0])
                bb['rx'] += 1
                if p['snr'] is not None:
                    bb['snrs'].append(p['snr'])
        for k in sorted(bins):
            v = bins[k]
            exp = v['secs']/self.period
            rows.append(dict(d0=k*BIN_M, d1=(k + 1)*BIN_M, secs=round(v['secs'], 1), exp=round(exp, 1),
                             rx=v['rx'], pdr=round(min(1.0, v['rx']/exp), 3) if exp >= 1 else None,
                             snr_mean=round(sum(v['snrs'])/len(v['snrs']), 1) if v['snrs'] else None,
                             snr_min=min(v['snrs']) if v['snrs'] else None))
        good = [r for r in rows if r['pdr'] is not None and r['pdr'] >= 0.9]
        summary = dict(run=self.run, started=iso(self.t0), updated=iso(time.time()),
                       carrier_mhz=CENTER_MHZ, tag_period_s=self.period, bin_m=BIN_M,
                       received=len(pk), expected=round((time.time() - self.t0)/self.period),
                       packets_with_distance=len(with_d),
                       farthest_packet_m=round(max((d for _, d in with_d), default=0), 1) or None,
                       farthest_bin_pdr90_m=good[-1]['d1'] if good else None,
                       receiver=dict(self.rx))
        return rows, summary

    def write_results(self):
        rows, summary = self.results()
        with open(os.path.join(self.dir, 'results_by_distance.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['dist_from_m', 'dist_to_m', 'time_s', 'expected', 'received', 'pdr',
                        'snr_mean_db', 'snr_min_db'])
            for r in rows:
                w.writerow([r['d0'], r['d1'], r['secs'], r['exp'], r['rx'],
                            '' if r['pdr'] is None else r['pdr'],
                            '' if r['snr_mean'] is None else r['snr_mean'],
                            '' if r['snr_min'] is None else r['snr_min']])
        with open(os.path.join(self.dir, 'summary.json'), 'w') as f:
            json.dump(summary, f, indent=2)

    # --- everything the local dashboard draws
    def gui_state(self):
        rows, summary = self.results()
        now = time.time()
        with self.lock:
            pk = list(self.packets)
            tr = list(self.track)
            marks = list(getattr(self, 'marks', []))
        packets = []
        for p in pk[-400:]:
            d = self.dist_at(p['t'])
            packets.append(dict(p, dist=None if d is None else round(d[0], 1),
                                acc=None if d is None else d[2]))
        step = max(1, len(tr)//600)
        last = tr[-1] if tr else None
        lag, win = 6.0, 120.0
        n = sum(1 for p in pk if now - lag - win <= p['t'] < now - lag)
        span = min(win, max(0.0, now - lag - self.t0))
        return dict(now=now, t0=self.t0, run=self.run, dir=self.dir, carrier_mhz=CENTER_MHZ, period=self.period,
                    rx=dict(self.rx), push=getattr(self, 'push_info', {}),
                    merged_at=getattr(self, 'merged_at', None),
                    phone=None if not last else dict(t=last[0], dist=last[1], brg=last[2], acc=last[3]),
                    pdr2=(min(1.0, n/(span/self.period)) if span >= 2*self.period else None),
                    packets=packets, track=[[round(t, 1), d] for t, d, *_ in tr[::step]],
                    marks=[dict(t=t, dist=d, note=nt) for t, d, nt, _ in marks],
                    bins=rows, summary=summary)


# ------------------------------------------------------------------ receivers
def measure_tone(cache):
    import numpy as np
    path = os.path.join(cache, 'tone.bin')
    subprocess.run(['airspy_rx', '-r', path, '-f', '%.6f' % CENTER_MHZ, '-a', str(int(FS_CAPTURE)),
                    '-t', '0', '-l', str(GAIN['lna']), '-m', str(GAIN['mix']), '-v', str(GAIN['vga']),
                    '-n', str(int(1.4*FS_CAPTURE))],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    x = np.fromfile(path, np.complex64)[int(0.3*FS_CAPTURE):]
    os.remove(path)
    N = 1 << 20
    k = int(np.argmax(np.abs(np.fft.fft(x[:N]*np.hanning(N)))))
    return float(np.fft.fftfreq(N, 1/FS_CAPTURE)[k]), float(max(np.abs(x.real).max(), np.abs(x.imag).max()))


def start_airspy(sess, cache):
    from lrfhss import config
    log('measuring carrier tone ...')
    tone, peak = measure_tone(cache)
    log('tone %+.0f Hz from %.3f MHz, peak %.3f%s' % (tone, CENTER_MHZ, peak,
                                                      '  ** CLIPPING: lower GAIN **' if peak > CLIP_PEAK else ''))
    config.retune(BW_KHZ*1e3, hdr_count=HEADER_REPLICAS, fs_capture=FS_CAPTURE)
    from lrfhss.sdr import LiveReceiver

    class TimedReceiver(LiveReceiver):
        def packets(self):
            """LiveReceiver.__iter__ that also yields the on-air time."""
            while not self._stop.is_set():
                try:
                    payloads = self._decoded.get(timeout=0.5)
                except queue.Empty:
                    self._check_alive()
                    continue
                for payload, t, key, snr in payloads:
                    if any(k == key and abs(t - tk) < self.packet_s for k, tk in self._recent):
                        self._stats['duplicates'] += 1
                        continue
                    self._recent.append((key, t))
                    self._stats['packets'] += 1
                    yield payload, t, snr

    off = SUBCARRIER_HZ + tone
    r = TimedReceiver(CENTER_MHZ*1e6 + off, 'airspy',
                      gain=dict(LNA=GAIN['lna'], MIX=GAIN['mix'], VGA=GAIN['vga']), offset_hz=off)
    t0 = time.time()
    sess.t0 = t0
    sess.rx.update(mode='airspy', ok=True, tone_hz=round(tone), peak=round(peak, 3),
                   gain='L%d/M%d/V%d' % (GAIN['lna'], GAIN['mix'], GAIN['vga']))

    def loop():
        try:
            for payload, t, snr in r.packets():
                sess.add(payload, t0 + t, snr)
        except Exception as e:
            sess.rx.update(ok=False, error=str(e))
            sess.changed.set()
            log('receiver stopped: %s' % e)

    def health():
        while True:
            s = r.stats
            sess.rx.update(overflows=s['overflows'], overruns=s['overruns'], windows=s['windows'])
            time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=health, daemon=True).start()
    return r


def start_simulator(sess):
    sess.rx.update(mode='simulate', ok=True, gain='-', tone_hz=0)

    def loop():
        n = 0
        while True:
            time.sleep(sess.period)
            n += 1
            air = time.time() - 1.0
            d = sess.dist_at(air)
            dist = d[0] if d else 20.0
            snr = 12 - 20*math.log10(max(dist, 5)/20) + random.gauss(0, 1)
            if snr < -6 + random.gauss(0, 2):
                continue
            sess.add(int(1950 + 30*math.sin(n/10)).to_bytes(2, 'big', signed=True), air, snr)

    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--remote', default=DEFAULT_REMOTE, help='repo SSH URL (push access)')
    ap.add_argument('--logdir', default=os.path.join(os.path.dirname(HERE), 'logs'))
    ap.add_argument('--cache', default=os.path.expanduser('~/.cache/lrfhss_online_test'),
                    help='bare clone used for pushing')
    ap.add_argument('--period', type=float, default=5.0, help='tag packet period, s')
    ap.add_argument('--simulate', action='store_true', help='fake packets, no radio')
    ap.add_argument('--centre', type=float, default=CENTER_MHZ, help='carrier frequency, MHz')
    ap.add_argument('--gui-port', type=int, default=GUI_PORT, help='local dashboard port')
    ap.add_argument('--no-browser', action='store_true', help='do not open the dashboard')
    a = ap.parse_args()
    globals()['CENTER_MHZ'] = a.centre

    relay = Relay(a.remote, a.cache)
    sess = Session(a.logdir, a.period)
    log('run %s, logs in %s' % (sess.run, sess.dir))
    rcv = start_simulator(sess) if a.simulate else start_airspy(sess, a.cache)

    def pusher():
        last = 0.0
        while True:
            sess.changed.wait(timeout=HEARTBEAT_S)
            wait = PUSH_MIN_S - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            sess.changed.clear()
            try:
                relay.push_live(sess.payload())
                last = time.time()
                sess.rx.pop('push_error', None)
                sess.push_info = dict(t=last, ok=True, count=relay.pushes)
            except Exception as e:
                log('push failed: %s' % e)
                sess.rx['push_error'] = str(e)[:200]
                sess.push_info = dict(t=time.time(), ok=False, error=str(e)[:200], count=relay.pushes)
                time.sleep(5)

    def merger():
        seen = None
        while True:
            try:
                sess.set_track(relay.phone_tracks())
                if sess.track and sess.track[-1][0] != seen:
                    t, d, b, acc, r = sess.track[-1]
                    seen = t
                    log('phone: %.0f m from start (bearing %s, ±%s m), fix %.0f s old'
                        % (d, b, acc, time.time() - t))
            except Exception as e:
                log('merge failed: %s' % e)
            time.sleep(MERGE_S)

    threading.Thread(target=pusher, daemon=True).start()
    threading.Thread(target=merger, daemon=True).start()
    start_gui(sess, a.gui_port, not a.no_browser)
    log('relaying to %s (branch live). Ctrl-C to stop.' % a.remote)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sess.set_track(relay.phone_tracks())
        except Exception:
            pass
        sess.write_results()
        if rcv:
            log(rcv.summary())
            rcv.close()
        log('logs: %s' % sess.dir)


# ------------------------------------------------------------------ local GUI
def start_gui(sess, port, open_browser):
    """Dashboard on http://localhost:<port> (this PC only)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    page = os.path.join(HERE, 'dashboard.html')

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith('/api/state'):
                body, ctype = json.dumps(sess.gui_state()).encode(), 'application/json'
            elif self.path in ('/', '/index.html'):
                body, ctype = open(page, 'rb').read(), 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    except OSError as e:
        log('dashboard not started (port %d: %s)' % (port, e))
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = 'http://localhost:%d/' % port
    log('dashboard: %s' % url)
    if open_browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


if __name__ == '__main__':
    main()
