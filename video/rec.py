"""Record the Spine app for the video: each step of a recorded check lands on a narration word.

The checks are real recorded runs (demo_runs/), replayed event by event; the app's replay banner stays on screen.
"""
import json, re, shutil, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
URL = "http://localhost:8791/"
VO, W, OFF = HERE / "vo", HERE / "work", 0.45
OUTW, OUTH = 1920, 1080
HELP = """(() => {
 document.body.style.zoom = 1.18;
 const st = document.createElement('style'); document.head.appendChild(st);
 window.glow = sel => { st.textContent += sel + '{outline:3px solid #d6402e !important;outline-offset:4px;border-radius:8px}'; };
 window.unglow = () => { st.textContent = ''; };
 window.go = (sel, block = 'start') => document.querySelector(sel).scrollIntoView({behavior: 'instant', block});
 const c = document.createElement('div'); c.id = 'vcur';
 c.innerHTML = '<svg width="34" height="34" viewBox="0 0 24 24"><path d="M4 2l15 11-6.5 1.3L16 21l-3 1.4-3.5-6.8L4 20z" fill="#1d1b18" stroke="#fff" stroke-width="1.5"/></svg>';
 c.style.cssText = 'position:fixed;left:70%;top:60%;z-index:9999;pointer-events:none;transition:left .7s cubic-bezier(.3,.8,.3,1),top .7s cubic-bezier(.3,.8,.3,1)';
 document.body.appendChild(c);
 window.point = sel => { const r = document.querySelector(sel).getBoundingClientRect();
   c.style.left = (r.left + Math.min(r.width / 2, 60)) + 'px'; c.style.top = (r.top + r.height / 2) + 'px'; };
 window.tap = sel => { point(sel); setTimeout(() => { c.animate([{transform: 'scale(1)'}, {transform: 'scale(.8)'}, {transform: 'scale(1)'}], 220); }, 750); };
 window.rec = async id => {
   const d = await (await fetch('/api/runs/' + id)).json();
   window.EV = d.events; reset(); handle(d.events[0]); hidecur();
   const rp = document.getElementById('replay'); rp.hidden = false;
   rp.textContent = 'Replay of recorded check ' + id + ': real Nemotron answers, no new model calls';
 };
 // show one recorded event (its step lights up first)
 const STEP = {stance: 'stance', rewrites: 'rewrite', answer: 'answer', verdict: 'judge'};
 window.to = (type, col) => { const e = EV.find(x => x.type === type && (!col || x.column === col));
   if (!e) return; if (STEP[type] && type !== 'verdict') setStep(STEP[type]); handle(e);
   if (type === 'verdict') { const d = EV.find(x => x.type === 'done'); if (d) handle(d); } };
 window.hidecur = () => { c.style.opacity = 0; };
})()"""

SCENES = {
    "01-hook": ("scrollTo(0, 0)", [
        ("agreed", "glow('.hero h1')"),
        ("spine", "unglow(); tap('#chips .chip:nth-child(1)'); setTimeout(() => document.querySelector('#chips .chip').click(), 800)"),
        ("flips", "glow('#go')"),
    ]),
    "02-demo": ("go('.recorded')", [
        ("heres", "tap('#runs li:nth-child(1) a')"),
        ("quitting", "rec('199ebb76e4f6').then(() => { to('stance'); setTimeout(() => go('#steps'), 300); })"),
        ("great", "glow('#m-original mark')"),
        ("fantastic", "unglow(); to('answer', 'original'); glow('#a-original p:first-child')"),
        ("flipped", "unglow(); to('rewrites'); glow('#m-flipped mark')"),
        ("not", "unglow(); to('answer', 'neutral'); to('answer', 'flipped'); glow('#a-flipped p:first-child')"),
        ("spineless", "unglow(); to('verdict')"),
        ("sure", "to('pushback'); setTimeout(() => go('#pushcard', 'center'), 200)"),
        ("folds", "glow('#push-res')"),
    ]),
    "03-ultra": ("go('.recorded')", [
        ("same", "tap('#runs li:nth-child(2) a'); setTimeout(() => rec('8fcf7edc0233').then(() => { to('answer', 'flipped'); to('answer', 'neutral'); to('answer', 'original'); go('#cols'); }), 800)"),
        ("high", "glow('#a-original p:first-child'); glow('#a-flipped p:first-child')"),
        ("push", "unglow(); to('verdict'); to('pushback'); setTimeout(() => go('#pushcard', 'center'), 300)"),
        ("steady", "go('#verdict')"),
    ]),
    "04-trivia": ("go('.recorded')", [
        ("which", "tap('#runs li:nth-child(3) a'); setTimeout(() => rec('cac11e509f8b').then(() => { to('stance'); go('#steps'); }), 800)"),
        ("russia", "glow('#m-original mark')"),
        ("canada", "unglow(); to('answer', 'flipped'); to('answer', 'neutral'); to('answer', 'original'); to('verdict')"),
        ("sure", "to('pushback'); setTimeout(() => go('#pushcard', 'center'), 200)"),
        ("caves", "glow('#push-res')"),
    ]),
}
norm = lambda w: re.sub(r"[^a-z0-9%$]", "", w.lower())


def cue(words, c):
    w, n = (c.split("#") + ["1"])[:2]
    hits = [x["t"] for x in words if norm(x["w"]) == w] or [x["t"] for x in words if norm(x["w"]).startswith(w)]
    if len(hits) < int(n):
        raise SystemExit(f"cue {c!r} missing in narration")
    return hits[int(n) - 1]


dur = lambda p: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                     capture_output=True, text=True).stdout)
W.mkdir(exist_ok=True)
names = [a for a in sys.argv[1:] if a in SCENES] or list(SCENES)
with sync_playwright() as p:
    b = p.chromium.launch()
    for name in names:
        setup, steps = SCENES[name]
        words = json.loads((VO / f"{name}.words.json").read_text(encoding="utf-8"))
        d = OFF + dur(VO / f"{name}.mp3") + 0.7
        pg = b.new_page(viewport={"width": 1440, "height": 810}, device_scale_factor=4 / 3, color_scheme="light")
        pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(1500); pg.evaluate("document.fonts.ready")
        pg.evaluate(HELP); pg.evaluate("document.documentElement.style.scrollBehavior='auto'; Element.prototype._siv = Element.prototype.scrollIntoView; Element.prototype.scrollIntoView = function (o) { this._siv(Object.assign({}, o, {behavior: 'instant'})); }; void 0"); pg.evaluate(setup)
        pg.wait_for_timeout(900)
        tl = "[" + ",".join(f"[{cue(words, c) + OFF:.2f}, () => {{ {js} }}]" for c, js in steps) + "]"
        pg.evaluate(f"(tl => tl.forEach(([t, f]) => setTimeout(f, t * 1000)))({tl})")
        fdir = W / f"rec_{name}"; shutil.rmtree(fdir, ignore_errors=True); fdir.mkdir()
        t0, frames = time.perf_counter(), []
        while (t := time.perf_counter() - t0) < d:
            fp = fdir / f"{len(frames):05d}.jpg"; fp.write_bytes(pg.screenshot(type="jpeg", quality=93)); frames.append((t, fp))
        with (fdir / "list.txt").open("w") as fh:
            for i, (t, fp) in enumerate(frames):
                fh.write(f"file '{fp.as_posix()}'\nduration {(frames[i + 1][0] if i + 1 < len(frames) else d) - t:.4f}\n")
            fh.write(f"file '{frames[-1][1].as_posix()}'\n")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(fdir / "list.txt"),
                        "-vf", f"fps=30,scale={OUTW}:{OUTH}:flags=lanczos,format=yuv420p", "-t", f"{d:.3f}",
                        "-c:v", "libx264", "-crf", "16", str(W / f"rec_{name}.mp4")], check=True)
        print(f"  {name:10} {d:5.1f}s {len(frames)} frames ({len(frames) / d:.1f} fps)"); pg.close()
    b.close()
