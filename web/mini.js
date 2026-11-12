/* 桌面歌词小窗的页面逻辑。
   为什么不在这里读主窗口的状态：主窗口最小化或被遮挡时，Chromium 会停掉它的 rAF
   （app.js 里那条实测结论），歌词跟着一起冻住 —— 而桌面歌词恰恰是给「主窗收进托盘」
   用的。所以进度的真值走服务端：主窗口按墙钟报，服务端按 rate 外推，这一页只管显示。
   Chromium 87：本项目一律不用可选链，函数里也不裸抛，取不到就退回「什么都不显示」。 */
(function () {
  'use strict';

  var POLL_MS = 700;          // 和主窗口报进度的节奏同量级：再快也只是空转
  var STALE_MS = 6000;        // 超过这个年龄还「在播」＝主窗口没在报了（崩溃/被杀），别再外推

  var lineEl = document.getElementById('line');
  var miniEl = document.getElementById('mini');
  var bodyEl = document.body;

  var st = {
    trackId: null, playing: false, rate: 1,
    // 本地时钟锚点：用 performance.now 而不是 Date.now，避免每次轮询都跟服务端对表
    anchorPos: 0, anchorAt: 0,
    lines: [], i: -1, plain: false, text: '',
    title: '', artist: '', age: null,
    scale: 1, onTop: true, fetching: false
  };

  try {
    var saved = JSON.parse(localStorage.getItem('salt:mini') || '{}');
    if (Number.isFinite(+saved.scale) && +saved.scale >= 0.6 && +saved.scale <= 2) st.scale = +saved.scale;
  } catch (e) { /* 读不出来就用默认，不值得为这个白屏 */ }

  function persist() {
    try { localStorage.setItem('salt:mini', JSON.stringify({ scale: st.scale })); } catch (e) { }
  }
  function applyScale() {
    document.documentElement.style.setProperty('--scale', String(st.scale));
  }

  function pos() {
    if (!st.playing) return st.anchorPos;
    var dt = (performance.now() - st.anchorAt) / 1000;
    if (dt < 0) dt = 0;
    return st.anchorPos + dt * (st.rate || 1);
  }

  // 有时间轴才逐行给；纯文本歌词给整段第一行；什么都没有就报歌名。
  function pick() {
    var p = pos(), L = st.lines, i = -1;
    for (var k = 0; k < L.length; k++) { if (L[k].t <= p) i = k; else break; }
    return { i: i, text: i >= 0 ? L[i].text : '' };
  }

  var lastText = null;
  /* st.i 是「这一行是第几句」的唯一读数面，验证脚本就认它。
     所以四个分支每一个都要给它一个当下的值：只在有轴那支赋值，换到没歌词的歌时
     它会留着上一首的下标（实测：上一首停在 11，换过去后 state 里 i 还是 11，
     看着像「歌词还在但对不上」），读数面就成了会说谎的地方。 */
  function paint() {
    var show = '', isTitle = false;
    if (st.trackId == null) {
      show = '没有播放';
      isTitle = true;
      st.i = -1;
    } else if (st.lines.length) {
      var p = pick();
      show = p.i >= 0 ? p.text : (st.artist ? st.artist + ' — ' + st.title : '（前奏）');
      isTitle = p.i < 0;
      st.i = p.i;
    } else if (st.plain && st.text) {
      show = String(st.text).split('\n')[0];
      st.i = -1;
    } else {
      show = (st.artist ? st.artist + ' — ' : '') + st.title;
      isTitle = true;
      st.i = -1;            // 这一支也要给当下值：留着上一首的下标，state.i 就成会说谎的读数面
    }
    bodyEl.dataset.state = st.playing ? 'playing' : 'idle';
    lineEl.classList.toggle('is-title', isTitle);
    if (show === lastText) return;
    lastText = show;
    lineEl.classList.add('swap');
    // 换行：先把旧的淡出，再上新行。用 timer 而不是 transitionend：
    // transitionend 在窗口不可见/被打断时可能永远不来，那一行就卡在透明上。
    setTimeout(function () {
      lineEl.textContent = show;
      lineEl.classList.remove('swap');
    }, 120);
  }

  function loadLyric(tid) {
    fetch('/api/lyrics?track_id=' + tid).then(function (r) { return r.json(); }).then(function (d) {
      if (st.trackId !== tid) return;                  // 已经换歌了，这份回来得太晚
      st.lines = (d && d.lines) || [];
      st.plain = !!(d && d.plain);
      st.text = (d && d.text) || '';
      lastText = null;
      paint();
    }).catch(function () {
      if (st.trackId === tid) { st.lines = []; st.plain = false; st.text = ''; paint(); }
    });
  }

  function poll() {
    if (st.fetching) return;
    st.fetching = true;
    fetch('/api/playback').then(function (r) { return r.json(); }).then(function (d) {
      st.fetching = false;
      if (!d || !d.ok) {                       // 服务端自己都说这份数不作数了
        st.playing = false;
        bodyEl.dataset.state = 'idle';
        paint();
        return;
      }
      var age = Number.isFinite(+d.age) ? +d.age : null;
      // 年龄超限＝主窗口已经不报数了（进程没了／页面被冻死）。这一步必须排在
      // 「采纳服务端那份位置」之前：服务端拿它最后一次上报按 rate 往外推，TTL 是 60 s，
      // 而这一页的门限是 6 s —— 先采纳再判，anchorPos 每 700 ms 被那个越推越远的数字顶一次，
      // 「停止外推」就成了一行注释（实测：掐掉主窗上报后 10 s，pos 还在 22 → 24.1 地走）。
      var stale = (age != null && age * 1000 > STALE_MS);
      st.playing = !!d.playing && !stale;
      st.rate = Number.isFinite(+d.rate) ? +d.rate : 1;
      if (!stale) {                            // 只采纳「还信得住」的那份进度
        st.anchorPos = +d.position || 0;
        st.anchorAt = performance.now();
      }
      st.age = age;
      st.title = d.title || '';
      st.artist = d.artist || '';
      st.onTop = d.on_top !== false;
      document.getElementById('bTop').setAttribute('aria-pressed', st.onTop ? 'true' : 'false');
      var tid = d.track_id == null ? null : +d.track_id;
      if (tid !== st.trackId) {
        st.trackId = tid;
        st.lines = []; st.plain = false; st.text = ''; lastText = null;
        if (tid != null) loadLyric(tid);
      }
      paint();
    }).catch(function () { st.fetching = false; bodyEl.dataset.state = 'idle'; });
  }

  document.getElementById('bPlus').onclick = function () {
    st.scale = Math.min(2, +(st.scale + 0.1).toFixed(2)); applyScale(); persist();
  };
  document.getElementById('bMinus').onclick = function () {
    st.scale = Math.max(0.6, +(st.scale - 0.1).toFixed(2)); applyScale(); persist();
  };
  document.getElementById('bTop').onclick = function () {
    st.onTop = !st.onTop;
    fetch('/api/window/mini', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ show: true, on_top: st.onTop })
    }).catch(function () { });
  };
  document.getElementById('bClose').onclick = function () {
    fetch('/api/window/mini', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ show: false })
    }).then(function () { window.close(); }).catch(function () { window.close(); });
  };

  // 给验证脚本留一个读数面（和主窗口 window.SaltShell.probe 同一套规矩：
  // 顶层 var 不挂 window，要显式交出去）。断言只看这些值，不去猜 DOM 之外的事。
  window.Mini = {
    state: function () {
      return {
        track_id: st.trackId, playing: st.playing, rate: st.rate,
        pos: Math.round(pos() * 10) / 10, i: st.i, lines: st.lines.length,
        text: lineEl.textContent, isTitle: lineEl.classList.contains('is-title'),
        state_attr: bodyEl.dataset.state, scale: st.scale, on_top: st.onTop,
        age: st.age
      };
    }
  };

  applyScale();
  paint();
  setInterval(poll, POLL_MS);
  poll();
  // 补间不靠 rAF 逐帧：这条只有一个「当前句」，行变了才换。但进度要在两次轮询之间自己走，
  // 所以要有一个比 POLL_MS 更密的检查点，否则换行会晚最多 0.7s。
  setInterval(function () { if (st.lines.length) paint(); }, 120);
})();
