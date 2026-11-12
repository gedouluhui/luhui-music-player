/* ==========================================================================
   LuHui音乐播放器 — 前端逻辑
   与本地 Python 服务对话：/api/* ；音频走 /api/stream/<id>（支持 Range 拖拽）
   ========================================================================== */
'use strict';

/* ------------------------------------------------------------------ 小工具 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
/* 图标只有一个入口：所有动态 innerHTML 都用 icon('name')，名字对应 index.html 里的
   <symbol id="i-name">。菜单条目还在用旧字形字符串（▶ ⤨ …），glyphIcon 负责翻译，
   这样几十处菜单定义不用逐个改，也不会漏掉某个没翻译的字形。 */
const icon = (name, cls) => `<svg class="ic${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const GLYPH_ICONS = {
  '▶': 'play', '⤨': 'shuffle', '⌂': 'folder', '♫': 'music', '♪': 'music',
  '＋': 'plus', '✕': 'x', '⌕': 'search', '▣': 'select', '♥': 'heart', '♡': 'heart',
  '⋯': 'more', '📌': 'pin', '↻': 'refresh', '⇥': 'arrow-right', '⟳': 'repeat',
  '🔂': 'repeat-one', '🔊': 'volume-high', '🔉': 'volume-med', '🔈': 'volume-low',
  '🔇': 'volume-x', '⤢': 'expand', '◷': 'timer', '≡': 'queue', '⚙': 'settings',
  '◍': 'artist', '✦': 'radio', '◈': 'home', '↺': 'clock',
  '⊘': 'ban', 'ⓘ': 'info', '⧉': 'copy', '↑': 'up', '↓': 'down', '✎': 'edit',
  '⇩': 'download', '🗑': 'trash', '↥': 'upload', '词': 'lyrics', '↶': 'undo',
  '✂': 'cut', '⌥': 'paste', '☰': 'copy',
};
/* 菜单条目既可以写旧字形（'▶'），也可以直接写图标名（'reveal'）。直接写名字时查一次
   sprite 里有没有对应的 symbol：有就渲染，没有才原样当文字 —— 这样新菜单不必再为一个
   图标回来改这张翻译表，而漏写名字也不会在菜单里冒出一串英文。 */
const glyphIcon = (g) => (GLYPH_ICONS[g]
  ? icon(GLYPH_ICONS[g])
  : (document.getElementById('i-' + g) ? icon(g) : (g || '')));
/* 封面地址失效时的统一兜底：<img> 自己不会消失，浏览器会把破图图标直接留在卡片/hero 上。
   error 不冒泡，所以用捕获阶段；按容器换成和「本来就没有封面」一样的占位，
   避免同一张专辑在列表是一个占位、点进去是碎图。 */
document.addEventListener('error', (e) => {
  const img = e.target;
  if (!img || img.tagName !== 'IMG' || img.dataset.broken) return;
  img.dataset.broken = '1';
  img.style.visibility = 'hidden';
  const ghost = img.closest('.np-cover, .p-cover, .fs-cover');
  if (ghost) { ghost.classList.add('noart'); return; }   // 这几个容器自带线性音符底纹，撤掉碎图即可
  const art = img.closest('.card-art, .hero-art');
  if (art) {
    if (!art.querySelector('.art-placeholder')) {
      const ph = el('div', 'art-placeholder');
      ph.innerHTML = icon(art.classList.contains('round') ? 'artist' : 'disc');
      art.appendChild(ph);
    }
    return;
  }
  const match = img.closest('.match-art');
  if (match) {
    img.remove();
    match.innerHTML = icon(match.classList.contains('round') ? 'artist' : 'disc');
    return;
  }
  if (img.classList.contains('mini-cover')) {
    const ph = el('span', 'mini-cover');
    ph.style.display = 'grid'; ph.style.placeItems = 'center';
    ph.innerHTML = icon('music');
    img.replaceWith(ph);
  }
}, true);
// 系统级「减少动态效果」：JS 发起的 scrollIntoView({behavior:'smooth'}) 不受 CSS 那条
// scroll-behavior:auto !important 约束（那是 CSS 属性，管不到显式传进去的参数），所以
// 凡是自己调 smooth 滚动的地方都先问一句这个。
const rmq = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
// 滑块轨道上「已走过」的那一段用 --fill 画（见 app.css 的 range 规则）。
// 原生 range 没法用 CSS 直接量出百分比，只能在值变化时写进自定义属性。
function paintRange(el) {
  const min = +el.min || 0, max = +el.max || 100, v = +el.value || 0;
  const p = max > min ? clamp((v - min) / (max - min), 0, 1) : 0;
  el.style.setProperty('--fill', (p * 100).toFixed(2) + '%');
}
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTime = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`;
};
const fmtLong = (s) => {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.round(s % 3600 / 60);
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
};
const fmtBytes = (n) => {
  n = Math.max(0, +n || 0);
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
};
const debounce = (fn, ms = 200) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const uniq = (a) => Array.from(new Set(a));

/* 列表一次只取/渲染这么多行。口径来自实测：2000 首的歌单整页 JSON 1.3 MB、
   2000 个 DOM 行（每行 5 个监听器），而首屏可视区按 --row-h 46px 只装得下十几行。
   300 行 ≈ 14 屏，滚到底由 setupPaging 再补一页，用户永远看不到「加载」这件事。
   点「播放全部」不受这个数限制：那是显式要整串，走 fetchAll() 一次取满（服务端 cap 5000）。 */
const LIST_PAGE = 300;
const FETCH_ALL = 5000;

/* 变速区间。椒盐音乐能做到 4×，本地内核这条链路的实际上限是 Chromium 的
   playbackRate 上限（远超 4），所以 4× 不是妥协值；0.5× 以下人声会开始出「跳字」，
   留给需要的人从设置页外面调（暂无入口）。两处 clamp 必须用同一对常量：
   applySettings 和设置页的滑块以前各写各的，改一档就会漂。 */
const RATE_MIN = 0.5;
const RATE_MAX = 4.0;
/* 歌词滚动的四档毫秒数（存进 settings.lyric_scroll_ms）。档位而不是自由滑条：
   缓动时间低于 200ms 肉眼看不出「在滚」，高于 900ms 会跟不上唱速。 */
const LY_SPEEDS = [[0, '不滚动（直接跳）'], [240, '快'], [460, '标准'], [800, '慢']];

/* 均衡器：10 段 BiquadFilter 的中心频率（Hz），等比铺在 31Hz–16kHz 之间。
   第一段 lowshelf 兜住低频、最后一段 highshelf 兜住高频，中间 peaking，
   这是图形 EQ 的常规做法（ shelves 在外两端才不会在截止点附近互相打架）。 */
const EQ_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const EQ_TYPE = ['lowshelf', ...Array(8).fill('peaking'), 'highshelf'];
const EQ_RANGE = 12;          // dB，每段的推拉行程
const EQ_PRESETS = {
  flat: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  rock: [5, 4, 3, 1, -1, -1, 2, 4, 5, 5],
  pop: [-2, 0, 2, 4, 5, 4, 2, 0, -1, -1],
  jazz: [3, 2, 1, 2, -1, -1, 1, 2, 3, 3],
  classical: [4, 3, 2, 0, 0, 0, 1, 3, 4, 4],
  dance: [6, 5, 2, 0, -1, 2, 5, 6, 5, 3],
  vocal: [-2, -1, 0, 2, 5, 6, 5, 2, 0, -1],
  bass: [9, 8, 6, 3, 0, -1, -2, -2, -1, 0],
  treble: [-2, -2, -1, 0, 1, 3, 5, 7, 8, 7],
};
const EQ_PRESET_NAMES = {
  flat: '平直', rock: '摇滚', pop: '流行', jazz: '爵士', classical: '古典',
  dance: '舞曲', vocal: '人声', bass: '低音增强', treble: '高音增强',
};
// 「自定义」不是一条曲线，是一个状态：手拖过任意一段之后曲线就不属于任何预设了。
EQ_PRESETS.custom = [];

async function api(path, opts = {}) {
  const o = { headers: { 'Content-Type': 'application/json' }, ...opts };
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const r = await fetch('/api/' + path, o);
  const ct = r.headers.get('Content-Type') || '';
  const data = ct.includes('json') ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok && !data.error) throw new Error(data || `HTTP ${r.status}`);
  return data;
}

/* ==========================================================================
   全局状态
   ========================================================================== */
const S = {
  settings: {},
  libraries: [],
  playlists: [],
  stats: { tracks: 0 },
  app: { native: false, pick_folder: false, name: 'LuHui音乐播放器' },
  queue: [],            // 播放队列（track 对象）
  qi: -1,               // 当前队列下标
  context: '',          // 队列来源描述
  track: null,          // 当前曲目
  pos: 0,               // 秒
  playing: false,
  lyric: { lines: [], i: -1, source: '', plain: false },
  view: 'home',
  viewState: {},
  history: [],          // 路由历史
  scanTimer: 0,
  sleepAt: 0,
  focusMode: false,
  shuffleOrder: [],
  playHist: [],         // 真实播放过的队列下标（随机模式下「上一曲」要回到刚才那首）
  _metaSeq: 0,          // renderMeta 的并发序号：切歌时只认最后一次
};
const audioA = $('#audio');
const audioB = $('#audio2');
/* 双元素：audio 永远是「正在计时 / 正在出声」的那一只，audio2 是待机预载的那一只。
   换曲交接时两只对调，而不是另设一个「当前元素」指针 —— tools/clock_probe.py 按字面
   `audio.currentTime` 认产品那把时钟尺，指针会让它失明。见下面「无缝衔接」一节。 */
let audio = audioA;
let audio2 = audioB;
window.Salt = S;

/* ==========================================================================
   播放引擎：chromium（默认，<audio>+WebAudio）| native（服务端 ffmpeg 浮点解码→WASAPI）
   --------------------------------------------------------------------------
   两条档共用同一套 UI（进度条 / 时间 / 歌词 / 频谱 / 自动连播），区别只在「谁在计时、
   谁在出数」：
     · chromium：进度读 audio 元素，均衡器/频谱在前端 WebAudio 图里算。
     · native：audio 元素整趟不参与（不赋 src、不出声）；位置 / 是否播完读
       /api/native/state，频谱读 /api/native/spectrum，控制 POST /api/native。
   服务端 _native_dsp / _native_out_format 读的就是同一份 S.settings，所以均衡器 /
   音量 / 声像 / 响度 / 淡入 / 倍速在两条档都生效，只是「算在哪一侧」不同（浏览器档
   WebAudio，原生档服务端 numpy）。
   默认档一行都不进 native 分支 —— 那条被量过账的 chromium 链路（闲置≈0 %core、帧预算、
   缓冲区间、错误重试）保持原样。native 只在用户显式选它时才接管，native 档下：
     · 只有「正在播」才轮询 state（约 4.5 Hz），暂停/停止就停轮询 —— 不把闲置账单花回去；
     · 进度用「服务端上报位置 + 墙钟按倍速外推」补平滑（rAF 只负责画，秒数不靠 5 Hz 台阶）。
   ========================================================================== */
S._nat = { duration: 0, playing: false, ended: true, mode: '', note: '', underruns: 0, ok: true, error: '', eq: null };
S._natAnchor = 0;      // 服务端上次报的位置（源秒）
S._natAnchorPerf = 0;  // 那一刻的墙钟（performance.now）
S._natLoadedId = null; // 服务端当前真正载着哪首（决定 play 还是 resume）
/* 当前这条流是按哪一组「开流参数」开的（设备/目标采样率/独占/倍速）。这四样在 play 请求
   到达服务端那一刻定死：configure 改不了、resume 也不会重开。空串＝还没开过流。 */
S._natShape = '';
/* 改开流参数时若起播请求还在路上，先记一笔；等那一趟落定（_nat.playing 翻真）再按新参数
   重开一次。2026-10-22 实测：点行起播后 0.3s 内把倍速从 4 改回 1，界面 1×、流仍 4×。 */
S._natReshapePending = false;
/* 控制请求的序号：每发起一次 play/pause/resume/seek/stop 就 +1。原生引擎的 seek 在独占档
   要重开解码/清环形，真机上量到响应能晚 0.5 s 以上；这期间用户再按一次 Home/End，旧响应回来
   仍会 `_natAnchorFrom` —— 把刚写下的新锚点拽回旧位置（2026-10-22 屏上复现：End 后 0.5 s
   按 Home，锚点从 0 跳回 235.897）。只有最后发出的那次控制的响应才配改状态。 */
S._natSeq = 0;
/* 「该从哪一秒起播」只有一个来源：S._natAnchor（posNow() 就是读它）。
   冷启动恢复、拖进度条、暂停后再按播放——全都往这一个锚上写，第一次 fresh play 也从这里取。
   原先另存了一份 S._natResumePos 专门给冷启动用，两份状态就会打架：恢复位置写进 resumePos，
   而用户在按下播放「之前」拖过进度条只改得到 anchor，playNative 读的是 resumePos
   ⇒ 拖完直接按空格，进度条闪回恢复点、拖的那一下白做。删掉第二份，留一处真相。 */
S._natPollT = 0;       // 轮询定时器（只在 playing 期间存在）
S._natCfgT = 0;        // configure 去抖（拖动滑块时别每像素发一次）
S._natSpecRaw = null;  // 原生频谱缓存（bands 个 0..1）

const isNative = () => S.settings.audio_engine === 'native';
/* 一只元素上的「卸流」只有这一份：有流才停、有源才卸、卸完 load 让 Chromium 释放网络
   与解码器。没 src 的元素碰都不碰 —— 既不是「暂停的流」，也没有后台解码器。 */
function unloadEl(el) {
  try {
    if (!el.hasAttribute('src')) return;
    if (!el.paused) el.pause();
    el.removeAttribute('src');
    el.load();              // 让 Chromium 释放网络/解码器，别留着一条后台流
  } catch (e) { /* 元素还没建好/已被拆掉：没有第二条流要停 */ }
}
/* 两条引擎必须互斥：原生档出声的是服务端 ffmpeg→WASAPI，两只 <audio> 只是备胎。
   以前切引擎只在「切回 Chromium」那半边暂停元素，Chromium→原生时元素还挂着旧 src
   继续放 —— 两路同一首歌、相位差几十毫秒，听感就是回声/梳状滤波（很自然的评价是
   「不清晰」）。这里把两只元素都停下并卸源；没有 src 时 load() 是空操作。
   交接途中（S._hx / S._ho）也一并收掉：切档之后旧的预载/淡出谁都不许再出声。 */
function silenceAudioElement() {
  cancelHandover(false);
  cancelPreload();
  unloadEl(audio);
  unloadEl(audio2);
}
/* 四个读数把「谁在计时」挡在门外：tick / 上报 / 进度条 / 键盘只看这四个函数。 */
function _natPos() {
  const sp = +S.settings.rate || 1;                 // 进度是源时间：外推也要乘倍速（与服务端 _pos_src_locked 同一把尺）
  const raw = S._nat.playing
    ? S._natAnchor + (performance.now() - S._natAnchorPerf) / 1000 * sp
    : S._natAnchor;
  const d = S._nat.duration || 0;
  return Math.max(0, d ? Math.min(raw, d) : raw);
}
function posNow() { return isNative() ? _natPos() : (audio.currentTime || 0); }
function durNow() { if (isNative()) return S._nat.duration || 0; const d = audio.duration; return (isFinite(d) && d > 0) ? d : 0; }
function _isPaused() { return isNative() ? !S._nat.playing : !!audio.paused; }
function bufferedFrac() { if (isNative()) return S._nat.playing ? durNow() : 0; const b = audio.buffered; return (b && b.length) ? b.end(b.length - 1) : 0; }
function _natAnchorFrom(st) { S._natAnchor = (+st.position) || 0; S._natAnchorPerf = performance.now(); }
/* 「服务端载着的」和「界面上这首」是不是同一个文件：比完整路径（Windows 大小写不敏感），
   不再只比 basename —— 两个目录各有一首 same.flac 时，文件名不是身份，会把控制动作
   发给错误的那首。path 缺失的行（远程/流式形状）才退回 filename。 */
function _natSameFile(serverPath, t) {
  if (!serverPath || !t) return false;
  const norm = (s) => String(s || '').replace(/\//g, '\\')
    .replace(/\\+/g, '\\').replace(/\\$/, '').toLowerCase();
  const sp = norm(serverPath);
  if (!sp) return false;
  if (t.path) return sp === norm(t.path);
  return !!t.filename && sp.split('\\').pop() === norm(t.filename);
}

/* 服务端 state() 的形状：{ok, engine:'native', path, playing, position, duration, rate(=采样率),
   channels, ended, mode, native_note, speed, underruns, gain, balance, loudness}。
   play/pause/resume/seek 的响应里就带着这份 state，就地重定锚，省掉一次轮询空窗。 */
async function nativeCtl(action, extra) {
  const seq = ++S._natSeq;
  let r;
  try { r = await api('native', { method: 'POST', body: Object.assign({ action }, extra || {}) }); }
  catch (e) { S._nat.ok = false; S._nat.error = (e && e.message) || String(e); return { ok: false, error: S._nat.error }; }
  /* 响应回来的这一刻，又有更新的控制发出去了 ⇒ 这一份 state 是旧的，位置/播放态一个字都别采纳。
     错误信息照旧写进 S._nat.error，EQ 回显和状态行也照旧渲染（它们不是「现在第几秒」）。 */
  const stale = seq !== S._natSeq;
  if (r && r.ok === false) { S._nat.ok = false; S._nat.error = r.error || '原生引擎错误'; }
  /* 采纳服务端那份 state 的前提是「它说的就是界面这一首」。
     响应里带 position ≠ 带身份：实测（57922，2026-10-13）页面挂着 Smile(296.7s)、
     引擎里还是上一趟真机测试留下的 RAT CITY(177.9s 已播完)，一次 seek 的响应把 anchor
     从刚拖到的 153.8 拽回 177.93、总时长也改成 177.9 —— 拖的那一下被旧状态吃掉，
     看起来就是「进度条拖不动」。身份对不上时一句都不采纳，并把「服务端已载本曲」收回，
     下一次播放走带页面锚点的 fresh play，而不是把这首当成已载入去 resume。 */
  const sameTrack = !!S.track && _natSameFile(r && r.path, S.track);
  if (!stale && r && r.engine === 'native' && typeof r.position === 'number' && sameTrack) {
    S._nat.duration = (+r.duration) || S._nat.duration;
    S._nat.playing = !!r.playing; S._nat.ended = !!r.ended;
    if (r.mode) S._nat.mode = r.mode;
    if (r.native_note) S._nat.note = r.native_note;
    /* 两种响应里的 position 都不是「界面上该停的那一秒」：
       · stop：它是「停在哪儿」（服务端不清 _base_pos/_consumed），而 stopPlayback() 紧接着
         就 seek(0)，这条 await 回来得比那句晚 —— 照抄会把进度条从 0 拽回旧位置；
       · seek：刚发起时本地已经把锚点写成请求值（还夹过量程），服务端那份要么是 clam 前的
         旧读数、要么因为「已播完」整条 seek 被早退拒绝，回来的位置是曲尾。照抄同样会拽回去。
       其余的 play/pause/resume 才需要把服务端落定的位置收进来。 */
    if (action !== 'stop' && action !== 'seek') _natAnchorFrom(r);
  } else if (!stale && r && r.engine === 'native' && r.path && !sameTrack) {
    S._natLoadedId = null;
  }
  if (!stale && r && r.ok !== false && (action === 'play') && sameTrack && !r.error) {
    if (typeof extra?.track_id === 'number') S._natLoadedId = extra.track_id;
    if (S.playing !== true) { S.playing = true; syncPlayUI(); }
  }
  /* 控件响应里本来就带着这份 state，音质面板那行就地抄一遍，不用等下一拍轮询：
     pause 之后那张表会被 _syncPollForState() 停掉，「暂停」这一句只能在这里说出口。
     只对服务端自己的回话（engine==='native'）渲染；track_mismatch 那种保护性空回也带 state，
     说引擎的真话没问题。 */
  if (r && r.engine === 'native') { adoptEqEcho(r); renderNativeStatus(r); natErrorNotice(r); }
  consumeNatReshape();   // 起播途中改过的开流参数：这一趟落定后按新参数补开一次
  _syncPollForState();
  return r;
}
/* A4/A5：解码坏掉 / 解码器起不来是「放不了」，不是「播完了」。
   服务端已经把两者分开报（state.error），这里负责说出口，并撤掉「服务端已载本曲」——
   否则用户再按播放会走 resume，而错误态下引擎拒绝 resume（放行等于进度条空转），
   看起来就是「点了没反应」。同一份错误只提示一次，免得 220ms 轮询刷满屏。 */
function natErrorNotice(st) {
  const err = st && st.error;
  if (!err) { S._natErrShown = ''; return; }
  const kind = String(err.kind || 'error');
  const detail = String(err.detail || '').trim().slice(0, 160);
  const sig = kind + '|' + (err.rc == null ? '' : err.rc) + '|' + detail;
  if (S._natErrShown === sig) return;
  S._natErrShown = sig;
  S._natLoadedId = null;                    // 下一次按播放走 fresh play，不是 resume
  const why = kind === 'decoder_unavailable'
    ? '解码器不可用（ffmpeg 起不来）'
    : '解码失败' + (err.rc != null ? '（ffmpeg 退出码 ' + err.rc + '）' : '');
  toast('无法播放：' + why + (detail ? ' — ' + detail : ''), 'err', 8000);
}
/* 轮询只在「原生且正在播」时活着：暂停/结束/离开原生都停表，闲置那笔账才守得住。
   第二个条件（S.playing）是给「服务端回了但形状里没有 state」留的后路：那种时候界面自认为在播，
   只认 S._nat.playing 的话轮询表永远起不来，进度条就冻在 0:00 —— 真机踩过一次（sp/api.py 的 play
   响应缺 state，已在服务端补齐）。这里让表先起来，由 nativePollTick 拿真状态把两边对齐：
   服务端要是并没在播，下面那行 _syncPollForState() 会立刻把表停掉，闲置账单不会漏出去。 */
function _syncPollForState() {
  if (isNative() && (S._nat.playing || S.playing === true)) startNativePoll();
  else stopNativePoll();
}
function startNativePoll() { if (!S._natPollT) S._natPollT = setInterval(nativePollTick, 220); }
function stopNativePoll() { if (S._natPollT) { clearInterval(S._natPollT); S._natPollT = 0; } }
async function nativePollTick() {
  if (!isNative()) { stopNativePoll(); return; }
  let st;
  const seq0 = S._natSeq;   // 这拍问出去时用户控制到第几次；回来时变了 ⇒ 这份 state 早于那次控制
  try { st = await api('native/state'); S._natFails = 0; }
  catch (e) {
    /* 服务端没起来 / 中途死了：连吃 20 拍（≈4.4 秒）就把表停掉并说明状态读不到。
       不封顶的话这是一台 220ms 的空转轮询 —— 闲置那笔账（#10 量过的那笔）不该为原生档重新开门。 */
    S._natFails = (S._natFails || 0) + 1;
    if (S._natFails >= 20) { S._nat.ok = false; S._nat.error = (e && e.message) || String(e); stopNativePoll(); }
    return;
  }
  if (!st || st.engine !== 'native') return;
  /* 出表之后用户按了播放/暂停/拖了进度条：这一拍读到的位置可能早于那次动作（seek 在独占档
     要重开解码，真机量到 0.5 s 以上），照抄会把刚拖到的锚点拽回去、进度条闪一下。
     丢掉的只是「位置/播放态」，下一拍照常接上；表的生死仍按当前状态重算。 */
  if (seq0 !== S._natSeq) { _syncPollForState(); return; }
  S._nat.duration = (+st.duration) || S._nat.duration;
  const wasPlaying = S._nat.playing;
  S._nat.playing = !!st.playing; S._nat.ended = !!st.ended;
  S._nat.mode = st.mode || S._nat.mode; S._nat.note = st.native_note || S._nat.note;
  S._nat.underruns = st.underruns || 0;
  _natAnchorFrom(st);
  adoptEqEcho(st);
  renderNativeStatus(st);
  natErrorNotice(st);
  if (st.playing !== S.playing) { S.playing = !!st.playing; syncPlayUI(); }
  if (st.playing) _fetchNativeSpectrum();
  /* 每一拍都按「服务端真在不在播」重定一次表的生死：上面那条兜底把表叫起来之后，
     如果服务端其实没在播（play 响应形状不对、或被别的入口停了），这一拍就得把它停回去 ——
     否则 220ms 的轮询会在暂停状态下一直空转，闲置那笔账就是这么漏出去的。 */
  _syncPollForState();
  // 播完：服务端把 ended 置真、playing 转假。这条边沿就是 chromium 档 audio 'ended' 事件的原生对应。
  if (wasPlaying && !st.playing && st.ended) { stopNativePoll(); nativeOnEnded(); }
}
/* 频谱只在「原生 + 正在播 + 画布可见」时随轮询顺带取一次（约 4.5 Hz），rAF 那侧只做插值绘制。 */
async function _fetchNativeSpectrum() {
  const cv = $('#spectrum');
  if (!cv || cv.hidden || !cv.clientWidth) { S._natSpecRaw = null; return; }
  try { const r = await api('native/spectrum?bands=18'); if (r && r.bands) S._natSpecRaw = r.bands; } catch (e) { /* 下一拍再试 */ }
}
/* 把「这一首到底走了哪条信号链」说成一句能核的话。每个数都来自 state()，前端不猜：
   source_ch=0 是「没量到」，不是单声道；limiter.on=false 只在独占档出现（共享档必接）；
   max_gr_db=0 是「这一段一次都没压」，不是「没接上」。这一行是为了不再出现
   「界面说切了、耳朵没变」——用户能对照它看清 5.1 下混、限幅和输出档位。 */
function nativeSignalPath(st) {
  if (!st || st.ok === false) return '';
  const lim = st.limiter || {};
  const db = v => ((+v || 0) > 0 ? '+' : '') + (+v || 0).toFixed(2) + ' dB';
  const seg = [];
  const ch = +st.source_ch || 0;
  const srate = +st.source_rate || 0;
  seg.push('源 ' + (ch > 0 ? ch + ' 声道' : '声道没量到')
    + (srate > 0 ? ' / ' + (Math.round(srate / 100) / 10) + ' kHz' : ''));
  const trim = +st.downmix_trim_db || 0;
  if (trim < -0.01) {
    // 预留是怎么来的必须说清：measured＝按这一首下混后的实测真峰算的；
    // fallback＝还没测到，退回避版前的 −6.00 dB（下一首/下次播放会换成实测值）。
    // 以前这一格只会写「下混预留 −6.00 dB」，用户看不出「为什么这首歌被压了 6 dB」。
    let why = '';
    if (st.downmix_basis === 'measured' && +st.source_tp > 0) {
      why = '（按本曲真峰 ' + db(20 * Math.log10(+st.source_tp)) + '）';
    } else if (st.downmix_basis === 'fallback') {
      why = '（真峰未测，先按兜底预留）';
    }
    seg.push('下混预留 ' + db(trim) + why);
  }
  if (lim.on && lim.frames > 0) {
    const ceil = 20 * Math.log10(+lim.ceiling || 1);
    seg.push('真峰限幅 ceiling ' + ceil.toFixed(2) + ' dBTP'
      + ' · 本段最大 GR ' + (+lim.max_gr_db || 0).toFixed(2) + ' dB'
      + ' · 压到 ' + (+lim.gain_lt_1_pct || 0).toFixed(1) + '% 帧');
  } else if (lim.on) {
    seg.push('真峰限幅已接上（还没处理音频）');
  } else {
    seg.push('无限幅器（独占档 bit-perfect 不经过它）');
  }
  seg.push('输出 ' + (+st.out_rate || +st.rate || 0) + ' Hz float32 · '
    + (st.mode === 'exclusive' ? '独占' : '共享混音器'));
  return '信号路径：' + seg.join(' → ');
}
/* 音质面板底部那行「原生：状态 … · 输出 … · 格式 …」是引擎状态的**镜子**，不是打开面板那一刻的快照。
   它先前定义在 viewSettings() 里面（同名 const 把这里这份遮蔽掉），只有开面板/换设备/换目标采样率
   三处会渲染，于是面板开着的时候服务端早已从暂停走到播放、采样率也从 44.1k 重开成 96k，那行仍写着
   「状态 未播放 · 格式 44100 Hz」——界面当着用户的面说假话（本机实测 57933，2026-10-13）。
   现在两处顺手刷新，都不额外发请求：轮询那份现成的 st（nativePollTick）和控件响应里那份 state
   （nativeCtl，pause 之后轮询表会停，所以「暂停」这一句只能由 pause 的响应来说）。
   面板没开着时 `#setNativeStatus` 不在这份 DOM 里，第一行就 return —— 闲置那笔账不重新开门。 */
async function renderNativeStatus(st) {
  const box = $('#setNativeStatus');
  if (!box) return;
  let text = '';
  let exText = '';
  let sigText = '';
  if (!isNative()) {
    text = '当前是浏览器内核：音量 / 均衡 / 倍速在前端 WebAudio 上算；下面这几行（设备 / 声像 / 响度 / 淡入）要切「原生」才参与。';
    exText = '独占与 bit-perfect 是原生那条链（服务端解码 → WASAPI）上的事，浏览器内核这一档不参与。';
    sigText = '信号路径：<audio> → WebAudio（音量 / 均衡 / 倍速）→ 系统混音器；切到「原生」后这里改显示源声道 / 下混预留 / 限幅器 / 输出档。';
  } else {
    if (st == null) { try { st = await api('native/state'); } catch (err) { st = null; } }
    if (!box.isConnected) return;   // 等这份 state 的期间面板关了，别往摘下来的节点上写
    if (!st || st.ok === false) {
      text = '原生引擎状态读不到' + (st && st.error ? '：' + st.error : '（服务进程没起来？）');
      sigText = '信号路径：引擎状态读不到 —— 这一行不敢猜。';
    } else {
      sigText = nativeSignalPath(st);
      const bits = [
        '状态 ' + (st.playing ? '播放中' : (st.ended ? '未播放' : '暂停')),
        // 解码错误要在这行镜子里面说清楚，不能只靠一条会消失的 toast：
        // 「暂停」和「这首歌坏了」是两件事，面板打开着的人有权看到是哪一种。
        st.error ? ('解码错误（' + (st.error.kind || 'error')
                   + (st.error.rc != null ? ' · rc ' + st.error.rc : '') + '）') : '',
        '输出 ' + (st.mode || 'shared'),
        '格式 ' + (st.rate || 0) + ' Hz / ' + (st.channels || 0) + ' 声道',
        st.speed && st.speed !== 1 ? ('流内倍速 ' + (+st.speed).toFixed(2) + '×') : '',
        st.loudness ? '响度均衡开' : '',
        '累计丢帧 ' + (st.underruns || 0) + ' 次',
      ].filter(Boolean);
      text = '原生：' + bits.join(' · ') + (st.native_note ? '　' + st.native_note : '');
      /* 独占这一行单独成文（#setExclusive）：它是「音质结论」，用户会拿它决定要不要继续用独占，
         所以每一格都只抄服务端算好的东西（sp/audioplay.py:_exclusive_view_locked），前端一个字
         都不自己推 —— 尤其「bit-perfect」这个肯定句，只有引擎逐条核过才许写。
         服务端给的是**原始量**（源位深/率/声道、设备最深位深、量没量化、抖没抖）加**结论位**
         （bit_perfect / bit_perfect_blocking）；这里只管措辞，不改判定。 */
      const ex = st.exclusive || {};
      const khz = h => (Math.round((+h || 0) / 100) / 10) + 'k';
      if (!ex.requested) {
        exText = '独占：未开启 —— 输出走系统混音器，其它程序的声音会一并混进来。';
      } else {
        const seg = [];
        if (!ex.format) {
          // 连协商都没谈成（或谈成了但 Initialize 变卦）：这时设备清单/源位深那些格子
          // 一个都没有，硬排出来会是「没探到 · 最深 ? 位」这种看着像数据的空话 —— 只说那一句原因。
          seg.push('没谈成独占' + (ex.note ? '：' + ex.note : '（服务端没给出原因）'));
        } else {
          seg.push(ex.stream_open
            ? ('已独占 ' + ex.format + ' · 周期 ' + (ex.period_ms || '?') + 'ms')
            : ('协商到 ' + ex.format + '，此刻没开流'
               + (ex.released ? '（' + ex.released + '）' : '')));
          seg.push('设备独占清单 ' + ((ex.device_rates || []).map(khz).join('/') || '没探到')
            + ' · 最深 ' + (ex.device_max_bits || '?') + ' 位');
          seg.push('源 ' + (ex.source_bits > 0 ? ex.source_bits + ' 位' : '位深没量到')
            + ' / ' + (ex.source_rate > 0 ? khz(ex.source_rate) : '采样率没量到')
            + (ex.source_ch ? (' / ' + ex.source_ch + ' 声道') : ''));
          seg.push(ex.quantizes
            ? (ex.dither ? '这一路要量化（已加 TPDF 抖动）' : '这一路要量化（抖动开关关着，误差不补偿）')
            : '这一路不量化（逐样本原样交给声卡）');
          seg.push(ex.bit_perfect ? 'bit-perfect：是'
            : ('bit-perfect：否 —— ' + ((ex.bit_perfect_blocking || []).join('；')
               || ex.why || '设备侧不满足')));
        }
        exText = '独占：' + seg.join(' · ');
      }
    }
  }
  // 每 220ms 刷一次，同一个串就别再碰 DOM（textContent 赋值会让那一块重新布局）
  if (box.textContent !== text) box.textContent = text;
  const exBox = $('#setExclusive');
  if (exBox && exBox.textContent !== exText) exBox.textContent = exText;
  const sigBox = $('#setSignalPath');
  if (sigBox && sigText && sigBox.textContent !== sigText) sigBox.textContent = sigText;
  /* 多声道提示只在「原生 + 真的多声道源」时出现。Atmos 的对象层是 ffmpeg 解不出来的
     能力边界（E-AC3 JOC 的 bed 是 5.1，objects 在元数据里），而微软播放器在系统装了
     Dolby Access 时会渲染对象层 —— 用户拿两者 A/B 时，这一句必须说在前面，否则
     「不如微软清晰」会被误解成我们少做了什么本可以做的事。 */
  const atmosBox = $('#setAtmosNote');
  if (atmosBox && atmosBox.style) {
    const mc = isNative() && st && st.ok !== false && +st.source_ch > 2;
    atmosBox.style.display = mc ? '' : 'none';
    if (mc) {
      const at = '多声道提示：5.1 / Atmos 素材按 ITU 矩阵下混到立体声（LFE 不参与）。'
        + '若是 Dolby Atmos（E-AC3 JOC），本播放器只解 5.1 bed、不渲染对象层；'
        + '系统装了 Dolby Access 时微软播放器会渲染对象层，两者不可直接 A/B。';
      if (atmosBox.textContent !== at) atmosBox.textContent = at;
    }
  }
}
/* 独占**能力表**单独一次拉取（GET native/exclusive_caps）：它不开流、不占有声卡，
   问的是「这台设备到底允许哪些独占格式」。为什么要在还没播放时就拉一次：本机实测默认端点
   的独占清单只有 44.1/48/96k 三档、**全是 16 位**，也就是 24 位的源在这台设备上必然量化 ——
   这是点开关之前就能知道的事实。只等起播后在状态行里说，等于把「你想要的 bit-perfect 拿不到」
   藏在一次点击后面。结果按设备缓存一份（S._exCaps），换设备时重拉。 */
async function loadExclusiveCaps(devVal) {
  const box = $('#setExCaps');
  if (!box) return;
  const key = devVal === undefined || devVal === null ? '' : String(devVal);
  if (S._exCaps && S._exCapsKey === key) {
    if (box.isConnected) box.textContent = S._exCaps;
    return;
  }
  let r = null;
  try { r = await api('native/exclusive_caps' + (key ? '?device=' + encodeURIComponent(key) : '')); }
  catch (err) { r = null; }
  if (!box.isConnected) return;
  let t;
  if (!r || r.ok === false) {
    t = '独占能力：读不到' + (r && r.error ? ' —— ' + r.error : '（原生依赖没装？）');
  } else {
    const khz = h => (Math.round((+h || 0) / 100) / 10) + 'k';
    t = '这台设备的独占能力：' + ((r.formats || []).map(f => f.tag).join(' / ') || '一个都不支持')
      + ' · 最高 ' + (r.max_bits || 0) + ' 位'
      + ' · 驱动默认周期 ' + (r.period_ms || '?') + 'ms'
      + ' · 系统混音格式 ' + (r.mix ? r.mix : '未知')
      + '（' + (r.device_name || '未命名设备') + '；这一行不开流、不锁声卡）';
    if (!(r.formats || []).length) t += ' —— 独占会被拒绝并自动回落共享';
  }
  // 只把**读到的结果**记进缓存。读失败（服务刚起、原生依赖没装好）如果也缓存，
  // 面板会把这句「读不到」冻到换设备或重启页面为止 —— 一次瞬时失败被当成长期事实。
  if (r && r.ok !== false) { S._exCaps = t; S._exCapsKey = key; }
  box.textContent = t;
}
function nativeOnEnded() {
  if (S.settings.play_mode === 'one_loop' && S.track) {
    S._natLoadedId = S.track.id;
    natShapeTake();
    nativeCtl('play', { track_id: S.track.id, pos: 0 });
  } else next(false);
}
async function nativeTeardown() {
  stopNativePoll();
  try { await api('native', { method: 'POST', body: { action: 'stop' } }); } catch (e) { /* 服务没了就随它 */ }
  S._nat = { duration: 0, playing: false, ended: true, mode: '', note: '', underruns: 0, ok: true, error: '', eq: null };
  S._natAnchor = 0; S._natAnchorPerf = 0; S._natLoadedId = null; S._natSpecRaw = null;
  S._natShape = ''; S._natReshapePending = false;
}
/* 音质参数（音量/声像/响度/淡入/倍速/均衡器）变了：native 档把当前设置推给服务端 configure。
   configure 只重算 DSP，不重开设备；换设备/采样率/独占走 nativeRestartIfPlaying。 */
function syncNativeConfig() {
  if (!isNative()) return;
  if (S._natCfgT) clearTimeout(S._natCfgT);
  /* configure 的参数是服务端从磁盘上的 settings 现读的（sp/api.py:_native_dsp），而
     saveSettings 有 320ms 防抖 —— 不先落盘就 POST，引擎按旧曲线配一次，症状是
     「拖完推子要再动一下才响」。所以这条把防抖提前结算掉再发。 */
  S._natCfgT = setTimeout(async () => {
    S._natCfgT = 0;
    try { await saveSettings.flush(); } catch (e) { /* 落盘失败照样把当前引擎配置重发一次 */ }
    nativeCtl('configure');
  }, 160);
}
function natShapeNow() {
  return [S.settings.audio_device == null ? '' : String(S.settings.audio_device),
          S.settings.audio_target_rate == null ? '' : String(S.settings.audio_target_rate),
          S.settings.audio_exclusive ? 'x' : 's',
          String(+S.settings.rate || 1)].join('|');
}
function natShapeTake() { S._natShape = natShapeNow(); }
function natShapeDirty() { return S._natShape !== natShapeNow(); }
/* 起播那一趟落定后把欠下的重开补上。调用点只有 nativeCtl 一处（任何原生响应之后），
   不在轮询里也做：轮询拍到 playing 时，起播响应早该处理过了；再补一次反而可能多开一条流。 */
function consumeNatReshape() {
  if (!S._natReshapePending || !S._nat.playing || !S.track) return;
  S._natReshapePending = false;
  nativeRestartIfPlaying();
}
/* 改变「输出流」形状的设置（设备 / 目标采样率 / 独占 / 倍速）：configure 只重算 DSP，动不了
   已经开着的 WASAPI 流和 ffmpeg 的 atempo，所以正在播时要用 play(当前位置) 重开一条流。
   没在播分两种：暂停（流还在、resume 只是解除暂停）→ 只落盘，playNative 下次按
   natShapeDirty() 改走 fresh play；起播请求还在路上 → 记 pending，等它落定再补，不能直接
   return（那一趟会用旧参数把流开起来，之后没人再改）。重开从当前位置接上，听着只是「顿一下」。 */
async function nativeRestartIfPlaying() {
  if (!isNative()) return;
  await saveSettings.flush();      // 服务端开流读的是落盘那份，防抖没到期就重启＝按旧参数开
  if (!S.track) return;
  if (!S._nat.playing) {
    if (S._natLoadedId !== S.track.id) S._natReshapePending = true;
    return;
  }
  S._natReshapePending = false;
  const pos = posNow();
  S._natLoadedId = S.track.id;
  natShapeTake();
  nativeCtl('play', { track_id: S.track.id, pos });
}
/* 起播一首：native 档不赋 audio.src，改由服务端 ffmpeg 解码。autoplay=false 时只备时长、不轮询。 */
async function startNative(track, autoplay) {
  // 原生档起播前再兜一次底：任何历史路径若在元素上留了 src，都必须在这里停干净。
  silenceAudioElement();
  S._nat.duration = +track.duration || 0;
  S._natAnchor = 0; S._natAnchorPerf = performance.now();
  S._nat.playing = false; S._nat.ended = false; S._nat.ok = true; S._nat.error = '';
  S._natLoadedId = null;
  S._natErrShown = '';            // 新起播 = 新的一轮：上一首的错误去重标记作废
  if (!autoplay) { S._nat.ended = false; return; }
  natShapeTake();                 // 这一趟按哪组开流参数开——之后判「设置变过没有」的底本
  const r = await nativeCtl('play', { track_id: track.id, pos: 0 });
  if (r && r.ok === false) { toast('无法播放：' + (r.error || '原生引擎错误'), 'err', 5000); autoAdvance(); }
}
/* play() 的原生对应：服务端已载本曲且没播完 → resume（含暂停后续播）；否则 fresh play。
   fresh play 的落点就是界面上正在显示的那一秒（posNow() 读 _natAnchor）：
   冷启动恢复的进度、按下播放前拖的那一下，都已经写在这个锚上，不必再另存一份。
   播完重播（服务端还载着本曲、且已 ended）从 0 起。 */
async function playNative() {
  if (!S.track) return;
  /* resume 只解除暂停，不会按新参数重开流：暂停期间改过设备/采样率/独占/倍速，
     这一下必须走 fresh play，否则界面写着 1×、耳朵还是旧那条流（2026-10-22 实测）。 */
  if (S._natLoadedId === S.track.id && !S._nat.ended && S._natAnchor > 0
      && !natShapeDirty()) { await nativeCtl('resume', { track_id: S.track.id }); nudgeFrames(); return; }
  // 落点只有一个来源：界面上正在显示的那一秒。唯一要改写它的是「播完后再按播放」＝
  // 从头重播 —— 而且只在这一秒确实还停在结尾上才改写：播完之后把进度条拖回中段再按播放，
  // 那就是「从这一秒起」，不许把拖的那一下吃掉。
  let pos = Math.max(0, posNow());
  const d = durNow();
  if (S._natLoadedId === S.track.id && S._nat.ended && (!d || pos >= d - 0.25)) pos = 0;
  natShapeTake();
  const r = await nativeCtl('play', { track_id: S.track.id, pos });
  if (r && r.ok === false) { toast('无法播放：' + (r.error || '原生引擎错误'), 'err', 5000); autoAdvance(); }
  else { nudgeFrames();
    // 预取的是「服务端到磁盘」那一段（/api/stream 把文件头读进系统缓存），ffmpeg 走同一份文件，
    // 所以原生档换曲同样受用 —— 逻辑和 chromium 档那 1200ms 后的 warmNext 一模一样。
    clearTimeout(S._warmTimer); S._warmTimer = setTimeout(warmNext, 1200); }
}
function pauseNow() {
  /* 原生档的暂停是「请服务端收尾」，最终时长要等那条响应回来才准；以前发出去就不管了，
     而暂停之后位置不再变 ⇒ tick 的闸门不放行 ⇒ 量程停在暂停那一刻之前的值。
     这里等响应落地再补一帧的预算，让响应里带回来的 duration 真有机会画上去。
     nativeCtl 自己吞错误（永远 resolve），finally 不会把异常漏给按暂停这条路。 */
  if (isNative()) nativeCtl('pause', { track_id: S.track && S.track.id }).finally(() => nudgeFrames());
  else {
    // 交接正在发生（或刚发生）：先把两条流收成一条，再暂停当前这只。否则交叉淡化里的
    // 旧元素会继续唱，界面已经写着「暂停」。
    if (S._hx || S._ho) cancelHandover(true);
    audio.pause();
  }
  S.playing = false; syncPlayUI();
}
function stopPlayback() {
  cancelHandover(false);
  cancelPreload();
  if (isNative()) { S._natLoadedId = null; S._nat.ended = true; nativeCtl('stop'); stopNativePoll(); }
  else audio.pause();
  seek(0); S.playing = false; syncPlayUI();
}
/* 切引擎：两把时钟（元素墙钟 vs 服务端源时间）交接最容易出错，所以按「从头播当前曲目」
   这个诚实、可解释的行为处理 —— 设置页的说明也写了这条。不假装能无缝续播。 */
async function switchEngine(nextEngine) {
  if (!nextEngine || nextEngine === S.settings.audio_engine) return;
  const wasPlaying = !_isPaused() && !!S.track;
  const hadTrack = !!S.track;
  const prevNative = isNative();
  S.settings.audio_engine = nextEngine;                 // isNative() 从这里起翻转
  saveSettings({ audio_engine: nextEngine });
  // 不论往哪边切，先把元素停下并卸源：Chromium→原生 这条路上旧流不能继续唱。
  // 事件处理器已按 isNative() 分流，所以这里的 pause 不会把原生档的 S.playing 改坏。
  silenceAudioElement();
  if (prevNative) await nativeTeardown();               // 关轮询 + 停服务端 + 清镜像
  if (hadTrack && S.track) {
    if (isNative()) await startNative(S.track, wasPlaying);
    else {
      audio.src = `/api/stream/${S.track.id}`; audio.currentTime = 0;
      if (wasPlaying) await play();                     // 此刻 isNative()=false，走元素那条路
    }
    loadLyric(S.track.id); updateMediaSession();
  }
  applySettings();
  renderView();
  toast(isNative() ? '已切到原生引擎（服务端 ffmpeg 浮点解码 → WASAPI）' : '已切回浏览器内核（<audio> + WebAudio）', '', 2400);
}


/* ==========================================================================
   桌面歌词小窗：那条药丸是原生窗口（sp/app.py 的第二个 QWebEngineView），
   网页这边隔着 /api/window/mini 使唤它，进度从 /api/playback 报给服务端。
   ========================================================================== */
/* 进度为什么要绕服务端一圈，而不是让小窗直接读主窗口：主窗口最小化或被遮挡时
   Chromium 会把它那条 rAF 停掉（闲置空转那笔账就是量这个量出来的），而桌面歌词
   存在的意义恰恰是「主窗不在眼前时也要有词」。上报挂在下面那条 1 秒的墙钟定时器上 ——
   它本来就不跟画面刷新走，所以冻帧的时候它还在报。
   只在开关开着时报：关掉时一个请求都不发，别把「闲置≈0」那笔账又花回去。 */
function reportPlayback() {
  const d = durNow();
  return api('playback', { method: 'POST', body: {
    track_id: S.track && S.track.id,
    position: posNow(),
    playing: !_isPaused(),
    rate: +S.settings.rate || 1,
    duration: isFinite(d) && d ? d : null,
  } }).catch(() => { /* 服务没在就当没这回事：主窗口自己听得见 */ });
}

/* 开关那一头和置顶那一头都走这里：两个数一起报，服务端不必猜另一个的默认值。 */
function syncMiniWindow() {
  const on = !!S.settings.desktop_lyric;
  if (on) reportPlayback();      // 刚打开就先报一次，别让药丸空着一秒才来字
  return api('window/mini', { method: 'POST', body: {
    show: on, on_top: S.settings.desktop_lyric_on_top !== false,
  } }).catch(() => { /* 浏览器模式（没有原生窗口）里这就是个空操作 */ });
}

/* 药丸右上角的 ✕ 是原生窗口里的一次点击，网页这边收不到通知。所以窗口重新拿到焦点时
   把这两个键从服务端抄回来 —— 只抄这两个：整份覆盖会把用户刚改、还在 320ms 防抖里的
   别的设置抹掉（那是这个 App 真踩过的坑，见 saveSettings 上面那段注释）。 */
window.addEventListener('focus', () => {
  api('settings').then((r) => {
    const s = (r && r.settings) || {};
    let changed = false;
    ['desktop_lyric', 'desktop_lyric_on_top'].forEach((k) => {
      if (Object.prototype.hasOwnProperty.call(s, k) && S.settings[k] !== s[k]) {
        S.settings[k] = s[k];
        changed = true;
      }
    });
    if (changed && S.view === 'settings') renderView();
  }).catch(() => {});
});

/* ==========================================================================
   启动
   ========================================================================== */
async function boot() {
  wireControls();
  try {
    const b = await api('bootstrap');
    S.settings = Object.assign(S.settings, b.settings || {});
    S.libraries = b.libraries || [];
    S.playlists = b.playlists || [];
    S.stats = b.stats || S.stats;
    S.app = Object.assign(S.app, b.app || {});
    applySettings();
    renderSidebar();
    route(location.hash || '#/' + (S.settings.last_view || 'home'));
    if (!S.stats.tracks && !S.libraries.length) showWelcome();
    else hideWelcome();
    if (b.scan && b.scan.running) watchScan();
    await restoreSession();
    // 上次退出时桌面歌词是开着的：把小窗一起叫回来（进度这时已经在报了）。
    if (S.settings.desktop_lyric) syncMiniWindow();
  } catch (e) {
    toast('本地服务未响应：' + e.message, 'err', 8000);
    $('#view').innerHTML = `<div class="welcome"><div class="welcome-card"><div class="welcome-icon">!</div>
      <h2>连不上本地服务</h2><p>请通过桌面的「LuHui音乐播放器」图标启动，<br>或先运行 <code>python salt_player.py --server</code>。</p></div></div>`;
  }
}

/* 上次播放的曲目 + 进度（App 端 settings.resume_on_start） */
async function restoreSession() {
  const q = await api('queue').catch(() => null);
  if (q && q.queue && q.queue.length) {
    S.queue = q.queue; S.context = q.context || '上次的队列';
    const i = S.queue.findIndex(t => t.id === q.track_id);
    if (i >= 0) { S.qi = i; await resumeInto(S.queue[i], q.position); renderQueue(); }
    return;
  }
  // 队列被清空（或从没建过）时 /api/queue 是空的，但 settings 里的 last_track 一直在写
  // ——flush 每 15 秒和退出前各写一次。原来这两个字段只写不读，所以「清完队列再重开」
  // 等于把上次听的那首也丢了。这里用 last_track + last_position 兜底。
  const tid = S.settings.last_track;
  if (!tid) return;
  const r = await api('track/' + tid).catch(() => null);
  const t = r && r.track;
  if (!t || t.missing || t.playable === 0) return;   // 文件移走/格式播不动：不如什么都不显示
  S.queue = [t]; S.qi = 0; S.context = '上次播放';
  await resumeInto(t, S.settings.last_position);
  renderQueue();
}

/** 把恢复出来的那一首挂上 src 并落回进度；只挂不播，等用户按空格。
    原生档多一步「问服务端」：那边的引擎是服务端进程里的真流，重新加载视图不会让它停声，
    所以开机时它可能已经在播了 —— 不采纳就会画出一个「暂停」的界面放着出声的歌。 */
async function resumeInto(t, pos) {
  cancelHandover(false);
  cancelPreload();
  resetGains();
  setTrack(t, false);
  const p = +pos || 0;
  if (isNative()) {
    // 原生档冷启动恢复：不赋 audio.src，只把时长与进度落在界面上。落点写进 _natAnchor
    // 这一个地方 —— 按下空格时 playNative 直接拿 posNow() 当第一次 fresh play 的位置，
    // 所以「恢复进度」和「用户在按播放前先拖了一下进度条」走的是同一条路，不会各存一份打架。
    S._nat.duration = +t.duration || 0;
    const from = (S.settings.resume_on_start && p > 3) ? p : 0;
    S._natAnchor = from; S._natAnchorPerf = performance.now(); S.pos = from;
    S._natLoadedId = null; S._nat.ended = false; S._nat.playing = false;
    /* 实测事故（57922 一次性服务器，2026-10-13）：页面重新加载后服务端仍在播这一首
       （state: playing=true、193 s→255 s 一路自走），而这份新页面按「没在播」画界面：
       needFrames() 假 ⇒ 一帧不画 ⇒ #tCur 恒 0:00、aria-valuemax=0、轮询不起，
       用户听到歌在放、看到的是暂停，按空格还要再 resume 一次。采纳服务端真状态是唯一
       不自欺的路：播着就照播着画，并把轮询表起起来（后面全交给 nativePollTick）。 */
    const st = await api('native/state').catch(() => null);
    if (st && st.engine === 'native' && st.playing) {
      S._nat.duration = (+st.duration) || S._nat.duration;
      S._nat.playing = true; S._nat.ended = !!st.ended;
      S._nat.mode = st.mode || S._nat.mode; S._nat.note = st.native_note || S._nat.note;
      S._nat.underruns = st.underruns || 0;
      _natAnchorFrom(st);
      const same = _natSameFile(st.path, t);
      if (same) S._natLoadedId = t.id;          // 载的就是这首：续播/暂停都走 resume
      S.playing = true; syncPlayUI(); _syncPollForState();
      nudgeFrames();
      return;                                    // 别说「按空格继续」——它就在播
    }
    if (S.settings.resume_on_start && p > 3)
      $('#pArtist').textContent = `已恢复到 ${fmtTime(p)} · 按空格继续`;
    /* 冷启动恢复只把时长写在 S._nat.duration 上，没人播就没人推帧 —— 不点这一下火，
       #tTot / aria-valuemax 会一直停在模板里的 0:00 / 0，直到用户按空格才补上。
       这里给一小段预算让 tick 把「恢复了多久 + 这首有多长」画出来，随后照常熄帧。 */
    nudgeFrames();
    return;
  }
  // 只挂 src、不自动播：preload=auto 会自己取回 duration，这样恢复的进度立刻落在
  // 进度条和时间标签上。以前这里不给 audio.src，seek() 只能排到 _pendingSeek，
  // 元素 readyState 一直是 0，用户看不到上次听到哪里。
  const want = `/api/stream/${t.id}`;
  if (!audio.src || !audio.src.endsWith(want)) audio.src = want;
  if (S.settings.resume_on_start && p > 3) {
    seek(p);
    $('#pArtist').textContent = `已恢复到 ${fmtTime(p)} · 按空格继续`;
  }
}

/* ==========================================================================
   设置 / 主题 / 外观
   ========================================================================== */
/* 合并窗口内多次调用的补丁，而不是「最后一次算数」。
   原来这里是 debounce(fn, 320)：防抖本身没问题，问题是它会把前一次调用整块丢掉。
   本机实测（Chromium 87 / 这个 App）：把歌词滚动速度改成 240，100ms 后切一次页
   ——切页会调 saveSettings({last_view})——于是 320ms 后落盘的只有 last_view，
   lyric_scroll_ms 在服务端仍是旧值，页面上却已经显示 240：用户改了设置却静默没保存。
   volume/applySettings 的先后顺序同理，任何「改完设置顺手触发一次别的保存」都会吃掉它。 */
const saveSettings = (() => {
  let pend = null, timer = null;
  const send = () => {
    const patch = pend; pend = null; timer = null;
    if (!patch) return Promise.resolve();
    Object.assign(S.settings, patch);
    return api('settings', { method: 'POST', body: { settings: patch } })
      .catch(() => { /* 离线时忽略 */ });
  };
  const impl = (patch) => {
    pend = Object.assign(pend || {}, patch || {});
    if (timer) clearTimeout(timer);
    timer = setTimeout(send, 320);
  };
  /* 防抖是为了少发请求，但有些设置是「服务端立刻要拿去用」的：原生引擎重开流时读的是
     磁盘上那份 settings（sp/api.py 的 load_settings），320ms 还没落盘就会按旧参数开流 ——
     症状是「换了设备要改两次才生效」。这类改动先 flush 再触发，返回 POST 的 promise 让调用方能等。 */
  impl.flush = () => { if (timer) clearTimeout(timer); return send(); };
  return impl;
})();

/** 图标按钮的名字：title 给鼠标、aria-label 给读屏，两个必须一起改。
    以前只写 title，于是静音/播放模式/定时这类图标变了名字却没变（读屏永远念「静音」），
    而 HTML 里写死的 aria-label 也会跟着图标一起说谎。 */
function nameEl(node, label) {
  const n = typeof node === 'string' ? $(node) : node;
  if (!n) return;
  n.title = label;
  n.setAttribute('aria-label', label);
}

function applySettings() {
  const s = S.settings;
  document.documentElement.dataset.theme = s.theme === 'light' ? 'light' : 'dark';
  // 主题一换，底色就变了：--accent-ink / --on-accent / --accent-up 是按「相对当前底色」
  // 算出来的派生档，落在 <html> 的行内样式上 —— 行内样式盖得住样式表里的主题块，
  // 所以它不会自己跟着换。不重推一遍的话，浅色主题会留着深色那支亮橙当文字用（1.9:1）。
  if (S._accent) setAccent(S._accent, S._accentFromCover);
  /* 两只元素都要吃同一份音量/静音：待机那只在交接起播的那一瞬就可能出声，只写当前那一只
     会让交叉淡化的起点跳一下。 */
  const baseVol = s.muted ? 0 : clamp(+s.volume, 0, 1);
  [audio, audio2].forEach((el) => { el.volume = baseVol; el.muted = !!s.muted; });
  const rate = clamp(+s.rate || 1, RATE_MIN, RATE_MAX);
  /* 两份都要写。只写 playbackRate 的话，换一首歌（start() 会赋 audio.src）或者启动时
     恢复上次播放（resumeInto 也赋 src），Chromium 会按规范把 playbackRate 打回
     defaultPlaybackRate —— 本机 Chromium 87 实测：赋 src 前是 4，赋完立刻变回 1。
     于是「倍速」这个功能只在当前这一首里有效，切歌就悄悄丢了，界面还显示 4.00×。
     给 default 那份也上同样的值，被打回去时落点就是用户要的速率。 */
  [audio, audio2].forEach((el) => {
    el.defaultPlaybackRate = rate;
    el.playbackRate = rate;
  });
  const volPct = Math.round(100 * (s.muted ? 0 : clamp(+s.volume, 0, 1)));
  $('#volFill').style.width = volPct + '%';
  $('#volThumb').style.left = $('#volFill').style.width;
  const vb = $('#volBar'); if (vb) { vb.setAttribute('aria-valuenow', volPct); vb.setAttribute('aria-valuetext', volPct + '%'); }
  nameEl($('#btnMute'), s.muted ? '取消静音' : '静音');
  $('#btnMute').innerHTML = icon(s.muted || +s.volume === 0 ? 'volume-x' : +s.volume > .66 ? 'volume-high' : +s.volume > .3 ? 'volume-med' : 'volume-low');
  document.documentElement.style.setProperty('--ly', s.lyric_size || 1);
  $$('.stage').forEach(n => n.classList.toggle('ly-top', s.lyric_mode === 'top'));
  syncAmbient();
  setPlaybarLine();   // 播放条歌词的开关可能刚从别处翻掉：那一行要么留词要么回艺人，不许停在旧文案上
  // 右侧播放面板的开合状态要跟着重启/刷新恢复（np_open 由 toggleNp 写入）
  const npOpen = s.np_open !== false;
  const stage = $('.stage');
  if (stage) stage.classList.toggle('np-hidden', !npOpen);
  const npBtn = $('#btnNpShow');
  if (npBtn) npBtn.hidden = npOpen;
  updateModeButton();
  // 均衡器的接线跟着设置走：开关从别处（快捷键/小窗/恢复会话）改动时也要重连一次。
  // 图还没建（第一次播放之前）时这两个函数自己 return，不会在这里造出半条链路。
  wireAudioGraph(); applyEqGains(); applyCrossfeed(); syncEqLabel();
  // 原生档：音量/倍速/均衡器/声像/响度这几样的「算」在服务端，上面那些 audio.* 写入对
  // 静音的 <audio> 元素是无害空操作，真正生效靠把当前设置推一次 configure（去抖，别每像素发）。
  syncNativeConfig();
  if (s.theme) api('window/theme', { method: 'POST', body: { dark: s.theme !== 'light' } }).catch(() => {});
}

/* 「已接入 / 直通」那句回显。放在模块级是因为它有两个来源：设置页里的开关，和
   applySettings（任何一条改动 play_mode 之外的路径都会走它）。写在闭包里就只能
   被设置页看见，开关从别处翻时那句说明就会留在旧话上。
   两条引擎的「接入」不是同一个东西，所以读数也必须分开：
     · chromium 档：S._eqLive —— 由 wireAudioGraph() 在真建起 WebAudio 图时写。
     · 原生档：服务端 state().eq —— 曲线在 numpy 双二阶上，_eqLive 永远不会亮。
       以前这里只认 _eqLive，于是原生档那句话永远停在「链路会在第一次播放时接上」，
       而 applySettings 早就 POST 过 configure 了（面板说的是假话）。 */
function syncEqLabel() {
  const n = $('#setEqV');
  if (!n) return;
  const g = Array.isArray(S.settings.eq_gains) ? S.settings.eq_gains : [];
  const peak = Math.max(0, ...g.slice(0, 10).map(x => Math.abs(+x || 0)));
  if (isNative()) {
    // 关着的时候不需要问曲线：控制组断了，问来也只写同一句「直通」。
    if (!S.settings.eq_enabled) { n.textContent = '未启用：链路直通'; return; }
    const eq = S._nat.eq;
    if (eq == null) {
      askEqEcho();
      n.textContent = '均衡器已开启：正在向服务端取当前曲线';
      return;
    }
    // 原生档的峰值也要说引擎里那一份：设置里的推子可能刚动、configure 还在路上，
    // 拿设置算 ±peak 会在那一小段里显示一个服务端还没挂上的数（面板比实现乐观）。
    const eg = Array.isArray(eq.gains_db) ? eq.gains_db.slice(0, 10).map(x => Math.abs(+x || 0)) : [];
    const p = eg.length ? Math.max(0, ...eg) : peak;
    if (eq.active) {
      n.textContent = '已接入（原生·服务端 ' + eq.bands + ' 段），最大偏移 ±' + p + ' dB';
    } else if (eq.applied) {
      // applied=True 且 active=False ⇒ configure 收下的就是一条全 0 曲线（_sos_eq 返回 None）。
      n.textContent = '已开启，但十条增益都是 0：引擎里没挂滤波器';
    } else {
      // 还没配过：这时候「增益都是 0」是替服务端猜原因，设置里可能明明有曲线。
      n.textContent = '已开启，曲线还没同步到服务端（原生引擎起播后生效）';
    }
    return;
  }
  n.textContent = S.settings.eq_enabled
    ? (S._eqLive ? '已接入，最大偏移 ±' + peak + ' dB' : '均衡器已开启，链路会在第一次播放时接上')
    : '未启用：链路直通';
}

/* 服务端那份 eq 的抄录：变了才重画那一行（220ms 一轮的轮询不许每拍都碰 DOM）。
   身份对不对与它无关 —— eq 说的是「引擎此刻挂着几条二阶节」，不是「这一首播到哪儿」，
   所以 track_mismatch 那种保护性空回里也照抄（进度/时长才需要 sameTrack 那道闸）。 */
let _natEqAsked = 0;
function adoptEqEcho(st) {
  const eq = st && st.eq;
  if (!eq || typeof eq.active !== 'boolean') return false;
  const bands = (+eq.bands) || 0;
  const gains = Array.isArray(eq.gains_db) ? eq.gains_db.slice(0, 10) : [];
  const applied = eq.applied === true;
  const before = S._nat.eq;
  if (before && before.active === eq.active && before.bands === bands
      && before.applied === applied
      && before.gains_db.join() === gains.join()) return false;
  S._nat.eq = { active: eq.active, bands: bands, gains_db: gains, applied: applied };
  _natEqAsked = 0;              // 取到了就把「补问」的门开回去（teardown 之后还要能再问一次）
  syncEqLabel();
  return true;
}
/* eq 还不知道的时候补问一次状态（一次 GET，不是轮询：闲置那笔账守的是「没在播就不许有空转」）。
   失败就不再追问，等下一次任何 native 回执把 eq 带回来。 */
function askEqEcho() {
  if (_natEqAsked) return;
  _natEqAsked = 1;
  api('native/state').then((st) => { if (st && st.engine === 'native') adoptEqEcho(st); })
    .catch(() => { /* 服务没起来：面板那句「正在取」就是当前最诚实的话 */ });
}

/* 音量：S.settings.volume 必须和 audio.volume 一起更新。
   saveSettings 有 320ms 防抖，如果只靠它写回，紧随其后的 applySettings 会读回旧值
   并把 audio.volume 打回去——↑/↓ 键的表现就是“每次慢一拍、看起来没反应”。 */
function setVolume(v) {
  const x = clamp(+v, 0, 1);
  S.settings.volume = x;
  applySettings();
  return x;
}
function nudgeVolume(delta) {
  if (S.settings.muted) { S.settings.muted = false; audio.muted = false; }   // 调音量先解除静音
  const x = setVolume(+S.settings.volume + delta);
  saveSettings({ volume: x, muted: false });
  toast(`音量 ${Math.round(x * 100)}%`, '', 1100);
}

const ACCENTS = ['#f0a04b', '#e2596b', '#5aa9f0', '#42c39a', '#b47cf0', '#f0d05a'];

function relLum(rgb) {
  const ch = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2]);
}
function contrast(a, b) {
  const l1 = relLum(a), l2 = relLum(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}
/** 当前主题的底色（合成用）：--bg 是主题令牌，读计算值而不是写死 —— 浅色主题下
    强调色要「压深」才够对比度，深色主题要「提亮」，方向得跟着底色走。 */
function themeBgRgb() {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
  return rgbOf(v || '#0c0d11');
}
/** 强调色「当文字用」的那一档：从强调色自身的明度出发，朝远离底色的方向一级级走，
    到 AA 正文级（4.6，留 0.1 余量）就停。走不到就退回中性文字档，绝不返回一个不够格的色。 */
function accentInk(h, s, l, bg) {
  const up = relLum(bg) < 0.18;
  for (let i = 0; i <= 60; i++) {
    const nl = clamp(l + (up ? i : -i), 6, 94);
    const rgb = hslToRgb(h, Math.min(s, 92), nl);
    if (contrast(rgb, bg) >= 4.6) return hex(rgb);
    if (nl === (up ? 94 : 6)) break;
  }
  return up ? '#f4f5f7' : '#3a3532';
}
/** 画在强调色填充上的墨色：深色墨 / 白墨取对比度高的那个（黄色强调色配黑墨、
    深蓝强调色配白墨，写死 #15100a 会在深色强调色上糊成一片）。 */
function onAccentInk(rgb) {
  const dark = [21, 16, 10], light = [255, 255, 255];
  return contrast(dark, rgb) >= contrast(light, rgb) ? '#15100a' : '#ffffff';
}

function setAccent(color, fromCover) {
  const r = document.documentElement.style;
  r.setProperty('--accent', color);
  // 这里以前写 `const [h, s, l] = rgbOf(color)`：rgbOf 返回的是 RGB，却被当 HSL 用，
  // 于是橙色 #f0a04b 算出 hsla(240, …) —— 蓝色。选中块、hover 底、描边线全都跟着
  // 封面色相跑偏（视觉上一眼看不出，因为叠的就是 17% 的 alpha）。
  const rgb = rgbOf(color);
  const [h, s, l] = rgbToHsl(rgb[0], rgb[1], rgb[2]);
  r.setProperty('--accent-soft', `hsla(${h}, ${s}%, ${clamp(l, 30, 70)}%, .17)`);
  r.setProperty('--accent-line', `hsla(${h}, ${s}%, ${clamp(l, 40, 75)}%, .46)`);
  // 材质三件：斜坡起点（比面亮一档，同色相）、当文字用的深档、画在面上的墨色。
  // 它们都依赖当前主题底色，所以换主题时必须重新调用 setAccent（见 applySettings）。
  r.setProperty('--accent-up', hex(hslToRgb(h, Math.max(s - 6, 0), clamp(l + 7, 0, 88))));
  r.setProperty('--accent-ink', accentInk(h, s, l, themeBgRgb()));
  r.setProperty('--on-accent', onAccentInk(rgb));
  S._accent = color;
  S._accentFromCover = !!fromCover;
  _specInk = '';   // 频谱的取色缓存跟着失效（否则换封面/换主题后柱子还是旧颜色）
}
function rgbOf(css) {
  const m = /^#?([0-9a-f]{3,6})$/i.exec(css.trim());
  if (!m) return [30, 70, 55];
  let hex = m[1];
  if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
  const n = parseInt(hex, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0; const l = (mx + mn) / 2;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  if (d !== 0) {
    if (mx === r) h = ((g - b) / d) % 6;
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h = Math.round(h * 60); if (h < 0) h += 360;
  }
  return [h, Math.round(s * 100), Math.round(l * 100)];
}
function hex(h) { return '#' + h.map(v => Math.round(v).toString(16).padStart(2, '0')).join(''); }
function hslToRgb(h, s, l) {
  s /= 100; l /= 100;
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r, g, b] = [c, x, 0]; else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x]; else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/* 从封面取主色：canvas 降采样 + 饱和度加权直方图 */
function extractAccent(imgUrl) {
  if (S.settings.accent_mode !== 'cover' || !imgUrl) { setAccent(S.settings.preset_accent || ACCENTS[0], false); return; }
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    try {
      const c = document.createElement('canvas'), w = c.width = 36, h = c.height = 36;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, w, h);
      const d = ctx.getImageData(0, 0, w, h).data;
      const bins = new Map();
      for (let i = 0; i < d.length; i += 4) {
        const a = d[i + 3]; if (a < 120) continue;
        const [hh, ss, ll] = rgbToHsl(d[i], d[i + 1], d[i + 2]);
        if (ll < 0.14 || ll > 0.94) continue;
        const s2 = Math.max(0, Math.min(1, ss * (0.35 + 0.65 * (1 - Math.abs(ll - 0.55) * 1.4))));
        const key = Math.round(hh / 12) * 12;
        const cur = bins.get(key) || { n: 0, s: 0, l: 0 };
        bins.set(key, { n: cur.n + 1, s: cur.s + s2, l: cur.l + ll });
      }
      let best = null;
      for (const [k, v] of bins) {
        const score = v.n * (0.35 + v.s / Math.max(1, v.n));
        if (!best || score > best.score) best = { k: +k, score, avgS: v.s / v.n, avgL: v.l / v.n };
      }
      if (!best) { setAccent(S.settings.preset_accent || ACCENTS[0], true); return; }
      const l = clamp(best.avgL * 100 + 12, 52, 72), s = clamp(Math.max(48, best.avgS * 100), 48, 88);
      setAccent(hex(hslToRgb(best.k, s, l)), true);
    } catch (e) { /* 跨域或解码失败：保持上一主题色 */ }
  };
  img.src = imgUrl;
}

function setBackdrop(url) {
  const a = $('#bdA'), b = $('#bdB');
  if (!url) { a.style.backgroundImage = b.style.backgroundImage = ''; return; }
  const next = a.style.backgroundImage && !a.dataset.locked ? b : a;
  const other = next === a ? b : a;
  next.dataset.locked = '1';
  next.style.backgroundImage = `url("${url}")`;
  next.style.opacity = '.5';
  other.style.opacity = '0';
}

/* ==========================================================================
   侧栏
   ========================================================================== */
let _plDrag = -1;      // 正在被拖动的歌单下标（见 renderSidebar 的注释：不用 dataTransfer）

/** 纯数组重排：把 from 挪到 to，返回新数组；不需要动就返回 null。 */
function movePlaylist(from, to) {
  const n = S.playlists.length;
  if (from < 0 || from >= n || to < 0 || to >= n || from === to) return null;
  const arr = S.playlists.slice();
  const [m] = arr.splice(from, 1);
  arr.splice(to, 0, m);
  return arr;
}

async function commitPlaylistOrder(arr) {
  if (!arr) return;
  // 先改本地再写库：拖动这种操作不能让界面等一个服务端往返，写失败也只是不持久化，
  // 界面本身并没有说谎（顺序确实按用户的意思排了）。
  S.playlists = arr;
  renderSidebar();
  try {
    await api('playlists/reorder', { method: 'POST', body: { ids: arr.map(p => p.id) } });
  } catch (e) {
    toast('歌单顺序没存下来，重启后会还原：' + e.message, 'err');
  }
}

function renderSidebar() {
  $('#navTrackCount').textContent = S.stats.tracks || '';
  $('#navFavCount').textContent = S.stats.favorites || '';
  const nav = $('#navPlaylists'); nav.textContent = '';
  if (!S.playlists.length) {
    // 空侧栏以前什么都没有（自动造的「我喜欢的音乐」歌单已经去掉，见 sp/db.py 的 _seed），
    // 第一次用的人不知道歌单是从哪儿来的，所以这里给一句指向＋号按钮的提示。
    // 不再是带边框的空卡：那只会在侧栏里多一个像坏掉的输入框，压过真正的导航。
    const tip = el('button', 'nav-empty');
    tip.type = 'button';
    tip.innerHTML = `${icon('plus')}<span>还没有歌单 · 点这里新建</span>`;
    tip.title = '也可以在歌曲上右键，选择「添加到歌单」';
    tip.addEventListener('click', newPlaylist);
    nav.appendChild(tip);
  }
  S.playlists.forEach((p, i) => {
    const a = el('a', 'nav-item'); a.href = `#/playlist/${p.id}`;
    a.innerHTML = `${icon(p.pinned ? 'pin' : 'music')}<span>${esc(p.name)}</span><em>${p.track_count || ''}</em>`;
    a.dataset.pl = p.id;
    a.addEventListener('contextmenu', ev => playlistMenu(ev, p, i));
    // 拖拽排序：索引记在模块变量里，不用 dataTransfer —— <a> 的默认拖拽会在
    // dragstart 之后把 URL 写进 text/plain，把我们放的索引覆盖掉。
    a.draggable = true;
    a.addEventListener('dragstart', () => { _plDrag = i; a.classList.add('dragging'); });
    a.addEventListener('dragend', () => { _plDrag = -1; a.classList.remove('dragging'); });
    a.addEventListener('dragover', ev => {
      if (_plDrag >= 0) {                   // 歌单之间：排序
        ev.preventDefault(); ev.dataTransfer.dropEffect = 'move';
        a.classList.toggle('dragover', _plDrag !== i);
        return;
      }
      // 歌曲行拖过来的：这就是「拖到侧栏＝加进这个歌单」。
      // dragover 阶段读不到 getData（规范就是这么定的），只能看 types；用 indexOf.call
      // 而不是 includes，因为 types 在 Chromium 里是 FrozenArray，不保证是带 includes 的数组。
      if (Array.prototype.indexOf.call(ev.dataTransfer.types, 'text/salt-tracks') < 0) return;
      ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy';
      a.classList.add('dragover');
    });
    a.addEventListener('dragleave', () => a.classList.remove('dragover'));
    a.addEventListener('drop', ev => {
      if (_plDrag >= 0) {
        ev.preventDefault(); ev.stopPropagation();
        a.classList.remove('dragover');
        commitPlaylistOrder(movePlaylist(_plDrag, i));
        _plDrag = -1;
        return;
      }
      const ids = (ev.dataTransfer.getData('text/salt-tracks') || '').split(',').filter(Boolean).map(Number);
      if (!ids.length) return;               // 资源管理器拖进来的文件：别拦，让窗口级导入接手
      ev.preventDefault(); ev.stopPropagation();
      a.classList.remove('dragover');
      addToPlaylist(p.id, ids);
    });
    nav.appendChild(a);
  });
  // 来源入口只写「是什么 + 有多少」；具体有哪几个目录放进菜单，不再在侧栏里排一串标签。
  const libBtn = $('#libSource');
  if (libBtn) {
    const n = S.libraries.length;
    const total = S.stats.tracks || 0;
    const one = n === 1 ? S.libraries[0] : null;
    $('#libSourceTitle').textContent = one ? (one.name || one.path) : n ? `${n} 个音乐文件夹` : '添加音乐文件夹';
    $('#libSourceSub').textContent = n ? `${total} 首${n > 1 ? '' : ' · 点击管理'}` : '选择或输入一个目录';
    libBtn.title = n
      ? S.libraries.map(l => `${l.name || l.path}（${l.track_count || 0} 首）`).join('\n')
      : '选择或输入一个音乐文件夹';
    libBtn.setAttribute('aria-label', n ? `音乐文件夹：${n} 个，共 ${total} 首` : '添加音乐文件夹');
  }
  markActiveNav();
  requestAnimationFrame(syncNavFade);
}

/* 侧栏的「还能往哪边滚」：把滚动位置翻成两个 class，CSS 用 mask 做渐隐。
   没有它，830px 高的窗口里 `.nav` 正好装不下，最后一张卡被 side-foot 的边线从字中间切断，
   看起来像布局坏了（截图 15_final_sidebarzoom 就是这么量出来的）。 */
function syncNavFade() {
  const n = $('#nav');
  if (!n) return;
  const up = n.scrollTop > 2;
  const down = n.scrollTop + n.clientHeight < n.scrollHeight - 2;
  n.classList.toggle('fade-up', up);
  n.classList.toggle('fade-down', down);
}

/* 详情页 → 列表页侧栏项：#/album/x 停在「专辑」下、#/artist/x 停在「艺术家」下……
   以前只有列表页本身会高亮，进详情后侧栏八条全是灰的，用户不知道自己从哪儿进来的。 */
const NAV_ALIAS = { album: 'albums', artist: 'artists', genre: 'genres', folder: 'folders' };

function markActiveNav() {
  $$('.nav-item').forEach(n => {
    const v = n.dataset.view;
    const on = !!v && (S.view === v || NAV_ALIAS[S.view] === v);
    n.classList.toggle('active', on);
    if (n.dataset.pl) n.classList.toggle('active', S.view === 'playlist' && String(n.dataset.pl) === String(S.viewState.id));
  });
}

/* ==========================================================================
   路由
   ========================================================================== */
function route(hash, pushHistory = true) {
  const target = hash || '#/home';
  const raw = target.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const seg = pathPart.split('/').filter(Boolean);
  const view = decodeURIComponent(seg[0] || 'home');
  const args = seg.slice(1).map(decodeURIComponent);
  const q = Object.fromEntries(new URLSearchParams(queryPart || ''));
  if (pushHistory) S.history.push(location.hash);
  // URL 必须跟着内部视图走：以前这里只渲染不写 location.hash，键盘 R 从首页进电台后
  // 地址栏还是 #/home，再点侧栏「此刻」（href 恰好是同一个 hash）浏览器不会发
  // hashchange，页面就停在电台上不动。先记下目标视图，再写 hash；同步写回
  // S._routedHash 用 location 的规范编码，避免中文关键词被编码后误判成第二次导航。
  if (location.hash !== target) location.hash = target;
  S.view = view;
  S.viewState = { view, args, q, id: args[0] };
  // 搜索框跟路由双向对齐：进搜索页要回填关键词（后退/前进、直接点历史链接都算），
  // 离开搜索页要清掉。以前只在「不是搜索页」时写空，从历史里回到搜索页输入框是空的，
  // 页面却显示上一次的结果，看起来像搜索框自己忘了词。
  $('#searchInput').value = view === 'search' ? (q.q || '') : '';
  syncSearchClear();
  // 窄窗口里点侧栏导航时抽屉还开着：先把焦点还给「打开导航」，再把抽屉收走。
  // 搜索页不写 last_view —— 那是「下次开机回到哪一页」和「清空搜索回哪一页」的同一份
  // 记忆，存成空搜索页会让下一次启动直接落在一张没有词的结果页上。
  const hadDrawer = !!_drawer.kind;
  closeDrawer(hadDrawer);
  if (!q.keepScroll) $('#scroller').scrollTop = 0;
  if (view !== 'search') saveSettings({ last_view: raw || 'home' });
  S._routedHash = location.hash || '#/home';   // 这次真的渲染成哪一页，以这里为准
  renderView();
}
/** 搜索框里有没有词，决定自绘清除键和 `/` 键帽谁露面。空值时不留下一个点了没反应的叉。 */
function syncSearchClear() {
  const inp = $('#searchInput'), b = $('#searchClear'), box = $('.search');
  if (!inp || !b) return;
  const has = inp.value.length > 0;
  b.hidden = !has;
  if (box) box.classList.toggle('has-value', has);
}
/** 清空搜索后该回哪一页：上一页如果本身就是搜索页（只可能是空查询的落地页），
    就回「此刻」而不是又落回一个空搜索页。 */
function lastViewHome() {
  const raw = String((S.settings && S.settings.last_view) || '').replace(/^#\/?/, '').split('?')[0];
  return !raw || raw === 'search' ? 'home' : raw;
}
window.addEventListener('hashchange', () => {
  /* 判「这条 hashchange 是不是我们自己刚渲染完的回声」不能拿 S.settings.last_view 比：
     那份要等 saveSettings 的 320ms 防抖到点才写进内存。连着快速换页时它一直滞后一步，
     于是新 hash 恰好和它相等 → 事件被当成回声吞掉，页面不动：URL 和侧栏高亮是 B 页，
     屏幕上还留着 A 页。本机 Chromium 87 实测：按 40~65ms 的间隔轮换七个视图，
     隔一轮就被吞一次（读数：view=folders，而要求的已经是 favorites）。 */
  if (location.hash !== S._routedHash) route(location.hash, false);
});

const VIEW_TITLES = {
  home: '此刻', favorites: '我喜欢的音乐', history: '最近播放', radio: '随意电台',
  songs: '所有歌曲', albums: '专辑', artists: '艺术家', folders: '文件夹', genres: '流派',
  album: '专辑', artist: '艺术家', folder: '文件夹', playlist: '歌单', settings: '设置', search: '搜索',
  genre: '流派',
};

/* renderView 的意思是「请把最新那一页渲染出来」，不是「此刻就动手」：
   视图函数要边等接口边往 #view 里写内容，两次换页同时在飞时，起得早、收得晚的那次
   会把已经画好的新页盖回旧页（标题写着新页，正文却还是旧页）。
   所以这里串一条链排队跑，并且只有「轮到我时仍然数我最新」的那次才真跑，旧的那次直接跳过。 */
let _renderSeq = 0;
let _renderTail = Promise.resolve();
let _renderDoneSeq = 0;      // 探针用它判断「最后一次请求的渲染已经落定」，不用猜睡眠时长
let _renderRuns = 0;         // 真正执行过 _renderOnce 的次数（被合掉的旧请求不计数）
function renderView() {
  const seq = ++_renderSeq;
  _renderTail = _renderTail.then(() => (seq === _renderSeq ? _renderOnce() : null))
    .catch(() => null)
    .then(() => { if (seq > _renderDoneSeq) _renderDoneSeq = seq; });
  return _renderTail;
}

async function _renderOnce() {
  _renderRuns++;    // 真跑了几次：探针用它确认「连点换页时旧的请求被合掉了」，而不是排了四次队
  const v = $('#view'), st = S.viewState;
  document.title = VIEW_TITLES[st.view] ? `${VIEW_TITLES[st.view]} · LuHui音乐播放器` : 'LuHui音乐播放器';
  $('#viewTitle').textContent = VIEW_TITLES[st.view] || 'LuHui音乐播放器';
  $('#viewSub').textContent = '';
  v.classList.remove('fade-in'); void v.offsetWidth; v.classList.add('fade-in');
  // 换页时把上一页的分页钩子摘掉：#scroller 是全App共用的那一个滚动容器，
  // 上一视图的 handler 还挂着的话，滚专辑页也会去取歌单的第 301 首。
  $('#scroller').onscroll = null;
  // 上一页虚拟列表的尺寸观察者也要摘：它还盯着同一个 #view，留着就是每换一页多一个。
  if (S._vlist && S._vlist._ro) { S._vlist._ro.disconnect(); S._vlist._ro = null; }
  $('#listFoot').hidden = true;
  // A–Z 字母轨挂在 .main（#scroller 的兄弟）上，不随 #view.innerHTML='' 一起消失；
  // 不在这里摘掉，滚过专辑页再进设置页，右侧会残着一条上一视图的字母轨。
  document.querySelectorAll('.main > .az-rail').forEach((n) => n.remove());
  document.querySelector('.main').classList.remove('has-az');
  // 换页时连「正在加载」标记一起清掉：#listFoot 是全 App 共用的一个节点，而 handler
  // 第一行就是 `if (foot.dataset.busy) return`。上一页在飞的那次请求如果在换页时被
  // 打断（异常、或页面被摘掉），那个标记就没人删——此后所有视图的分页都静默不再取数，
  // 用户看到的是「列表下面空着、滚不动」，而且没有任何报错。实测这条不是想象：
  // 手工把 data-busy 置成 '1' 再跑分页探针，读数就是 rows 停在 7、calls=0、
  // trail 一路「7>7>7…」，和真实产品坏掉时的形状完全一样。
  delete $('#listFoot').dataset.busy;
  S._paging = null;
  S._vlist = null;
  markActiveNav();
  hideWelcome();
  const fns = {
    home: viewHome, favorites: viewFavorites, history: viewHistory, radio: viewRadio,
    songs: viewSongs, albums: viewAlbums, artists: viewArtists, genres: viewGenres, folders: viewFolders,
    album: viewAlbum, artist: viewArtistDetail, folder: viewFolder, playlist: viewPlaylist,
    genre: viewGenre, settings: viewSettings, search: viewSearch,
  };
  try {
    await (fns[st.view] || viewSongs)(v, st);
    // 卡片墙搭完再量：content-visibility 的占位高必须以真实卡高为准（见 calibrateCardIntrinsic）。
    calibrateCardIntrinsic(v);
  } catch (e) {
    // 以前这里只把错误文案写进页面：本地服务重启一下就恢复了，用户却只能刷新整扇。
    v.innerHTML = '';
    const box = el('div', 'section');
    box.appendChild(el('div', '', `这一页没加载出来：${e.message}`));
    const again = el('button', 'link-btn', '重试');
    again.type = 'button';
    again.onclick = () => renderView();
    box.appendChild(again);
    v.appendChild(box);
    toast('页面加载失败：' + e.message, 'err');
  }
}

/* -------------------------------------------------------------- 列表渲染 */
function listHeader(cols) {
  // 第四列（专辑/文件名）在 ≤1180px 时靠 .albumcell { display:none } 和行一起收掉。
  // 行是 trackRow 硬写的，表头却由各视图自己传文案 —— 以前有三个视图忘了带 cls，
  // 结果窄窗口里行只剩 5 列、表头还是 6 列，「时长」的表头压在「专辑」那一格上。
  // 这里统一补上，不再依赖每个调用点都记得。
  const cells = cols.map((c, i) => (i === 3 ? { ...c, cls: ((c.cls || '') + ' albumcell').trim() } : c));
  const h = el('div', 'list-head');
  h.innerHTML = cells.map(c => `<span class="${c.cls || ''} ${c.sort ? 'sortable' : ''}" data-sort="${c.sort || ''}">${esc(c.t)}</span>`).join('');
  h.querySelectorAll('.sortable').forEach(s => s.addEventListener('click', () => {
    const key = s.dataset.sort; if (!key) return;
    const cur = S.viewState.q.order === key ? (S.viewState.q.dir === 'desc' ? '' : '&dir=desc') : '';
    const order = S.viewState.q.order === key && S.viewState.q.dir === 'desc' ? '' : `&order=${key}${cur}`;
    location.hash = `#/${S.viewState.view}/${(S.viewState.args || []).map(encodeURIComponent).join('/')}${location.hash.includes('?') ? '' : '?'}${order}`;
  }));
  return h;
}

/* -------------------------------------------------------------- A–Z 首字母轨   三处目录（歌曲/艺术家/专辑）共用一条右侧字母轨。首字母一律由后端算（sp/textidx，
   pypinyin 离线导出的查表），前端只消费服务端给的 letter/idx，绝不自己再算一遍拼音——
   前端用 JS 再算必和后端歪，点 R 就会停在非 R 的行上，正是用户点名要修的毛病。
   · 歌曲：分页列表，点字母＝按该字母过滤（服务端 order=az&letter=R），落点即那一块；
   · 艺术家/专辑：整表已在 DOM 里，点字母＝滚到那个字母的分区表头。
   两种都满足「点 R 立马跳到 R 开头那一片」，各自贴合自己的加载模型。 */
const AZ_LETTERS = (() => {
  const a = [];
  for (let c = 65; c <= 90; c++) a.push(String.fromCharCode(c));
  a.push('#');
  return a;
})();

function azRail(host, present, active, onPick) {
  const rail = el('div', 'az-rail');
  rail.setAttribute('role', 'toolbar');
  rail.setAttribute('aria-label', '首字母索引');
  rail.setAttribute('aria-orientation', 'vertical');
  const keys = [];
  const mk = (label, val, dim) => {
    const b = el('button', 'az-key' + (dim ? ' dim' : '') + (val === active ? ' on' : ''), label);
    b.type = 'button';
    if (dim) {
      /* 空字母不是“可点但没反应”：设 disabled 才会从 tab 序列里消失。
         以前只挂 aria-disabled，键盘仍要空穿 20 多个死按钮。 */
      b.disabled = true; b.tabIndex = -1;
      b.setAttribute('aria-disabled', 'true'); b.title = label;
    } else {
      b.onclick = () => onPick(val);
      b.tabIndex = -1;
      b.setAttribute('aria-label', val ? `跳到 ${label}` : '显示全部');
      // 歌曲页是筛选（有当前字母），艺术家/专辑页只是滚动索引（active===null）。
      if (active !== null) b.setAttribute('aria-pressed', val === active ? 'true' : 'false');
      b.addEventListener('focus', () => { keys.forEach(k => { k.tabIndex = k === b ? 0 : -1; }); });
      keys.push(b);
    }
    rail.appendChild(b);
  };
  mk('全部', '');                       // 清空字母，回到完整目录
  for (const L of AZ_LETTERS) mk(L, L, !present.has(L));
  /* 28 个字母逐个进 Tab 序列＝按 Tab 要空穿一条索引轨。整条轨只留一个停点，
     ↑↓/←→ 换字母、Home/End 跳两端；焦点落在当前筛选字母（没有就落在「全部」）。 */
  const init = keys.find(k => k.classList.contains('on')) || keys[0];
  if (init) init.tabIndex = 0;
  rail.addEventListener('keydown', (e) => {
    const i = keys.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') j = (i + 1) % keys.length;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') j = (i - 1 + keys.length) % keys.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = keys.length - 1;
    if (j === null) return;
    e.preventDefault(); e.stopPropagation();
    keys.forEach((k, n) => { k.tabIndex = n === j ? 0 : -1; });
    keys[j].focus();
  });
  (host || $('#scroller')).appendChild(rail);
  const main = rail.closest('.main');
  if (main) main.classList.add('has-az');
  return rail;
}

// 把 [{idx,...}] 按 A–Z（# 垫尾）分组成 [[letter, items…]]，跳过空组、保持组内原序。
function azGroup(items) {
  const by = new Map();
  for (const it of items) {
    const L = it.idx || '#';
    (by.get(L) || by.set(L, []).get(L)).push(it);
  }
  const out = [];
  for (const L of AZ_LETTERS) if (by.has(L)) out.push([L, by.get(L)]);
  return out;
}

// 艺术家/专辑整表已在 DOM：按首字母分区块，每块一个可吸附的表头，右侧字母轨点了就滚过去。
// cardFn 决定这一格卡片长什么样（沿用各视图原有的 onClick/onPlay，不改行为）。
function azSections(v, items, cardFn) {
  const groups = azGroup(items);
  for (const [L, arr] of groups) {
    const head = el('div', 'az-head', L);
    head.dataset.letter = L;
    v.appendChild(head);
    v.appendChild(cardGrid(arr.map(cardFn)));
  }
  return new Set(groups.map(([L]) => L));
}

function azScrollRail(v, present) {
  // active=null：这是滚动索引，不是筛选器 —— 不要给「全部」挂 aria-pressed 的选中态。
  azRail($('.main') || v, present, null, (L) => {
    const h = v.querySelector(`.az-head[data-letter="${L}"]`);
    if (h) h.scrollIntoView({ behavior: rmq() ? 'auto' : 'smooth', block: 'start' });
  });
}

function trackRow(t, idx, list, opts = {}) {
  const row = el('div', 'row' + (S.track && S.track.id === t.id ? ' playing' : ''));
  row.dataset.id = t.id;
  row.dataset.idx = idx;   // 窗口化列表靠它判断这一行在全表里的绝对位置
  /* 键盘路径：整张表只有一个 roving tabindex（见 initRoving），↑↓ 换行、回车播放。
     收藏按钮不再单独进 tab 序列（300 多行会造出 300 多个看不见的停点）；键盘收藏走
     Shift+F10 菜单里的「喜欢」，鼠标仍可直接点行尾的 ♡。 */
  row.tabIndex = -1;
  row.setAttribute('aria-label', `${t.title}${t.artist ? ' — ' + t.artist : ''}${t.favorite ? '，已喜欢' : ''}，回车播放`);
  if (S.track && S.track.id === t.id) row.setAttribute('aria-current', 'true');
  row.addEventListener('focus', () => {
    const parent = row.parentElement;
    if (!parent) return;
    parent.querySelectorAll('.row').forEach(r => { r.tabIndex = r === row ? 0 : -1; });
  });
  row.addEventListener('keydown', (e) => {
    if (e.target !== row) return;   // 行内按钮的键事件不重复触发整行动作
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); e.stopPropagation();          // 别让全局空格再 toggle 一次
      playFrom(list, list.indexOf(t));
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const down = e.key === 'ArrowDown';
      const sib = down ? row.nextElementSibling : row.previousElementSibling;
      if (sib && sib.classList.contains('row')) {
        e.preventDefault(); e.stopPropagation();        // 全局 ↑↓ 是音量；行内有焦点时让位给选行
        sib.focus(); sib.scrollIntoView({ block: 'nearest' });
      } else {
        // 窗口化列表：窗口边缘的外面还有行，交给窗口控制器滚一行再交还焦点。
        // 已经是全表头/尾时 step 返回 false，↑↓ 照旧落回全局音量键。
        const vl = row.parentElement && row.parentElement.__vl;
        if (vl && vl.step(row, down ? 1 : -1)) { e.preventDefault(); e.stopPropagation(); }
      }
    }
  });
  // 行本身可拖：拖到侧栏歌单上＝加入那个歌单，拖到歌单详情页里＝加入当前歌单。
  // 这条载荷以前没有任何写入方（只有队列行写 text/plain），所以详情页那对 drop 监听
  // 一直是死代码 —— 补了 CSS 高亮也拖不进去，得把源头一起接上。
  // 用自定义 MIME 而不是 text/plain：从资源管理器拖音频文件进来时 types 里只有
  // Files，窗口级那套文件导入逻辑不该被我们自己的行拖拽抢走。
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/salt-tracks', String(t.id));
    e.dataTransfer.effectAllowed = 'copy';
    row.classList.add('dragging');
  });
  row.addEventListener('dragend', () => row.classList.remove('dragging'));
  const cover = t.has_cover ? `/api/cover?track_id=${t.id}&size=list` : '';
  // 文件夹页那一列要显示文件名而不是专辑名（表头也写着「文件名」）：
  // 以前是渲染完再回头改 DOM 文本，分页追加的那几行就会漏改，同一页两种口径。
  const third = opts.filename ? (t.filename || '') : (t.album || '');
  // 行号口径：列表里显示「第几行」，只有专辑页才显示专辑内的 track_no。
  // 以前所有视图都拿 t.track_no 顶替，结果“所有歌曲”第 13 行起跳成 3,14,15,1,2…
  const rowNo = opts.trackNo ? (t.track_no || idx + 1) : (idx + 1);
  row.innerHTML = `
    <span class="num">${rowNo}</span>
    <span class="cell titlecell">
      ${cover ? `<img class="mini-cover" loading="lazy" decoding="async" src="${cover}" alt="">` : `<span class="mini-cover noart">${icon('music')}</span>`}
      <span class="title">${esc(t.title)}</span>
      ${S.track && S.track.id === t.id ? '<span class="eq"><span></span><span></span><span></span></span>' : ''}
    </span>
    <span class="cell sub">${esc(t.artist)}</span>
    <span class="cell sub albumcell">${esc(third)}</span>
    <span class="cell tiny">${fmtTime(t.duration)}</span>
    <button class="icon-btn tiny fav ${t.favorite ? 'on' : ''}" tabindex="-1" title="${t.favorite ? '取消喜欢' : '喜欢'}" aria-label="${t.favorite ? '取消喜欢' : '喜欢'}" aria-pressed="${t.favorite ? 'true' : 'false'}">${icon('heart', t.favorite ? 'fill' : '')}</button>`;
  row.addEventListener('click', e => {
    if (e.target.closest('.fav')) { toggleFav(t); return; }
    playFrom(list, list.indexOf(t));
  });
  row.addEventListener('dblclick', () => playFrom(list, list.indexOf(t)));
  row.addEventListener('contextmenu', e => trackMenu(e, t, list));
  return row;
}

/* 一张表只留一个 Tab 停点：第一行 tabindex=0，其余 -1；焦点进哪行就轮到哪行。
   这样 Tab 不会在 300 行里逐个停，↑↓ 才是行内换行的主通道。 */
function initRoving(container) {
  container.querySelectorAll('.row').forEach((r, i) => { r.tabIndex = i === 0 ? 0 : -1; });
}

/* ---- 长列表窗口化 ----------------------------------------------------------
   1792 首 = 21k 个节点、82k px 滚动高度。无头 Chrome 实测（同一台机器、同一份
   曲库）：300 行空闲 60 fps，装满 1792 行后空闲 35 fps、滚动 p50 33 ms；而
   Performance 里 ScriptDuration 只有 9 ms —— 钱花在浏览器为整张列表维护布局、
   命中测试和滚动几何上，不是我们的 JS。所以 DOM 里只留视口前后几十行，用两个
   撑高块补齐滚动条长度：

     .vlist
       .vlist-pad  height = start * 46px
       .row …      （只放窗口内的行）
       .vlist-pad  height = (n - end) * 46px

   行仍由 trackRow 生成：绝对下标写进 dataset.idx，播放上下文仍是完整的 list 数组，
   行号、切歌、收藏、右键菜单、拖拽、键盘换行全部照旧。窗口只在可见区离边缘还剩
   3 行时整体平移，一次只增删边缘那几行；只有拖滚动条/点字母轨这种大跨度跳转才重建
   整窗。当前曲目切换时 setTrack 会给已有行打状态，新进窗口的行由 trackRow 自己带上
   playing/eq，不会出现「滚回来歌就不知道自己正在播」。

   不在 #scroller 里的容器（tools/probe_paging.py 那种挂在 body 上的离线探针）退回
   旧的「一次 append 所有行」——那是探针在量的分页机器，不要用窗口化改变它的语义。 */
const VL_ROW_H = 46;
const VL_ABOVE = 12;
const VL_BELOW = 20;

function makeVirtualList(container, opts) {
  if (container.__vlist && container.__vlist.wrap.isConnected) return container.__vlist;
  const scroller = $('#scroller');
  const wrap = el('div', 'vlist');
  const topPad = el('div', 'vlist-pad');
  const botPad = el('div', 'vlist-pad');
  wrap.appendChild(topPad);
  wrap.appendChild(botPad);
  container.appendChild(wrap);
  const vl = {
    wrap, topPad, botPad, opts, data: [], rows: [], start: 0, end: 0, page: null, _top: null,
    /** 列表内容顶端在滚动内容里的像素位置：容器前面可能还有 hero、列头。 */
    topPx() {
      /* 滚动事件每帧都要问这个位置，而 getBoundingClientRect 会强制一次布局。
         同一个视图里列表上方的 hero/表头高度不变，所以算一次就够；resize 和换视图
         才作废（换视图会换掉整个 vl，resize 在下方显式清缓存）。 */
      if (this._top === null) {
        const sr = scroller.getBoundingClientRect();
        this._top = wrap.getBoundingClientRect().top - sr.top + scroller.scrollTop;
      }
      return this._top;
    },
    viewport() {
      const first = Math.floor((scroller.scrollTop - this.topPx()) / VL_ROW_H);
      const count = Math.ceil(scroller.clientHeight / VL_ROW_H) + 2;
      return [first, first + count];
    },
    pads() {
      const t = (this.start * VL_ROW_H) + 'px';
      const b = Math.max(0, (this.data.length - this.end) * VL_ROW_H) + 'px';
      // 每帧都写同一条 style 也会让样式/布局失效，先比再写。
      if (this.topPad.style.height !== t) this.topPad.style.height = t;
      if (this.botPad.style.height !== b) this.botPad.style.height = b;
    },
    roving() {
      if (!this.rows.length) return;
      const focused = this.rows.indexOf(document.activeElement);
      const kept = this.rows.findIndex(r => r.dataset.tabStop === '1');
      const pick = focused >= 0 ? focused : (kept >= 0 ? kept : 0);
      this.rows.forEach((r, i) => {
        r.tabIndex = i === pick ? 0 : -1;
        if (i === pick) r.dataset.tabStop = '1'; else delete r.dataset.tabStop;
      });
    },
    render(force) {
      const n = this.data.length;
      if (!n) {
        this.rows.forEach(r => r.remove());
        this.rows = []; this.start = this.end = 0;
        this.topPad.style.height = this.botPad.style.height = '0px';
        return;
      }
      let [a, b] = this.viewport();
      a = Math.max(0, Math.min(n, a));
      b = Math.max(a + 1, Math.min(n, b));
      this.pads();                       // 数据变长（新一页到了）时先补滚动条高度
      if (!force && this.rows.length && a >= this.start + 3 && b <= this.end - 3) return;
      this.window(Math.max(0, a - VL_ABOVE), Math.min(n, b + VL_BELOW));
    },
    window(ns, ne) {
      const n = this.data.length;
      ns = Math.max(0, Math.min(ns, n));
      ne = Math.max(ns, Math.min(ne, n));
      if (ns === this.start && ne === this.end) { this.pads(); return; }
      const overlap = this.rows.length && Math.min(ne, this.end) > Math.max(ns, this.start);
      if (!overlap) {
        this.rows.forEach(r => r.remove());
        this.rows = [];
        const frag = document.createDocumentFragment();
        for (let i = ns; i < ne; i++) {
          const r = trackRow(this.data[i], i, this.data, this.opts);
          this.rows.push(r); frag.appendChild(r);
        }
        this.wrap.insertBefore(frag, this.botPad);
      } else {
        while (this.rows.length && +this.rows[0].dataset.idx < ns) this.rows.shift().remove();
        while (this.rows.length && +this.rows[this.rows.length - 1].dataset.idx >= ne) this.rows.pop().remove();
        let next = this.rows.length ? +this.rows[this.rows.length - 1].dataset.idx + 1 : ns;
        for (let i = next; i < ne; i++) {
          const r = trackRow(this.data[i], i, this.data, this.opts);
          this.rows.push(r); this.wrap.insertBefore(r, this.botPad);
        }
        let prev = this.rows.length ? +this.rows[0].dataset.idx - 1 : ne - 1;
        for (let i = prev; i >= ns; i--) {
          const r = trackRow(this.data[i], i, this.data, this.opts);
          this.rows.unshift(r); this.wrap.insertBefore(r, this.rows[1] || this.botPad);
        }
      }
      this.start = ns; this.end = ne;
      this.pads();
      this.roving();
    },
    /* 键盘走到窗口边缘：把下一行滚进视野、立即重排窗口，再把焦点交过去。
       返回 false 表示已经是整张表的头/尾，调用方维持原焦点即可。 */
    step(row, dir) {
      const i = +row.dataset.idx + dir;
      if (!(i >= 0 && i < this.data.length)) return false;
      const listTop = this.topPx();
      const top = listTop + i * VL_ROW_H;
      const viewTop = scroller.scrollTop, viewBot = viewTop + scroller.clientHeight;
      if (top < viewTop + 34 || top + VL_ROW_H > viewBot - 6) {
        const y = top < viewTop + 34 ? Math.max(0, top - 34) : top + VL_ROW_H - scroller.clientHeight + 6;
        const old = scroller.style.scrollBehavior;
        scroller.style.scrollBehavior = 'auto';
        scroller.scrollTop = y;
        scroller.style.scrollBehavior = old;
      }
      this.render(true);
      const el2 = this.rows[this.rows.findIndex(r => +r.dataset.idx === i)];
      if (el2) { el2.focus({ preventScroll: true }); return true; }
      return false;
    },
  };
  wrap.__vl = vl;
  container.__vlist = vl;
  S._vlist = vl;
  /* 列表顶端的缓存不能只信第一次量到的值：专辑页先挂列表、随后才把 hero 的
     innerHTML 填上（列表会整块下移 200px），播放面板开合也会改视图宽度。
     盯住整个 #view 的尺寸，变了就作废缓存；下一次滚动再用新的顶端算窗口。
     窗口平移本身由上下 pad 互补，总高不变，不会触发这里。 */
  if (typeof ResizeObserver !== 'undefined') {
    const view = container.closest('#view') || container;
    vl._ro = new ResizeObserver(() => { vl._top = null; });
    vl._ro.observe(view);
  }
  // 一条滚动入口同时做两件事：移动窗口、触发分页。（setupPaging 只把 page 挂进来，
  // 不再自己抢 scroller.onscroll，否则两者只能活一个。）
  scroller.onscroll = () => { vl.render(); if (vl.page) vl.page(); };
  return vl;
}

let _vlResizeTimer = 0;
window.addEventListener('resize', () => {
  clearTimeout(_vlResizeTimer);
  _vlResizeTimer = setTimeout(() => {
    const vl = S._vlist;
    if (vl && vl.wrap.isConnected) { vl._top = null; vl.render(true); }
  }, 140);
});

function renderTracks(container, tracks, opts = {}) {
  if (!tracks.length) {
    container.appendChild(el('div', 'section', opts.empty || '这里还是空的。'));
    return;
  }
  const scroller = $('#scroller');
  if (!scroller || !scroller.contains(container)) {
    tracks.slice(0, opts.limit || tracks.length).forEach((t, i) => container.appendChild(trackRow(t, i, tracks, opts)));
    initRoving(container);
    return;
  }
  const vl = makeVirtualList(container, opts);
  vl.data = opts.limit ? tracks.slice(0, opts.limit) : tracks;
  vl.rows.forEach(r => r.remove());
  vl.rows = []; vl.start = vl.end = 0;
  vl.render(true);
}

/* 统一的分页列表入口：先渲染一页，滚到底再补下一页。
   返回 {items, total, fetchAll}：
     items   —— 已经渲染出来的那些行，同时是点击播放的「上下文队列」（setupPaging 往里 push）；
     total   —— 服务端总数，用来写副标题和判断翻到底没有；
     fetchAll —— 「播放全部/随机」这类显式要整串的动作走它，不受 LIST_PAGE 限制。
   为什么必须分页而不是「limit 写大一点」：2000 首的歌单实测整页 JSON 1.3 MB、
   2000 个 DOM 行 × 5 个监听器，而首屏按 --row-h 46px 只装得下十几行。 */
async function pagedTracks(container, url, emptyMsg, opts = {}) {
  const sep = url.indexOf('?') >= 0 ? '&' : '?';
  const page = (offset) => api(`${url}${sep}limit=${LIST_PAGE}&offset=${offset}`);
  const first = await page(0);
  const items = first.items || [];
  const total = typeof first.total === 'number' ? first.total : items.length;
  renderTracks(container, items, { ...opts, empty: emptyMsg });
  if (items.length) setupPaging(container, page, items, total, opts);
  return {
    items, total,
    fetchAll: async () => ((await api(`${url}${sep}limit=${FETCH_ALL}`)).items) || [],
  };
}

/* -------------------------------------------------------------- 各视图 */
async function viewHome(v) {
  const [rec, songs, albums] = await Promise.all([
    api('history?limit=8').then(r => r.items),
    api('tracks?limit=8&order=added').then(r => r.items),
    // 首页只要 8 张封面，以前不带 limit 会把 640 张专辑连同 ids 一起拖下来再切片。
    api('albums?limit=8').then(r => r.items),
  ]);
  $('#viewSub').textContent = `${S.stats.tracks} 首 · ${S.stats.albums} 张专辑`;
  v.innerHTML = '';
  if (!S.stats.tracks) { viewEmptyLibrary(v); return; }
  S._homeNewest = songs[0] || null;
  const greet = hourGreeting();
  const hero = el('div', 'section');
  hero.innerHTML = `<div class="hero">
      <div class="hero-art" id="homeHeroArt"></div>
      <div class="hero-meta">
        <div class="hero-kind" id="homeHeroKind">${esc(greet)}</div>
        <div class="hero-title" id="homeHeroTitle"></div>
        <div class="hero-sub" id="homeHeroSub"></div>
        <div class="hero-actions">
          <button class="primary" id="homePlay"></button>
          <button class="ghost" id="homeRadio">${icon('radio')} 随意电台</button>
        </div>
      </div></div>`;
  v.appendChild(hero);
  syncHomeHero();
  $('#homePlay').onclick = () => S.queue.length ? togglePlay() : playFrom(songs, 0);
  $('#homeRadio').onclick = () => location.hash = '#/radio';
  if (rec.length) v.appendChild(section('最近播放', rowSection(rec), '全部', () => location.hash = '#/history'));
  v.appendChild(section('新入库', rowSection(songs), '全部', () => location.hash = '#/songs'));
  v.appendChild(section('专辑', cardGrid(albums.map(a => ({
    name: a.album, sub: a.artist, img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    round: false, onClick: () => location.hash = albumHash(a),
    onPlay: () => playAlbum(a),
  }))), '全部', () => location.hash = '#/albums'));
}
function hourGreeting() {
  const h = new Date().getHours();
  return h < 5 ? '凌晨了，来点安静的' : h < 11 ? '早上好' : h < 14 ? '午间' : h < 18 ? '下午好' : h < 23 ? '晚上好' : '夜深了';
}
/** 首页 hero 是「此刻」的真实读数：有当前曲目就显示它，没有才退到最新入库。
    它会被换曲/播放态变化叫醒，所以只改内容、不重建节点 —— 否则正在播放的封面
    每按一次暂停都会闪一下。 */
function syncHomeHero() {
  const art = $('#homeHeroArt');
  if (!art) return;
  const t = S.track || S._homeNewest;
  const hasArt = !!(t && t.has_cover);
  const key = hasArt ? String(t.id) : '';
  if (art.dataset.key !== key) {
    art.dataset.key = key;
    art.innerHTML = hasArt
      ? `<img src="/api/cover?track_id=${t.id}" alt="">`
      : `<div class="art-placeholder">${icon('disc')}</div>`;
  }
  const title = $('#homeHeroTitle'), sub = $('#homeHeroSub'), play = $('#homePlay');
  if (S.track) {
    const c = S.track;
    const who = [c.artist, c.album].filter(Boolean).join(' · ');
    title.textContent = c.title;
    sub.textContent = `${S.playing ? '正在播放' : '已暂停'}${who ? ' · ' + who : ''}`;
    play.innerHTML = `${icon(S.playing ? 'pause' : 'play')} ${S.playing ? '暂停' : '继续播放'}`;
  } else if (t) {
    title.textContent = t.title;
    sub.textContent = `新入库 · ${t.artist || '未知艺人'}`;
    play.innerHTML = `${icon('play')} 播放最新入库`;
  } else {
    title.textContent = '今天想听点什么';
    sub.textContent = `${S.stats.plays ? `已播放 ${S.stats.plays} 次 · ` : ''}曲库总时长 ${fmtLong(S.stats.duration)}`;
    play.innerHTML = `${icon('play')} 随机播放`;
  }
}
function section(title, node, linkLabel, linkFn) {
  const s = el('div', 'section');
  const h = el('div', 'section-head');
  h.innerHTML = `<h2>${esc(title)}</h2>`;
  if (linkLabel) {
    const b = el('button', 'link');
    b.innerHTML = `${esc(linkLabel)} ${icon('right')}`;
    b.onclick = linkFn; h.appendChild(b);
  }
  s.appendChild(h); s.appendChild(node);
  return s;
}
function rowSection(tracks) {
  const box = el('div');
  tracks.slice(0, 8).forEach((t, i) => box.appendChild(trackRow(t, i, tracks)));
  initRoving(box);
  return box;
}
function cardGrid(items) {
  const g = el('div', 'grid');
  items.forEach((it, i) => {
    // 第一张不跳过布局：_renderOnce 结束时靠它量出离屏卡片该占多高（见 app.css .card）。
    // 每张都量没有意义 —— 同一视图里列宽一致，一张就够。
    const c = el('div', 'card' + (i === 0 ? ' card-measure' : ''));
    c.innerHTML = `<div class="card-art ${it.round ? 'round' : ''}">
        ${it.img ? `<img loading="lazy" decoding="async" src="${it.img}" alt="">` : `<div class="art-placeholder">${icon(it.round ? 'artist' : 'disc')}</div>`}
        <button class="card-play" title="播放">${icon('play')}</button></div>
      <div class="card-name">${esc(it.name)}</div><div class="card-sub">${esc(it.sub || '')}</div>`;
    c.onclick = it.onClick;
    c.querySelector('.card-play').onclick = (e) => { e.stopPropagation(); it.onPlay && it.onPlay(); };
    c.oncontextmenu = it.onMenu ? (e => it.onMenu(e)) : null;
    g.appendChild(c);
  });
  return g;
}
/** 搜索页的紧凑结果条：一枚 30px 缩略图 + 两行字，点一下进艺术家/专辑。
    它和 cardGrid 的差别不是装饰，而是「别让六个结果占掉两屏」——搜索的第一需求
    仍然是下面的歌曲列表。 */
function matchRow(items) {
  const row = el('div', 'match-row');
  items.forEach(it => {
    const b = el('button', 'match');
    b.type = 'button';
    b.innerHTML = `<span class="match-art${it.round ? ' round' : ''}">${it.img
      ? `<img loading="lazy" decoding="async" src="${it.img}" alt="">`
      : icon(it.round ? 'artist' : 'disc')}</span>
      <span class="match-text"><b>${esc(it.name)}</b><em>${esc(it.sub || '')}</em></span>`;
    b.title = it.sub ? `${it.name} · ${it.sub}` : it.name;
    b.onclick = it.onClick;
    row.appendChild(b);
  });
  return row;
}

/* 给 content-visibility 的卡片算占位高。
   contain-intrinsic-size 写的是内容盒高度，而卡片还有 6px 下内边距、宽度又随窗口变，
   所以不能拿一个常数糊过去：差 6px/行，443 张专辑累计起来 A–Z 就会跳偏一个字母。
   量最靠前那张仍在布局的卡（.card-measure；没有就挑一张视口内的），减掉纵向内边距，
   写回 #view 的 --card-ih。量不到（还没卡片）就保持默认值，不影响功能。 */
function calibrateCardIntrinsic(v) {
  if (!v) return;
  let card = v.querySelector('.card.card-measure');
  if (!card) {
    const sc = $('#scroller');
    const sr = sc && sc.getBoundingClientRect();
    card = Array.prototype.find.call(v.querySelectorAll('.card'), (n) => {
      const r = n.getBoundingClientRect();
      return !sr || (r.bottom > sr.top && r.top < sr.bottom);
    });
  }
  if (!card) return;
  const cs = getComputedStyle(card);
  const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  const h = card.getBoundingClientRect().height - pad;
  if (h > 0.5) v.style.setProperty('--card-ih', (Math.round(h * 100) / 100) + 'px');
  v.querySelectorAll('.card.card-measure').forEach(n => n.classList.remove('card-measure'));
}

/* 窗口改宽后列宽也跟着变：占位高要重算，否则离屏卡片的估算会慢慢偏掉。
   只在有卡片墙的视图上动手，180 ms 防抖，不跟 resize 抢帧。 */
let _cardCvTimer = 0;
window.addEventListener('resize', () => {
  clearTimeout(_cardCvTimer);
  _cardCvTimer = setTimeout(() => {
    const v = $('#view');
    if (v && v.querySelector('.card')) calibrateCardIntrinsic(v);
  }, 180);
});

async function viewSongs(v, st) {
  const q = st.q;
  const letter = (q.letter || '').toUpperCase();
  const dir = q.dir || 'asc';
  const base = q.artist ? `&artist=${encodeURIComponent(q.artist)}` : '';
  // 有字母 → order=az&letter=L：服务端把那一块单独取出来按码点序分页（落点即那块）；
  // 没字母 → 维持原来的排序（default/标题…），整条目录照旧懒加载。
  const url = letter
    ? `tracks?order=az&dir=${dir}&letter=${encodeURIComponent(letter)}${base}`
    : `tracks?order=${q.order || 'default'}&dir=${dir}${base}`;
  v.innerHTML = '';
  v.appendChild(listHeader([{ t: '#', sort: '' }, { t: '标题', sort: 'title' }, { t: '艺术家', sort: 'artist' }, { t: '专辑', sort: 'album', cls: 'albumcell' }, { t: '时长', sort: 'duration' }, { t: '' }]));
  const [r, idx] = await Promise.all([
    pagedTracks(v, url, '没有匹配的歌曲。先在左下方添加一个音乐文件夹。'),
    api(`index?order=az&dir=${dir}${base}`).catch(() => ({ blocks: [] })),
  ]);
  const present = new Set((idx.blocks || []).map(b => b.letter));
  azRail($('.main') || v, present, letter, (L) => {
    const nl = L === letter ? '' : L;   // 再点一次当前字母＝取消，回到整条目录
    location.hash = `#/songs/?${nl
      ? `order=az&dir=${dir}&letter=${encodeURIComponent(nl)}${base}`
      : `order=${q.order || 'default'}&dir=${dir}${base}`}`;
  });
  $('#viewSub').textContent = letter ? `${letter} · ${r.total} 首` : `共 ${r.total} 首`;
}

async function viewFavorites(v) {
  v.innerHTML = '';
  const bar = el('div', 'hero-actions');
  bar.innerHTML = `<button class="primary">${icon('play')} 播放全部</button><button class="ghost">${icon('shuffle')} 随机</button>`;
  v.appendChild(bar);
  const r = await pagedTracks(v, 'favorites', '还没有喜欢的歌。点歌曲右侧的心形收藏。');
  $('#viewSub').textContent = r.items.length === r.total && r.total
    ? `${r.total} 首 · ${fmtLong(r.items.reduce((a, t) => a + (t.duration || 0), 0))}`
    : `${r.total} 首`;
  if (!r.total) {
    // 空页上没有「播全部/随机」的对象：按钮消失，换成能去别处的空态。
    v.innerHTML = '';
    const es = el('div', 'empty-state');
    es.innerHTML = `<div class="es-glyph">${icon('heart')}</div>
      <h3>还没有喜欢的歌</h3>
      <p>在歌曲行尾点心形，或右键选「喜欢」。收藏会在这里聚成一张随时能播的歌单。</p>
      <button class="ghost">去听点什么</button>`;
    es.querySelector('button').onclick = () => { location.hash = '#/songs'; };
    v.appendChild(es);
    return;
  }
  // 「播放全部」是显式要整串：不能只播已经渲染出来的这一页。
  bar.children[0].onclick = () => playAll(r, '我喜欢的');
  bar.children[1].onclick = () => playAll(r, '我喜欢的（随机）', true);
}

/* 播放全部：整串取回来再开播。
   为什么不「先播这一页、后面偷偷补」：补队列要改 S.queue/S.qi/S.shuffleOrder 三处下标，
   换歌瞬间正好撞上自动下一首就会跳错；而实测 2000 首整串只要 67 ms（tools/scale_after.log，
   20k 库、本机 keep-alive），点一下等 0.07 秒完全感觉不到。分页省的是「进页面就渲染 2000 行」
   那 1.3 MB JSON + 2000 个 DOM 行的钱，不是省用户点这一次按钮的钱。 */
async function playAll(r, context, shuffle) {
  if (!r.total) { toast('这里还没有歌'); return; }
  let all = r.items;
  if (r.total > r.items.length) {
    try { all = await r.fetchAll(); } catch (e) { /* 取整串失败就用手上这一页，别什么都不播 */ }
  }
  if (!all.length) { toast('这里还没有歌'); return; }
  playFrom(shuffle ? shuffled(all) : all, 0, context);
}

async function viewHistory(v) {
  const items = (await api('history?limit=100')).items;
  $('#viewSub').textContent = `${items.length} 首`;
  v.innerHTML = '';
  renderTracks(v, items, { empty: '还没播放过任何歌曲。' });
}

async function viewRadio(v) {
  // 随机池交给服务端抽：以前是 limit=5000 把整库取回浏览器再洗，20k 首的库等于
  // 每进一次「随意电台」就拉几十 MB JSON，而页面上只放 60 行。
  const [r, head] = await Promise.all([
    api('tracks?limit=1&order=default'),
    api('tracks?limit=60&order=random'),
  ]);
  const pool = head.items;
  if (!pool.length) { viewEmptyLibrary(v); return; }
  $('#viewSub').textContent = `从 ${r.total} 首中随机 ${pool.length} 首`;
  v.innerHTML = '';
  const bar = el('div', 'hero-actions');
  bar.innerHTML = `<button class="primary">${icon('play')} 开始连播</button><button class="ghost">${icon('refresh')} 换一批</button>`;
  v.appendChild(bar);
  bar.children[0].onclick = () => playFrom(pool, 0, '随意电台');
  bar.children[1].onclick = () => renderView();
  renderTracks(v, pool);
}

async function viewAlbums(v, st) {
  const items = (await api('albums')).items;
  $('#viewSub').textContent = `${items.length} 张`;
  v.innerHTML = '';
  const present = azSections(v, items, a => ({
    name: a.album, sub: `${a.artist}${a.year ? ' · ' + a.year : ''}`,
    img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    onClick: () => location.hash = albumHash(a),
    onPlay: () => playAlbum(a),
  }));
  if (items.length) azScrollRail(v, present);
}
/* 专辑的身份是 (专辑名, 分组艺人)：曲库里「单曲」这名字被十几位艺人共用，
   只拿专辑名当键会把不同人的歌混进同一页、同一张卡片的播放也串。 */
function albumHash(a) {
  return `#/album/${encodeURIComponent(a.album)}`
    + (a.artist ? `?artist=${encodeURIComponent(a.artist)}` : '');
}
function albumTracksUrl(a) {
  return `tracks?album=${encodeURIComponent(a.album)}`
    + (a.artist ? `&group_artist=${encodeURIComponent(a.artist)}` : '');
}

function playAlbum(a) {
  return playQuery(albumTracksUrl(a), `专辑 · ${a.artist ? a.artist + ' / ' : ''}${a.album}`)();
}

async function viewArtists(v) {
  const items = (await api('artists')).items;
  $('#viewSub').textContent = `${items.length} 位`;
  v.innerHTML = '';
  const present = azSections(v, items, a => ({
    name: a.artist, sub: `${a.tracks} 首 · ${a.albums} 张专辑`, round: true,
    img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    onClick: () => location.hash = `#/artist/${encodeURIComponent(a.artist)}`,
    onPlay: () => playArtist(a.artist),
  }));
  if (items.length) azScrollRail(v, present);
}
function playArtist(name) {
  return playQuery(`tracks?artist=${encodeURIComponent(name)}`, `艺术家 · ${name}`, true)();
}

async function viewGenres(v) {
  const items = (await api('genres')).items;
  $('#viewSub').textContent = `${items.length} 种`;
  v.innerHTML = '';
  if (!items.length) { v.appendChild(el('div', 'section', '标签里没有流派信息（多数本地文件不写流派，属正常现象）。')); return; }
  v.appendChild(cardGrid(items.map(g => ({
    name: g.name, sub: `${g.tracks} 首`, img: '',
    onClick: () => { location.hash = `#/genre/${encodeURIComponent(g.name)}` },
  }))));
}

/* 以前点流派是「先跳 #/songs 再把整页 DOM 覆掉」：地址栏写着 songs、侧栏高亮对不上、
   刷新一下就回到全部歌曲，而且那一屏是不分页的整串。现在走真路由。 */
async function viewGenre(v, st) {
  const name = st.args[0];
  v.innerHTML = '';
  v.appendChild(listHeader([{ t: '#' }, { t: '标题' }, { t: '艺术家' }, { t: '专辑', sort: 'album' }, { t: '时长', sort: 'duration' }, { t: '' }]));
  const r = await pagedTracks(v, `tracks?genre=${encodeURIComponent(name)}`, `「${name}」里还没有歌。`);
  $('#viewSub').textContent = `${r.total} 首`;
}

async function viewAlbum(v, st) {
  const name = st.args[0];
  let groupArtist = st.q.artist || '';
  // 旧链接（升级前存下的 #/album/名字，或是用户手抄的短地址）没有艺人身份。
  // 只有一组同名专辑时直接补全地址；有多组时绝不替用户挑一个——「单曲」这种名字
  // 在曲库里被十几位艺人共用，猜错就是整页别人的歌。
  const all = await api('albums').then(x => x.items).catch(() => null);
  if (all) {
    const groups = all.filter(x => x.album === name);
    if (!groups.some(g => g.artist === groupArtist)) groupArtist = '';
    if (!groupArtist) {
      if (!groups.length) { v.innerHTML = ''; v.appendChild(el('div', 'section', '找不到这张专辑。')); return; }
      if (groups.length > 1) {
        v.innerHTML = '';
        v.appendChild(section(`《${name}》有 ${groups.length} 个版本`, cardGrid(groups.map(g => ({
          name: g.album, sub: `${g.artist} · ${g.tracks} 首`,
          img: g.cover_track ? `/api/cover?track_id=${g.cover_track}` : '', round: false,
          onClick: () => { location.hash = albumHash(g); },
          onPlay: () => playAlbum(g),
        })))));
        return;
      }
      groupArtist = groups[0].artist;
      const fixed = albumHash({ album: name, artist: groupArtist });
      S.viewState.q = { ...st.q, artist: groupArtist };
      S._routedHash = fixed;
      try { history.replaceState(null, '', fixed); } catch (e) { }
    }
  }
  v.innerHTML = '';
  const hero = el('div', 'hero');
  const list = el('div', 'tracklist');
  list.appendChild(listHeader([{ t: '#' }, { t: '标题' }, { t: '艺术家' },
    { t: '专辑', sort: 'album', cls: 'albumcell' }, { t: '时长', sort: 'duration' }, { t: '' }]));
  v.appendChild(hero); v.appendChild(list);
  const r = await pagedTracks(list, albumTracksUrl({ album: name, artist: groupArtist }),
    '找不到这张专辑。', { trackNo: true });
  if (!r.items.length) { hero.remove(); list.textContent = ''; v.appendChild(el('div', 'section', '找不到这张专辑。')); return; }
  const a = r.items[0];
  const meta = (all || []).find(x => x.album === name && (!groupArtist || x.artist === groupArtist)) || {};
  const who = groupArtist || meta.artist || a.album_artist || a.artist || '';
  // 首数和总时长用聚合接口的值：分页后手上只有第一页，拿它求和会少报。
  hero.innerHTML = `<div class="hero-art">${meta.cover_track ? `<img loading="lazy" src="/api/cover?track_id=${meta.cover_track}" alt="">` : `<div class="art-placeholder">${icon('disc')}</div>`}</div>
    <div class="hero-meta"><div class="hero-kind">专辑${a.year ? ' · ' + a.year : ''}</div>
      <div class="hero-title">${esc(name)}</div>
      <div class="hero-sub">${esc(who)}${a.genre ? ' · ' + esc(a.genre) : ''}</div>
      <div class="hero-stats">${meta.tracks ?? r.total} 首 · ${fmtLong(meta.duration ?? r.items.reduce((s, t) => s + t.duration, 0))}</div>
      <div class="hero-actions"><button class="primary">${icon('play')} 播放</button><button class="ghost">${icon('shuffle')} 随机</button><button class="ghost">${icon('plus')} 加入歌单</button></div>
    </div>`;
  const btns = hero.querySelectorAll('.hero-actions button');
  const ctx = `专辑 · ${who ? who + ' / ' : ''}${name}`;
  btns[0].onclick = () => playAll(r, ctx);
  btns[1].onclick = () => playAll(r, ctx + '（随机）', true);
  btns[2].onclick = (e) => addToPlaylistMenu(e, r.items.map(t => t.id));
}

/* 「把这一堆全部排队」的公共入口：专辑卡上的 ▶、艺术家卡上的 ▶、文件夹页的播放全部……
   这些动作要的是整串，所以显式取满（服务端 cap 5000），而不是靠页面里那一页。 */
function playQuery(url, context, shuffle) {
  return async () => {
    const sep = url.indexOf('?') >= 0 ? '&' : '?';
    let rows = [];
    try { rows = ((await api(`${url}${sep}limit=${FETCH_ALL}`)).items) || []; }
    catch (e) { return toast('取歌曲列表失败：' + e.message, 'err'); }
    if (!rows.length) return toast('这里还没有歌');
    playFrom(shuffle ? shuffled(rows) : rows, 0, context);
  };
}

async function viewArtistDetail(v, st) {
  const name = st.args[0];
  v.innerHTML = '';
  const hero = el('div', 'hero');
  v.appendChild(hero);
  // 首数/张数/总时长都来自聚合接口（一次查询算好的），曲目只要前 8 行做「热门曲目」。
  // 以前这里为了一屏 8 行去取 limit=2000 的整串，等于每进一位艺术家的主页都传一遍他全部的歌。
  const [meta, albums, tracks] = await Promise.all([
    api('artists').then(r => r.items.find(a => a.artist === name) || {}),
    api('albums').then(r => r.items.filter(a => a.artist === name)),
    api(`tracks?artist=${encodeURIComponent(name)}&limit=8`).then(r => r.items),
  ]);
  if (!tracks.length) { v.innerHTML = '<div class="section">找不到这位艺术家。</div>'; return; }
  hero.innerHTML = `<div class="hero-art"${meta.cover_track ? '' : ' style="border-radius:50%"'}>${meta.cover_track ? `<img loading="lazy" src="/api/cover?track_id=${meta.cover_track}" alt="" style="border-radius:50%">` : `<div class="art-placeholder">${icon('artist')}</div>`}</div>
    <div class="hero-meta"><div class="hero-kind">艺术家</div><div class="hero-title">${esc(name)}</div>
      <div class="hero-sub">${meta.tracks ?? tracks.length} 首 · ${albums.length} 张专辑 · ${fmtLong(meta.duration || 0)}</div>
      <div class="hero-actions"><button class="primary">${icon('play')} 播放全部</button><button class="ghost">${icon('shuffle')} 随机</button></div></div>`;
  const b = hero.querySelectorAll('button');
  b[0].onclick = playQuery(`tracks?artist=${encodeURIComponent(name)}`, `艺术家 · ${name}`);
  b[1].onclick = playQuery(`tracks?artist=${encodeURIComponent(name)}`, `艺术家 · ${name}（随机）`, true);
  if (albums.length) v.appendChild(section('专辑', cardGrid(albums.map(a => ({
    name: a.album, sub: `${a.tracks} 首${a.year ? ' · ' + a.year : ''}`,
    img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    onClick: () => location.hash = albumHash(a), onPlay: () => playAlbum(a),
  })))));
  v.appendChild(section('热门曲目', rowSection(tracks)));
}

async function viewFolders(v) {
  const flat = !!S.settings.folder_flat;
  const r = await api(flat ? 'folders?flat=1' : 'folders');
  const items = r.items;
  S.excludes = r.excluded || [];
  v.innerHTML = '';
  // 顶栏：树/平铺切换。两个形态解决的不是同一个问题 —— 树看「我的库是怎么组织的」，
  // 平铺把每一层目录拉平成一行（按名字排），找「某个具体的文件夹」不用一路点开五层。
  // 每行带的曲目数/时长，是树那种形态里要展开才看得到的东西。选择记进设置，下次打开还在同一个形态。
  const bar = el('div', 'view-bar');
  const setFlat = (x) => { S.settings.folder_flat = x; saveSettings({ folder_flat: x }); renderView(); };
  const bt = el('button', 'ghost' + (flat ? '' : ' on'), '树状'), bf = el('button', 'ghost' + (flat ? ' on' : ''), '平铺');
  bt.onclick = () => setFlat(false);
  bf.onclick = () => setFlat(true);
  bar.append(bt, bf);
  v.appendChild(bar);
  $('#viewSub').textContent = flat ? `${items.length} 个目录 · 右键可排除扫描`
                                   : `${items.length} 个根目录 · 右键可排除扫描`;
  if (!items.length) { viewEmptyLibrary(v); return; }
  if (flat) {
    const list = el('div', 'folder-flat');
    items.forEach(d => {
      const row = el('div', 'ff-row');
      row.innerHTML = `<span class="ff-nm">${icon('folder')}<span>${esc(d.name)}</span></span>
        <span class="ff-ct">${d.tracks} 首</span><span class="ff-du">${fmtLong(d.duration)}</span>
        ${d.excluded ? '<span class="ff-ex" title="已在排除名单里，下次扫描起效">已排除</span>' : ''}`;
      row.title = d.path;
      row.onclick = () => { location.hash = `#/folder/${encodeURIComponent(d.path)}`; };
      row.oncontextmenu = (e) => folderMenu(e, d.path, d.name, d.excluded);
      list.appendChild(row);
    });
    v.appendChild(list);
    return;
  }
  items.forEach(lib => {
    const box = el('div', 'section');
    const head = el('div', 'section-head');
    head.innerHTML = `<h2 class="folder-title" title="${esc(lib.path)}">${icon('folder')}<span>${esc(lib.name)}</span></h2>`;
    const b1 = el('button', 'link', '播放全部'), b2 = el('button', 'link', '重新扫描'), b3 = el('button', 'link', '移除');
    b1.onclick = playQuery(`tracks?folder=${encodeURIComponent(lib.path)}`, `文件夹 · ${lib.name}`);
    b2.onclick = () => startScan({ id: lib.lib_id }, '已开始重新扫描');
    b3.onclick = () => removeLibrary(lib);
    const acts = el('div', 'section-actions');
    acts.append(b1, b2, b3);
    head.append(acts);
    box.appendChild(head);
    box.appendChild(folderTree(lib.children, lib.path));
    v.appendChild(box);
  });
}

/* 文件夹右键菜单：进出播放 + 把这一层加进扫描排除名单。
   排除是服务端的一条持久规则（下次扫描起效），不是把当前列表过滤掉 —— 所以这里必须
   立刻喊一次扫描，让用户看到的行数真的跟着变，否则「排除」听起来像没生效。 */
function folderMenu(ev, path, name, already) {
  ev.preventDefault();
  showMenu(ev.clientX, ev.clientY, [
    { label: '播放此文件夹', icon: '▶', fn: playQuery(`tracks?folder=${encodeURIComponent(path)}`, `文件夹 · ${name}`) },
    { label: '随机播放', icon: '⤨', fn: playQuery(`tracks?folder=${encodeURIComponent(path)}`, `文件夹 · ${name}（随机）`, true) },
    { label: '打开所在位置', icon: '⌂', fn: () => api('window/reveal', { method: 'POST', body: { path } }).then(r => { if (!r.ok) toast(r.error || '打不开', 'err'); }) },
    { sep: 1 },
    { label: already ? '已在排除名单里' : '不再扫描此文件夹', icon: '⊘', danger: !already, disabled: !!already, fn: async () => {
        if (!await confirmDialog(`不再扫描「${name}」？`, '这个目录连同它的子目录都会被跳过。曲目会从库里标记为失效（文件本身不动），随时可以取消排除后重扫找回。')) return;
        try {
          await api('excludes/add', { method: 'POST', body: { path } });
          toast(`已排除「${name}」`, 'ok', 1600);
          startScan({}, '正在按新的排除名单重新扫描');
        } catch (e) { toast('排除失败：' + (e.message || e), 'err'); }
      } },
  ]);
}
function folderTree(children, root) {
  const wrap = el('div', 'tree');
  const build = (nodes) => {
    const box = el('div');
    nodes.forEach(n => {
      const row = el('div', 'tree-node');
      const hasKids = n.children && n.children.length;
      const path = n.path || joinPath(root, n.name);
      row.innerHTML = `<span class="tw">${hasKids ? icon('right') : ''}</span><span class="nm">${icon('folder')}<span>${esc(n.name)}</span></span><span class="ct">${n.tracks || ''}</span>`;
      row.querySelector('.nm').onclick = () => { location.hash = `#/folder/${encodeURIComponent(path)}`; };
      // 右键走的是与平铺列表同一套动作；路径直接用服务端给的 n.path（同名子目录时
      // 按名字拼会拼错，这是建树那次一起修掉的错位）。
      row.oncontextmenu = (e) => folderMenu(e, path, n.name, isExcluded(path));
      box.appendChild(row);
      if (hasKids) {
        const kid = el('div', 'tree-children'); kid.hidden = true;
        kid.appendChild(build(n.children));
        box.appendChild(kid);
        row.querySelector('.tw').onclick = (e) => {
          e.stopPropagation();
          kid.hidden = !kid.hidden; row.classList.toggle('open', !kid.hidden);
        };
      }
    });
    return box;
  };
  wrap.appendChild(build(children || []));
  return wrap;
}
const joinPath = (root, name) => /[\\/]$/.test(root) ? root + name : root + (/^\w:/.test(root) ? '\\' : '/') + name;

/* 只用来决定右键菜单那一行写什么字（权威判定在服务端：excludes/add 幂等、命中会返回
   already）。规则与 db.covers 对齐：大小写不敏感、认整棵子树、两种分隔符都算。
   「两种分隔符都算」必须先把两边归成同一种再比：以前只在结尾那一下试了 '/' 和 '\'
   两种，路径中间混着斜杠时（k 是 c:/a/b、o 是 c:\a）两边就永远对不上，注释里的承诺
   落空。服务端 normalize_path 会把两种都收进反斜杠形式，这里跟着归一次才对得上。 */
function isExcluded(path) {
  const k = dirKey(path);
  if (!k) return false;
  return (S.excludes || []).some(b => {
    const o = dirKey(b);
    return !!o && (k === o || k.startsWith(o + '\\'));
  });
}
const dirKey = (p) => String(p || '').toLowerCase().replace(/[\\/]+$/, '').replace(/\//g, '\\');

async function viewFolder(v, st) {
  const path = st.args[0];
  v.innerHTML = '';
  const hero = el('div', 'hero');
  hero.innerHTML = `<div class="hero-meta"><div class="hero-kind">文件夹</div>
    <div class="hero-title" style="font-size:22px">${esc(path)}</div>
    <div class="hero-actions"><button class="primary">${icon('play')} 播放</button><button class="ghost">${icon('shuffle')} 随机</button></div></div>`;
  const list = el('div', 'tracklist');
  list.appendChild(listHeader([{ t: '#' }, { t: '标题' }, { t: '艺术家' }, { t: '文件名' }, { t: '时长' }, { t: '' }]));
  v.appendChild(hero); v.appendChild(list);
  const r = await pagedTracks(list, `tracks?folder=${encodeURIComponent(path)}`, '这个文件夹里没有可播放的音频。', { filename: true });
  $('#viewSub').textContent = `${r.total} 首`;
  const b = hero.querySelectorAll('button');
  b[0].onclick = playQuery(`tracks?folder=${encodeURIComponent(path)}`, `文件夹 · ${path}`);
  b[1].onclick = playQuery(`tracks?folder=${encodeURIComponent(path)}`, `文件夹 · ${path}（随机）`, true);
}

async function viewPlaylist(v, st) {
  const pid = st.id;
  const p = S.playlists.find(x => String(x.id) === String(pid));
  v.innerHTML = '';
  const hero = el('div', 'hero');
  const list = el('div', 'tracklist');
  list.appendChild(listHeader([{ t: '#' }, { t: '标题' }, { t: '艺术家' }, { t: '专辑' }, { t: '时长' }, { t: '' }]));
  v.appendChild(hero); v.appendChild(list);
  const r = await pagedTracks(list, `playlist/${pid}/tracks`, '把歌曲右键 → 添加到歌单，就能收藏在这里。');
  S.viewState.list = r.items;
  const cov = r.items.find(t => t.has_cover);
  hero.innerHTML = `<div class="hero-art">${cov ? `<img loading="lazy" src="/api/cover?track_id=${cov.id}" alt="">` : `<div class="art-placeholder">${icon('music')}</div>`}</div>
    <div class="hero-meta"><div class="hero-kind">歌单</div><div class="hero-title">${esc(p ? p.name : '歌单')}</div>
      <div class="hero-stats">${r.total} 首</div>
      <div class="hero-actions"><button class="primary">${icon('play')} 播放</button><button class="ghost">${icon('shuffle')} 随机</button><button class="ghost">${icon('more')} 管理</button></div></div>`;
  const b = hero.querySelectorAll('.hero-actions button');
  b[0].onclick = () => playAll(r, `歌单 · ${p && p.name}`);
  b[1].onclick = () => playAll(r, `歌单 · ${p && p.name}（随机）`, true);
  b[2].onclick = (e) => playlistMenu(e, p || { id: pid });
  $('#viewSub').textContent = `${r.total} 首`;
  // 拖进来加歌：监听器挂在本次视图自己的 list 节点上。
  // 以前挂在常驻的 #view 上，每进一次歌单就多一对 dragover/drop，而且闭包里写死的是
  // 那一次的 pid —— 之后拖到任何页面都会往第一个歌单里塞歌，监听器也永远摘不掉。
  list.addEventListener('dragover', e => {
    // 只认我们自己的载荷：从资源管理器拖音频文件经过列表时不该亮成「可放」，
    // 那是窗口级导入逻辑的地盘。
    if (Array.prototype.indexOf.call(e.dataTransfer.types, 'text/salt-tracks') < 0) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'copy';
    list.classList.add('dragover');
  });
  list.addEventListener('dragleave', () => list.classList.remove('dragover'));
  list.addEventListener('drop', async e => {
    e.preventDefault(); list.classList.remove('dragover');
    const ids = (e.dataTransfer.getData('text/salt-tracks') || '').split(',').filter(Boolean).map(Number);
    if (ids.length) await addToPlaylist(pid, ids);
  });
}

async function viewSearch(v, st) {
  const q = (st.q.q || '').trim();
  $('#viewSub').textContent = q ? `关键词「${q}」` : '';
  v.innerHTML = '';
  if (!q) {
    // 空查询不是「错误」，是搜索页的落地态：给它和收藏空页同一套版式，
    // 而不是一行漂在左上角的灰字。
    const es = el('div', 'empty-state');
    es.innerHTML = `<div class="es-glyph">${icon('search')}</div>
      <h3>搜索整个曲库</h3>
      <p>输入歌曲、专辑或艺术家名称，边打字边出结果。搜索会匹配标题、艺术家、专辑、流派和文件名。<br>按 <kbd>/</kbd> 随时聚焦搜索框，按 <kbd>Esc</kbd> 退出。</p>`;
    v.appendChild(es);
    return;
  }
  // 边打字边搜会并发发好几组请求，谁后回来谁盖页面 —— 慢的那次往往是旧关键词，
  // 于是「搜 j 却显示 ja 的结果」。每轮搜索一个序号，回来对不上号就整段丢掉。
  const seq = (S._searchSeq = (S._searchSeq || 0) + 1);
  const stale = () => S._searchSeq !== seq;
  // 专辑/艺术家只要前几行，其余交给服务端截：以前是不带 limit 拉完整张表再在浏览器里
  // slice，搜索「a」时等于为了 8 张卡片解析几百行 JSON。total 仍然由服务端给全量数字。
  // 4 张就够当作「可能有这张专辑」的提示：更多会把歌曲区推到两屏之后。
  const [songPage, albumRes, artistRes] = await Promise.all([
    api(`tracks?q=${encodeURIComponent(q)}&limit=${LIST_PAGE}`),
    api(`albums?q=${encodeURIComponent(q)}&limit=4`),
    api(`artists?q=${encodeURIComponent(q)}&limit=6`),
  ]);
  if (stale()) return;
  const songs = songPage.items || [];
  const albums = albumRes.items || [];
  const artists = artistRes.items || [];
  const total = typeof songPage.total === 'number' ? songPage.total : songs.length;
  const albumTotal = typeof albumRes.total === 'number' ? albumRes.total : albums.length;
  const artistTotal = typeof artistRes.total === 'number' ? artistRes.total : artists.length;
  // 副标题报的是「全量命中」，不是「这一页放了几张卡片」：以前写 albums.length，
  // 搜出 40 张专辑只显示 8 张，数字和下面「全部」的语义对不上。
  $('#viewSub').textContent = `${total} 首 · ${albumTotal} 张专辑 · ${artistTotal} 位艺术家`;
  // 搜索页的主角是歌：艺术家/专辑用紧凑的结果条，不再用 200px 的大卡片把
  // 「歌曲」推到两屏之后。条目的点击仍然直达艺术家/专辑页。
  if (artists.length) v.appendChild(section('艺术家', matchRow(artists.map(a => ({
    name: a.artist, sub: `${a.tracks} 首`, round: true,
    img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    onClick: () => location.hash = `#/artist/${encodeURIComponent(a.artist)}`,
  })))));
  if (albums.length) v.appendChild(section('专辑', matchRow(albums.map(a => ({
    name: a.album, sub: a.artist, img: a.cover_track ? `/api/cover?track_id=${a.cover_track}` : '',
    onClick: () => location.hash = albumHash(a),
  })))));
  if (songs.length) {
    const s = el('div', 'section'); const h = el('div', 'section-head');
    // 搜到一整串时最常做的两件事就是「全播」和「打乱全播」；没有这两个入口，
    // 用户只能逐条点，或者先把结果另存成歌单。取数走 playQuery（cap 5000），
    // 不拿当前这一页充数 —— 与专辑/艺术家页的语义保持一致。
    h.innerHTML = `<h2>歌曲</h2><div style="display:flex;gap:14px;align-items:center">
      <button class="link" data-act="all">播放全部</button>
      <button class="link" data-act="shuffle">随机播放</button></div>`;
    const qurl = `tracks?q=${encodeURIComponent(q)}`;
    h.querySelector('[data-act="all"]').onclick = playQuery(qurl, `搜索 · ${q}`);
    h.querySelector('[data-act="shuffle"]').onclick = playQuery(qurl, `搜索 · ${q}（随机）`, true);
    s.appendChild(h);
    v.appendChild(s);
    const url = `tracks?q=${encodeURIComponent(q)}`;
    const page = (offset) => api(`${url}&limit=${LIST_PAGE}&offset=${offset}`);
    renderTracks(s, songs);
    setupPaging(s, page, songs, total);
  }
  if (!songs.length && !albums.length && !artists.length) {
    const es = el('div', 'empty-state');
    es.innerHTML = `<div class="es-glyph">${icon('search')}</div>
      <h3>没有找到「${esc(q)}」</h3>
      <p>换一种写法，或只输入名字的一部分。标题、艺术家、专辑、流派与文件名都会被匹配。</p>
      <div class="welcome-actions">
        <button class="primary" id="searchReset">清空搜索</button>
        <button class="ghost" id="searchBrowse">浏览所有歌曲</button>
      </div>`;
    v.appendChild(es);
    $('#searchReset').onclick = () => {
      const inp = $('#searchInput'); inp.value = ''; syncSearchClear();
      route('#/' + lastViewHome(), false); inp.focus();
    };
    $('#searchBrowse').onclick = () => { location.hash = '#/songs'; };
  }
}

function viewEmptyLibrary(v) {
  v.innerHTML = '';
  const w = el('div', 'welcome-card');
  w.innerHTML = `<div class="welcome-icon">${icon('folder')}</div><h2>曲库是空的</h2>
    <p>添加一个音乐文件夹，或直接把文件夹拖进窗口。</p>
    <div class="welcome-actions"><button class="primary" id="emptyPick">选择文件夹</button><button class="ghost" id="emptyManual">输入路径</button></div>`;
  v.appendChild(w);
  $('#emptyPick').onclick = pickFolder;
  $('#emptyManual').onclick = manualFolder;
}

function shuffled(list) {
  const a = list.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

function setupPaging(container, fetcher, list, total, opts = {}) {
  const state = { offset: list.length, total, list, done: false, dead: false };
  const scroller = $('#scroller');
  const foot = $('#listFoot');
  // 窗口化列表自己持有 scroller.onscroll（见 makeVirtualList）；分页只把 page 挂上去。
  // 不在 #scroller 里的离线探针容器仍走老路：直接 append 行、自己接管滚动。
  const vl = container.__vlist && container.__vlist.wrap.isConnected ? container.__vlist : null;
  if (vl) vl.opts = opts;
  foot.hidden = state.offset >= state.total;
  const loadPage = async () => {
    foot.dataset.busy = '1'; foot.hidden = false;
    try {
      const r = await fetcher(state.offset);
      // 视图已经换掉（container 不在文档里了）就别再往这个摘掉的节点里塞行：
      // 老代码是这么一路 append 到 detached 节点上，看着没崩，其实是白干一遍。
      // 这个 return 同时跳过了末尾那两行页脚收尾（#listFoot 已经归新视图管）。
      // 但 finally 挡不住：busy 标记会被这里删掉——而那个标记此刻属于**新视图**正在飞的
      // 那次请求，删了它下一次滚动就会拿同一个 offset 重取。所以删之前先认 dead。
      if (!document.body.contains(container)) { state.done = state.dead = true; return; }
      const got = (r.items || []).length;
      if (!got) state.done = true;   // 空页 = 到底了。不置 done 的话「补满一屏」那条 while 会拿同一 offset 无限重取
      (r.items || []).forEach((t, i) => {
        state.list.push(t);
        if (!vl) container.appendChild(trackRow(t, state.offset + i, state.list, opts));
      });
      state.offset += got;
      state.total = r.total ?? state.total;
      if (vl) vl.render();            // 数据变长：撑高块要跟着长，滚动条才有下一屏
    } finally {
      if (!state.dead) delete foot.dataset.busy;   // dead 时这个标记属于下一个视图，不碰
    }
    foot.hidden = state.offset >= state.total;
    if (state.offset >= state.total) state.done = true;
  };
  const handler = async () => {
    if (state.done || foot.dataset.busy) return;
    if (scroller.scrollTop + scroller.clientHeight > scroller.scrollHeight - 600) {
      await loadPage();
      // 第一页没铺满一屏（大窗口 / 行很矮）时 onscroll 永远不会再触发，
      // 用户看到的就是「下面空着但再也没有歌」。这里主动补到能滚为止。
      while (!state.done && !foot.dataset.busy && scroller.scrollHeight <= scroller.clientHeight + 8) {
        await loadPage();
      }
    }
  };
  if (vl) vl.page = handler;
  else scroller.onscroll = handler;
  S._paging = handler;
}

/* ==========================================================================
   播放核心
   ========================================================================== */
const MODES = {
  list_loop: { icon: 'repeat', label: '列表循环' },
  one_loop: { icon: 'repeat-one', label: '单曲循环' },
  shuffle: { icon: 'shuffle', label: '随机播放' },
  normal: { icon: 'arrow-right', label: '顺序播放' },
};
function updateModeButton() {
  const m = MODES[S.settings.play_mode] || MODES.list_loop;
  const label = '播放模式：' + m.label;
  [$('#btnMode'), $('#fsMode')].forEach(b => { if (b) { b.innerHTML = icon(m.icon); nameEl(b, label); } });
}

function playFrom(list, index, context) {
  if (!list || !list.length) return toast('列表是空的');
  S.queue = list.slice();
  S.context = context || VIEW_TITLES[S.view] || '列表';
  S.qi = clamp(index, 0, list.length - 1);
  S.shuffleOrder = S.settings.play_mode === 'shuffle' ? shuffled(S.queue.map((_, i) => i)) : [];
  S.playHist = [];      // 手动选曲 = 开一段新的播放历史，上一曲不该跳回旧列表里的歌
  start(S.queue[S.qi]);
  renderQueue();
}

async function start(track, autoplay = true) {
  if (!track) return;
  cancelHandover(false);       // 手动换曲：正在淡出的那条尾巴先收掉
  cancelPreload();
  resetGains();
  S._hoBlocked = false;        // 新的一首，交接重试额度重新算
  setTrack(track, false);
  if (isNative()) {
    // 原生档：audio 元素整趟不沾（不赋 src），换由服务端 ffmpeg 解码 → WASAPI 出声。
    await startNative(track, autoplay);
    /* 切曲时位置归零、时长换人：位置那一维没有变化量，量程那一维有 —— 不在这里点一次火，
       新版 tick 的时长闸门要等到用户下次动进度条才有机会跑。 */
    nudgeFrames();
    api('play', { method: 'POST', body: { track_id: track.id } }).catch(() => {});
    saveQueue();
    loadLyric(track.id);
    updateMediaSession();
    refreshPreload();          // 原生档这里只负责把可能残留的预载收干净
    return;
  }
  audio.src = `/api/stream/${track.id}`;
  audio.currentTime = 0;
  if (autoplay) await play();
  api('play', { method: 'POST', body: { track_id: track.id } }).catch(() => {});
  saveQueue();
  loadLyric(track.id);
  updateMediaSession();
  refreshPreload();
}
function queuePayload() {
  // S.pos 只在 rAF tick 里更新；窗口被遮挡时 Chromium 会停掉 rAF，进度就冻在上一次的值，
  // 用它持久化会让重启后的进度对不上（实测 5s 存成 20s）。元素时间才是真相 ——
  // 原生档则是 posNow()：它按墙钟从服务端上报的位置外推，同样不跟着 rAF 一起冻。
  const live = posNow();
  const pos = (isFinite(live) && live > 0) ? live : (S.pos || 0);
  return { items: S.queue.map(t => t.id), index: S.qi, context: S.context, track_id: S.track && S.track.id, position: pos };
}

/** 把当前队列写回服务端 —— restoreSession 读的就是它。失败不弹错：队列是「尽力而为」
    的续航数据，为一次没存上打断用户更糟。 */
function saveQueue() {
  api('queue', { method: 'POST', body: queuePayload() }).catch(() => { });
}

function setTrack(t, keepPanel) {
  S.track = t;
  const cover = t.has_cover ? `/api/cover?track_id=${t.id}` : '';
  $('#pTitle').textContent = t.title;
  // 这一行是「艺人 — 专辑」，开了播放条歌词时它会被当前这句词临时顶掉（见 setPlaybarLine）。
  // 换曲这一刻必须先把它放回艺人：上一首的词还挂在 S.lyric 里，直接读会把旧歌词演成新的。
  S.pbArtist = `${t.artist || '未知艺人'}${t.album ? ' — ' + t.album : ''}`;
  setPlaybarLine(true);
  $('#npTitle').textContent = t.title;
  $('#npArtist').textContent = `${t.artist}${t.album ? ' · ' + t.album : ''}`;
  $('#fsTitle').textContent = t.title;
  $('#fsArtist').textContent = `${t.artist} — ${t.album || ''}`;
  setCover(cover);
  $$('.row').forEach(r => {
    const on = +r.dataset.id === t.id;
    r.classList.toggle('playing', on);
    if (on && !r.querySelector('.eq')) {
      const s = el('span', 'eq'); s.innerHTML = '<span></span><span></span><span></span>';
      r.querySelector('.titlecell').appendChild(s);
    } else if (!on) { const e2 = r.querySelector('.eq'); if (e2) e2.remove(); }
  });
  $$('.q-row').forEach(r => r.classList.toggle('on', +r.dataset.id === t.id));
  // 播放条/全屏上的 ♥ 以前只在 toggleFav 里改，所以「下一曲」之后心形还停在上一首的状态。
  syncFavGlyphs(!!t.favorite);
  if (t.favorite === undefined) {
    // 队列是从 /api/queue 恢复的老数据时可能没带 favorite 标志：按 id 补一次，只认最新那首。
    api('track/' + t.id).then(r => {
      const f = r && r.track;
      if (f && S.track && f.id === S.track.id) syncFavGlyphs(!!f.favorite);
    }).catch(() => { });
  }
  document.title = `${t.title} · ${t.artist} — LuHui音乐播放器`;
  if (!keepPanel) renderMeta(t);
  syncHomeHero();
  S.settings.last_track = t.id;
  saveSettings({ last_track: t.id });
}
function setCover(url) {
  const imgs = [$('#pCoverImg'), $('#npCoverImg'), $('#fsCoverImg')];
  imgs.forEach(img => {
    if (url) { img.src = url; img.hidden = false; }
    else { img.removeAttribute('src'); }
  });
  $('#pCover').classList.toggle('noart', !url);
  $('#npCover').classList.toggle('noart', !url);
  $('#fsCover').classList.toggle('noart', !url);
  setBackdrop(url);
  extractAccent(url);
}

async function play() {
  if (isNative()) { await playNative(); return; }
  try {
    await ensureAudioGraph();
    if (!S._ho && !S._hx) resetGains();   // 交接中的淡入淡出不许被这一下抹平
    await audio.play();
    S.playing = true; syncPlayUI();
    // 起播那一刻别抢带宽：等本曲的缓冲跑起来再预取下一首
    clearTimeout(S._warmTimer);
    S._warmTimer = setTimeout(warmNext, 1200);
    refreshPreload();                     // 第二只元素从这一刻起备着下一首
  } catch (e) {
    if (e && e.name !== 'AbortError') {
      if (!audio.src) return;
      toast('无法播放：' + (audio.error ? mediaErr() : e.message), 'err', 5000);
      autoAdvance();
    }
  }
}
function mediaErr() {
  const map = { 3: '解码失败（格式可能不受支持）', 4: '不支持的编码或文件损坏', 2: '网络/读取中断', 1: '已取消' };
  return map[audio.error.code] || ('错误码 ' + audio.error.code);
}
function togglePlay() {
  if (!S.track) {
    if (S.queue.length) return start(S.queue[0]);
    return api('tracks?limit=50&order=random').then(r => r.items.length && playFrom(r.items, 0, '随机'));
  }
  if (_isPaused()) play(); else pauseNow();
}
function syncPlayUI() {
  const on = S.playing;
  $('#btnPlay').classList.toggle('on', on);
  $('#fsPlay').classList.toggle('on', on);
  nameEl($('#btnPlay'), on ? '暂停 (空格)' : '播放 (空格)');
  nameEl($('#fsPlay'), on ? '暂停 (空格)' : '播放 (空格)');
  $('#npCover').parentElement.classList.toggle('paused', !on);
  $('#pCover').classList.toggle('paused', !on);
  $$('.eq').forEach(e => e.parentElement.parentElement.classList.toggle('paused', !on));
  syncAmbient();   // 流光的 play-state 只认 data-play：不在播就停，闲置账单才有得守
  drawSpectrum();   // 循环可能在面板收起时自己停了，这里顺手重新拉起（已在跑就是空操作）
  // 播放态一变，进度条/歌词/时间这几样都可能有得刷：给一个短余量把尾巴走完，然后循环自停。
  // 放在 drawSpectrum() 之后是故意的——暂停那一路要先让频谱循环看到「没帧可要」当场停下来，
  // 不然它会再白画 0.7 秒。
  syncHomeHero();
  nudgeFrames();
  updateMediaSession();
}

/** 纯函数：下一首播队列里的第几首。next() 和 warmNext() 共用这一个答案，免得两份
    逻辑各自演化后「预取了根本不会播的那首」。
    -1 = 该停了（顺序播放走到队尾）；-2 = 猜不准（随机序要重排或还没生成）。 */
function nextIdx(mode, qi, len, order, manual) {
  if (len <= 0) return -1;
  if (mode === 'one_loop' && !manual) return qi;
  if (mode === 'shuffle') {
    if (!order || order.length !== len) return -2;
    const at = order.indexOf(qi);
    if (at < 0) return order[0];
    return at >= len - 1 ? -2 : order[at + 1];
  }
  const i = qi + 1;
  if (i >= len) return mode === 'normal' ? -1 : 0;
  return i;
}

function next(manual = true) {
  if (!S.queue.length) return;
  const mode = S.settings.play_mode;
  let i;
  if (mode === 'shuffle') {
    // 随机模式带状态：走到队尾要重排一轮，这一步没法交给纯函数
    if (!S.shuffleOrder.length) S.shuffleOrder = shuffled(S.queue.map((_, k) => k));
    const at = S.shuffleOrder.indexOf(S.qi);
    i = S.shuffleOrder[(at + 1 + S.shuffleOrder.length) % S.shuffleOrder.length];
    if (at >= S.shuffleOrder.length - 1) { S.shuffleOrder = shuffled(S.queue.map((_, k) => k)); i = S.shuffleOrder[0]; }
  } else {
    i = nextIdx(mode, S.qi, S.queue.length, S.shuffleOrder, manual);
    if (i < 0) { stopPlayback(); return; }
  }
  if (S.qi >= 0 && i !== S.qi) {          // 记下真正播过的那首，单曲循环不记
    S.playHist.push(S.qi);
    if (S.playHist.length > 80) S.playHist.shift();
  }
  S.qi = clamp(i, 0, S.queue.length - 1);
  start(S.queue[S.qi]);
}
function prev() {
  if (!S.queue.length) return;
  // 过了 4 秒再按「上一曲」是重播本曲（椒盐音乐同款两义语义）；
  // 4 秒以内优先按真实播放历史回退——随机模式下「上一首」是刚才那首，不是队列里的前一首。
  if (posNow() > 4) { seek(0); return; }
  const h = S.playHist.pop();
  if (h !== undefined) { S.qi = clamp(h, 0, S.queue.length - 1); start(S.queue[S.qi]); return; }
  if (S.qi <= 0) { seek(0); return; }
  S.qi--; start(S.queue[S.qi]);
}
/* ------------------------------------------------------- 下一曲预热
   预取的对象是「服务端到磁盘那一段」，不是浏览器的 HTTP 缓存 —— /api/stream 写死
   no-store，缓存那条路从原理上就是死的。实测（本机 C 盘，2026-10-07）：从没被读过
   的大文件头 1MB 冷读 1.7~342 ms（6 个样本，两个 300 ms+），读第二遍只要 0.4 ms；
   而示例曲库那 8 个早就在系统缓存里的 WAV，换歌空隙只有 5.7 ms（中位数）。
   所以这段代码救的是「很久没听的歌 + 机械盘/网络盘」那种一百毫秒级的空白，
   在缓存热的库上本来就听不见。花一次 localhost 的 1MB 传输，不占播放线程。
   封面反过来：/api/cover 是 immutable 可缓存的，用 Image 预热就是真缓存命中，
   换歌时不会再闪一下空封。 */
const _warm = new Set();
function warmNext() {
  const len = S.queue.length;
  if (!len) return;
  const i = nextIdx(S.settings.play_mode, S.qi, len, S.shuffleOrder, true);
  if (i < 0) return;                       // 马上要停了，预取没有意义
  const nx = S.queue[i];
  if (!nx || (S.track && nx.id === S.track.id)) return;   // 单曲循环：下一首就是本曲
  if (S._nx && S._nx.tid === nx.id) return;               // 第二只元素已在整首载入，别再偷 1MB
  if (_warm.has(nx.id)) return;
  if (_warm.size > 300) _warm.clear();     // 一屏内存都嫌多，纯去重表而已
  _warm.add(nx.id);
  if (nx.has_cover) { const im = new Image(); im.src = `/api/cover?track_id=${nx.id}`; }
  fetch(`/api/stream/${nx.id}`, { headers: { Range: 'bytes=0-1048575' } })
    .then(r => r.arrayBuffer())            // 读进来就扔：目的是让文件头进系统缓存
    .catch(() => { _warm.delete(nx.id); });
}

/* ------------------------------------------------------- 无缝衔接 / 交叉淡化
   单元素换歌 = 先卸旧流再挂新源，中间必然有一段「没人在出声」的空白（解码器起跑 +
   HTTP 头 + 事件循环，本机实测 20~120 ms；冷盘/网络盘可达三百毫秒）。这一节让第二只
   <audio> 在待机时把下一首整首载好，当前这首走到尾巴时两只交接：旧的在 WebAudio 图里
   等功率淡出，新的从 0 淡入。交叉淡化设 0 时只做 80 ms 的防爆音衔接 —— 听感是一整张
   专辑连着放，而不是一首一首地起停。

   角色对调而不是增设「当前元素」指针：tools/clock_probe.py 按字面 audio.currentTime
   认产品那把尺，指针会让它失明。交接时对调 audio / audio2，读数层一个字不改。

   什么时候不交接（宁可留一次空白，也不把状态弄脏）：
     · 关了「无缝衔接」；· 待机那只还没到 canplay；· 队列刚被改过、预载对不上；
     · 随机序走到本圈末尾（下一首要重排，此刻猜不准）；· 顺序播放的最后一首；
     · 浏览器拒绝这次起播（自动播放策略）：退回 ended → next() 的老路，一曲只试一次。 */
const XF_GAP_FADE = 0.08;   // 无缝档的衔接长度（秒）：够压住爆音，又听不出重叠
const XF_LEAD = 0.06;       // 提前量：吸收 play() 到真正出声的那几毫秒
S._nx = null;               // 待机元素上预载的是哪首 {el, idx, tid, ready}
S._ho = null;               // 已提交的交接 {old, until, timer}
S._hx = null;               // 起播请求还在路上的交接（新元素还没成当前）
S._hoBlocked = false;       // 浏览器拒绝过一次：本曲不再自动试，等下一首

/* 下一首会是谁（只猜「不需要改状态就能确定」的那种）。猜不准返回 null，交给 ended 兜底。
   和 next()/nextIdx 共用同一套规则，但**不动** S.shuffleOrder —— 预载不能有副作用。 */
function peekNext() {
  const len = S.queue.length;
  if (len <= 0 || S.qi < 0) return null;
  const mode = S.settings.play_mode;
  let i;
  if (mode === 'shuffle') {
    if (!S.shuffleOrder || S.shuffleOrder.length !== len) return null;
    const at = S.shuffleOrder.indexOf(S.qi);
    if (at < 0 || at >= len - 1) return null;   // 本圈到头：next() 要重排，这里不猜
    i = S.shuffleOrder[at + 1];
  } else {
    i = nextIdx(mode, S.qi, len, S.shuffleOrder, false);
  }
  if (i < 0 || !S.queue[i]) return null;
  return { idx: i, track: S.queue[i] };
}

function gainOf(el) { return (el && el._gn) ? el._gn : null; }
function setGainNow(el, v) {
  const g = gainOf(el);
  if (!g || !S._ac) return;
  try {
    g.gain.cancelScheduledValues(0);
    g.gain.value = v;
  } catch (e) { /* 图还没建好：这只元素还没经过 WebAudio */ }
}
function resetGains() { setGainNow(audio, 1); setGainNow(audio2, 0); }

function cancelPreload() {
  const nx = S._nx;
  if (!nx) return;
  S._nx = null;
  unloadEl(nx.el);
}

/* 按当前队列/模式重新决定「待机那只载哪首」。幂等：已经载对了就什么都不做。
   交接进行中（起播在路上 / 淡出还没收完）时不动 —— 由交接收尾后自己再叫一次。 */
function refreshPreload() {
  if (S._hx) return;
  if (S._ho) {
    if (performance.now() < S._ho.until) return;
    finishHandoverNow();      // 定时器被节流了也会在这里补收
  }
  if (isNative() || !S.settings.gapless || !S.track) { cancelPreload(); return; }
  const nx = peekNext();
  if (!nx) { cancelPreload(); return; }
  const cur = S._nx;
  if (cur && cur.el === audio2 && cur.idx === nx.idx && cur.tid === nx.track.id) return;
  cancelPreload();
  const el = audio2;
  S._nx = { el: el, idx: nx.idx, tid: nx.track.id, ready: false };
  try {
    el.preload = 'auto';
    el.defaultPlaybackRate = +S.settings.rate || 1;
    el.playbackRate = el.defaultPlaybackRate;
    el.volume = audio.volume;          // 待机那只与当前那只同音量；淡入靠 gain 节点
    el.muted = audio.muted;
    el.src = `/api/stream/${nx.track.id}`;
  } catch (e) { S._nx = null; }
}

/* 待机元素报 canplay：记下「备好了」。如果当前这首已经在尾巴上（用户拖到了末尾），
   这一下也能立刻把交接踢起来，不必等下一帧。 */
function onPreloadReady(el) {
  const nx = S._nx;
  if (isNative() || !nx || nx.el !== el) return;
  nx.ready = true;
  if (!_isPaused()) maybeHandover();
}

/* 每帧被 tick 叫一次（暂停/原生档/已交接时立刻返回）。判据只有一条：
   当前这首按**墙钟**算还剩多少 = (时长 − 位置) / 倍速，进入「交叉长度 + 80 ms 提前量」
   就交给下一首。倍速要除掉：4× 时 0.56 源秒只剩 140 ms 墙钟，按源秒判会来不及起播。 */
function maybeHandover() {
  if (isNative() || S._hx || S._hoBlocked || !S.settings.gapless || !S.track) return;
  if (!S.queue.length || _isPaused()) return;
  if (S._ho) {
    if (performance.now() < S._ho.until) return;
    finishHandoverNow();
  }
  const d = durNow();
  if (!(d > 0)) return;
  const rate = +S.settings.rate || 1;
  const xfMax = Math.min(20, Math.max(0, +S.settings.crossfade_sec || 0));
  const remWall = (d - posNow()) / rate;
  /* 先用「最长可能的淡化」粗筛：还没进这个窗口就一帧都不往下算。
     peekNext 在随机序上是 O(n) 的 indexOf，没有理由每帧跑一次。 */
  if (remWall > xfMax + XF_GAP_FADE + XF_LEAD) return;
  const want = peekNext();
  if (!want) { cancelPreload(); return; }
  /* 下一首就是本曲（单曲循环 / 重复相邻）时只做 80 ms 的无缝循环，不按交叉淡化长度提前
     起播 —— 同一段音频和自己重叠 5 秒不是淡化，是把尾巴切了。 */
  const selfLoop = !!(S.track && want.track.id === S.track.id);
  const xf = selfLoop ? 0 : xfMax;
  if (remWall > xf + XF_GAP_FADE + XF_LEAD) return;
  const nx = S._nx;
  if (!nx || nx.el !== audio2 || nx.idx !== want.idx || nx.tid !== want.track.id) {
    refreshPreload();          // 队列刚变过：这一帧先换预载，下一帧再交接
    return;
  }
  if (!nx.ready && nx.el.readyState < 3) return;   // 还没备好：等 canplay，或交给 ended 兜底
  doHandover(nx, want.track);
}

/* 当前这首真的走到头了（不是交接）。单曲循环原地重来，其余交给 next(false)。
   交接请求还在路上时一分不动：那一声 ended 多半来自正在收尾的旧元素，
   这时候跳曲会把刚起播的新歌打断，退化成「切了两首、只听到一截」。 */
function onActiveEnded() {
  if (S._hx) return;
  if (S.settings.play_mode === 'one_loop') { seek(0); play(); }
  else next(false);
}

/* 让新元素起播（此刻还是静音的：gain=0 / 无图时 volume=0），等 play() 真落地再提交。
   为什么不在调用 play() 之前就提交：浏览器拒绝时要把状态原样退回，不留下半套交接。 */
function doHandover(nx, track) {
  if (S._hx || S._ho) return;
  const info = { old: audio, el: nx.el, idx: nx.idx, track: track, oldTrack: S.track };
  S._hx = info;
  S._nx = null;
  try {
    nx.el.currentTime = 0;
    if (!S._ac || !gainOf(nx.el)) nx.el.volume = 0;   // 没有图可淡：先静音，提交时再抬
  } catch (e) { /* 元数据还没到时 currentTime 写不进去，0 就是 0 */ }
  let p;
  try { p = nx.el.play(); }
  catch (e) { S._hx = null; S._hoBlocked = true; unloadEl(nx.el); return; }
  if (!p || !p.then) { commitHandover(info); return; }
  p.then(function () { if (S._hx === info) commitHandover(info); })
   .catch(function () {
     if (S._hx !== info) return;
     S._hx = null;
     S._hoBlocked = true;          // 本曲不再试；ended 时走老路，下一首重新开始
     unloadEl(nx.el);
     const d = durNow(), t = posNow();
     if (d > 0 && t >= d - 0.05) onActiveEnded();  // 旧的那首已经走到头：别停在静默里
   });
}

/* 交接提交：角色对调 → 界面/队列/歌词/上报都换成新一首 → 排两条等功率包络。
   旧元素继续放（它正被淡出），到期由 finishHandoverNow 收走并重新预载下一首。 */
function commitHandover(info) {
  if (S._hx !== info || info.el === audio) return;
  S._hx = null;
  const oldEl = info.old, newEl = info.el, track = info.track;
  const rate = +S.settings.rate || 1;
  const remWall = Math.max(0, (durNow() - posNow()) / rate);   // 交换之前量：交换后读的是新元素
  const same = !!(info.oldTrack && info.oldTrack.id === track.id);
  /* 同一首接自己（单曲循环 / 重复相邻）只用防爆音长度：5 秒的「自己和自己交叉」
     听起来是相位梳，不是过渡。 */
  const xf = same ? 0 : Math.min(20, Math.max(0, +S.settings.crossfade_sec || 0));
  const fadeIn = xf > 0 ? xf : XF_GAP_FADE;
  const outDur = Math.max(0.03, remWall);
  audio2 = oldEl; audio = newEl;                                // ← 从这一刻起读数是新这首
  S._src = newEl._srcNode || S._src;
  S._src2 = oldEl._srcNode || S._src2;
  S.qi = info.idx;
  if (!same) {
    // 单曲循环 / 单曲队列的「同一首重放」不刷详情、不记播放次数 —— 那是同一次聆听。
    setTrack(track, false);
    api('play', { method: 'POST', body: { track_id: track.id } }).catch(() => {});
    saveQueue();
    loadLyric(track.id);
    updateMediaSession();
  }
  S._skipGuard = 0;
  S.pos = 0;
  const gOld = gainOf(oldEl), gNew = gainOf(newEl);
  if (!S._ac || !gOld || !gNew) {
    // 没有 WebAudio 图：元素音量直接切，宁可有一次极轻的爆点，也不能让两首叠着唱。
    const baseVol = S.settings.muted ? 0 : Math.min(1, Math.max(0, +S.settings.volume || 0));
    try { oldEl.volume = 0; newEl.volume = baseVol; } catch (e) {}
    S._ho = { old: oldEl, until: performance.now() + 60, timer: setTimeout(finishHandoverNow, 60) };
  } else {
    const ac = S._ac;
    const t0 = ac.currentTime + 0.005;
    try {
      gOld.gain.cancelScheduledValues(0);
      gNew.gain.cancelScheduledValues(0);
      gOld.gain.value = 1;
      gNew.gain.value = 0;
      gNew.gain.setValueCurveAtTime(xfadeCurve(true), t0, Math.max(0.03, fadeIn));
      gOld.gain.setValueCurveAtTime(xfadeCurve(false), t0, outDur);
    } catch (e) {
      setGainNow(oldEl, 0); setGainNow(newEl, 1);
    }
    const span = Math.max(fadeIn, outDur) * 1000 + 80;
    S._ho = { old: oldEl, until: performance.now() + span, timer: setTimeout(finishHandoverNow, span) };
  }
  nudgeFrames();
}

/* 等功率包络：sin/cos 的平方和恒为 1 —— 两条不同内容的音轨重叠时总功率不塌，
   听感是「换过去了」而不是「中间凹了一下」。 */
function xfadeCurve(up) {
  const n = 33, a = [];
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1) * Math.PI / 2;
    a.push(up ? Math.sin(x) : Math.cos(x));
  }
  return (typeof Float32Array !== 'undefined') ? new Float32Array(a) : a;
}

/* 淡出结束：把旧元素卸干净、两只增益归位、再武装下一趟预载。 */
function finishHandoverNow() {
  const ho = S._ho;
  if (!ho) return;
  S._ho = null;
  clearTimeout(ho.timer);
  unloadEl(ho.old);
  resetGains();
  refreshPreload();
}

/* 取消交接（暂停/拖进度/手动换曲/切引擎/队列改动）。
   rearm=false 时不动预载 —— 调用方马上就要自己设新队列，这里再载一首纯属浪费。 */
function cancelHandover(rearm) {
  const hx = S._hx;
  if (hx) {
    S._hx = null;
    unloadEl(hx.el);             // 还没成当前：直接收掉，旧的那首继续
  }
  const ho = S._ho;
  if (ho) {
    S._ho = null;
    clearTimeout(ho.timer);
    unloadEl(ho.old);
    resetGains();
  }
  if (rearm !== false) refreshPreload();
}

function autoAdvance() {
  S._skipGuard = (S._skipGuard || 0) + 1;
  if (S._skipGuard > 8) {
    S.playing = false; syncPlayUI();
    toast('连续 8 首无法播放，已停止自动跳曲。多为格式不受内核解码（wma/ape 等），可在设置中确认。', 'err', 9000);
    return;
  }
  next(false);
}

function seek(sec) {
  if (isNative()) {
    // 原生档：先就地重定锚（进度条/时间/歌词立刻跟着动），再决定是否要通知服务端。
    // 「服务端此刻载的是不是这首」决定了两种语义：
    //   · 载着本曲且没播完 → 真的去 seek（正在播就原位重开解码流，暂停着也只挪指针不出声，
    //     暂停态由服务端 seek 原样保留，见 sp/audioplay.py:seek）；
    //   · 没载 / 载的是别的歌 / 这首已播完 → 只把这一秒记成「下一次起播的落点」，
    //     发 seek 过去要么改的是别人的歌、要么被早退吞掉，而响应里的旧 state 还会把锚点拽回去。
    //     按下空格时 playNative 用 posNow() 起流，拖的那一下照样算数。
    if (!isFinite(sec)) return;
    const d = S._nat.duration || 0;
    const x = clamp(sec, 0, d > 0 ? d - 0.05 : Math.max(0, sec));
    S._natAnchor = x; S._natAnchorPerf = performance.now(); S.pos = x;
    const tid = S.track && S.track.id;
    if (S._natLoadedId === tid && !S._nat.ended) nativeCtl('seek', { pos: x, track_id: tid });
    nudgeFrames();
    return;
  }
  // 交接中拖进度条：先把两条流合成一条，再按用户落点重定位。否则「当前元素」是新的那首、
  // 旧的那首还在淡出，拖的是谁说不清。
  if (S._hx || S._ho) cancelHandover(true);
  if (!isFinite(sec) || !audio.duration) { S._pendingSeek = sec; return; }
  audio.currentTime = clamp(sec, 0, (audio.duration || 0) - 0.05);
  S.pos = sec;
  // 暂停着 seek 也得有人把进度条、时间标签、歌词刷过去。以前这是常驻 rAF 循环顺手干的活，
  // 现在循环会停，所以「位置变了」这件事必须自己喊一声。
  nudgeFrames();
}

/* --------------------------------------------------------- MediaSession */
function updateMediaSession() {
  if (!('mediaSession' in navigator) || !S.track) return;
  try {
    const t = S.track;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist, album: t.album || '',
      artwork: t.has_cover ? [{ src: `/api/cover?track_id=${t.id}`, sizes: '512x512' }] : [],
    });
    navigator.mediaSession.setActionHandler('play', () => play());
    navigator.mediaSession.setActionHandler('pause', () => pauseNow());
    navigator.mediaSession.setActionHandler('previoustrack', prev);
    navigator.mediaSession.setActionHandler('nexttrack', () => next());
    navigator.mediaSession.setActionHandler('stop', () => stopPlayback());
    navigator.mediaSession.setActionHandler('seekto', (d) => { if (d.seekTime != null) seek(d.seekTime); });
    navigator.mediaSession.playbackState = S.playing ? 'playing' : 'paused';
  } catch (e) { /* QtWebEngine 的旧版实现可能缺接口 */ }
}

/* --------------------------------------------------------- WebAudio 频谱 */
async function ensureAudioGraph() {
  if (S._ac) { if (S._ac.state === 'suspended') await S._ac.resume(); return; }
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ac = new AC();
    const an = ac.createAnalyser();
    an.fftSize = 128; an.smoothingTimeConstant = .82;
    /* 两只元素各一条源＋一条增益：无缝衔接的等功率包络就排在这两条增益上。
       createMediaElementSource 对同一只元素一辈子只能建一次，建了之后这只元素的声音
       就只从图里走（元素自己的 volume/muted 仍然生效，作用在源节点之前）。
       图必须在两只元素都还没被接走时一次建好 —— 只接当前那只的话，等下一首变成
       待机再建，第二次调用会抛「already connected」。 */
    S._ac = ac; S._an = an;
    [audioA, audioB].forEach((el) => {
      if (el._srcNode) return;
      const src = ac.createMediaElementSource(el);
      const gn = ac.createGain();
      gn.gain.value = (el === audio) ? 1 : 0;
      src.connect(gn);
      el._srcNode = src; el._gn = gn;
    });
    S._src = audio._srcNode || null;
    S._src2 = audio2._srcNode || null;
    /* 图必须落到 destination：createMediaElementSource 一建，元素的原生输出就交给图了，
       分析器若只进不出，Chromium 档整条链是静的（频谱也永远是 0）。这行只接一次，
       wireAudioGraph 重接增益时不碰它。 */
    an.connect(ac.destination);
    S._freq = new Uint8Array(an.frequencyBinCount);
    // 十个滤波器一次建好，之后只接线、只改 gain —— createBiquadFilter 每首换曲都来一遍
    // 是白给的对象垃圾，而「线接错一次」是这类图最容易出的事故。
    S._eq = EQ_HZ.map((hz, i) => {
      const f = ac.createBiquadFilter();
      f.type = EQ_TYPE[i]; f.frequency.value = hz; f.Q.value = 1.0; f.gain.value = 0;
      return f;
    });
    /* 虚拟扬声器（耳机交叉馈电）的 WebAudio 版：和原生引擎同一条 0.3 ms 延时、
       700 Hz 一阶低通、1/(1+g) 直通回退。原生档在服务端 numpy 算，这一套只在
       chromium 档出声；开关/强度两档共用。
       拓扑：in → split → 直通 L/R → merge；对侧 L/R → delay → lowpass → cross → merge。
       IIRFilterNode 的系数按 ac.sampleRate 现算，换设备采样率不会漂。 */
    try {
      const a1 = Math.exp(-2 * Math.PI * 700 / ac.sampleRate);
      const cf = { in: ac.createGain(), split: ac.createChannelSplitter(2),
                   merge: ac.createChannelMerger(2),
                   direct: [], cross: [], delay: [] };
      cf.in.channelCount = 2;
      cf.in.channelCountMode = 'explicit';
      cf.in.connect(cf.split);
      for (let i = 0; i < 2; i++) {
        const dg = ac.createGain(), cg = ac.createGain();
        const d = ac.createDelay(0.01);
        const lp = ac.createIIRFilter([1 - a1, 0], [1, -a1]);
        d.delayTime.value = 0.0003;
        dg.gain.value = 1; cg.gain.value = 0;
        cf.split.connect(dg, i); dg.connect(cf.merge, 0, i);
        cf.split.connect(d, i); d.connect(lp); lp.connect(cg); cg.connect(cf.merge, 0, 1 - i);
        cf.direct.push(dg); cf.cross.push(cg); cf.delay.push(d);
      }
      S._cf = cf;
    } catch (e) { S._cf = null; }
    wireAudioGraph();
    applyEqGains();
    applyCrossfeed();
    drawSpectrum();
  } catch (e) { S._ac = null; $('#spectrum').hidden = true; }
}

/* 播放链路接线：要么「源 → 频谱」直通，要么「源 → 十段 → 频谱」。
   只在开/关那一刻重连一次。为什么不留着十个 gain=0 的滤波器挂在链上：听着确实一样，
   但 AudioContext 在暂停时也在按 128 帧量子跑图，那十次双二阶就一直在算 ——
   而「闲置时不空转」这条是量过账的（tools/measure_idle_cpu.py：暂停时 raf 120/s、
   整树 21~29 %core；不空转后 0 次/s、0.3 %core）。不能因为加了均衡器又把它花回去。 */
function wireAudioGraph() {
  if (!S._ac || !S._an || !S._eq) return;
  const on = !!S.settings.eq_enabled;
  const cfOn = !!S._cf && crossfeedOn();
  const head = cfOn ? S._cf.in : S._an;
  S._eq.forEach((f) => { try { f.disconnect(); } catch (e) { /* 本来就没接 */ } });
  if (on) {
    for (let i = 0; i < S._eq.length - 1; i++) S._eq[i].connect(S._eq[i + 1]);
    S._eq[S._eq.length - 1].connect(head);
  }
  // 两条增益都要重接：交接前后的「当前那只」会轮换，只接一条的话，另一首要么没声、
  // 要么绕过均衡器与频谱直通扬声器。
  [audio, audio2].forEach((el) => {
    const gn = gainOf(el);
    if (!gn) return;
    try { gn.disconnect(); } catch (e) { /* 还没接上 */ }
    if (on) gn.connect(S._eq[0]); else gn.connect(head);
  });
  if (S._cf) {
    // 交叉馈电关掉时把 merge 从频谱上摘走：里面那两只 delay/IIR 没被 destination
    // 拉动就不算账，闲置时不会替用户空转。
    try { S._cf.merge.disconnect(); } catch (e) { /* 本来就没接 */ }
    if (cfOn) S._cf.merge.connect(S._an);
  }
  S._eqLive = on;
}

function crossfeedAmount() {
  const raw = S.settings.audio_speakers_amount;
  return clamp(raw == null ? 0.35 : +raw || 0, 0, 1);
}
function crossfeedOn() {
  return !!S.settings.audio_speakers && crossfeedAmount() > 1e-4;
}
function applyCrossfeed() {
  if (!S._cf) return;
  const on = crossfeedOn(), g = 0.6 * crossfeedAmount(), den = 1 + g;
  S._cf.direct.forEach((n) => { try { n.gain.value = on ? 1 / den : 1; } catch (e) {} });
  S._cf.cross.forEach((n) => { try { n.gain.value = on ? g / den : 0; } catch (e) {} });
}

/* 把设置里的十条增益推到滤波器上；顺带把「当前生效的曲线」回显到详情用的字符串。 */
function applyEqGains() {
  // 前端这条只管 WebAudio 那十段；原生档同一组增益在服务端重算（numpy 双二阶），
  // 所以两条引擎都要把最新曲线推出去。syncNativeConfig 在非原生档直接 return，
  // 原生档带 160ms 防抖 —— 拖十根推子不会发十次 HTTP。
  syncNativeConfig();
  if (!S._eq) return;
  const g = Array.isArray(S.settings.eq_gains) ? S.settings.eq_gains : [];
  S._eq.forEach((f, i) => {
    const v = clamp(+g[i] || 0, -EQ_RANGE, EQ_RANGE);
    try { f.gain.value = v; } catch (e) { /* 图没建起来时这里会空，忽略 */ }
  });
}
function drawSpectrum() {
  const cv = $('#spectrum'); if (!cv || cv.hidden) { S._specRAF = 0; return; }
  if (!isNative() && !S._an) return;      // chromium 且还没建音频图：别空转一条只会 return 的循环
  // 暂停着或没有交互余量时不画也不续帧：analyser 里没有新数据，这条循环每帧都在把
  // 同一组柱子原样重画一遍（18 次取数 + 18 个圆角矩形 + 一次 clearRect）。
  // 和 tick 共用 needFrames() 这一个判据，两条循环的开关只有这一个。
  if (!needFrames()) { S._specRAF = 0; return; }
  if (S._specRAF) return;                 // 只能有一条循环：重复调用会把帧率叠成两倍
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height, bars = 18;
  const step = W / bars;
  if (isNative() && (!S._natFreqSmooth || S._natFreqSmooth.length !== bars)) S._natFreqSmooth = new Float32Array(bars);
  const paint = (i, v) => {
    const h = Math.max(2, Math.pow(v, 1.35) * (H - 3));
    ctx.fillStyle = _specInk || (_specInk = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#f0a04b');
    ctx.globalAlpha = .35 + .65 * v;
    ctx.beginPath();
    const x = i * step + 1.6, w = step - 3.2, y = H - h;
    ctx.roundRect ? ctx.roundRect(x, y, w, h, 2) : ctx.rect(x, y, w, h);
    ctx.fill();
  };
  const loop = () => {
    // 画布看不见（播放器条被收起、窗口最小化后 display:none）时不再续这条循环。
    // 判可见用 clientWidth：display:none 时它是 0，而不像 offsetParent 那样会被
    // position:fixed 的祖先干扰。以前 rAF 一启动就永远自我续期，看不见也在每帧算 18 根柱子。
    if (!cv.isConnected || cv.hidden || !cv.clientWidth || !cv.clientHeight || !needFrames()) { S._specRAF = 0; return; }
    S._specRAF = requestAnimationFrame(loop);
    ctx.clearRect(0, 0, W, H);
    if (isNative()) {
      // 原生档的带值来自 /api/native/spectrum（随轮询约 4.5Hz 更新，见 _fetchNativeSpectrum）。
      // 这里按帧向最新值插值，把 4.5Hz 的台阶补成顺滑的 60fps —— 但绝不每帧发 HTTP。
      const raw = S._natSpecRaw || [];
      for (let i = 0; i < bars; i++) {
        const target = (+raw[i]) || 0;
        S._natFreqSmooth[i] += (target - S._natFreqSmooth[i]) * 0.45;
        paint(i, S._natFreqSmooth[i]);
      }
      ctx.globalAlpha = 1; return;
    }
    if (!S._an) return;
    S._an.getByteFrequencyData(S._freq);
    for (let i = 0; i < bars; i++) paint(i, S._freq[Math.floor(i * S._freq.length / bars / 1.7)] / 255);
    ctx.globalAlpha = 1;
  };
  loop();
}
/* --accent 的每一次改动都要让这个缓存失效（否则换主题后频谱还留着旧颜色）。 */
let _specInk = '';

/* --------------------------------------------------------- 进度与 rAF 循环 */
let lastTick = 0;
/* 上一次画出来的「总时长」指纹（秒文本＋aria 的取整秒）。只记位置不记时长，就会出现
   原生档暂停/切曲时量程仍停在上一首：暂停后位置不动、新时长却到了，闸门永远不放行。 */
let lastDurKey = '';

/* 帧预算：只有「接下来确实还有东西要刷」时才向框架要帧。
   为什么要有这一段：rAF 的性质是自我续期就永远续期 —— 暂停着挂在桌面上时画面和上一帧
   一模一样，这两条循环（tick 刷进度、drawSpectrum 画频谱）仍然每秒各问框架 60 次。
   这笔钱是实测过的（tools/measure_idle_cpu.py，本机 60 Hz 屏 / 20 核，每档 6 s）：
     paused    120 次/s、整棵进程树 21~29 %core
     缩进任务栏 0 次/s、0.3 %core      ← 同棵树，差别只有「页面有没有在出帧」
     页内再挂一条空转 rAF 正好 +60 次/s，撤掉后精确回到 120 ← 尺子可逆，才敢这么归因
   放行条件就三条，全都是「之后还会变」：
     1) audio 元素自己在动 —— 读元素而不是读 S.playing：标志位有漂移的可能，元素不会。
        这里没有用 `audio.seeking`：seek 这件事的所有入口（seek() 本身、seeked、timeupdate）
        都已经各喊过一次 nudgeFrames，再把它放进放行条件只多一个风险 —— 网络卡住时
        `seeking` 能长时间挂在 true，那这条循环就又变成永不退役的了。
     2) 刚有过交互（按暂停、拖进度条、换曲、时长变了）—— 留 SETTLE_MS 把异步尾巴走完；
     3) 手还按在进度条上。
   其余情况不续帧；下次有动作时由 nudgeFrames() 重新点火。定时关闭和进度记账本来就不靠帧
   （下面那个 setInterval 兜着），所以停帧不会把这两件事一起停掉。 */
const SETTLE_MS = 700;
let _settleUntil = 0, _rafId = 0, _barHeld = false;
function needFrames() {
  // 放行只看「谁在计时」：chromium 档读元素、原生档读 S._nat.playing（_isPaused 已把两档挡住）。
  // 原生档若还用 audio.paused（永远是 true，因为元素不沾），这条循环就再也点不着，进度会冻住。
  return !_isPaused() || _barHeld || performance.now() < _settleUntil;
}
function pumpFrames() { if (!_rafId && needFrames()) _rafId = requestAnimationFrame(tick); }
function nudgeFrames(ms) { _settleUntil = performance.now() + (ms || SETTLE_MS); pumpFrames(); }
/* _barHeld 的兜底：屏幕上只要松了手就当作没有手按着条。正常路径里 up 处理函数已经清过，
   这条是给「拖到窗口外松手」「合成事件只发了 pointerdown」这类拿不到 up 的情况兜底 ——
   漏一次就把闲置账单永远锁回 120 次/s，代价太大，所以宁可多挂两个空监听。 */
window.addEventListener('pointerup', () => { _barHeld = false; }, true);
window.addEventListener('pointercancel', () => { _barHeld = false; }, true);
window.addEventListener('blur', () => { _barHeld = false; }, true);

function tick(ts) {
  _rafId = 0;
  if (!needFrames()) return;        // 没事做就不续帧，账单当场归零
  _rafId = requestAnimationFrame(tick);
  const d = durNow() || (S.track && S.track.duration) || 0;
  const t = posNow();
  /* 放行条件不能只盯位置：原生档暂停/切曲时位置可以一动不动，量程却换了人。只认位置的话，
     新时长永远落不到 aria-valuemax 和两个总时长标签上，界面就一直停在上一首的长度。
     dKey 取「显示出来的秒文本＋aria 的取整秒」，正好是这段代码真正写进 DOM 的那两个数。 */
  const dKey = fmtTime(d) + '|' + Math.round(d);
  if (Math.abs(t - lastTick) > 0.08 || dKey !== lastDurKey) {
    lastTick = t; lastDurKey = dKey; S.pos = t;
    const pct = d ? clamp(t / d, 0, 1) * 100 : 0;
    $('#barFill').style.width = pct + '%'; $('#barThumb').style.left = pct + '%';
    $('#fsFill').style.width = pct + '%'; $('#fsThumb').style.left = pct + '%';
    // 进度条在 HTML 里就写着 role="slider"：只给 role 不给当前值，读屏软件念出来是
    // 「0%」，比不标更误导。这里跟着填充宽度一起报值和口播文本。
    // 值是「秒」不是百分比：口播文本念的就是秒，valuenow 报百分比会让读屏的
    // 方向键步长（默认按 (max-min)/10）跟用户按 ← → 的 5 秒对不上（run 16 量到 now=5 / vt=0:01）。
    const vt = fmtTime(t) + ' / ' + fmtTime(d);
    [$('#seekBar'), $('#fsSeek')].forEach(b => {
      if (!b) return;
      b.setAttribute('aria-valuenow', Math.floor(t));
      b.setAttribute('aria-valuemax', Math.round(d));
      b.setAttribute('aria-valuetext', vt);
    });
    $('#tCur').textContent = fmtTime(t); $('#fsCur').textContent = fmtTime(t);
    $('#tTot').textContent = fmtTime(d); $('#fsTot').textContent = fmtTime(d);
    const bufEnd = bufferedFrac();
    if (bufEnd && d) { $('#barBuf').style.width = clamp(bufEnd / d, 0, 1) * 100 + '%'; $('#fsBuf').style.width = $('#barBuf').style.width; }
    if ('mediaSession' in navigator && d) { try { navigator.mediaSession.setPositionState({ duration: d, position: t, playbackRate: +S.settings.rate || 1 }); } catch (e) { } }
    syncLyric(t);
    checkSleep();
  }
  // 交接的判据是「按墙钟还剩多少」，本身就要求每帧看一眼。放在位置闸门外面：
  // 位置每帧都在动时闸门虽然也放行，但那是巧合；交接不能在「最后一帧恰好没跨过 0.08s」
  // 的那一帧漏判。
  maybeHandover();
}

/* rAF 在窗口被遮挡/最小化时会被 Chromium 停掉（实测：进度和 S.pos 一起冻住）。
   进度记账与定时关闭是墙钟上的事，不该跟着画面刷新走，所以再用 setInterval 兜一层。
   桌面歌词的进度也走这一条：主窗冻帧时它还在跑，小窗才不至于跟着一起冻。
   门关着就一个请求都不发（reportPlayback 只在开关打开时调用），别把「闲置≈0」花回去。 */
setInterval(() => {
  const t = posNow();
  if (Math.abs(t - (S.pos || 0)) > 0.5) S.pos = t;
  checkSleep();
  if (S.settings.desktop_lyric) reportPlayback();
  // 主窗被遮挡/最小化时 rAF 会被 Chromium 停掉，tick 那条交接判据跟着熄火。
  // 这条墙钟定时器还在跑，所以在这里也踢一脚：后台换曲才不会次次退化成「留一段空白」。
  // 不在播时 maybeHandover 一进门就返回，闲置账单不受影响。
  maybeHandover();
}, 1000);

/* ==========================================================================
   歌词
   ========================================================================== */
async function loadLyric(tid) {
  S.lyric = { lines: [], i: -1, source: '', plain: false };
  renderLyric();
  try {
    const r = await api(`lyrics?track_id=${tid}`);
    if (S.track && S.track.id === tid) {
      S.lyric = { lines: r.lines || [], i: -1, source: r.source || '', plain: r.plain, text: r.text || '' };
      renderLyric();
    }
  } catch (e) { /* 无歌词即可 */ }
}

function renderLyric() {
  const box = $('#lyricLines'), fs = $('#fsLyric'), empty = $('#lyricEmpty');
  const pane = $('#lyricBox');
  box.textContent = ''; fs.textContent = '';
  fs.classList.remove('is-empty');
  const L = S.lyric.lines || [];
  // 侧栏的 #lyricEmpty 是带「挂载 .lrc」按钮的那块静态空态；旧代码又在 #lyricLines
  // 里补了一条同样的文字，空态时会出现两遍「暂无歌词」。现在只留静态块，文案随状态改。
  pane.classList.toggle('is-empty', !L.length);
  empty.hidden = !!L.length;
  if (!L.length) {
    const es = empty.querySelector('.le-title');
    if (es) es.textContent = S.lyric.plain ? '歌词（无时间轴）' : '暂无歌词';
    if (S.lyric.plain && S.lyric.text) {
      const pre = el('div', 'ly', S.lyric.text);
      pre.style.whiteSpace = 'pre-wrap'; pre.style.textAlign = 'center';
      box.appendChild(pre);
      // 全屏同样把整段铺出来：之前明明有词，全屏却只写「暂无歌词」。
      const fpre = el('div', 'ly', S.lyric.text);
      fpre.style.whiteSpace = 'pre-wrap';
      fs.appendChild(fpre);
    } else {
      const fb = el('div', 'fs-empty');
      fb.innerHTML = `<div class="fs-empty-icon">${icon('lyrics')}</div>
        <div class="fs-empty-title">暂无歌词</div>
        <div class="fs-empty-hint">给这首歌挂一份 .lrc，全屏就能跟着唱。</div>
        <button class="ghost">${icon('upload')} 挂载 .lrc 文件</button>`;
      fb.querySelector('button').onclick = () => attachLyric(S.track);
      fs.appendChild(fb);
      fs.classList.add('is-empty');
    }
    setPlaybarLine();          // 没有带时间轴的词：播放条那一行回到艺人
    return;
  }
  L.forEach((ln, i) => {
    const mk = (target) => {
      const n = el('div', 'ly');
      n.dataset.i = i; n.dataset.t = ln.t;
      n.innerHTML = `<span>${esc(ln.text)}</span>${ln.trans && S.settings.show_translation !== false ? `<span class="tr">${esc(ln.trans)}</span>` : ''}`;
      n.onclick = () => { seek(Math.max(0, ln.t + lyricOffset())); play(); };
      target.appendChild(n);
    };
    mk(box); mk(fs);
  });
  S.lyric.i = -1;
  setPlaybarLine();            // 换词之后先把播放条那行收回艺人，等 syncLyric 走到第一行再顶上
}

function lyricOffset() { return +S.settings.lyric_offset || 0; }

/** 按当前播放位置把歌词整体重算一遍。syncLyric 在「行号没变」时会提前 return，所以改完
    偏移或滚动速度想立刻看到效果，得先把 S.lyric.i 作废再叫它 —— 暂停时没有帧循环，
    不做这一步高亮就停在旧行上。
    位置一律取自 posNow()，不许在这里直接读 audio.currentTime：原生档那个元素整趟不沾
    （start() 不赋 src），currentTime 恒 0，于是按「歌词偏移 ±0.5」或改「滚动速度」会把
    高亮拽回第一句 —— 界面按新设置重算了，只是算在 0 秒上。 */
function syncLyricNow() {
  if (S.lyric.lines && S.lyric.lines.length) { S.lyric.i = -1; syncLyric(posNow()); }
}

/* 播放条第二行（艺人的位置）在有词时改播当前这句 —— 小窗不放歌词面板时至少能看到词在走。
   没有词、词还没走到第一行、或设置里关了，那一行都还是「艺人 — 专辑」，不许留空。
   传 true = 强制回艺人（换曲时用：那会儿上一首的词可能还留在 S.lyric 里）。 */
function setPlaybarLine(forceArtist) {
  const n = $('#pArtist');
  if (!n) return;
  const i = S.lyric.i, L = S.lyric.lines;
  const txt = (forceArtist || S.settings.playbar_lyric === false || i < 0 || !L || !L[i]) ? '' : String(L[i].text || '');
  const want = txt || S.pbArtist || ((S.track && (S.track.artist || '未知艺人')) || '—');
  if (n.textContent !== want) n.textContent = want;
  n.classList.toggle('is-lyric', !!txt);
  n.title = txt ? `歌词 · ${txt}` : '';
}

function syncLyric(t) {
  const L = S.lyric.lines;
  if (!L.length) return;
  const ms = lyricScrollMs();
  // settings.lyric_offset 表示「整首歌词往后推几秒」：正数 = 唱得比词早、词要等一等。
  // 等价于拿 (t - offset) 去比对时间戳，这样每一行自己的 t 不用改，偏移也不会在
  // 反复调节时越叠越歪（LRC 自带的 [offset:] 头已经在解析时并进 t 里了）。
  const tt = t - lyricOffset();
  let i = S.lyric.i;
  if (i < 0 || i >= L.length || tt < L[i].t) i = -1;
  while (i + 1 < L.length && L[i + 1].t <= tt + 0.12) i++;
  if (i === S.lyric.i) {
    // 速度刚被改过：行号没变，但那一行的补间还得按新时长重跑一次，否则设置像没生效。
    // 作废 i 再整体重算（syncLyricNow），比在这里私搭一次 animateScroll 稳 —— 后者会漏掉
    // 面板/全屏两处的同步和 class 状态。
    if (ms !== S._lyMs) { S._lyMs = ms; syncLyricNow(); }
    return;
  }
  S._lyMs = ms;
  const prev = S.lyric.i;
  S.lyric.i = i;
  setPlaybarLine();
  // 只改「状态真的变了」的那几行。一行歌词有 on/past/near 三个态，换一行受影响的只是
  // prev..i 这一段加上下一个 i+1；以前是每次换行把两个面板里所有行全部 toggle 一遍，
  // 一首 120 行的词从头放到尾要白写两万次 class 读写。
  const lo = Math.max(0, Math.min(prev < 0 ? 0 : prev, i));
  const hi = Math.max(prev, i) + 1;
  [$('#lyricLines'), $('#fsLyric')].forEach((root) => {
    if (!root) return;
    const kids = root.children;
    for (let k = lo; k <= hi && k < kids.length; k++) {
      const n = kids[k];
      if (!n.classList || !n.classList.contains('ly')) continue;
      n.classList.toggle('on', k === i);
      n.classList.toggle('past', k < i);
      n.classList.toggle('near', k === i + 1);
    }
    const cur = kids[i];
    if (cur && root === $('#lyricLines')) {
      const host = $('#lyricBox');
      const target = cur.offsetTop - host.clientHeight / 2 + cur.clientHeight / 2;
      animateScroll(host, Math.max(0, target), ms);
    } else if (cur && root === $('#fsLyric')) {
      animateScroll(root, Math.max(0, cur.offsetTop - 90), ms);
    }
  });
}

/* ==========================================================================
   队列面板
   ========================================================================== */
/* 队列面板一次只铺这么多行，滚到底再补。
   以前每次换歌（自动下一首也算）都把整串队列重建成 DOM：2000 首 = 2000 个节点 × 5 个
   监听器，一小时循环歌单放着放着就在后台重建两万多次监听器。事件改成容器代理后，
   一行只值它自己的那几个节点。 */
const QUEUE_PAGE = 150;

function queueRow(t, i) {
  const r = el('div', 'q-row' + (i === S.qi ? ' on' : ''));
  r.dataset.id = t.id;
  r.dataset.i = i;
  r.draggable = true;
  r.innerHTML = `${t.has_cover ? `<img loading="lazy" decoding="async" src="/api/cover?track_id=${t.id}&size=list" alt="">` : `<span class="mini-cover noart">${icon('music')}</span>`}
    <span class="qn"><b>${esc(t.title)}</b><span>${esc(t.artist)}</span></span>
    <button class="icon-btn tiny x" title="从队列移除" aria-label="从队列移除">${icon('x')}</button>`;
  return r;
}

function appendQueuePage(box) {
  const st = box._st;
  if (!st || st.shown >= S.queue.length) return;
  const end = Math.min(S.queue.length, st.shown + QUEUE_PAGE);
  const frag = document.createDocumentFragment();
  for (let i = st.shown; i < end; i++) frag.appendChild(queueRow(S.queue[i], i));
  st.shown = end;
  box.appendChild(frag);
}

function renderQueue() {
  const box = $('#queueList');
  box.textContent = '';
  $('#queueCount').textContent = `${S.queue.length} 首` + (S.context ? ` · ${S.context}` : '');
  box._st = { shown: 0 };
  appendQueuePage(box);
}

/* 队列上的点击 / 拖拽排序：全部走容器代理，一行不再自带监听器。
   下标一律从 data-i 现取，所以「补下一页」「撤销移除」之后不会拿到过期闭包里的旧下标。 */
function wireQueue(box) {
  box.addEventListener('click', (e) => {
    const row = e.target.closest('.q-row');
    if (!row) return;
    const i = +row.dataset.i;
    if (!isFinite(i) || !S.queue[i]) return;
    if (e.target.closest('.x')) {
      const was = S.queue[i];
      S.queue.splice(i, 1);
      if (i < S.qi) S.qi--;
      else if (i === S.qi) {
        if (!S.queue.length) { S.qi = -1; }
        // 删掉正在放的那一行：停着的时候只把界面换到下一首，不许因为「删了一行」
        // 就替用户按下播放。S.playing 是 play/pause 事件同步过来的，不是猜的。
        else { S.qi = Math.min(S.qi, S.queue.length - 1); start(S.queue[S.qi], S.playing); }
      }
      renderQueue();
      saveQueue();
      refreshPreload();       // 队列变了：已预载的下一首可能已经不是「下一首」
      // 手滑点错是很常见的：这一首可能是整串列表里唯一想听的那张，重播得从列表里再找。
      toast(`已从队列移除《${was.title}》`, '', 1800, {
        action: {
          label: '撤销', fn: () => {
            S.queue.splice(Math.min(i, S.queue.length), 0, was);
            // 塞回去会让它后面的下标全体 +1，所以「正在放的那首」的位置不自己算，
            // 按对象重新认一遍 —— 和拖拽排序那套是同一个处理。
            S.qi = S.queue.indexOf(S.track);
            S.playHist = [];      // 历史按下标存，重排后无从对应
            renderQueue(); saveQueue(); refreshPreload(); toast('已放回队列', 'ok', 1500);
          },
        },
      });
      return;
    }
    S.qi = i; start(S.queue[i]); renderQueue();
  });
  /* 队列行的右键：以前掉进「空地」分支，什么都不弹。队列是临时拼的列表，这里给
     立即播放 / 移除 / 详情 / 复制路径 / 资源管理器；「移除」不复制那套下标修正逻辑，
     而是去按那颗 ✕，复用同一条容器处理（删当前行怎么处理，这里就怎么处理）。 */
  box.addEventListener('contextmenu', (e) => {
    const row = e.target.closest('.q-row');
    if (!row) return;
    e.preventDefault();
    const i = +row.dataset.i;
    const t = S.queue[i];
    if (!isFinite(i) || !t) return;
    // 菜单打开后队列可能被别处改过：动作一律先按对象核对，不拿旧下标动另一首。
    const stale = () => S.queue[i] !== t;
    showMenu(e.clientX, e.clientY, [
      { label: '立即播放', icon: '▶', fn: () => {
          if (stale()) return toast('队列已变化，请重新右键');
          S.qi = i; start(t); renderQueue();
        } },
      { label: '从队列移除', icon: '✕', danger: true, fn: () => {
          if (stale()) return toast('队列已变化，请重新右键');
          const x = document.querySelector('.q-row[data-i="' + i + '"] .x');
          if (x) x.click();
        } },
      { sep: 1 },
      { label: '曲目详情', icon: 'ⓘ', fn: () => { showNpTab('info'); setTrack(t, true); } },
      { label: '复制文件路径', icon: '⧉', fn: async () => { const f = await fullTrack(t); if (navigator.clipboard) await navigator.clipboard.writeText(f.path || ''); toast(f.path ? '路径已复制' : '复制失败：拿不到该文件路径', f.path ? '' : 'err'); } },
      { label: '在资源管理器中显示', icon: 'reveal', fn: () => revealFile(t) },
    ]);
  });
  box.addEventListener('dragstart', e => {
    const row = e.target.closest('.q-row');
    if (row) e.dataTransfer.setData('text/plain', row.dataset.i);
  });
  box.addEventListener('dragover', e => e.preventDefault());
  box.addEventListener('drop', e => {
    e.preventDefault();
    const row = e.target.closest('.q-row');
    if (!row) return;
    const to = +row.dataset.i;
    const from = +e.dataTransfer.getData('text/plain');
    if (!isFinite(from) || !isFinite(to) || from === to || !S.queue[from]) return;
    const [m] = S.queue.splice(from, 1);
    S.queue.splice(to, 0, m);
    S.qi = S.queue.indexOf(S.track);
    S.playHist = [];   // 下标全变了，旧历史无从对应：清空后「上一曲」退回队列前一首
    renderQueue();
    saveQueue();
    refreshPreload();   // 排序变了：预载的那首可能已经被挪走
  });
  // 滚到底补下一页（队列面板自己滚，和主列表的 #scroller 不是一个容器）
  box.addEventListener('scroll', () => {
    if (box.scrollTop + box.clientHeight > box.scrollHeight - 300) appendQueuePage(box);
  });
}

/* 清空队列：正在放的这首保留（不然界面会指向一首不在队列里的歌），后面的全部去掉。
   只影响本次播放列表，曲库、歌单、收藏都不动。 */
function clearQueue() {
  if (!S.queue.length) { toast('队列已经是空的', '', 1300); return; }
  // 快照整套状态：光还原 S.queue 不够，随机序和「上一曲」历史都是按下标存的，
  // 清空把它们清了却只恢复列表，下一首就会跳到别的歌上。
  const snap = {
    queue: S.queue.slice(), qi: S.qi, context: S.context,
    shuffleOrder: S.shuffleOrder.slice(), playHist: S.playHist.slice(),
  };
  const n = snap.queue.length;
  const cur = S.qi >= 0 ? S.queue[S.qi] : S.track;
  S.queue = cur ? [cur] : [];
  S.qi = cur ? 0 : -1;
  S.shuffleOrder = [];
  S.playHist = [];      // 队列都没了，历史下标也就不存在了
  S.context = '';
  renderQueue();
  syncPlayUI();
  saveQueue();          // 立刻落库：不写这一发，清空后 15 秒内退出会把旧队列又存回去
  refreshPreload();     // 队列只剩当前这首：待机那只的预载跟着收掉
  toast(`已清空队列（原有 ${n} 首）`, '', 1800, {
    action: {
      label: '撤销', fn: () => {
        S.queue = snap.queue; S.qi = snap.qi; S.context = snap.context;
        S.shuffleOrder = snap.shuffleOrder; S.playHist = snap.playHist;
        renderQueue(); syncPlayUI(); saveQueue(); refreshPreload();
        toast('队列已还原', 'ok', 1600);
      },
    },
  });
}

function renderMeta(t) {
  const box = $('#metaBox');
  if (!box || !t) return;
  const seq = ++S._metaSeq;
  // 列表行是「窄投影」（没有 path / 内嵌歌词这些大字段，见 sp/db.py 的 LIST_COLUMNS），
  // 详情面板要的正是整条记录，所以这里按 id 补一次单条查询（热路径实测 0.6 ms）。
  // seq 用来防「切歌太快，上一首的详情盖到新歌上」。
  const job = t.path ? Promise.resolve(t) : api('track/' + t.id).then(r => (r && r.track) || t).catch(() => t);
  job.then(full => {
    if (seq !== S._metaSeq) return;
    const rows = [
      ['标题', full.title], ['艺术家', full.artist], ['专辑', full.album], ['专辑艺术家', full.album_artist],
      ['年份', full.year || '—'], ['流派', full.genre || '—'], ['音轨', full.track_no ? `${full.track_no}${full.disc_no && full.disc_no > 1 ? ' / 碟' + full.disc_no : ''}` : '—'],
      ['格式', `${(full.fmt || '').toUpperCase()}${full.bitrate ? ` · ${Math.round(full.bitrate / 1000)} kbps` : ''}`],
      ['采样率', full.sample_rate ? `${(full.sample_rate / 1000).toFixed(1)} kHz · ${full.channels === 2 ? '立体声' : full.channels || '—'} 声道` : '—'],
      ['时长', fmtTime(full.duration)], ['大小', full.size ? (full.size / 1048576).toFixed(1) + ' MB' : '—'],
      ['歌词', S.lyric.source ? (S.lyric.source === 'embedded' ? '内嵌' : '文件') : (full.has_lyric ? '有（未加载）' : '无')],
      ['路径', full.path || '—'],
    ];
    box.innerHTML = rows.map(([k, v]) => `<div class="meta-kv"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join('')
      + `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
          ${full.bitrate >= 900000 ? '<span class="badge hi">无损</span>' : full.bitrate >= 250000 ? '<span class="badge hi">高品质</span>' : '<span class="badge">标准</span>'}
          <span class="badge">${esc((full.fmt || '').toUpperCase())}</span>
          ${full.playable === 0 ? '<span class="badge">此格式不支持播放</span>' : ''}
          ${full.favorite ? '<span class="badge hi">已收藏</span>' : ''}
         </div>`;
  });
}

/* ==========================================================================
   交互装配
   ========================================================================== */
function wireControls() {
  /* --- audio events ---
     两只元素都要挂。待机那只只认 canplay（预载就绪）和 error（预载作废）两件事；
     凡是会改界面/队列的事件，一律先认 el === audio —— 交接时待机那只也会派发
     play/pause，不挡住的话，「上一首还在收尾，界面先跳到下一首」。 */
  function wireAudioEl(el) {
    const isCur = () => el === audio;
    el.addEventListener('loadedmetadata', () => {
      if (!isCur() || isNative()) return;   // 原生档不认元素这条时钟（见 silenceAudioElement）
      $('#tTot').textContent = fmtTime(audio.duration);
      nudgeFrames();          // 时长刚定下来：进度条、总时长、歌词首行都得按新的 d 重画一次
      if (S._pendingSeek != null) { seek(S._pendingSeek); S._pendingSeek = null; }
    });
    el.addEventListener('canplay', () => onPreloadReady(el));
    el.addEventListener('playing', () => { if (isCur() && !isNative()) S._skipGuard = 0; });
    el.addEventListener('play', () => { if (!isCur() || isNative()) return; S.playing = true; syncPlayUI(); });
    el.addEventListener('pause', () => { if (!isCur() || isNative()) return; S.playing = false; syncPlayUI(); });
    el.addEventListener('ended', () => { if (!isCur() || isNative()) return; onActiveEnded(); });
    el.addEventListener('error', () => {
      if (isNative()) return;       // 卸源会走一次媒体加载流程，不能让备胎的错误去切原生档的歌
      if (!isCur()) {
        // 待机那只加载失败（文件坏了/格式不支持）：作废这次预载，本曲不再试交接；
        // 它真轮到时仍会走 ended → next()，由当前曲目的 error 处理器报错并跳曲。
        // 先认「还挂着源」：unloadEl 主动卸源也可能补发一个 error，那不是加载失败。
        if (!el.hasAttribute('src')) return;
        if (S._nx && S._nx.el === el) { cancelPreload(); S._hoBlocked = true; }
        return;
      }
      if (!el.src || el.src.endsWith('/')) return;
      toast(`《${(S.track && S.track.title) || '该曲目'}》${el.error ? mediaErr() : '无法播放'}`, 'err', 4200);
      renderMeta(S.track || {});
      autoAdvance();
    });
    el.addEventListener('timeupdate', () => {
      if (!isCur() || isNative()) return;
      // 元素的播放位置每一次变化都要有人把它画出来：暂停时被外部改掉 currentTime、
      // seek 完成、恢复播放后的头几帧，以前都靠常驻循环顺手兜住，现在得自己喊一嗓子。
      // timeupdate 在 Chromium 里大约 4 Hz，暂停态下也会在 seeked 之后发一次，成本可忽略。
      // 这里原先还有一段「听过半首就记进 S._recorded」：那个 Set 全项目没有第二个读者，
      // 也不通知服务端（最近播放由 start() 的 POST /api/play 记，两档都记），而原生档这个
      // 事件压根不触发 —— 一段只在备胎引擎里空转的死代码，删掉比留着更容易被当成在做事。
      nudgeFrames();
    });
    el.addEventListener('seeked', () => { if (isCur() && !isNative()) nudgeFrames(); });
    el.addEventListener('durationchange', () => { if (isCur() && !isNative()) nudgeFrames(); });
    el.addEventListener('ratechange', () => { if (isCur() && !isNative()) nudgeFrames(); });
  }
  [audioA, audioB].forEach(wireAudioEl);

  /* --- transport --- */
  $('#btnPlay').onclick = togglePlay;
  $('#fsPlay').onclick = togglePlay;
  $('#btnNext').onclick = () => next();
  $('#fsNext').onclick = () => next();
  $('#btnPrev').onclick = prev;
  $('#fsPrev').onclick = prev;
  $('#btnMode').onclick = cycleMode;
  $('#fsMode').onclick = cycleMode;
  $('#btnShuffleAll').onclick = async () => {
    const r = await api('tracks?limit=5000');
    if (r.items.length) playFrom(shuffled(r.items), 0, '所有歌曲（随机）');
  };
  $('#btnFav').onclick = $('#btnFav2').onclick = () => S.track && toggleFav(S.track);
  $('#fsFav').onclick = () => S.track && toggleFav(S.track);
  $('#btnMute').onclick = () => { S.settings.muted = !S.settings.muted; audio.muted = S.settings.muted; saveSettings({ muted: S.settings.muted }); applySettings(); };
  $('#btnQueue').onclick = () => showNpTab('queue');
  $('#btnFull').onclick = () => enterFs(true);
  $('#btnFsClose').onclick = () => enterFs(false);
  $('#btnTheme').onclick = () => { S.settings.theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'; applySettings(); saveSettings({ theme: S.settings.theme }); };
  $('#btnMini').onclick = toggleFocus;
  nameEl($('#btnMini'), '专注模式（隐藏侧栏，快捷键 Z）');
  $('#btnDrawer').onclick = (e) => openDrawer('side', e.currentTarget);
  $('#btnNpMobile').onclick = (e) => openDrawer('np', e.currentTarget);
  $('#btnSideClose').onclick = () => closeDrawer(true);
  $('#scrim').onclick = () => closeDrawer(true);
  $('#btnNpHide').onclick = () => { if (isNarrow()) closeDrawer(true); else toggleNp(false); };
  $('#btnNpShow').onclick = () => toggleNp(true);
  $('#btnLyricSize').onclick = () => {
    const steps = [0.85, 1, 1.15, 1.35];
    const i = (steps.indexOf(+(S.settings.lyric_size || 1)) + 1) % steps.length;
    S.settings.lyric_size = steps[i]; applySettings(); saveSettings({ lyric_size: steps[i] });
    toast(`歌词字号 ${Math.round(steps[i] * 100)}%`, '', 1500);
  };
  $('#libSource').onclick = librarySourceMenu;
  $('#btnQueueClear').onclick = () => clearQueue();
  $('#btnNewPlaylist').onclick = newPlaylist;
  $('#btnWelcomePick').onclick = pickFolder;
  $('#btnWelcomeManual').onclick = manualFolder;
  $('#btnWelcomeGo').onclick = manualFolder;
  $('#btnSleep').onclick = sleepMenu;
  $('#btnAttachLyric').onclick = attachLyric;
  $('#lyricFile').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (!S.track) { e.target.value = ''; return toast('先选一首歌'); }
    const text = await f.text();
    e.target.value = '';        // 不清空的话，第二次选同一个文件不会再触发 change
    applyLyricText(text, f.name);
  };
  $$('#npTabs .np-tab').forEach(b => b.onclick = () => showNpTab(b.dataset.tab));

  /* --- sliders --- */
  bindBar($('#seekBar'), (frac, committing) => {
    const d = durNow() || (S.track && S.track.duration) || 0;
    if (!d) return;
    if (committing) seek(frac * d); else { $('#barFill').style.width = (frac * 100) + '%'; $('#barTip').hidden = false; }
  }, (frac) => {
    const d = durNow() || (S.track && S.track.duration) || 0;
    $('#barTip').textContent = fmtTime(frac * d);
  });
  bindBar($('#fsSeek'), (frac, committing) => {
    const d = durNow() || 0; if (!d) return;
    if (committing) seek(frac * d); else $('#fsFill').style.width = (frac * 100) + '%';
  });
  bindBar($('#volBar'), (frac, committing) => {
    audio.volume = clamp(frac, 0, 1); S.settings.volume = frac; S.settings.muted = frac === 0;
    applySettings(); if (committing) saveSettings({ volume: frac, muted: S.settings.muted });
  });

  /* --- 侧栏滚动反馈 ---：只在能滚的时候画渐隐与滑轨；窗口尺寸变化后「能不能滚」
     也会变，所以 resize 一并重算，而不是等下一次 renderSidebar。 */
  const nav = $('#nav');
  if (nav) {
    nav.addEventListener('scroll', syncNavFade, { passive: true });
    window.addEventListener('resize', syncNavFade);
  }

  /* --- search --- */
  const doSearch = debounce(async () => {
    syncSearchClear();
    const q = $('#searchInput').value.trim();
    if (!q) {
      // 只在搜索页上「删空」才退回上一页；在别的页面临时敲了几个字又删掉，不该把人送走。
      if (S.view === 'search') route('#/' + lastViewHome(), false);
      return;
    }
    location.hash = '#/search?q=' + encodeURIComponent(q);
  }, 260);
  $('#searchInput').addEventListener('input', doSearch);
  $('#searchInput').addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    if (e.target.value) { e.target.value = ''; syncSearchClear(); }
    if (S.view === 'search') route('#/' + lastViewHome(), false);
    else e.target.blur();
  });
  $('#searchClear').onclick = () => {
    const inp = $('#searchInput');
    inp.value = '';
    syncSearchClear();
    if (S.view === 'search') route('#/' + lastViewHome(), false);
    inp.focus({ preventScroll: true });
  };
  /* ARIA tabs 的键盘通道：←/→ 在三个面板间移动并激活，Home/End 到两端。
     与 role=tab/tabpanel、aria-selected/tabindex 的 roving 一起才算一套完整的页签。 */
  const npTabs = $('#npTabs');
  npTabs.addEventListener('keydown', e => {
    const tabs = [...npTabs.querySelectorAll('.np-tab')];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = tabs.length - 1;
    if (j === null) return;
    e.preventDefault(); e.stopPropagation();
    tabs[j].focus();
    showNpTab(tabs[j].dataset.tab);
  });
  $('#btnBack').onclick = () => { const h = S.history.pop(); if (h) location.hash = h; else location.hash = '#/home'; };

  /* --- global click-away --- */
  document.addEventListener('click', e => {
    // 打开菜单的那一次点击还会继续冒泡到 document（onclick 先跑、这一条后跑），
    // 不挡就会「菜单刚出现就自己关掉」——侧栏来源菜单实测就是这样一闪而过。
    if (performance.now() - _menuShownAt < 80) return;
    if (!e.target.closest('.menu')) hideMenu();
  });
  document.addEventListener('keydown', onKey);
  // 浮层的 Tab 循环：对话框和窄窗抽屉都不能把焦点漏到遮罩后面。
  document.addEventListener('keydown', (e) => {
    if (!$('#dialogMask').hidden) trapFocus(e, $('#dialog'));
    else if (_drawer.kind) trapFocus(e, drawerPanel(_drawer.kind));
  });
  $('#dialogCancel').onclick = closeDialog;
  $('#dialogMask').onclick = e => { if (e.target === $('#dialogMask')) closeDialog(); };

  /* --- drag & drop in browser mode --- */
  let hint;
  window.addEventListener('dragover', e => {
    if (S.app.native) return;
    e.preventDefault();
    if (!hint) { hint = el('div', 'drop-hint', '把音乐文件夹拖进来（需在 App 中才能读取完整路径）'); document.body.appendChild(hint); }
  });
  window.addEventListener('dragleave', e => { if (e.relatedTarget === null && hint) { hint.remove(); hint = null; } });
  window.addEventListener('drop', e => { if (!S.app.native) { e.preventDefault(); if (hint) { hint.remove(); hint = null; } } });

  /* --- 队列面板：事件代理装一次，之后每次 renderQueue 都不再往行上挂监听器 --- */
  wireQueue($('#queueList'));

  /* --- unload: persist position --- */
  window.addEventListener('beforeunload', flush);
  // 开机只点火一次、给 1.2 s 余量把首屏的时间标签/进度/歌词落位，之后没东西要刷就不再多要帧。
  // 以前这里是 `requestAnimationFrame(tick)` 起一条永不退役的循环：暂停挂着不动时
  // 它每秒照问框架 60 次（实测 paused 120 次/s，含频谱那条）。
  nudgeFrames(1200);
}

function cycleMode() {
  const order = ['list_loop', 'one_loop', 'shuffle', 'normal'];
  const i = (order.indexOf(S.settings.play_mode) + 1) % order.length;
  S.settings.play_mode = order[i];
  if (order[i] === 'shuffle') S.shuffleOrder = shuffled(S.queue.map((_, k) => k));
  updateModeButton(); saveSettings({ play_mode: order[i] });
  refreshPreload();          // 「下一首」的规则变了：已预载的那首要重认或收掉
  toast(MODES[order[i]].label, '', 1400);
}
function toggleFocus() {
  S.focusMode = !S.focusMode;
  $('.stage').classList.toggle('side-hidden', S.focusMode);
  const b = $('#btnMini');
  b.classList.toggle('on', S.focusMode);
  b.setAttribute('aria-pressed', S.focusMode ? 'true' : 'false');
  nameEl(b, S.focusMode ? '退出专注模式（恢复侧栏，快捷键 Z）' : '专注模式（隐藏侧栏，快捷键 Z）');
}
/* ==========================================================================
   窄窗口抽屉（≤940px）
   窄窗没有三栏可放：侧栏与歌词面板改成左右抽屉。它们仍然是网页里的真实节点，
   所以必须自己管三件事 —— 谁被拉出来、焦点在哪、关掉之后焦点回哪。
   ========================================================================== */
const NARROW_MQ = window.matchMedia('(max-width: 940px)');
const _drawer = { kind: '', opener: null };
function isNarrow() { return NARROW_MQ.matches; }
function drawerPanel(kind) { return kind === 'np' ? $('#npPanel') : $('#side'); }
function openDrawer(kind, opener) {
  if (!isNarrow() || !drawerPanel(kind)) return;
  const prevOpener = _drawer.opener;
  if (_drawer.kind && _drawer.kind !== kind) closeDrawer(false);
  const panel = drawerPanel(kind);
  // 从侧栏切到播放面板时，焦点原本在侧栏里（马上要藏起来）；关掉播放面板后
  // 应该回到当初打开抽屉的那个按钮，而不是一个已经不可见的导航项。
  _drawer.opener = prevOpener || opener || document.activeElement;
  _drawer.kind = kind;
  panel.classList.add('mobile-open');
  panel.removeAttribute('aria-hidden');
  $('#scrim').classList.add('show');
  // 把焦点送进抽屉：侧栏落在当前页对应的那一项，歌词面板落在当前页签。
  // 不这么做的话，键盘用户的下一格 Tab 还在被遮罩盖住的正文里走。
  const target = kind === 'np'
    ? (panel.querySelector('.np-tab.active') || panel.querySelector('.np-tab'))
    : (panel.querySelector('.nav-item.active') || panel.querySelector('.lib-source'));
  (target || panel).focus({ preventScroll: true });
}
function closeDrawer(restore) {
  const s = $('#scrim');
  if (s) s.classList.remove('show');
  if (!_drawer.kind) return;
  const panel = drawerPanel(_drawer.kind);
  if (panel) {
    panel.classList.remove('mobile-open');
    panel.removeAttribute('aria-hidden');
  }
  const op = _drawer.opener;
  _drawer.kind = '';
  _drawer.opener = null;
  if (restore && op && op.isConnected && typeof op.focus === 'function') {
    try { op.focus({ preventScroll: true }); } catch (e) { /* 触发它的按钮已经不在了 */ }
  }
}
/* 抽屉开着时 Tab 不许跑到遮罩后面：在抽屉内部循环，和对话框同一套纪律。 */
function trapFocus(e, root) {
  if (e.key !== 'Tab' || !root) return;
  const list = [...root.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter(n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden');
  if (!list.length) return;
  const first = list[0], last = list[list.length - 1], a = document.activeElement;
  if (e.shiftKey && (a === first || !root.contains(a))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (a === last || !root.contains(a))) { e.preventDefault(); first.focus(); }
}
NARROW_MQ.addEventListener('change', (e) => { if (!e.matches) closeDrawer(false); });

function toggleNp(show) {
  if (isNarrow()) {
    // 窄窗的歌词面板是抽屉：收起来不等于用户关掉了「常驻面板」这个偏好。
    // 因此这里只动抽屉，不写 np_open，回到宽窗时仍按用户原来的习惯排版。
    if (show) openDrawer('np', document.activeElement);
    else closeDrawer(true);
    return;
  }
  S.settings.np_open = show;              // 立即可读，避免与 applySettings 的 320ms 防抖抢跑
  $('.stage').classList.toggle('np-hidden', !show);
  $('#btnNpShow').hidden = show;
  saveSettings({ np_open: show });
}
function showNpTab(name) {
  $$('#npTabs .np-tab').forEach(b => {
    const on = b.dataset.tab === name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;      // 与 role=tab 配套的 roving tabindex
  });
  $$('.np-pane').forEach(p => p.hidden = p.dataset.pane !== name);
  const wrap = $('#npCoverWrap');
  if (wrap) wrap.classList.toggle('compact', name !== 'lyric');
  toggleNp(true);
}
function enterFs(on) {
  $('#fs').hidden = !on;
  // 全屏那套进度条（#fsFill/#fsCur/#fsTot）只有 tick 会写。暂停时展开全屏，
  // 没有这一嗓子它就会停在最后一次刷新时的宽度上——循环现在会停，展开动作必须自己点火。
  nudgeFrames();
  if (on && S.track) {
    // 别写 src=''：Chromium 会把它解析成页面自身 URL，取回一个非图片就画碎图图标
    const u = S.track.has_cover ? `/api/cover?track_id=${S.track.id}` : '';
    const im = $('#fsCoverImg');
    if (u) { im.src = u; im.hidden = false; } else { im.removeAttribute('src'); }
    $('#fsCover').classList.toggle('noart', !u);
  }
  document.body.style.overflow = on ? 'hidden' : '';
}

/* 通用拖动条 */
function bindBar(bar, onFrac, onHover) {
  const fill = bar.querySelector('.bar-fill'), thumb = bar.querySelector('.bar-thumb');
  const tip = bar.querySelector('.bar-tip');
  const frac = (ev) => {
    const r = bar.getBoundingClientRect();
    return clamp((ev.clientX - r.left) / Math.max(1, r.width), 0, 1);
  };
  bar.addEventListener('pointerdown', ev => {
    if (!fill) return;
    // 合成事件（脚本驱动的 pointerId）或某些指针设备无法捕获：不影响拖动本身
    try { bar.setPointerCapture(ev.pointerId); } catch (e) { }
    bar.classList.add('dragging');
    // 手按在条上这段时间必须出帧：拖动要出缓冲进度、时间气泡和「松手前」的那次跟随刷新。
    _barHeld = true; nudgeFrames();
    const f = frac(ev); fill.style.width = f * 100 + '%'; if (thumb) thumb.style.left = f * 100 + '%';
    const move = e2 => { const ff = frac(e2); fill.style.width = ff * 100 + '%'; if (thumb) thumb.style.left = ff * 100 + '%'; if (tip) { tip.style.left = ff * 100 + '%'; tip.hidden = false; onHover && onHover(ff); } };
    const up = e2 => {
      bar.classList.remove('dragging');
      _barHeld = false;               // 交还给 settle：松手后只剩 seek 带来的那点刷新
      onFrac(frac(e2), true);
      if (tip) setTimeout(() => tip.hidden = true, 400);
      bar.removeEventListener('pointermove', move); bar.removeEventListener('pointerup', up); bar.removeEventListener('pointercancel', up);
    };
    bar.addEventListener('pointermove', move); bar.addEventListener('pointerup', up); bar.addEventListener('pointercancel', up);
  });
  bar.addEventListener('pointermove', ev => {
    const f = frac(ev);
    if (tip) { tip.hidden = false; tip.style.left = f * 100 + '%'; onHover && onHover(f); }
  });
  bar.addEventListener('pointerleave', () => { if (tip && !bar.classList.contains('dragging')) tip.hidden = true; });
  // role="slider" + tabindex="0" 就得能用键盘走完整个量程。← →（±5 秒 / ±5%）由
  // onKey 全局兜住了，但 Home/End 谁也没接：读屏用户进不了首尾。三条进度条共用这一份。
  bar.addEventListener('keydown', ev => {
    if (ev.key === 'Home') { ev.preventDefault(); ev.stopPropagation(); onFrac(0, true); }
    else if (ev.key === 'End') { ev.preventDefault(); ev.stopPropagation(); onFrac(1, true); }
  });
}

/* --------------------------------------------------------- 快捷键 */
/* 快捷键总览：设置页那张表是静态的，用户正在听歌时按 ? 就能随手查，不必离开列表。
   内容与 onKey 的真实分支一一对应；改快捷键时两处一起改（下面这段就是唯一的清单）。 */
const SHORTCUTS = [
  ['播放 / 暂停', '空格'],
  ['快退 / 快进 5 秒', '← / →'],
  ['音量 − / +', '↓ / ↑'],
  ['上一首 / 下一首', 'P / N'],
  ['列表内换行 / 播放 / 菜单', '↑ ↓ / Enter / Shift+F10'],
  ['搜索', '/'],
  ['快捷键总览', '?'],
  ['歌词 / 队列 / 详情', 'L / Q / I'],
  ['全屏播放页', 'F'],
  ['播放模式', 'M'],
  ['专注模式（隐藏侧栏）', 'Z'],
  ['随意电台', 'R'],
  ['清空播放队列（保留当前曲目）', 'Delete'],
  ['关闭浮层 / 退出全屏', 'Esc'],
];
function showShortcuts() {
  if (!$('#dialogMask').hidden) return;      // 已经开着一个浮层：别叠第二个，Esc 会分不清先关谁
  const box = el('div', 'keys');
  box.innerHTML = SHORTCUTS.map(([d, k]) => `<div><span>${esc(d)}</span><kbd>${esc(k)}</kbd></div>`).join('');
  dialog({ title: '键盘快捷键', body: box, okLabel: '知道了' });
}
function onKey(e) {
  const t = e.target;
  const k = e.key;
  const typing = !!(t.matches && t.matches('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
  const dialogOpen = !$('#dialogMask').hidden;
  const menuOpen = !$('#ctxMenu').hidden;
  if (k === 'Escape' && typing) return;      // 输入框自己的 Esc（清词/取消对话框）优先
  if (k === 'Escape') {
    // 从最上层往下关：对话框 → 右键菜单 → 窄窗抽屉 → 全屏。
    e.preventDefault();
    if (dialogOpen) closeDialog();
    else if (menuOpen) hideMenu(true);
    else if (_drawer.kind) closeDrawer(true);
    else if (!$('#fs').hidden) enterFs(false);
    return;
  }
  // 浮层开着时，下面的全局快捷键一律不接：菜单里按 ↑↓ 不能再偷偷调音量。
  if (dialogOpen || menuOpen || typing) return;
  if (k === ' ' || k === 'Spacebar') {
    // 焦点在按钮/链接/页签上时，空格是「按下这个控件」，不是「播放/暂停」。
    // 以前这里无条件 preventDefault，键盘用户按不到任何按钮。
    if (t.matches && t.matches('button, a[href], [role="button"], [role="tab"], [role="menuitem"], summary, [role="slider"]')) return;
    e.preventDefault(); togglePlay();
  }
  else if ((k === 'ArrowLeft' || k === 'ArrowRight') && t.closest && t.closest('#volWrap')) {
    e.preventDefault(); nudgeVolume(k === 'ArrowLeft' ? -.05 : .05);
  }
  else if (k === 'ArrowRight' && !e.shiftKey) { e.preventDefault(); seek(posNow() + 5); }
  else if (k === 'ArrowLeft' && !e.shiftKey) { e.preventDefault(); seek(Math.max(0, posNow() - 5)); }
  else if (k === 'ArrowUp') { e.preventDefault(); nudgeVolume(.05); }
  else if (k === 'ArrowDown') { e.preventDefault(); nudgeVolume(-.05); }
  else if (k === 'PageDown' || (k === 'n' || k === 'N')) next();
  else if (k === 'PageUp' || (k === 'p' || k === 'P')) prev();
  else if (k === '?' || (k === '/' && e.shiftKey) || k === 'F1') { e.preventDefault(); showShortcuts(); }
  else if (k === '/') { e.preventDefault(); $('#searchInput').focus(); }
  else if (k === 'l' || k === 'L') showNpTab('lyric');
  else if (k === 'q' || k === 'Q') showNpTab('queue');
  else if (k === 'f' || k === 'F') enterFs($('#fs').hidden);
  else if (k === 'm' || k === 'M') cycleMode();
  else if (k === 'z' || k === 'Z') toggleFocus();
  else if (k === 'r' || k === 'R') route('#/radio');
  else if (k === 'i' || k === 'I') showNpTab('info');
  else if (k === 'Delete') { clearQueue(); }
}

/* Qt 外壳的媒体键入口 */
window.SaltShell = {
  key(action) {
    if (action === 'playpause') togglePlay();
    else if (action === 'next') next();
    else if (action === 'prev') prev();
    else if (action === 'stop') stopPlayback();
  },
  async refreshAll(reloadView) {
    const b = await api('bootstrap').catch(() => null);
    if (!b) return;
    S.libraries = b.libraries; S.playlists = b.playlists; S.stats = b.stats;
    renderSidebar();
    // 欢迎页的隐藏以前只挂在「扫描完成」那一路（watchScan），所以通过原生对话框
    // 加文件夹时，库里已经有歌了但欢迎页还杵在中间。这里按真实曲目数决定。
    if (S.stats && S.stats.tracks) hideWelcome(); else showWelcome();
    if (reloadView) renderView();
  },
  /** 外壳（原生对话框 / 拖文件夹进窗口）加完曲库后的统一入口。 */
  async onFolderAdded() {
    watchScan();                 // 后台扫描进度条：扫完会自动再拉一次 bootstrap
    await this.refreshAll(true); // 先把新库名和「0 首」显示出来，不用等扫描
  },
  /** 外壳在窗口被激活时发现曲库目录的 mtime 变了（外部加/删了文件）。
      扫描由前端发起，这样进度条和扫完的刷新都走同一条路。 */
  onFilesChanged() {
    startScan({}, '曲库文件夹有变动，正在更新…');
  },
  flush,
  /** 只给自动化探针用的读数口。顶栏 const 声明不在 window 上（那是 ES 词法作用域，
      不是 window 属性），所以探针要读 isExcluded/lyricScrollMs 只能从这里走 ——
      不写死返回值：每个都当场调用真的那个函数，实现改了读数自动跟着改。 */
  probe: {
    isExcluded,
    dirKey,
    lyricScrollMs,
    syncLyricNow,
    excludes: () => (S.excludes || []).slice(),
    settings: () => JSON.parse(JSON.stringify(S.settings || {})),
    queueLen: () => (S.queue && S.queue.items ? S.queue.items.length : 0),
    /** 换页的落定信号：req = 最后一次「请求渲染」的序号，done = 已经跑完的序号。
        探针用它等页面画完，而不是拍脑袋 sleep 一秒。view/routedHash 用来看
        「URL 说的」「屏幕上这份」和「数据那份」是不是一致。 */
    renderState: () => ({ req: _renderSeq, done: _renderDoneSeq, runs: _renderRuns,
                          view: S.view, routedHash: S._routedHash || '',
                          urlHash: location.hash }),
    render: () => renderView(),
  },
};

function flush() {
  try {
    const patch = {
      volume: clamp(+S.settings.volume, 0, 1), muted: !!S.settings.muted, play_mode: S.settings.play_mode,
      last_track: S.track && S.track.id, last_position: posNow() || 0,
    };
    localStorage.setItem('salt:flush', JSON.stringify(patch));
    navigator.sendBeacon && navigator.sendBeacon('/api/settings', new Blob([JSON.stringify({ settings: patch })], { type: 'application/json' }));
    saveQueue();
  } catch (e) { }
}
S.flush = flush;

/* --------------------------------------------------------- 收藏 / 歌单操作 */
/**
 * 同步心形按钮的图标与按下态。
 * 不带 id 时只改播放条/全屏上的三个（它们始终跟着 S.track 走）；
 * 带 id 时把列表里所有「就是这一首」的行一起改掉 —— 同一首歌可以同时出现在
 * 此刻、最近播放、搜索结果三处，以前只更新被点的那一行，别的行就长期显示反了。
 */
function syncFavGlyphs(on, id) {
  [$('#btnFav'), $('#btnFav2'), $('#fsFav')].forEach(b => {
    if (!b) return;
    b.classList.toggle('on', on);
    b.querySelector('.ic')?.classList.toggle('fill', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('aria-label', on ? '取消喜欢' : '喜欢');
  });
  if (id == null) return;
  $$(`.row[data-id="${id}"] .fav`).forEach(b => {
    b.classList.toggle('on', on);
    b.querySelector('.ic')?.classList.toggle('fill', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('aria-label', on ? '取消喜欢' : '喜欢');
  });
}

async function toggleFav(t) {
  let r;
  try {
    r = await api('favorites/toggle', { method: 'POST', body: { track_id: t.id } });
  } catch (e) {
    return toast('收藏失败了：' + e.message, 'err');
  }
  if (!r || typeof r.favorite !== 'boolean') return toast('收藏失败了：服务返回异常', 'err');
  t.favorite = r.favorite;
  syncFavGlyphs(r.favorite, t.id);
  S.stats.favorites = r.count; renderSidebar();
  toast(r.favorite ? '已加入我喜欢的音乐' : '已取消喜欢', '', 1600);
  if (S.view === 'favorites') renderView();
}

async function newPlaylist() {
  const name = await promptDialog({ title: '新建歌单', placeholder: '歌单名称', value: '' });
  if (!name) return;
  let r;
  try { r = await api('playlists', { method: 'POST', body: { name } }); }
  catch (e) { return toast('创建失败：' + e.message, 'err'); }
  // 同名时服务端返回 409 + error 文案（api() 对「带 error 的响应」不抛异常），
  // 这里把它原样端出来，比原来那句猜的「可能同名了」准。
  if (!r || !r.ok) return toast((r && r.error) || '创建失败：本地服务没有回应', 'err');
  S.playlists = r.items; renderSidebar();
  location.hash = `#/playlist/${r.id}`;
}
function playlistMenu(ev, p, idx) {
  ev.preventDefault();
  const at = idx == null ? S.playlists.findIndex(x => x.id === p.id) : idx;
  const reload = async () => {
    const r = await api('playlists').catch(() => null);
    if (r) { S.playlists = r.items; renderSidebar(); }
  };
  showMenu(ev.clientX, ev.clientY, [
    { label: '播放', icon: '▶', fn: () => api(`playlist/${p.id}/tracks`).then(r => playFrom(r.items, 0, `歌单 · ${p.name}`)) },
    { label: '随机播放', icon: '⤨', fn: () => api(`playlist/${p.id}/tracks`).then(r => playFrom(shuffled(r.items), 0, `歌单 · ${p.name}`)) },
    { sep: 1 },
    { label: p.pinned ? '取消置顶' : '置顶', icon: '📌', fn: async () => {
        try { await api('playlists/pin', { method: 'POST', body: { id: p.id, pinned: !p.pinned } }); await reload(); }
        catch (e) { toast('置顶没生效：' + e.message, 'err'); }
      } },
    { label: '上移', icon: '↑', fn: () => commitPlaylistOrder(movePlaylist(at, at - 1)) },
    { label: '下移', icon: '↓', fn: () => commitPlaylistOrder(movePlaylist(at, at + 1)) },
    { sep: 1 },
    { label: '重命名', icon: '✎', fn: async () => {
        const name = await promptDialog({ title: '重命名歌单', value: p.name });
        if (!name) return;
        try {
          await api('playlists/rename', { method: 'POST', body: { id: p.id, name } });
        } catch (e) { return toast('重命名失败：' + e.message, 'err'); }
        await reload(); renderView();
      } },
    { label: '导出为 m3u 文件', icon: '⇩', fn: async () => {
        // 服务端写成 UTF-8+BOM 的 .m3u8（不带 BOM 时部分播放器按本地代码页读，中文曲名乱码），
        // 落到 App 自己的导出目录；这里只负责把路径端出来 + 给一个「在文件夹中显示」。
        let r;
        try { r = await api('playlists/export', { method: 'POST', body: { id: p.id } }); }
        catch (e) { return toast('导出失败：' + e.message, 'err'); }
        if (!r || !r.ok) return toast((r && r.error) || '导出失败：本地服务没有回应', 'err');
        const gone = r.missing ? `，其中 ${r.missing} 首的文件已不在磁盘上` : '';
        toast(`已导出「${p.name}」：${r.count} 首${gone}`, 'ok', 2600, {
          action: {
            label: '在文件夹中显示', fn: async () => {
              const x = await api('window/reveal', { method: 'POST', body: { path: r.path } }).catch(() => ({ ok: false }));
              if (x && x.ok) return;
              if (navigator.clipboard) await navigator.clipboard.writeText(r.path).catch(() => {});
              toast((x && x.error) || '打不开资源管理器，路径已复制进剪贴板', 'err', 2800);
            },
          },
        });
      } },
    { label: '删除歌单', icon: '🗑', danger: true, fn: async () => {
        if (!await confirmDialog(`删除歌单「${p.name}」？`, '只删除歌单本身，不会删除音频文件。')) return;
        // 删之前先把整份快照拿到手（条目顺序也在里面），撤销才有东西可写回。
        // 拿失败也要继续删 —— 只是这一次没了后悔药，气泡里就不给撤销按钮。
        let snap = null;
        try {
          const r = await api(`playlist/${p.id}`).catch(() => null);
          snap = {
            id: p.id, name: p.name, sort_order: p.sort_order || 0, pinned: p.pinned || 0,
            created_at: p.created_at, track_ids: ((r && r.items) || []).map(x => x.id),
          };
          await api('playlists/delete', { method: 'POST', body: { id: p.id } });
        } catch (e) { return toast('删除失败：' + e.message, 'err'); }
        await reload();
        // 只有正看着这个歌单时才需要跳走；别的视图跳回「此刻」等于把用户从他在看的地方赶走。
        if (S.view === 'playlist' && String(S.viewState.id) === String(p.id)) location.hash = '#/home';
        const undo = snap && {
          action: {
            label: '撤销', fn: async () => {
              try {
                const r = await api('playlists/restore', { method: 'POST', body: snap });
                if (r && r.ok) { S.playlists = r.items; renderSidebar(); toast(`「${snap.name}」已恢复（${snap.track_ids.length} 首）`, 'ok'); }
                else toast((r && r.error) || '恢复失败', 'err');
              } catch (e2) { toast('恢复失败：' + e2.message, 'err'); }
            },
          },
        };
        toast(`已删除歌单「${p.name}」` + (snap ? `（${snap.track_ids.length} 首）` : ''), '', 1800, undo || {});
      } },
  ]);
}

/** 把若干 track 加进某个歌单：三条入口（右键菜单、侧栏拖放、歌单详情页拖放）共用一份。
    以前只有右键菜单那条走 API，另两条各自零散地写，改一处会漏另一处。
    加完要顺手刷一次歌单列表：侧栏那个 `<em>` 计数是服务端算出来的 track_count，
    不刷新的话拖三首进去数字还站着不动，界面就在说谎。 */
async function addToPlaylist(pid, ids) {
  const p = S.playlists.find(x => String(x.id) === String(pid)) || {};
  let r;
  try { r = await api(`playlists/${pid}/add`, { method: 'POST', body: { track_ids: ids } }); }
  catch (e) { return toast('添加失败：' + e.message, 'err'); }
  const list = await api('playlists').catch(() => null);
  if (list && list.items) { S.playlists = list.items; renderSidebar(); }
  // added=0 不算失败：那几首本来就在歌单里（服务端按主键跳过）。
  toast(r && r.added ? `已添加 ${r.added} 首到「${p.name || '歌单'}」`
                     : `「${p.name || '歌单'}」里已经有了`, '', 2000);
  if (S.view === 'playlist' && String(pid) === String(S.viewState.id)) renderView();
}

function addToPlaylistMenu(ev, ids) {
  showMenu(ev.clientX, ev.clientY, S.playlists.map(p => ({
    label: p.name, icon: '♫', fn: () => addToPlaylist(p.id, ids),
  })).concat([{ sep: 1 }, { label: '新建歌单', icon: '＋', fn: newPlaylist }]));
}

/** 从当前歌单移除一首。撤销时把它放回「原来那一格」，而不是甩到末尾。 */
async function removeFromPlaylist(pid, t) {
  const pos = (S.viewState.list || []).findIndex(x => x.id === t.id);
  const name = (S.playlists.find(x => String(x.id) === String(pid)) || {}).name || '歌单';
  try { await api(`playlists/${pid}/remove`, { method: 'POST', body: { track_id: t.id } }); }
  catch (e) { return toast('移除失败：' + e.message, 'err'); }
  renderView();
  toast(`已从「${name}」移除《${t.title}》`, '', 1800, {
    action: {
      label: '撤销', fn: async () => {
        let r;
        try { r = await api(`playlists/${pid}/add`, { method: 'POST', body: { track_ids: [t.id], position: pos } }); }
        catch (e) { return toast('放回失败：' + e.message, 'err'); }
        if (r && r.added) { renderView(); toast('已放回原来的位置', 'ok', 1500); }
        else toast('放不回去了：这首歌可能已不在曲库', 'err');
      },
    },
  });
}

function trackMenu(ev, t, list) {
  ev.preventDefault();
  /* 键盘（Shift+F10 / 菜单键）发来的 contextmenu 没有鼠标坐标，Chromium 给的是 0,0；
     直接拿它 showMenu 会把菜单甩到窗口左上角。落在当前行上更符合“对哪行操作”。 */
  let mx = ev.clientX, my = ev.clientY;
  if (!mx && !my) {
    const r = ev.currentTarget && ev.currentTarget.getBoundingClientRect
      ? ev.currentTarget.getBoundingClientRect() : null;
    if (r) { mx = r.left + Math.min(120, r.width / 2); my = r.top + r.height / 2; }
  }
  const idx = list.indexOf(t);
  showMenu(mx, my, [
    { label: '立即播放', icon: '▶', fn: () => playFrom(list, idx) },
    /* 这两条以前只改内存：重启后「稍后播放」的那首就没了（renderQueue 不落库，
       start() 只在真正开始播时才写）。菜单是队列的入口之一，改完必须 saveQueue。 */
    { label: '下一首播放', icon: '⇥', fn: () => { S.queue.splice(S.qi + 1, 0, t); renderQueue(); saveQueue(); refreshPreload(); toast(`稍后播放：${t.title}`); } },
    { label: '加入队列末尾', icon: '＋', fn: () => { S.queue.push(t); if (S.qi < 0) { S.qi = 0; start(t); } renderQueue(); saveQueue(); refreshPreload(); toast('已加入队列'); } },
    { sep: 1 },
    { label: t.favorite ? '取消喜欢' : '喜欢', icon: t.favorite ? '♥' : '♡', fn: () => toggleFav(t) },
    { label: '添加到歌单', icon: '♫', sub: true, fn: (e) => addToPlaylistMenu(e, [t.id]) },
    // 只有在歌单里才谈得上「从歌单移除」——这一条以前压根没有入口（服务端接口和验证脚本
    // 都有，界面上却够不着），现在按当前视图挂上。
    ...(S.view === 'playlist' && S.viewState.list && S.viewState.list.includes(t)
      ? [{ label: '从本歌单移除', icon: '✕', danger: true, fn: () => removeFromPlaylist(S.viewState.id, t) }]
      : []),
    { sep: 1 },
    { label: '查看歌词', icon: '词', fn: () => { showNpTab('lyric'); loadLyric(t.id); } },
    { label: '曲目详情', icon: 'ⓘ', fn: () => { showNpTab('info'); setTrack(t, true); } },
    { label: '挂载 .lrc 歌词', icon: '↥', fn: () => attachLyric(t) },
    { sep: 1 },
    { label: '复制文件路径', icon: '⧉', fn: async () => { const f = await fullTrack(t); if (navigator.clipboard) await navigator.clipboard.writeText(f.path || ''); toast(f.path ? '路径已复制' : '复制失败：拿不到该文件路径', f.path ? '' : 'err'); } },
    { label: '在资源管理器中显示', icon: 'reveal', fn: () => revealFile(t) },
  ]);
}

/* 列表行是窄投影（没有 path），需要整条记录的动作先按 id 补一次单条查询。 */
async function fullTrack(t) {
  if (!t) return t;
  if (t.path) return t;
  try { const r = await api('track/' + t.id); return (r && r.track) || t; } catch (e) { return t; }
}

async function revealFile(t) {
  const f = await fullTrack(t);
  const r = await api('window/reveal', { method: 'POST', body: { path: f.path } }).catch(() => ({ ok: false }));
  if (!r.ok) {
    const alt = (f.path || '').replace(/[\\/]+[^\\/]*$/, '') || f.folder || '';
    if (navigator.clipboard && alt) await navigator.clipboard.writeText(alt);
    toast(r.error || 'App 打不开资源管理器，已把文件夹放进剪贴板', 'err', 2800);
  }
}

/* --------------------------------------------------------- 歌词挂载 */
async function attachLyric(t) {
  const target = t || S.track;
  if (!target) return toast('先选一首歌');
  const r = await api('library/pick', { method: 'POST', body: { file: true, path: target.folder } }).catch(() => null);
  if (r && r.ok && r.path) {
    let a;
    try { a = await api('lyrics/attach', { method: 'POST', body: { track_id: target.id, path: r.path } }); }
    catch (e2) { return toast('挂载失败：' + e2.message, 'err'); }
    if (a.ok) { toast('歌词已挂载'); loadLyric(target.id); } else toast(a.error || '挂载失败', 'err');
    return;
  }
  $('#lyricFile').click();
}
function applyLyricText(text, name) {
  const t = S.track;
  if (!t) return;
  // 先本地画，再落盘：文件选完到看到歌词之间不该有一次网络等待，
  // 而落盘失败也必须继续显示 —— 只是要诚实说清「这次播放有效」。
  const lines = parseLrcLocal(text);
  S.lyric = { lines, i: -1, source: name || '手动', plain: !lines.length, text };
  renderLyric();
  api('lyrics/save', { method: 'POST', body: { track_id: t.id, text } })
    .then((r) => {
      if (r && r.ok) {
        toast('歌词已存好，之后每次播放都会带上');
        if (S.track && S.track.id === t.id) loadLyric(t.id);   // 以服务端解析的行数为准
      } else {
        toast('存不下来：' + ((r && r.error) || '服务端没认这段歌词') + '；本次播放还能看', 'err', 5200);
      }
    })
    .catch((e) => toast('存不下来：' + e.message + '；本次播放还能看', 'err', 5200));
}
/* 浏览器端轻量 LRC 解析，用于挂载本地文件预览 */
function parseLrcLocal(text) {
  const out = [];
  let offset = 0;
  String(text).replace(/\r/g, '').split('\n').forEach(line => {
    const st = line.match(/^\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g);
    const off = /^\[offset:\s*([+-]?\d+)\]/i.exec(line.trim());
    if (off) { offset = parseInt(off[1], 10) / 1000; return; }
    if (!st) return;
    const body = line.replace(/\[[^\]]*\]/g, '').trim();
    if (!body) return;
    st.forEach(tag => {
      const m = /\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/.exec(tag);
      const s = parseInt(m[2], 10); if (s > 59) return;
      out.push({ t: Math.max(0, +m[1] * 60 + s + (+((m[3] || '0') + '00').slice(0, 3) / 1000) - offset), text: body, trans: '' });
    });
  });
  out.sort((a, b) => a.t - b.t);
  const merged = [];
  out.forEach(l => {
    const prev = merged[merged.length - 1];
    if (prev && Math.abs(prev.t - l.t) < 0.05) { if (!prev.trans) prev.trans = l.text; return; }
    merged.push(l);
  });
  return merged;
}

/* --------------------------------------------------------- 文件夹 / 扫描 */
/* 侧栏底部的来源入口：目录列表 + 添加/管理。列表项直达该目录的歌曲页，
   右侧的数字就是扫描进库的曲目数（和文件夹视图同一份口径）。 */
function librarySourceMenu() {
  const b = $('#libSource');
  const r = b.getBoundingClientRect();
  const items = [];
  if (S.libraries.length) {
    items.push({ label: `音乐库 · ${S.libraries.length} 个文件夹` });
    S.libraries.forEach(l => items.push({
      label: l.name || l.path, icon: 'folder', title: l.path,
      k: l.track_count ? String(l.track_count) : '',
      fn: () => { location.hash = `#/folder/${encodeURIComponent(l.path)}`; },
    }));
    items.push({ sep: 1 });
  }
  items.push({ label: '添加音乐文件夹…', icon: 'plus', fn: pickFolder });
  items.push({ label: '输入文件夹路径…', icon: 'edit', fn: manualFolder });
  if (S.libraries.length) {
    items.push({ sep: 1 });
    items.push({ label: '管理音乐文件夹…', icon: 'settings', fn: () => { location.hash = '#/settings'; } });
  }
  showMenu(r.left, r.top - 6, items);
}

async function pickFolder() {
  let r;
  try { r = await api('library/pick', { method: 'POST', body: { path: (S.libraries[0] || {}).path || '' } }); }
  catch (e) { return toast('打不开选择框：' + e.message, 'err'); }
  if (r.ok && r.path) { await addLibrary(r.path); return; }
  if (r.need_manual || r.cancelled) return manualFolder();
  toast(r.error || '未能打开选择框', 'err');
}
async function manualFolder() {
  const val = await promptDialog({
    title: '输入音乐文件夹路径', placeholder: '例如 D:\\Music',
    value: $('#welcomePath') && $('#welcomePath').value || '',
    hint: '支持中文路径。扫描在后台进行，几秒后即可看到内容。',
  });
  if (!val) return;
  $('#welcomePath') && ($('#welcomePath').value = val);
  await addLibrary(val);
}
async function addLibrary(path) {
  toast('正在导入：' + path, '', 2500);
  let r;
  try { r = await api('library/add', { method: 'POST', body: { path } }); }
  catch (e) { return toast('导入失败：' + e.message, 'err', 6000); }
  if (!r.ok) return toast(r.error || '导入失败', 'err', 6000);
  watchScan();
  await window.SaltShell.refreshAll(false);
}
async function removeLibrary(lib) {
  // 两个入口给的对象不是一套字段：设置里是 libraries 的行（id），文件夹视图的顶层是
  // /api/folders 造的（lib_id）。少这一刀归一化，从文件夹页点「移除」就是把 undefined
  // 发给服务端 —— 它报 500，界面则什么都不说。
  const lid = lib.id != null ? lib.id : lib.lib_id;
  if (lid == null) return toast('不知道要移除哪个文件夹（数据里没有 id）', 'err');
  if (!await confirmDialog(`移除「${lib.name}」？`, '曲库记录会删除，硬盘上的文件不会被动过。')) return;
  try {
    await api('library/remove', { method: 'POST', body: { id: lid, keep_tracks: false } });
  } catch (e) { return toast('移除失败：' + e.message, 'err'); }
  await window.SaltShell.refreshAll(true);
  toast(`已移除「${lib.name}」`, '', 2000, {
    action: {
      label: '撤销', fn: async () => {
        try { await addLibrary(lib.path); toast('文件夹已找回', 'ok'); }
        catch (e) { toast('找回失败：' + e.message, 'err'); }
      },
    },
  });
}
/** 发起一次扫描并挂上进度条。四个入口（设置里的两个按钮、文件夹行、开关）都走这里，
    免得某一处忘了接错误。 */
function startScan(body, okMsg) {
  api('scan', { method: 'POST', body: body || {} })
    .then(() => { watchScan(); if (okMsg) toast(okMsg, '', 1800); })
    .catch(e => toast('扫描启动失败：' + e.message, 'err'));
}
/** 等扫描线程停下来（默认最多 60 s），返回最后一次的状态；超时返回 null。
    「先扫再清」这类流程需要它：POST /api/scan 只是把线程丢出去，不 await 就等于猜。 */
async function waitScanIdle(ms = 60000) {
  const until = Date.now() + ms;
  let s = null;
  while (Date.now() < until) {
    s = await api('scan').catch(() => null);
    if (!s || s.running !== true) return s;
    await new Promise(r => setTimeout(r, 350));
  }
  return null;
}
function watchScan() {
  clearInterval(S.scanTimer);
  $('#scanPill').hidden = false;
  S.scanTimer = setInterval(async () => {
    const s = await api('scan').catch(() => null);
    if (!s) return clearInterval(S.scanTimer);
    const pct = s.total ? Math.round(s.done * 100 / s.total) : 0;
    $('#scanBarFill').style.width = pct + '%';
    $('#scanText').textContent = s.running
      ? `扫描中 ${s.done}/${s.total}${s.current ? ' · ' + s.current : ''}`
      : `扫描完成：新增 ${s.added} · 更新 ${s.updated} · 移除 ${s.removed}${s.errors && s.errors.length ? ` · ${s.errors.length} 个提示` : ''}`;
    if (!s.running) {
      clearInterval(S.scanTimer);
      setTimeout(() => { $('#scanPill').hidden = true; }, 5200);
      const b = await api('bootstrap').catch(() => null);
      if (b) {
        S.libraries = b.libraries; S.playlists = b.playlists; S.stats = b.stats;
        renderSidebar();
        // 一个文件夹扫完仍可能是 0 首（比如全是受支持列表外的格式），这时欢迎页要回来。
        if (S.stats.tracks) hideWelcome(); else showWelcome();
        renderView();
      }
      if (s.errors && s.errors.length) console.warn('[scan]', s.errors);
    }
  }, 700);
}

function showWelcome() { $('#welcome').hidden = false; }
function hideWelcome() { $('#welcome').hidden = true; }
$('#welcomePath') && ($('#welcomePath').addEventListener('keydown', e => { if (e.key === 'Enter') manualFolder(); }));

/* --------------------------------------------------------- 定时停止 */
function sleepMenu(ev) {
  const opts = [15, 30, 45, 60, 90, 0].map(min => ({
    label: min ? `${min} 分钟后停止` : '取消定时',
    icon: '◷',
    fn: () => {
      S.sleepAt = min ? Date.now() + min * 60000 : 0;
      const b = $('#btnSleep');
      b.classList.toggle('on', !!min);
      b.setAttribute('aria-pressed', min ? 'true' : 'false');
      nameEl(b, min ? `${min} 分钟后停止播放（点击修改）` : '定时停止播放');
      S._sleepLabel = '';            // 让 checkSleep 重新算一次剩余分钟
      toast(min ? `将在 ${min} 分钟后停止` : '已取消定时');
    },
  }));
  const r = ev ? { clientX: ev.clientX, clientY: ev.clientY } : { clientX: $('#btnSleep').getBoundingClientRect().left, clientY: $('#btnSleep').getBoundingClientRect().top - 220 };
  showMenu(r.clientX, r.clientY, opts);
}
function checkSleep() {
  if (!S.sleepAt) return;
  const left = S.sleepAt - Date.now();
  if (left <= 0) { S.sleepAt = 0; $('#btnSleep').classList.remove('on'); pauseNow(); toast('定时结束，已停止播放'); return; }
  if (left < 60000 && !S._sleepWarned) { S._sleepWarned = 1; toast('还有 1 分钟停止播放'); }
  const label = `还剩 ${Math.ceil(left / 60000)} 分钟停止播放`;
  if (S._sleepLabel !== label) { S._sleepLabel = label; nameEl($('#btnSleep'), label); }
}

/* 动态流光的开关：三个条件都要成立才真的在飘 —— 设置开着、真的在播、页面可见。
   三个条件汇成一个 data-play / data-ambient 写在 <html> 上，CSS 只认这两个属性
   （animation-play-state 的判定留在样式里，JS 不去逐帧摸 style）。
   静止的合成层不花钱，一直在飘的层花钱：闲置账单就是这么守住的。 */
function syncAmbient() {
  const h = document.documentElement;
  h.dataset.ambient = S.settings.ambient === false ? 'off' : 'on';
  h.dataset.play = (S.playing && !document.hidden) ? '1' : '0';
}
document.addEventListener('visibilitychange', () => syncAmbient());

/* 歌词滚动：一次换行给多少毫秒。不用 behavior:'smooth' 是因为它没有速度旋钮，
   而且长距离时浏览器自己的缓动会一路滑过好几行。这里自己补间，关键是每条 burst
   都有终点：跑满 ms 就 cancel 并把这条动画从表里摘掉，闲置时不会留下在空转的 rAF。
   触发点只有「换行」，而换行只发生在 tick 里（那时帧循环本来就在跑）。 */
const _lyScroll = new Map();
/* .lyric-box 在 app.css 里写着 scroll-behavior: smooth，这条 CSS 对我们是干扰：只要它生效，
   赋 scrollTop 就会被浏览器理解成「再起一次平滑滚动」，于是
   —— 「不滚动（直接跳）」并不会当场到位（本机 Chromium 87 实测：赋完立刻读还是旧位置，
      几百毫秒才爬过去），设置里写着「直接跳」却仍在动画；
   —— 自己补间的每一帧也会被 CSS 再缓一层，实际轨迹比 ms 慢，读到的位置还不是我们算出来的位置。
   所以接管这个 root 期间把 behavior 按成 auto，落地再还原成接管前的内联值（不写死、不清空），
   CSS 规则本身不动。 */
const _lyBehavior = new Map();
function ownScroll(root) {
  if (!_lyBehavior.has(root)) _lyBehavior.set(root, root.style.scrollBehavior);
  root.style.scrollBehavior = 'auto';
}
function freeScroll(root) {
  if (!_lyBehavior.has(root)) return;
  root.style.scrollBehavior = _lyBehavior.get(root);
  _lyBehavior.delete(root);
}
// 顶层 const 不挂在 window 上（app.js 只把 S 交出去），所以这条「还在动的补间」要显式给
// 验证脚本一个读数：它的大小就是「此刻有几条动画没落地」，闲置账单的守卫断言要看它。
window.Salt._lyScroll = _lyScroll;
function lyricScrollMs() {
  const ms = +S.settings.lyric_scroll_ms;
  return Number.isFinite(ms) ? clamp(ms, 0, 1500) : 460;
}
function animateScroll(root, top, ms) {
  if (!root) return;
  const from = root.scrollTop;
  const to = Math.max(0, Math.min(top, root.scrollHeight - root.clientHeight));
  const reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const prev = _lyScroll.get(root);
  if (prev) cancelAnimationFrame(prev.raf);
  _lyScroll.delete(root);
  if (ms <= 0 || reduce || Math.abs(to - from) < 2) {
    ownScroll(root);
    root.scrollTop = to;
    freeScroll(root);
    return;
  }
  const st = { from, to, ms, t0: performance.now(), raf: 0 };
  _lyScroll.set(root, st);
  ownScroll(root);
  const step = () => {
    const s = _lyScroll.get(root);
    if (!s) return;                      // 被更新的一次调用接管了（接管方自己已持有 behavior）
    const k = Math.min(1, (performance.now() - s.t0) / s.ms);
    root.scrollTop = s.from + (s.to - s.from) * (1 - Math.pow(1 - k, 3));   // ease-out cubic
    if (k < 1) s.raf = requestAnimationFrame(step);
    else { _lyScroll.delete(root); freeScroll(root); }   // 到点就摘：这条 Map 的大小就是「还在动的动画数」
  };
  st.raf = requestAnimationFrame(step);
}

/* ------------------------------------------------------------ 滚轮滑行 ------
   Chromium 的滚轮是「一格一落位」：一格 100px 直接跳过去，看起来就是闪一下。
   这里接管 #scroller 的滚轮，把每一格的位移累到目标位置，再用一阶低通追过去：
   大格约 75ms、触控板的小增量约 45ms 收拢，连续几格自然叠成一段滑行。
   每帧只写一次 scrollTop；虚拟列表的窗口平移/pads 仍由 onscroll 驱动，和以前
   一样（缓存了 topPx 后，普通帧的滚动处理只剩两次样式属性比较）。
   不接管：Ctrl/Alt/Shift+滚轮、reduced-motion、输入框/嵌套滚动区、已经到顶/底
   还要继续滚的那一下 —— 交回浏览器处理。滚轮滑行期间只要滚动位置被外部改动
   （拖滚动条、键盘翻页、字母轨补间），立刻退出，不跟用户抢控制权。 */
function initWheelGlide() {
  const sc = $('#scroller');
  if (!sc || sc.__glide) return;
  sc.__glide = true;
  const st = { target: 0, pos: 0, raf: 0, wrote: 0, max: 0,
               lastWheel: 0, last: 0, tau: 70, active: false };
  window.Salt._wheelGlide = st;
  const maxTop = () => Math.max(0, sc.scrollHeight - sc.clientHeight);
  const write = (v) => { sc.scrollTop = v; st.wrote = v; };
  const stop = () => {
    if (st.raf) cancelAnimationFrame(st.raf);
    st.raf = 0;
    if (st.active) { st.active = false; freeScroll(sc); }
  };
  const run = () => {
    st.raf = 0;
    const now = performance.now();
    const dt = Math.min(64, Math.max(1, now - st.last));
    st.last = now;
    // 有人从旁边改了滚动位置：这轮滑行让位。
    if (Math.abs(sc.scrollTop - st.wrote) > 2) {
      st.pos = st.target = st.wrote = sc.scrollTop;
      stop();
      return;
    }
    if (st.target < 0) st.target = 0;
    else if (st.target > st.max) st.target = st.max;
    st.pos += (st.target - st.pos) * (1 - Math.exp(-dt / st.tau));
    if (Math.abs(st.target - st.pos) < .4) {
      st.pos = st.target;
      write(st.pos);
      if (now - st.lastWheel > 90) { stop(); return; }
    } else {
      write(st.pos);
    }
    st.raf = requestAnimationFrame(run);
  };
  sc.addEventListener('wheel', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (rmq()) return;
    const tag = e.target && e.target.tagName;
    if (tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'INPUT') return;
    let dy = e.deltaY;
    if (!dy) return;
    if (e.deltaMode === 1) dy *= 40;              // 行
    else if (e.deltaMode === 2) dy *= sc.clientHeight;   // 页
    st.max = maxTop();
    const cur = sc.scrollTop;
    // 已经到头、这一下还要往外滚：让原生那一下（回弹/链式滚动）发生。
    // 但动画还在跑时不放行 —— 反向的第一格必须能立刻改目标，否则会继续往反方向飘。
    if (!st.raf && ((cur <= 0 && dy < 0) || (cur >= st.max - .5 && dy > 0))) return;
    e.preventDefault();
    if (!st.active) {
      st.active = true;
      st.pos = st.target = st.wrote = cur;
      st.last = performance.now();
      ownScroll(sc);
    } else if (Math.abs(cur - st.wrote) > 2) {
      st.pos = st.target = st.wrote = cur;         // 滑行中被拖走：按新位置续
    }
    st.target = Math.max(0, Math.min(st.max, st.target + dy));
    // 大格拖尾长一点，触控板小增量跟手一点。
    st.tau = Math.min(95, Math.max(45, 44 + Math.abs(dy) * .32));
    st.lastWheel = performance.now();
    if (!st.raf) st.raf = requestAnimationFrame(run);
  }, { passive: false });
}
initWheelGlide();

/* ==========================================================================
   设置视图
   ========================================================================== */
/* 曲库分析的渲染。数据是 /api/quality 一次算好的三份分组（格式 / 码率档 / 采样率），
   这里只负责把它摆出来，不再在前端二次聚合 —— 前端再算一遍就会有「两份总数不一样」
   这种账（服务端按 missing=0 过滤，前端拿到的是已分页的行）。 */
function renderQuality(box, q) {
  box.textContent = '';
  if (!q || !q.ok) { box.appendChild(el('div', 'note', '没拿到分析结果。')); return; }
  const t = q.total || {};
  const head = el('div', 'qual-head');
  head.innerHTML = `<div><b>${t.tracks || 0}</b><span>首</span></div>
    <div><b>${fmtLong(t.duration || 0)}</b><span>总时长</span></div>
    <div><b>${fmtBytes(t.size || 0)}</b><span>占用</span></div>
    <div><b>${Math.round((q.lossless || {}).share * 100) || 0}%</b><span>无损占比</span></div>
    <div><b>${Math.round((t.avg_bitrate || 0) / 1000)}k</b><span>平均码率</span></div>`;
  box.appendChild(head);
  const maxFmt = Math.max(1, ...(q.formats || []).map(f => f.tracks));
  const table = el('div', 'qual-table');
  (q.formats || []).forEach(f => {
    const r = el('div', 'qual-row');
    r.innerHTML = `<span class="qual-f">${esc(f.fmt.toUpperCase())}${f.lossless ? ' <i>无损</i>' : ''}</span>
      <span class="qual-bar"><i style="width:${Math.max(2, 100 * f.tracks / maxFmt).toFixed(1)}%"></i></span>
      <span class="qual-n">${f.tracks} 首</span><span class="qual-b">${Math.round(f.avg_bitrate / 1000)}k</span>
      <span class="qual-d">${fmtLong(f.duration)}</span><span class="qual-s">${fmtBytes(f.size)}</span>`;
    table.appendChild(r);
  });
  box.appendChild(table);
  const bands = el('div', 'qual-bands');
  (q.bands || []).forEach(b => {
    if (!b.tracks) return;
    const chip = el('div', 'qual-chip');
    chip.innerHTML = `<span>${esc(b.label)}</span><b>${b.tracks}</b><em>${Math.round(b.share * 100)}%</em>`;
    bands.appendChild(chip);
  });
  box.appendChild(bands);
  const srs = (q.sample_rates || []).filter(r => r.hz).slice(0, 8)
    .map(r => `${(r.hz / 1000).toFixed(1)}k×${r.tracks}`).join('　');
  if (srs) box.appendChild(el('div', 'note', '采样率分布：' + srs));
  if (t.unplayable) box.appendChild(el('div', 'note', `另有 ${t.unplayable} 首内核解不了（wma/ape 等），仍会在列表里显示。`));
}

async function viewSettings(v) {
  $('#viewSub').textContent = `${S.app.native ? 'App 模式' : '浏览器模式'}`;
  const libs = S.libraries;
  v.innerHTML = `
  <div class="settings">
    <div class="set-block">
      <h3>音乐文件夹</h3><div class="desc">扫描在后台增量进行：改动过的文件才会重新读标签。</div>
      <div id="setLibs"></div>
      <div class="set-row"><div><label>添加文件夹</label><div class="note">也可以直接把文件夹拖到窗口上</div></div>
        <div style="display:flex;gap:8px"><button class="ghost" id="setPick">浏览…</button><button class="ghost" id="setManual">输入路径</button></div></div>
      <div class="set-row"><div><label>包含无法播放的格式</label><div class="note">wma / ape / dsf 等：显示出来，但浏览器内核无法解码</div></div>
        <button class="switch ${S.settings.include_unsupported ? 'on' : ''}" data-set="include_unsupported"></button></div>
      <div class="set-row"><div><label>清理已失效记录</label><div class="note">先重新扫描核对，再删除标记为失效的条目（文件刚被移走时不重扫是查不出来的）</div></div>
        <button class="ghost" id="setPurge">核对并清理</button></div>
      <div class="set-row" id="setExRow"><div><label>排除的文件夹</label><div class="note">这些目录（连同里面的子目录）扫描时整个跳过；取消排除后重扫会把曲目找回来</div></div>
        <button class="ghost" id="setExPick">添加…</button></div>
      <div id="setExcludes"></div>
    </div>

    <div class="set-block">
      <h3>外观</h3>
      <div class="set-row"><label>主题</label>
        <div style="display:flex;gap:8px"><button class="ghost" id="setThemeDark">深色</button><button class="ghost" id="setThemeLight">浅色</button></div></div>
      <div class="set-row"><div><label>强调色来自封面</label><div class="note">跟随当前曲目封面的主色，连同背景模糊图</div></div>
        <button class="switch ${S.settings.accent_mode === 'cover' ? 'on' : ''}" data-set="accent_mode" data-on="cover" data-off="preset"></button></div>
      <div class="set-row"><label>固定强调色</label><div class="swatches" id="setSwatch"></div></div>
      <div class="set-row"><div><label>歌词位置</label><div class="note">当前行居中或靠上显示</div></div>
        <div style="display:flex;gap:8px"><button class="ghost" id="setLyCenter">居中</button><button class="ghost" id="setLyTop">靠上</button></div></div>
      <div class="set-row"><div><label>歌词时间偏移</label><div class="note">词比唱得早就调负、比唱得晚就调正，一格 0.5 秒（LRC 自带的 offset 头之外再加）</div></div>
        <div class="stepper"><button class="ghost" id="setLyOffDown" title="提前 0.5 秒">−0.5</button>
          <span class="time" id="setLyOffV">${(+S.settings.lyric_offset || 0).toFixed(1)}s</span>
          <button class="ghost" id="setLyOffUp" title="延后 0.5 秒">+0.5</button>
          <button class="link-btn" id="setLyOffReset">复位</button></div></div>
      <div class="set-row"><label>显示双语第二行</label>
        <button class="switch ${S.settings.show_translation !== false ? 'on' : ''}" data-set="show_translation"></button></div>
      <div class="set-row"><div><label>播放条上跑歌词</label><div class="note">有词时把播放条第二行换成当前这句；没词（或还没走到第一行）时它还是「艺人 — 专辑」</div></div>
        <button class="switch ${S.settings.playbar_lyric !== false ? 'on' : ''}" data-set="playbar_lyric"></button></div>
      <div class="set-row"><div><label>歌词滚动速度</label><div class="note">换行那一下的缓动给多少毫秒；「不滚动」是直接跳过去</div></div>
        <select id="setLySpeed">${(() => { const cur = lyricScrollMs();
            const list = LY_SPEEDS.some(([ms]) => ms === cur) ? LY_SPEEDS : LY_SPEEDS.concat([[cur, cur + ' 毫秒']]);
            return list.map(([ms, nm]) => `<option value="${ms}" ${ms === cur ? 'selected' : ''}>${nm}</option>`).join(''); })()}</select></div>
      <div class="set-row"><div><label>背景动态流光</label><div class="note">跟着封面取色的两层慢速光带；暂停、或窗口看不见时自己停住</div></div>
        <button class="switch ${S.settings.ambient !== false ? 'on' : ''}" data-set="ambient"></button></div>
      <div class="set-row"><div><label>桌面歌词</label><div class="note">独立置顶的歌词小窗，主窗口收进托盘后继续跟着唱；拖动那条词就能换位置</div></div>
        <button class="switch ${S.settings.desktop_lyric ? 'on' : ''}" data-set="desktop_lyric"></button></div>
      <div class="set-row"><div><label>小窗保持置顶</label><div class="note">关掉后别的窗口会盖住它；不置顶时它仍然不会被任务栏压掉</div></div>
        <button class="switch ${S.settings.desktop_lyric_on_top !== false ? 'on' : ''}" data-set="desktop_lyric_on_top"></button></div>
    </div>

    <div class="set-block">
      <h3>性能</h3>
      <div class="set-row"><div><label>图形加速</label>
        <div class="note" id="setGpuNote">正在检查图形状态…</div></div></div>
    </div>

    <div class="set-block">
      <h3>播放</h3>
      <div class="set-row"><label>默认播放模式</label>
        <select id="setMode">${Object.entries(MODES).map(([k, m]) => `<option value="${k}" ${S.settings.play_mode === k ? 'selected' : ''}>${m.label}</option>`).join('')}</select></div>
      <div class="set-row"><div><label>无缝衔接</label><div class="note">浏览器内核下生效：下一首在当前这首放完前就预载进第二只播放器，到点直接接上。缓存冷 / 机械盘上那 20~300 ms 的空白就听不到了；随机模式每圈末尾与顺序播放最后一首仍按原样收尾</div></div>
        <button class="switch ${S.settings.gapless !== false ? 'on' : ''}" data-set="gapless"></button></div>
      <div class="set-row"><div><label>交叉淡化</label><div class="note">浏览器内核下生效：「关闭」= 只做 80 ms 防爆音的无缝衔接；大于 0 时两首按等功率曲线重叠这么久（适合随机播放换氛围）。原生引擎换曲仍会重开一条流</div></div>
        <select id="setCrossfade">${[[0, '关闭（无缝衔接）'], [1, '1 秒'], [2, '2 秒'], [3, '3 秒'], [5, '5 秒'], [8, '8 秒'], [12, '12 秒']]
          .map(([v, nm]) => `<option value="${v}" ${(+(S.settings.crossfade_sec) || 0) === v ? 'selected' : ''}>${nm}</option>`).join('')}</select></div>
      <div class="set-row"><div><label>记住上次的进度</label><div class="note">启动时自动停在离开时的位置（不会自动播放）</div></div>
        <button class="switch ${S.settings.resume_on_start ? 'on' : ''}" data-set="resume_on_start"></button></div>
      <div class="set-row"><label>播放速度</label>
        <div style="display:flex;align-items:center;gap:10px"><input type="range" min="${RATE_MIN}" max="${RATE_MAX}" step="0.05" value="${clamp(+S.settings.rate || 1, RATE_MIN, RATE_MAX)}" id="setRate" style="width:150px"><span class="time" id="setRateV">${clamp(+S.settings.rate || 1, RATE_MIN, RATE_MAX).toFixed(2)}×</span></div></div>
      <div class="set-row"><div><label>关闭主窗口时缩到托盘</label><div class="note">仅 App 模式有效</div></div>
        <button class="switch ${S.settings.close_to_tray ? 'on' : ''}" data-set="close_to_tray"></button></div>
    </div>

    <div class="set-block">
      <h3>音质 / 播放引擎</h3>
      <div class="desc">默认走浏览器内核（<code>&lt;audio&gt;</code> + WebAudio）。切到「原生」后，服务端用 ffmpeg 做 <b>float32 浮点解码</b>、经 <b>WASAPI</b> 输出，均衡器 / 声像 / 响度 / 淡入 / 倍速都改在服务端这条全 float32 链路上算 —— 这是椒盐那批「会员音质」项的落点。切换引擎会从头播放当前曲目。</div>
      <div class="set-row"><label>播放引擎</label>
        <select id="setEngine">
          <option value="chromium" ${S.settings.audio_engine !== 'native' ? 'selected' : ''}>浏览器内核（默认）</option>
          <option value="native" ${S.settings.audio_engine === 'native' ? 'selected' : ''}>原生（ffmpeg 浮点解码 → WASAPI）</option>
        </select></div>
      <div class="set-row"><div><label>输出设备</label><div class="note">原生引擎下生效；「系统默认」即跟随默认 WASAPI 输出设备</div></div>
        <select id="setDevice"><option value="">加载中…</option></select></div>
      <div class="set-row"><div><label>目标采样率</label><div class="note">只在「独占输出」档生效；共享输出固定用设备混音率（WASAPI 共享不做重采样，选别的率会开不出声）。「跟随设备默认」= 独占也按设备混音率协商。</div></div>
        <select id="setTargetRate">${[[0, '跟随设备默认'], [44100, '44.1 kHz'], [48000, '48 kHz'], [96000, '96 kHz'], [192000, '192 kHz']]
          .map(([hz, nm]) => `<option value="${hz}" ${(+(S.settings.audio_target_rate) || 0) === hz ? 'selected' : ''}>${nm}</option>`).join('')}</select></div>
      <div class="set-row"><div><label>声像（左右平衡）</label><div class="note">原生引擎下生效；−1 全左 / +1 全右，等功率声像</div></div>
        <div style="display:flex;align-items:center;gap:10px"><input type="range" id="setBalance" min="-1" max="1" step="0.05" value="${clamp(+S.settings.audio_balance || 0, -1, 1)}" style="width:150px"><span class="time" id="setBalanceV">${clamp(+S.settings.audio_balance || 0, -1, 1).toFixed(2)}</span></div></div>
      <div class="set-row"><div><label>响度均衡</label><div class="note">原生引擎下生效；把峰值软归一到约 −0.4 dBFS，忽大忽小的音量更稳</div></div>
        <button class="switch ${S.settings.audio_loudness ? 'on' : ''}" data-set="audio_loudness"></button></div>
      <div class="set-row"><div><label>起播淡入</label><div class="note">原生引擎下生效；每次起播 / 续播的头 N 秒线性爬升，消除首帧爆音</div></div>
        <div style="display:flex;align-items:center;gap:10px"><input type="range" id="setFade" min="0" max="3" step="0.1" value="${clamp(+S.settings.audio_fade_in || 0, 0, 3)}" style="width:120px"><span class="time" id="setFadeV">${clamp(+S.settings.audio_fade_in || 0, 0, 3).toFixed(1)}s</span></div></div>
      <div class="set-row"><div><label>独占输出（WASAPI Exclusive）</label><div class="note">原生引擎下生效。绕过系统混音器，把解码后的样本直接交给声卡：系统其它声音在此期间不出声，音量键也不起作用。是否真的 bit-perfect 由服务端逐条算，见下面那行读数 —— 勾上不等于就是。</div></div>
        <button class="switch ${S.settings.audio_exclusive ? 'on' : ''}" data-set="audio_exclusive"></button></div>
      <div class="set-row"><div><label>输出抖动（TPDF）</label><div class="note">只在「源比设备容器深」那一档起作用（比如 24 位的源落进 16 位独占）：量化必然发生，加三角抖动把量化误差从与信号相关的失真变成一条恒定低电平噪声。设备给 float32 或位深本来就放得下时，服务端算出「不量化」，这个开关不产生任何作用。改它不断流，下一首生效。</div></div>
        <button class="switch ${S.settings.audio_dither !== false ? 'on' : ''}" data-set="audio_dither"></button></div>
      <div class="set-row"><div><label>虚拟扬声器（耳机交叉馈电）</label><div class="note">两种引擎都生效。把对侧声道延迟 0.3 ms、700 Hz 低通后少量混入，模拟前方一对音箱，减轻耳机的头中效应；建议戴耳机，外放时提升有限。浏览器内核走 WebAudio 同规格图，原生引擎走服务端；这是交叉馈电，不是多声道 HRTF 影院环绕。</div></div>
        <button class="switch ${S.settings.audio_speakers ? 'on' : ''}" data-set="audio_speakers"></button></div>
      <div class="set-row"><label>虚拟扬声器强度</label>
        <div style="display:flex;align-items:center;gap:10px"><input type="range" id="setSpk" min="0" max="1" step="0.05" value="${clamp(S.settings.audio_speakers_amount == null ? 0.35 : +S.settings.audio_speakers_amount, 0, 1)}" style="width:150px"><span class="time" id="setSpkV">${clamp(S.settings.audio_speakers_amount == null ? 0.35 : +S.settings.audio_speakers_amount, 0, 1).toFixed(2)}</span></div></div>
      <div id="setExCaps" class="note" style="margin-top:6px"></div>
      <div id="setExclusive" class="note" style="margin-top:6px"></div>
      <div id="setSignalPath" class="note" style="margin-top:6px"></div>
      <div id="setAtmosNote" class="note" style="margin-top:6px;display:none"></div>
      <div id="setNativeStatus" class="note" style="margin-top:6px"></div>
    </div>

    <div class="set-block">
      <h3>均衡器</h3>
      <div class="desc">十段双二阶（BiquadFilter）。浏览器内核走前端 WebAudio，原生引擎走服务端 numpy —— 同一条曲线、同一组中心频率，两种引擎都生效。开关和曲线都存在设置里，重启还在。</div>
      <div class="set-row"><div><label>启用均衡器</label><div class="note">关掉时链路是直通，不经过任何滤波器</div></div>
        <button class="switch ${S.settings.eq_enabled ? 'on' : ''}" data-set="eq_enabled"></button></div>
      <div class="set-row"><label>预设</label><select id="setEqPreset">${
          Object.entries(EQ_PRESETS).map(([k, g]) => `<option value="${k}" ${(S.settings.eq_preset || 'flat') === k ? 'selected' : ''}>${EQ_PRESET_NAMES[k] || k}</option>`).join('')
        }<option value="custom" ${S.settings.eq_preset === 'custom' ? 'selected' : ''}>自定义</option></select></div>
      <div id="setEqBands" class="eq-bands"></div>
      <div class="set-row"><label></label><div style="display:flex;gap:8px">
        <button class="ghost" id="setEqReset">全部归零</button><span class="note" id="setEqV"></span></div></div>
    </div>

    <div class="set-block">
      <h3>曲库分析</h3>
      <div class="desc">按格式、码率档、采样率给整个曲库分一次层；数字来自本地标签读取，不是猜的。</div>
      <div class="set-row"><label></label><button class="ghost" id="setQuality">分析曲库</button></div>
      <div id="setQualityBox"></div>
    </div>

    <div class="set-block">
      <h3>快捷键</h3>
      <div class="keys" style="margin-top:10px">
        ${[['空格', '播放 / 暂停'], ['← →', '快退 / 快进 5 秒'], ['↑ ↓', '音量'], ['N / P', '下一曲 / 上一曲'],
          ['↑ ↓ · Enter', '列表内换行 / 播放'], ['Shift+F10', '当前行菜单'],
          ['/', '搜索'], ['?', '快捷键总览'], ['L', '歌词'], ['Q', '队列'], ['I', '详情'], ['F', '全屏播放页'], ['M', '播放模式'],
          ['Z', '专注模式'], ['R', '随意电台'], ['Del', '清空播放队列（保留当前曲目）'], ['Esc', '关闭浮层 / 退出全屏']]
            .map(([k, d]) => `<div><span>${esc(d)}</span><kbd>${esc(k)}</kbd></div>`).join('')}
      </div>
    </div>

    <div class="set-block">
      <h3>关于</h3>
      <div class="desc">LuHui音乐播放器 1.0 — 本地文件播放，不联网、不上传。<br>
      数据目录（沿用旧版，不迁移）：<code>%LOCALAPPDATA%\\SaltPlayerPro</code>　曲目：${S.stats.tracks} 首 / ${fmtLong(S.stats.duration)}<br>
      本工程为独立实现，功能对标椒盐音乐（本地播放器）：曲库、歌单、LRC 歌词、封面取色。</div>
      <div class="set-row"><label>重新扫描全部</label><div style="display:flex;gap:8px">
        <button class="ghost" id="setScan">开始扫描</button><button class="ghost" id="setAbout">系统信息框</button></div></div>
    </div>
  </div>`;

  const libBox = $('#setLibs');
  libs.forEach(l => {
    const row = el('div', 'set-row');
    row.innerHTML = `<div style="min-width:0"><label>${esc(l.name)}</label><div class="note">${esc(l.path)} · ${l.track_count} 首${l.last_scan ? ' · ' + new Date(l.last_scan * 1000).toLocaleString() : ''}</div></div>`;
    const b1 = el('button', 'ghost', '重扫'), b3 = el('button', 'ghost', '改名'), b2 = el('button', 'ghost danger', '移除');
    b1.style.cssText = b2.style.cssText = b3.style.cssText = 'padding:6px 14px;font-size:12.5px';
    b1.onclick = () => startScan({ id: l.id }, '已开始重新扫描');
    b3.onclick = async () => {
      const name = await promptDialog({ title: '重命名曲库条目', value: l.name || '', hint: '只改界面显示的名字，磁盘上的目录和文件不动。' });
      if (!name || name === l.name) return;
      try { await api('library/rename', { method: 'POST', body: { id: l.id, name } }); }
      catch (e) { return toast('改名失败：' + e.message, 'err'); }
      await window.SaltShell.refreshAll(true);
      toast('名字已更新', 'ok', 1500);
    };
    b2.onclick = () => removeLibrary(l);
    const acts = el('div', 'row-actions');
    acts.append(b1, b3, b2);
    row.appendChild(acts); libBox.appendChild(row);
  });
  if (!libs.length) libBox.appendChild(el('div', 'note', '尚未添加任何文件夹。'));

  /* 图形状态只读回显：GPU 开关在进程启动时定死，这里只能解释「现在为什么这样」，
     写入 RTSS 排除项需要管理员权限，只能由用户双击程序目录里的 .cmd 触发。 */
  api('rtss').then(r => {
    const n = $('#setGpuNote');
    if (!n) return;
    if (!r || !r.ok) { n.textContent = '读不到图形状态。'; return; }
    if (!r.running) {
      n.textContent = r.gpu
        ? '硬件栅格化已开启（ANGLE D3D11）；界面模糊、圆角和动画由显卡合成。'
        : '当前为软件渲染。若驱动不稳定，可用环境变量 SALT_NO_GPU=1 保持软件渲染。';
    } else if (r.excluded) {
      n.textContent = '微星小飞机 / RTSS 正在运行，但已安装按程序排除项：播放器保持硬件栅格化，OSD 不会出现。';
    } else {
      n.textContent = '检测到微星小飞机 / RTSS：为避免 OSD，本次自动使用软件渲染。'
        + '要同时保留硬件加速，双击程序目录里的“启用显卡加速.cmd”，允许一次管理员权限后重启播放器。';
    }
  }).catch(() => {});

  /* ------------------------------------------------ 排除的文件夹
     列表来自服务端（不是前端记的一份）：扫描生效的是服务端那一份，界面只是它的镜子。 */
  const exBox = $('#setExcludes');
  const drawExcludes = (items) => {
    exBox.textContent = '';
    if (!items.length) {
      exBox.appendChild(el('div', 'note', '目前没有排除任何目录。'));
      return;
    }
    items.forEach(p => {
      const row = el('div', 'set-row');
      row.innerHTML = `<div style="min-width:0"><label>${esc(p.split(/[\\/]/).filter(Boolean).pop() || p)}</label>
        <div class="note">${esc(p)}</div></div>`;
      const b = el('button', 'ghost', '取消排除');
      b.style.cssText = 'padding:6px 14px;font-size:12.5px';
      b.onclick = async () => {
        try {
          const r = await api('excludes/remove', { method: 'POST', body: { path: p } });
          drawExcludes(r.items || []);
          startScan({}, '已取消排除，正在重新扫描找回曲目');
        } catch (e) { toast('取消排除失败：' + e.message, 'err'); }
      };
      row.appendChild(b); exBox.appendChild(row);
    });
  };
  api('excludes').then(r => drawExcludes((r && r.items) || [])).catch(() => drawExcludes([]));
  $('#setExPick').onclick = async () => {
    const r = await api('library/pick', { method: 'POST', body: {} }).catch(() => null);
    let path = r && r.ok ? r.path : '';
    if (!path) {
      path = await promptDialog({ title: '要排除的文件夹', placeholder: '例如 D:\\Music\\_demo', hint: '该目录连同它的子目录都不会再被扫描。' });
      if (!path) return;
    }
    try {
      const out = await api('excludes/add', { method: 'POST', body: { path } });
      drawExcludes(out.items || []);
      startScan({}, '已排除，正在重新扫描');
    } catch (e) { toast('排除失败：' + (e.message || e), 'err'); }
  };

  /* ------------------------------------------------ 曲库分析 */
  $('#setQuality').onclick = async () => {
    const btn = $('#setQuality');
    btn.disabled = true; btn.textContent = '分析中…';
    let q = null;
    try { q = await api('quality'); }
    catch (e) { btn.disabled = false; btn.textContent = '分析曲库'; return toast('分析失败：' + e.message, 'err'); }
    btn.disabled = false; btn.textContent = '重新分析';
    renderQuality($('#setQualityBox'), q);
  };

  /* ------------------------------------------------ 均衡器 */
  const eqBox = $('#setEqBands');
  const gains = () => {
    const g = Array.isArray(S.settings.eq_gains) ? S.settings.eq_gains.slice(0, 10) : [];
    while (g.length < 10) g.push(0);
    return g.map(x => clamp(+x || 0, -EQ_RANGE, EQ_RANGE));
  };
  const drawEq = () => {
    const g = gains();
    eqBox.textContent = '';
    EQ_HZ.forEach((hz, i) => {
      const one = el('div', 'eq-band');
      // 竖向是「转出来的」，不是 orient="vertical"：那是 Firefox 专有属性，Chromium 87 不认，
      // 而 -webkit-appearance: slider-vertical 已经废弃。绝对定位 + rotate(-90deg) 让原生
      // 键盘支持（←/→ 调增益）和自带的轨道样式都留下来，版面只算 .eq-slot 那一格。
      one.innerHTML = `<span class="eq-slot"><input type="range" class="eq-slider" min="${-EQ_RANGE}" max="${EQ_RANGE}" step="1" value="${g[i]}" data-i="${i}" aria-label="${hz}Hz 增益"></span>
        <span class="eqv">${g[i] > 0 ? '+' + g[i] : g[i]}</span><span class="eqh">${hz >= 1000 ? (hz / 1000) + 'k' : hz}</span>`;
      eqBox.appendChild(one);
    });
    $$('.eq-slider', eqBox).forEach(sl => sl.oninput = (e) => {
      const i = +e.target.dataset.i, val = clamp(+e.target.value, -EQ_RANGE, EQ_RANGE);
      const g2 = gains(); g2[i] = val;
      S.settings.eq_gains = g2;
      const band = e.target.closest('.eq-band');
      if (band) band.querySelector('.eqv').textContent = val > 0 ? '+' + val : val;
      // 拖完之后曲线就不再等于任何预设；把下拉框拨回「自定义」，别让界面显示一条它其实没有的预设名。
      if (S.settings.eq_preset !== 'custom') { S.settings.eq_preset = 'custom'; $('#setEqPreset').value = 'custom'; }
      applyEqGains();
      saveSettings({ eq_gains: g2, eq_preset: 'custom' });
    });
    syncEqLabel();
  };
  drawEq();
  $('#setEqPreset').onchange = (e) => {
    const key = e.target.value;
    if (key === 'custom') { S.settings.eq_preset = 'custom'; return saveSettings({ eq_preset: 'custom' }); }
    const g = (EQ_PRESETS[key] || EQ_PRESETS.flat).slice(0, 10);
    S.settings.eq_gains = g; S.settings.eq_preset = key;
    applyEqGains(); drawEq();
    saveSettings({ eq_gains: g, eq_preset: key });
  };
  $('#setEqReset').onclick = () => {
    S.settings.eq_gains = EQ_PRESETS.flat.slice(); S.settings.eq_preset = 'flat';
    $('#setEqPreset').value = 'flat';
    applyEqGains(); drawEq();
    saveSettings({ eq_gains: S.settings.eq_gains, eq_preset: 'flat' });
    toast('均衡器已归零', 'ok', 1400);
  };

  $$('.switch', v).forEach(btn => btn.onclick = () => {
    const key = btn.dataset.set;
    if (btn.dataset.on) {
      const on = S.settings[key] === btn.dataset.on;
      S.settings[key] = on ? btn.dataset.off : btn.dataset.on;
      btn.classList.toggle('on', !on);
      saveSettings({ [key]: S.settings[key] });
    } else {
      btn.classList.toggle('on');
      S.settings[key] = btn.classList.contains('on');
      saveSettings({ [key]: S.settings[key] });
      // 开关刚按下去时 saveSettings 还在防抖里（320ms），服务端此刻读到的可能是旧设置 ——
      // 所以这一趟把标志显式带上，不让服务端再去读一遍。
      if (key === 'include_unsupported') {
        const on = !!S.settings.include_unsupported;
        startScan({ include_unsupported: on }, on ? '正在重新扫描（含不可播格式）' : '正在重新扫描（只收可播格式）');
      }
      if (key === 'accent_mode') {
        const row = $('#setSwatch');
        if (row) row.classList.toggle('dim', S.settings.accent_mode === 'cover');
        applySettings();
      }
      // 独占是「开流参数」：已经开着的那条流要么共享要么独占，中途不会自己变，所以正在播时
      // 必须按新值重开一次（同 nativeRestartIfPlaying，位置接得上，听着只是顿一下）。
      // 抖动不用重开——它进的是**下一次协商**（configure 只更新引擎里那个开关位），
      // 这里就不为了「看起来有反应」而把用户的歌打断。
      if (key === 'audio_exclusive') nativeRestartIfPlaying();
      // 无缝衔接是「预载与交接的总开关」：关掉时 refreshPreload 会立刻把待机那只卸干净，
      // 不留一条没人会用的后台流；重新打开则当场按当前队列备好下一首。
      if (key === 'gapless') refreshPreload();
      // 这两个键的「真值」有一部分在原生窗口那边（✕ 关掉小窗时网页是事后才知道的），
      // 所以这里除了存设置还要把窗口本身叫起来/关掉；服务端会把它收到的值再落一次盘。
      if (key === 'desktop_lyric' || key === 'desktop_lyric_on_top') syncMiniWindow();
    }
    applySettings();
  });
  $('#setPick').onclick = pickFolder;
  $('#setManual').onclick = manualFolder;
  $('#setPurge').onclick = async () => {
    // purge_missing 只删「最近一次扫描标了 missing=1」的行（sp/db.py:376）。文件刚被移走
    // 就点清理，得到的是 removed=0 + 「已清理 0 条」——那不是「没有失效条目」，是还没去看。
    // 实测踩过：删掉探针文件后直接 purge 返回 0，先扫一遍再 purge 才返回 1。
    // 所以这里先扫描、等它跑完，再清理，报出来的数才是核对过的。
    const btn = $('#setPurge');
    if (btn.disabled) return;
    btn.disabled = true;
    startScan({}, '先核对文件是否还在，再清理…');
    await waitScanIdle();
    let r;
    try { r = await api('purge', { method: 'POST', body: {} }); }
    catch (e) { btn.disabled = false; return toast('清理失败：' + e.message, 'err'); }
    btn.disabled = false;
    const n = (r && r.removed) || 0;
    toast(n ? `已清理 ${n} 条失效记录` : '核对完毕，没有失效记录', '', 2600, {
      // 清掉的是扫描时判定「文件已经不在了」的记录，找回办法 = 重扫一遍那个文件夹。
      action: n ? { label: '重扫找回来', fn: () => startScan({}, '已在后台重新扫描') } : null,
    });
    SaltShell.refreshAll(true);
  };
  $('#setThemeDark').onclick = () => { S.settings.theme = 'dark'; applySettings(); saveSettings({ theme: 'dark' }); renderView(); };
  $('#setThemeLight').onclick = () => { S.settings.theme = 'light'; applySettings(); saveSettings({ theme: 'light' }); renderView(); };
  $('#setLyCenter').onclick = () => { S.settings.lyric_mode = 'center'; applySettings(); saveSettings({ lyric_mode: 'center' }); };
  $('#setLyTop').onclick = () => { S.settings.lyric_mode = 'top'; applySettings(); saveSettings({ lyric_mode: 'top' }); };
  /* 歌词时间偏移：±0.5 一格，范围 ±10 秒（再大就不叫调歌词了）。 */
  const setLyOffLabel = () => { $('#setLyOffV').textContent = `${(+S.settings.lyric_offset || 0).toFixed(1)}s`; };
  const nudgeLyOff = (d) => {
    const cur = +S.settings.lyric_offset || 0;
    const next = Math.round(clamp(cur + d, -10, 10) * 10) / 10;
    if (next === cur) return toast(d > 0 ? '已经到 +10.0s 了' : '已经到 −10.0s 了', '', 1300);
    S.settings.lyric_offset = next;
    setLyOffLabel();
    saveSettings({ lyric_offset: next });
    // 暂停时没有 rAF tick，高亮会停在旧行上 —— 立刻按新偏移重算一次，调完就能看到效果。
    syncLyricNow();
  };
  $('#setLyOffDown').onclick = () => nudgeLyOff(-0.5);
  $('#setLyOffUp').onclick = () => nudgeLyOff(0.5);
  $('#setLyOffReset').onclick = () => nudgeLyOff(-(+S.settings.lyric_offset || 0));
  $('#setMode').onchange = e => {
    S.settings.play_mode = e.target.value;
    if (e.target.value === 'shuffle' && S.shuffleOrder.length !== S.queue.length)
      S.shuffleOrder = shuffled(S.queue.map((_, k) => k));
    updateModeButton(); saveSettings({ play_mode: e.target.value });
    refreshPreload();       // 下一首是谁刚换了规则：已经预载的那只要重认（或收掉）
  };
  $('#setCrossfade').onchange = e => {
    const sec = clamp(+e.target.value || 0, 0, 20);
    S.settings.crossfade_sec = sec;
    saveSettings({ crossfade_sec: sec });
    refreshPreload();
    toast(sec > 0 ? `交叉淡化 ${sec} 秒` : '交叉淡化已关闭（保留 80 ms 防爆音衔接）', '', 1500);
  };
  $('#setLySpeed').onchange = e => {
    const ms = clamp(+e.target.value || 0, 0, 1500);
    S.settings.lyric_scroll_ms = ms;
    saveSettings({ lyric_scroll_ms: ms });
    // 改完立刻能看到：把当前行按新速度重新滚一次。不重滚的话要等下一句词，
    // 用户会以为这个下拉框没起作用。
    if (S.lyric.i >= 0 && S.lyric.lines[S.lyric.i]) syncLyricNow();
  };
  /* 滑块这条也要同时写 defaultPlaybackRate，理由和 applySettings 里那条一样：
     只写 playbackRate 的话，用户拖到 4× 再切一首歌，速率会被 src 赋值抹回 1×。
     原生档没有 audio 元素可写（那条赋值落在一个从没赋过 src 的元素上，无害也无用），
     倍速是 play() 的参数（ffmpeg atempo），所以只能重开流。 */
  $('#setRate').oninput = e => { const r = clamp(+e.target.value || 1, RATE_MIN, RATE_MAX);
    S.settings.rate = r;
    // 两只元素都要写：待机那只若留着旧倍速，交接过去的新歌会按旧速度唱。
    [audio, audio2].forEach(el => { el.defaultPlaybackRate = r; el.playbackRate = r; });
    $('#setRateV').textContent = r.toFixed(2) + '×'; saveSettings({ rate: r });
    nativeRestartIfPlaying(); };

  /* ------------------------------------------------ 音质 / 播放引擎
     两类参数的生效方式不一样，混着用会出现「改一次没反应、再改一次才生效」这种解释不清的延迟：
       · DSP 参数（音量 / 均衡 / 声像 / 响度 / 淡入）—— 流不用断，POST configure 当场换 → syncNativeConfig()
       · 开流参数（输出设备 / 目标采样率 / 倍速）—— 定在流格式里，只能重开流 → nativeRestartIfPlaying()
     浏览器内核下这两个函数都直接 return，WebAudio 那条路由 applySettings() 负责。 */
  $('#setEngine').onchange = e => switchEngine(e.target.value);
  const deviceSel = $('#setDevice');
  /* 那行状态由模块级 renderNativeStatus() 渲染（见文件上方 nativePollTick 附近）：
     这里曾经定义过一个同名 const 闭包，它把全局那份遮蔽掉，于是这一行只在「打开面板 /
     换设备 / 换目标采样率」三个瞬间渲染一次，面板开着时服务端早已改状态，它还挂着旧快照。 */
  (async () => {
    let r = null;
    try { r = await api('native/devices'); } catch (err) { r = null; }
    const items = (r && r.items) || [];
    deviceSel.textContent = '';
    const mk = (val, txt, selected) => {
      const o = el('option', '', txt); o.value = val; if (selected) o.selected = true;
      deviceSel.appendChild(o); return o;
    };
    const cur = S.settings.audio_device;
    mk('', '系统默认（跟随默认 WASAPI 设备）', cur == null);
    items.forEach(d => mk(String(d.index),
      `${d.name} · ${Math.round((d.rate || 0) / 100) / 10}k / ${d.channels || 0}ch${d.is_default ? '（默认）' : ''}`,
      cur != null && +cur === +d.index));
    if (!items.length) {
      mk('', r && r.available === false ? '依赖未安装（ffmpeg / pyaudiowpatch）' : '没枚举到 WASAPI 输出设备', true);
      deviceSel.disabled = true;
    }
    renderNativeStatus();
    loadExclusiveCaps(S.settings.audio_device == null ? '' : String(S.settings.audio_device));
  })();
  deviceSel.onchange = () => {
    const v = deviceSel.value;
    S.settings.audio_device = v === '' ? null : +v;
    saveSettings({ audio_device: S.settings.audio_device });
    nativeRestartIfPlaying();
    renderNativeStatus();
    // 能力表是按设备的：换一台就重新拉一次（缓存键里带设备值，见 loadExclusiveCaps）
    S._exCaps = null;
    loadExclusiveCaps(v);
  };
  $('#setTargetRate').onchange = e => {
    const hz = +e.target.value || 0;
    S.settings.audio_target_rate = hz;
    saveSettings({ audio_target_rate: hz });
    nativeRestartIfPlaying();
    renderNativeStatus();
  };
  $('#setBalance').oninput = e => {
    const x = clamp(+e.target.value || 0, -1, 1);
    $('#setBalanceV').textContent = x.toFixed(2);
    S.settings.audio_balance = x;
    saveSettings({ audio_balance: x });
    syncNativeConfig();
  };
  $('#setFade').oninput = e => {
    const sec = clamp(+e.target.value || 0, 0, 3);
    $('#setFadeV').textContent = sec.toFixed(1) + 's';
    S.settings.audio_fade_in = sec;
    saveSettings({ audio_fade_in: sec });
    syncNativeConfig();
  };
  $('#setSpk').oninput = e => {
    const x = clamp(+e.target.value || 0, 0, 1);
    const wasOn = crossfeedOn();
    $('#setSpkV').textContent = x.toFixed(2);
    S.settings.audio_speakers_amount = x;
    saveSettings({ audio_speakers_amount: x });
    applyCrossfeed();
    // 强度拖到 0 或从 0 拖起来会跨过「旁路/接入」那条线：只有这一下需要重接图，
    // 中间的每一像素都只改增益。
    if (wasOn !== crossfeedOn()) wireAudioGraph();
    syncNativeConfig();
  };
  $('#setScan').onclick = () => startScan({}, '全库扫描已在后台开始');
  $('#setAbout').onclick = () => api('window/about', { method: 'POST', body: {} }).then(r => { if (!r.native) toast(`${S.app.name} · ${S.app.native ? 'App' : '浏览器'}模式`); });
  const sw = $('#setSwatch');
  // 「来自封面」开着时这一排是失效状态：把已存的固定色画成选中会谎报当前强调色的来源。
  const coverMode = S.settings.accent_mode === 'cover';
  sw.classList.toggle('dim', coverMode);
  ACCENTS.forEach(c => {
    const b = el('button', 'swatch'); b.style.background = c;
    const sel = !coverMode && (S.settings.preset_accent || '') === c;
    b.title = '强调色 ' + c;
    b.setAttribute('aria-label', '强调色 ' + c);
    b.setAttribute('aria-pressed', String(sel));
    b.classList.toggle('on', sel);
    b.onclick = () => {
      S.settings.preset_accent = c; S.settings.accent_mode = 'preset'; sw.classList.remove('dim');
      setAccent(c, false); saveSettings({ preset_accent: c, accent_mode: 'preset' });
      $$('.swatch', sw).forEach(x => { x.classList.remove('on'); x.setAttribute('aria-pressed', 'false'); });
      b.classList.add('on'); b.setAttribute('aria-pressed', 'true');
    };
    sw.appendChild(b);
  });
  /* 只在设置面板里画轨道：EQ 那排滑块有自己的一套视觉（.eq-slider），不要一起改。
     监听挂在这里而不是每个控件各写一遍，以后再加滑块不会漏掉 --fill。 */
  $$('.set-block input[type="range"]:not(.eq-slider)', v).forEach(r => {
    paintRange(r);
    r.addEventListener('input', () => paintRange(r));
  });
}

/* ==========================================================================
   菜单 / 对话框 / 提示
   ========================================================================== */
let _menuOpener = null;
let _menuShownAt = 0;
function showMenu(x, y, items) {
  const m = $('#ctxMenu');
  // 记住「从哪儿打开的」：Esc / 关闭后焦点要回到原处（某一行、某个按钮），不是掉到 body。
  // 菜单项点开的第二层是整层替换，这时焦点还在旧菜单里，不能把旧菜单按钮当恢复目标。
  if (m.hidden && !m.contains(document.activeElement)) _menuOpener = document.activeElement;
  m.textContent = '';
  items.forEach(it => {
    if (it.sep) { const s = el('div', 'sep'); s.setAttribute('role', 'separator'); return m.appendChild(s); }
    if (it.label && !it.fn) { const l = el('div', 'label', it.label); l.setAttribute('role', 'presentation'); return m.appendChild(l); }
    const b = el('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    if (it.title) b.title = it.title;
    // sub:true 的条目（「添加到歌单」）点开的是第二层菜单，光看文字猜不到后面还有东西，
    // 所以给它一个箭头 + aria-haspopup。.sub/.submenu 那两条 CSS 是给真浮动层用的，
    // 现在是「点击后整层替换」，不占用 .sub 类名以免 hover 规则误伤。
    const arrow = it.sub ? `<span class="sub-arrow">${icon('right')}</span>` : '';
    b.innerHTML = `<i>${glyphIcon(it.icon)}</i><span>${esc(it.label)}</span>${arrow}${it.k ? `<span class="k">${esc(it.k)}</span>` : ''}`;
    if (it.sub) b.setAttribute('aria-haspopup', 'menu');
    if (it.danger) b.classList.add('danger');
    // disabled 用于「这件事已经处于该状态」的条目：保留在菜单里（否则用户会以为功能缺失），
    // 但灰掉、吃掉点击，并让键盘 Tab 跳过它。
    if (it.disabled) { b.disabled = true; b.setAttribute('aria-disabled', 'true'); }
    // 先藏起菜单再执行动作：动作可能当场打开第二层，也可能只是播一首歌。
    // 不走 hideMenu() 是为了把 _menuOpener 留给下一层 / 下一次 Esc。
    else b.onclick = (e) => { e.stopPropagation(); m.hidden = true; it.fn(e); };
    m.appendChild(b);
  });
  m.hidden = false;
  _menuShownAt = performance.now();
  m.scrollTop = 0;
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(6, Math.min(x, innerWidth - r.width - 10)) + 'px';
  m.style.top = Math.max(6, Math.min(y, innerHeight - r.height - 10)) + 'px';
  const fb = m.querySelector('button:not(:disabled)');   // 全是禁用/分隔条的菜单没有可聚焦项，别在这儿抛异常
  if (fb) fb.focus({ preventScroll: true });
}
/** 关闭菜单。restore=true 时把焦点还给打开它的那个控件（Esc / 键盘关闭走这条），
    鼠标点空白处关闭则不抢焦点 —— 用户已经点到了新的落点。 */
function hideMenu(restore) {
  const m = $('#ctxMenu');
  if (m.hidden) return;
  m.hidden = true;
  const o = _menuOpener;
  _menuOpener = null;
  if (restore && o && document.contains(o) && typeof o.focus === 'function') {
    try { o.focus({ preventScroll: true }); } catch (e) { /* 元素已经不可聚焦 */ }
  }
}
/* 菜单也是键盘能走完的浮层：↑↓/Home/End 换条目，Tab 在菜单内循环，Esc 关闭并回到原处。
   以前这里只有「打开时聚焦第一条」，剩下的方向键全被全局快捷键接走（↑↓ 改音量），
   键盘用户打开菜单后反而在调音量。 */
$('#ctxMenu').addEventListener('keydown', (e) => {
  const m = e.currentTarget;
  const btns = $$('#ctxMenu button:not(:disabled)');
  const i = btns.indexOf(document.activeElement);
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); hideMenu(true); return; }
  if (i < 0) return;
  let j = null;
  if (e.key === 'ArrowDown') j = (i + 1) % btns.length;
  else if (e.key === 'ArrowUp') j = (i - 1 + btns.length) % btns.length;
  else if (e.key === 'Home') j = 0;
  else if (e.key === 'End') j = btns.length - 1;
  else if (e.key === 'Tab') j = e.shiftKey ? (i - 1 + btns.length) % btns.length : (i + 1) % btns.length;
  else return;
  e.preventDefault(); e.stopPropagation();
  btns[j].focus({ preventScroll: true });
  if (m.getBoundingClientRect().height > 0) btns[j].scrollIntoView({ block: 'nearest' });
});

/* 右键纪律：App 里**任何地方都不许冒出浏览器默认菜单** —— 那正是「看着像浏览器」的来源。
   Qt 侧还压着一道 setContextMenuPolicy(NoContextMenu)（sp/app.py），这里管内容：
   ① 行/歌单/文件夹的专用菜单已经 preventDefault（事件先到它们），不重复弹；
   ② 输入框给一条原生编辑菜单（撤销/剪切/复制/粘贴/全选）；
   ③ 其余地方：有选中的文字就给「复制」，没有就什么都不弹（原生音乐 App 的空地行为）。
   编辑菜单要先把焦点和选区抢回来再执行：菜单按钮一聚焦，输入框的选区就没了，
   不还原的话「复制」永远复制到空串。 */
function _editSel(editable) {
  if (editable.isContentEditable) {
    const sel = window.getSelection();
    return { ce: sel && sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null };
  }
  return { ss: editable.selectionStart || 0, se: editable.selectionEnd || 0 };
}
function _restoreSel(editable, s) {
  editable.focus();
  if (editable.isContentEditable) {
    if (s.ce) { const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(s.ce); }
  } else {
    try { editable.setSelectionRange(s.ss, s.se); } catch (e) { /* 某些 input 类型不支持选区 */ }
  }
}
function _selText(editable, s) {
  if (editable.isContentEditable) return s.ce ? String(s.ce) : '';
  return String(editable.value || '').slice(s.ss, s.se);
}
async function pasteInto(editable, s) {
  let text = '';
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error('no clipboard api');
    text = await navigator.clipboard.readText();
  } catch (e) {
    return toast('读不到剪贴板：请按 Ctrl+V 粘贴', 'err', 2200);
  }
  _restoreSel(editable, s);
  let ok = false;
  try { ok = document.execCommand('insertText', false, text); } catch (e) { ok = false; }
  if (!ok && !editable.isContentEditable) {
    // execCommand 被拒时的兜底：手写进 value，再补一次 input 事件让页面自己的状态跟上。
    const v = String(editable.value || ''), a = s.ss, b = s.se;
    editable.value = v.slice(0, a) + text + v.slice(b);
    try { editable.setSelectionRange(a + text.length, a + text.length); } catch (e) { /* ignore */ }
    editable.dispatchEvent(new Event('input', { bubbles: true }));
  }
}
async function copyPlain(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return toast('已复制', '', 1200);
    }
  } catch (e) { /* 退到 execCommand */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    toast(ok ? '已复制' : '复制失败', ok ? '' : 'err', 1400);
  } catch (e) { toast('复制失败', 'err'); }
}
function onDocContextMenu(e) {
  if (e.defaultPrevented) return;      // 专用菜单已经处理过（trackMenu/playlistMenu/folderMenu）
  e.preventDefault();                  // 其余情况一律压掉浏览器菜单
  const t = e.target;
  const editable = t && t.closest
    ? t.closest('input:not([type="range"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"], [contenteditable=""]')
    : null;
  if (editable) {
    const s = _editSel(editable);
    const has = _selText(editable, s).length > 0;
    showMenu(e.clientX, e.clientY, [
      { label: '撤销', icon: '↶', fn: () => { _restoreSel(editable, s); document.execCommand('undo'); } },
      { sep: 1 },
      { label: '剪切', icon: '✂', disabled: !has, fn: () => { _restoreSel(editable, s); document.execCommand('cut'); } },
      { label: '复制', icon: '⧉', disabled: !has, fn: () => { _restoreSel(editable, s); document.execCommand('copy'); } },
      { label: '粘贴', icon: '⌥', fn: () => pasteInto(editable, s) },
      { sep: 1 },
      { label: '全选', icon: '▣', fn: () => { _restoreSel(editable, s); document.execCommand('selectAll'); } },
    ]);
    return;
  }
  // 歌词：右键给「复制这一句 / 复制全部」。歌词区是 user-select:none（拖动歌词条不该
  // 变成选字），所以没有这条右键就只能靠截图抄词。
  const ly = t && t.closest ? t.closest('#lyricLines .ly, #fsLyric .ly') : null;
  if (ly) {
    const i = parseInt(ly.dataset.i, 10);
    const L = (S.lyric && S.lyric.lines) || [];
    const one = (isFinite(i) && L[i] && String(L[i].text || '')) || '';
    const all = L.map(x => String((x && x.text) || '')).filter(Boolean).join('\n')
      || String((S.lyric && S.lyric.text) || '');
    showMenu(e.clientX, e.clientY, [
      { label: '复制这一句', icon: '⧉', disabled: !one, fn: () => copyPlain(one) },
      { label: '复制全部歌词', icon: '☰', disabled: !all, fn: () => copyPlain(all) },
    ]);
    return;
  }
  const sel = window.getSelection();
  const text = sel ? String(sel) : '';
  if (text.trim()) {
    showMenu(e.clientX, e.clientY, [{ label: '复制', icon: '⧉', fn: () => copyPlain(text) }]);
    return;
  }
  hideMenu();
}
document.addEventListener('contextmenu', onDocContextMenu);

/* 原生壳里不该出现浏览器快捷键：F5/Ctrl+R 重载会把界面回炉；Ctrl+P 打印、Ctrl+U 看源码、
   Ctrl+S 另存、Ctrl+O 打开文件、F7 光标浏览、F12/Ctrl+Shift+I·J·C 开发者工具，都是
   「这是浏览器」的破绽。只压这些组合，不碰输入框的撤销/复制/粘贴，也不拦应用的单击键。
   preventDefault 不 stopPropagation：页面自己的 onKey 仍然照常收到事件。 */
function onDocBrowserKey(e) {
  const k = String(e.key || '').toLowerCase();
  const ctrl = e.ctrlKey || e.metaKey;
  const plainCtrl = ctrl && !e.altKey && !e.shiftKey;
  const shiftCtrl = ctrl && e.shiftKey && !e.altKey;
  const block = e.key === 'F5' || e.key === 'F7' || e.key === 'F12'
    || (plainCtrl && (k === 'r' || k === 'p' || k === 'f' || k === 'u' || k === 's' || k === 'o'))
    || (shiftCtrl && (k === 'r' || k === 'i' || k === 'j' || k === 'c'));
  if (block) e.preventDefault();
}
document.addEventListener('keydown', onDocBrowserKey);

let _dialogSettle = null;   // 当前对话框的落地函数：任何关闭路径（确定/取消/Esc/遮罩）都必须走它
let _dialogOpener = null;   // 打开对话框时焦点在哪：关闭后必须还回去

function dialog({ title, body, okLabel = '确定' }) {
  return new Promise(resolve => {
    _dialogOpener = document.activeElement;
    $('#dialogTitle').textContent = title;
    const b = $('#dialogBody'); b.textContent = ''; b.appendChild(body);
    $('#dialogOk').textContent = okLabel;
    $('#dialogMask').hidden = false;
    // settle 是唯一的出口，且只认「自己还是当前那个」的情况：双击、Esc 撞上遮罩、
    // 关闭后残留的 onclick 都可能再来一次，Promise 第二次 resolve 是无声的，
    // 但 _dialogSettle 会指错地方，所以先自证身份。
    const settle = (val) => {
      if (_dialogSettle !== settle) return;
      _dialogSettle = null;
      $('#dialogMask').hidden = true;
      resolve(val);
      const op = _dialogOpener;
      _dialogOpener = null;
      if (op && op.isConnected && typeof op.focus === 'function') {
        try { op.focus({ preventScroll: true }); } catch (e) { /* 触发它的控件已经不在 */ }
      }
    };
    _dialogSettle = settle;
    // 处理器挂上就不许再置 null：以前 done() 里把 onclick 清空，第二次对话框之后的
    // 「取消」和遮罩点击就没人应答了（wireControls 里那次绑定只撑着第一个）。
    $('#dialogOk').onclick = () => settle(b.dataset.getValue ? b.dataset.getValue() : true);
    $('#dialogCancel').onclick = () => settle(null);
    const inp = b.querySelector('input');
    if (inp) {
      inp.focus(); inp.select();
      inp.onkeydown = e => {
        if (e.key === 'Enter') settle(b.dataset.getValue ? b.dataset.getValue() : true);
        else if (e.key === 'Escape') { e.stopPropagation(); settle(null); }
      };
    } else {
      // 没有输入框时把焦点给「确定」，Esc/Enter 才有的可退。
      $('#dialogOk').focus({ preventScroll: true });
    }
  });
}
/** 关闭对话框：必须让 Promise 落地，否则 await dialog() 的调用方永远悬在半路。 */
function closeDialog() { if (_dialogSettle) _dialogSettle(null); }

async function promptDialog({ title, placeholder = '', value = '', hint = '' }) {
  const box = el('div');
  const inp = el('input'); inp.type = 'text'; inp.placeholder = placeholder; inp.value = value;
  box.appendChild(inp);
  if (hint) box.appendChild(el('div', 'hint', hint));
  box.dataset.getValue = () => inp.value.trim();
  const v = await dialog({ title, body: box });
  return typeof v === 'string' ? v : null;
}
async function confirmDialog(title, hint) {
  const box = el('div', 'hint', hint || '');
  return !!(await dialog({ title, body: box, okLabel: '确定' }));
}

let toastN = 0;
/**
 * msg 文案，kind 走 CSS（'err' / 'ok'），ms 停留时间。
 * opts.action = {label, fn} 在右边加一个按钮（撤销用）。带按钮的气泡活得比默认久：
 * 2.6 秒不够把手抬起来点它，而撤销只在气泡消失前有效。
 */
function toast(msg, kind = '', ms = 2600, opts = {}) {
  if (!msg) return;
  const box = $('#toasts');
  if (box.children.length > 3) box.firstChild.remove();
  const t = el('div', 'toast ' + kind);
  t.appendChild(el('span', 'toast-msg', msg));
  const act = opts && opts.action;
  if (act && act.label && typeof act.fn === 'function') {
    const b = el('button', 'toast-act', act.label);
    b.type = 'button';
    b.onclick = () => { t.remove(); try { act.fn(); } catch (e) { toast('操作没成功：' + e.message, 'err'); } };
    t.appendChild(b);
  }
  box.appendChild(t);
  const life = act ? Math.max(ms, 6500) : ms;
  const id = ++toastN;
  setTimeout(() => { t.style.transition = 'opacity .3s, transform .3s'; t.style.opacity = '0'; t.style.transform = 'translateX(16px)'; setTimeout(() => t.remove(), 320); }, life);
  return id;
}

/* --------------------------------------------------------- 曲库空态入口 */
boot();
