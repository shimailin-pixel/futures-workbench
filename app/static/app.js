/* 期货分析工作台 — 前端逻辑 */
(function () {
  "use strict";

  const state = {
    symbols: [],
    meta: null,
    interval: "1d",
    count: 300,
    sub: "macd",
    series: null,
    signals: null,
    dataAsOf: null,
    research: null,
    term: null,
    chan: null,        // 缠论标注数据（笔/线段/中枢/买卖点）
    chanOn: false,     // 用户是否开启了缠论叠加（切换周期后据此自动重算）
    chanReq: 0,        // 缠论请求序号，用于丢弃过期响应
    loadReq: 0,        // 行情请求序号，用于丢弃过期响应
    fundamentals: null,
    fundLoaded: false,
    fundReq: 0,        // 基本面请求序号，防止切换品种时旧响应覆盖新面板
    charts: {},
    es: null,          // SSE 连接
    liveOn: true,      // 实时推送开关
    liveSpot: null,    // 最近一次实时快照
    liveTimer: null,
    lastQuoteAt: 0,
    aiResult: null,   // 当前 AI 研判结果（品种/数据变化时作废）
    aiMeta: {},       // 每份 AI 研判绑定的数据指纹/关键读数，用于判定是否过期
  };

  const $ = (id) => document.getElementById(id);
  const UP = "#d94a3d", DOWN = "#1e9e6a", ACCENT = "#2c5f8d", WARM = "#e08a3c";

  // ---------------- 工具 ----------------
  function toast(msg, ms) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), ms || 2400);
  }

  function setStatus(text, kind) {
    const bar = $("statusBar");
    bar.className = "status" + (kind ? " " + kind : "");
    $("statusText").textContent = text;
  }

  function fmt(n, digits) {
    if (n === null || n === undefined || n === "") return "--";
    const d = digits === undefined ? 2 : digits;
    return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtInt(n) {
    if (n === null || n === undefined) return "--";
    return Math.round(n).toLocaleString("zh-CN");
  }

  function cls(v) { return v > 0 ? "up" : (v < 0 ? "down" : ""); }

  async function api(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // ---------------- 初始化 ----------------
  async function init() {
    buildCharts();
    bindUI();
    try {
      const data = await api("/api/symbols");
      state.symbols = data.symbols;
      fillSymbolSelect(data.groups);
      const saved = localStorage.getItem("futures_symbol");
      const target = state.symbols.find(s => s.code === saved) || state.symbols.find(s => s.code === "RB");
      $("symbolSelect").value = target.code;
      await loadSymbol(target);
      loadWatchRow();
      if (state.liveOn) connectStream();
      // 行情先出，基本面数据随后异步补上，不阻塞首屏
      setTimeout(() => loadFundamentals(), 700);
    } catch (err) {
      setStatus("初始化失败：" + err.message, "err");
    }
  }

  function fillSymbolSelect(groups) {
    const sel = $("symbolSelect");
    sel.innerHTML = "";
    groups.forEach(g => {
      const items = state.symbols.filter(s => s.group === g);
      if (!items.length) return;
      const og = document.createElement("optgroup");
      og.label = g;
      items.forEach(s => {
        const o = document.createElement("option");
        o.value = s.code;
        o.textContent = `${s.name}  ${s.code}`;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
  }

  function bindUI() {
    updateChanBtn();
    $("symbolSelect").addEventListener("change", async (e) => {
      const meta = state.symbols.find(s => s.code === e.target.value);
      if (meta) {
        localStorage.setItem("futures_symbol", meta.code);
        closeStream();
        await loadSymbol(meta);
        if (state.liveOn) connectStream();
      }
    });

    $("intervalSeg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      [...$("intervalSeg").children].forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.interval = btn.dataset.iv;
      loadSymbol(state.meta, true);
      // 周期会影响推送内容（是否带合并后的日线末根），重连以带上新周期
      if (state.liveOn) connectStream();
    });

    $("countSelect").addEventListener("change", (e) => {
      state.count = parseInt(e.target.value, 10);
      loadSymbol(state.meta, true);
    });

    $("refreshBtn").addEventListener("click", () => loadSymbol(state.meta, true));

    $("liveToggle").addEventListener("change", (e) => {
      state.liveOn = e.target.checked;
      if (state.liveOn) {
        connectStream();
        toast("已开启实时推送");
      } else {
        disconnectStream();
        toast("已关闭实时推送");
      }
    });

    $("aiFundBtn").addEventListener("click", () => runAI("fundamental"));
    $("aiDiagBtn").addEventListener("click", () => runAI("diagnose"));
    $("termExpand").addEventListener("click", openTermModal);
    $("termModalClose").addEventListener("click", closeTermModal);
    $("chanBtn").addEventListener("click", () => {
      // 用 chanOn 而不是 state.chan 判断开关：
      // 切换周期时 state.chan 会被清空，但用户「开着缠论」的意图要保留。
      if (state.chanOn) {
        state.chanOn = false;
        updateChanBtn();
        clearChan();
      } else {
        state.chanOn = true;
        updateChanBtn();
        loadChan();
      }
    });
    $("termModal").addEventListener("click", (e) => {
      if (e.target === $("termModal")) closeTermModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("termModal").hidden) closeTermModal();
    });
    document.querySelectorAll('[data-act="netcheck"]').forEach(b =>
      b.addEventListener("click", runNetCheck));

    $("subChartSeg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      [...$("subChartSeg").children].forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.sub = btn.dataset.sub;
      renderCharts();
    });

    $("tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      [...$("tabs").children].forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".tab-panel").forEach(p => {
        p.classList.toggle("active", p.dataset.panel === btn.dataset.tab);
      });
      if (btn.dataset.tab === "term") loadTermStructure();
      if (btn.dataset.tab === "fund") loadFundamentals();
      if (btn.dataset.tab === "combine") { collectResearch(false); renderCombine(); }
      setTimeout(() => { Object.values(state.charts).forEach(c => c && c.resize()); }, 60);
    });

    $("refreshFund").addEventListener("click", () => loadFundamentals(true));
    $("fillFacts").addEventListener("click", fillFactsFromAuto);

    $("addFact").addEventListener("click", () => addFactRow());
    $("addUnverified").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.value.trim()) {
        addUnverified(e.target.value.trim());
        e.target.value = "";
      }
    });
    $("saveResearch").addEventListener("click", saveResearch);
    $("loadResearch").addEventListener("click", loadResearch);

    // 基本面输入变化 → 实时刷新综合诊断
    ["fBias", "fHorizon", "fConfidence", "fDate", "fDrivers", "fCounter", "fWatch",
     "fWarehouse", "fForecast", "fRatio", "fRisk", "fDelivery"].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", () => collectResearch());
      el.addEventListener("input", () => collectResearch());
    });
  }

  // ---------------- 品种加载 ----------------
  function resetAIPanels(reason) {
    // 品种或数据变了，旧的 AI 研判就失效了——留着会误导
    state.aiResult = null;
    state.aiMeta = {};
    ["aiFundBody", "aiDiagBody"].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.classList.remove("ai-stale");
      el.querySelectorAll(".ai-stale-bar,.ai-data-badge,.ai-warn").forEach(n => n.remove());
    });
    $("aiFundBody").innerHTML =
      `<span class="ai-placeholder">${reason || "点击「生成研判」，模型将逐条复述读数并给出分析过程与结论"}</span>`;
    $("aiDiagBody").innerHTML =
      `<span class="ai-placeholder">${reason || "点击「生成诊断」，模型将逐步对照三方证据并写出结论"}</span>`;
  }

  // 把 AI 研判与「面板当前数据」绑定：指纹不同就明确标记过期，不再静默保留旧结论。
  // 面板刷新（含 loadFundamentals 命中缓存的分支）都会走到这里。
  function syncAIStaleness(fund) {
    const current = fund && fund.data_fingerprint;
    if (!current) return;
    [["fundamental", "aiFundBody"], ["diagnose", "aiDiagBody"]].forEach(([scope, id]) => {
      const el = $(id);
      const meta = state.aiMeta[scope];
      if (!el || !meta || !meta.fingerprint) return;
      el.querySelectorAll(".ai-stale-bar").forEach(n => n.remove());
      if (meta.fingerprint === current) {
        el.classList.remove("ai-stale");
        return;
      }
      el.classList.add("ai-stale");
      const bar = document.createElement("div");
      bar.className = "ai-stale-bar";
      bar.innerHTML = `<b>⚠ 数据已更新，下方研判基于旧数据</b>` +
        `<span>该研判生成时的数据指纹 ${esc(meta.fingerprint)}，当前面板为 ${esc(current)}。` +
        `请以面板读数为准，或点右侧按钮用当前数据重新生成。</span>` +
        `<button type="button" class="mini-btn" data-ai-refresh="${esc(scope)}">重新生成</button>`;
      el.insertBefore(bar, el.firstChild);
      bar.querySelector("[data-ai-refresh]").addEventListener("click", () => runAI(scope));
    });
  }

  async function loadSymbol(meta, force) {
    if (!meta) return;
    // 行情请求序号：快速连点周期按钮时，慢的旧响应必须丢弃，
    // 否则旧周期的 K 线会盖掉新周期的。
    const req = ++state.loadReq;
    state.meta = meta;
    state.fundamentals = null;
    resetAIPanels();
    // 品种/周期/数量变了，旧的缠论标注属于上一状态，必须作废
    clearChan();
    setStatus("加载行情…", "busy");
    $("refreshBtn").classList.add("loading");

    $("qName").textContent = meta.name;
    $("qCode").textContent = meta.exchange + " · " + meta.code;
    $("qGroup").textContent = meta.group;

    try {
      const url = `/api/analysis?market=${meta.market}&symbol=${meta.continuous}&interval=${state.interval}&count=${state.count}${force ? "&force=1" : ""}`;
      const data = await api(url);
      if (req !== state.loadReq) return;   // 过期响应，丢弃
      if (data.error) throw new Error(data.error);
      state.series = data.series;
      state.signals = data.signals;
      state.dataAsOf = data.data_asof;
      state.mergeNote = data.merge_note || "";
      renderQuote();
      renderCharts();
      renderTechPanel();
      renderVetoList();
      renderCombine();
      if (!$("fDate").value) $("fDate").value = new Date().toISOString().slice(0, 10);
      if (!$("factBody").children.length) addFactRow();
      if (data.spot) applySpot(data.spot, null);
      setStatus(`已更新 · 数据截止 ${data.data_asof}`, "ok");
      if (state.fundLoaded) loadFundamentals(true);
      // 缠论是开着的话，按新的品种/周期/数量重算一遍，
      // 否则图上会留着上一周期的缠论，或者干脆空白。
      if (state.chanOn) loadChan();
      // 期限结构页签开着时同步刷新曲线，避免停留在上一品种的图形
      const termPanel = document.querySelector('.tab-panel[data-panel="term"]');
      if (termPanel && termPanel.classList.contains("active")) loadTermStructure();
    } catch (err) {
      if (req !== state.loadReq) return;
      setStatus("行情获取失败：" + err.message, "err");
      toast("行情获取失败：" + err.message, 3500);
    } finally {
      if (req === state.loadReq) $("refreshBtn").classList.remove("loading");
    }
  }

  // ---------------- 实时行情推送 ----------------
  function closeStream() {
    if (state.es) {
      try { state.es.close(); } catch (e) { /* ignore */ }
      state.es = null;
    }
    if (state.liveTimer) {
      clearInterval(state.liveTimer);
      state.liveTimer = null;
    }
  }

  function connectStream() {
    closeStream();
    if (!state.meta || state.meta.market !== "domestic") {
      setLiveStatus("该品种暂不支持实时推送", "off");
      return;
    }
    const url = `/api/stream?codes=${encodeURIComponent(state.meta.code)}&interval=${state.interval}`;
    let es;
    try {
      es = new EventSource(url);
    } catch (err) {
      setLiveStatus("实时连接失败：" + err.message, "err");
      return;
    }
    state.es = es;

    es.addEventListener("quote", (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch (e) { return; }
      if (payload.symbol !== state.meta.code) return;
      state.liveSpot = payload.spot;
      state.lastQuoteAt = Date.now();
      applySpot(payload.spot, payload);
    });

    es.addEventListener("error", () => {
      // EventSource 会自动重连；仅在持续失败时提示
      if (es.readyState === EventSource.CLOSED) {
        setLiveStatus("实时连接已断开", "err");
      }
    });

    es.onopen = () => setLiveStatus("实时推送中", "on");
    es.onerror = () => setLiveStatus("实时连接重试中…", "busy");
  }

  function disconnectStream() {
    closeStream();
    setLiveStatus("已停止", "off");
  }

  function setLiveStatus(text, kind) {
    const flag = $("liveFlag");
    if (!flag) return;
    flag.textContent = text;
    flag.className = "live-flag " + (kind || "");
  }

  function applySpot(spot, payload) {
    if (!spot || !spot.ok) {
      setLiveStatus("行情不可用", "err");
      $("liveNote").textContent = (spot && spot.error) || "实时行情不可用";
      return;
    }
    const dir = spot.change > 0 ? "up" : (spot.change < 0 ? "down" : "");
    const priceEl = $("livePrice");
    const prev = state.liveSpot && state.liveSpot.last;
    priceEl.textContent = fmt(spot.last, 2);
    priceEl.className = "live-price " + dir;
    // 价格变动时闪烁提示
    if (prev !== undefined && prev !== null && prev !== spot.last) {
      priceEl.classList.remove("flash-up", "flash-down");
      void priceEl.offsetWidth;
      priceEl.classList.add(spot.last > prev ? "flash-up" : "flash-down");
    }
    // 图表头部同步最新价与涨跌（昨结算基准）
    state.liveSpot = spot;
    if (state.signals) renderQuote();

    $("liveChange").textContent = `${spot.change > 0 ? "+" : ""}${fmt(spot.change, 2)} (${spot.change_pct > 0 ? "+" : ""}${fmt(spot.change_pct, 2)}%)`;
    $("liveChange").className = "live-change " + dir;
    $("liveBid").textContent = fmt(spot.bid, 2);
    $("liveAsk").textContent = fmt(spot.ask, 2);
    $("liveOpen").textContent = fmt(spot.open, 2);
    $("liveHigh").textContent = fmt(spot.high, 2);
    $("liveLow").textContent = fmt(spot.low, 2);
    $("liveAvg").textContent = fmt(spot.avg_price, 2);
    $("liveVol").textContent = fmtInt(spot.volume);
    $("liveOi").textContent = fmtInt(spot.open_interest);
    $("liveTime").textContent = spot.time || "--";
    const sess = (payload && payload.session) || null;
    if (sess) $("liveSession").textContent = sess.label;
    $("liveNote").textContent = `数据来自于盘立方 · 涨跌基准：${spot.reference_kind} ${fmt(spot.reference, 2)}`
      + (payload && payload.merge_note ? " · " + payload.merge_note : "");

    // 日线模式：把实时快照写回最后一根 K 线
    if (payload && payload.last_bar && state.series) {
      const bar = payload.last_bar;
      const dates = state.series.datetime;
      const idx = dates.indexOf(bar.datetime);
      if (idx >= 0) {
        state.series.open[idx] = bar.open;
        state.series.high[idx] = bar.high;
        state.series.low[idx] = bar.low;
        state.series.close[idx] = bar.close;
        if (bar.open_interest != null) state.series.open_interest[idx] = bar.open_interest;
        renderCharts();
      }
    }
  }

  function renderQuote() {
    const s = state.signals.stats;
    // 涨跌幅基准：国内期货惯例以「昨结算价」为基准，而非当日开盘价。
    // 有实时快照时用快照的昨结算口径；否则退回日线序列的结算价。
    const spot = state.liveSpot;
    let price = s.close;
    let change = s.change;
    let changePct = s.change_pct;
    let base = null;
    if (spot && spot.ok && spot.reference) {
      price = spot.last;
      change = spot.change;
      changePct = spot.change_pct;
      base = `${spot.reference_kind} ${fmt(spot.reference, 2)}`;
    }
    const c = cls(change);
    $("qPrice").textContent = fmt(price, 2);
    $("qPrice").className = "price " + c;
    $("qChange").textContent = `${change > 0 ? "+" : ""}${fmt(change)} (${changePct > 0 ? "+" : ""}${fmt(changePct)}%)`;
    $("qChange").className = "change " + c;
    $("qChange").title = base ? "涨跌基准：" + base : "涨跌基准：当日开盘价";

    const stats = [
      ["近 5 日", s.ret5 !== null ? (s.ret5 > 0 ? "+" : "") + fmt(s.ret5) + "%" : "--", cls(s.ret5)],
      ["近 20 日", s.ret20 !== null ? (s.ret20 > 0 ? "+" : "") + fmt(s.ret20) + "%" : "--", cls(s.ret20)],
      ["价格分位", s.percentile !== null ? fmt(s.percentile, 1) + "%" : "--", ""],
      ["ATR(14)", s.atr !== null ? fmt(s.atr) : "--", ""],
      ["ATR 占比", s.atr_pct !== null ? fmt(s.atr_pct) + "%" : "--", ""],
      ["量比(5日)", s.volume_ratio5 !== null ? fmt(s.volume_ratio5) + "x" : "--", ""],
      ["持仓变动", s.oi_delta !== null ? (s.oi_delta > 0 ? "+" : "") + fmtInt(s.oi_delta) : "--", cls(s.oi_delta)],
    ];
    $("quoteStats").innerHTML = stats.map(([k, v, c2]) =>
      `<div class="qstat"><span class="k">${k}</span><span class="v ${c2}">${v}</span></div>`
    ).join("");
  }

  // ---------------- 图表 ----------------
  function buildCharts() {
    state.charts.main = echarts.init($("mainChart"));
    state.charts.vol = echarts.init($("volChart"));
    state.charts.sub = echarts.init($("subChart"));
    window.addEventListener("resize", () => {
      Object.values(state.charts).forEach(c => c && c.resize());
    });
  }

  // ---------------- 缠论图层 ----------------
  // 把 chan.py 算出的笔/线段/中枢/买卖点叠加到 K 线上。
  // 中枢用矩形(markArea)，笔和线段用自定义线段(custom series)，
  // 买卖点用 scatter + 三角箭头 + 文字标签。
  const CHAN_COLORS = {
    bi: "#2c5f8d",      // 笔：深蓝
    seg: "#b45fc0",    // 线段：紫
    zsFill: "rgba(224,138,60,.14)",  // 中枢填充：暖橙
    zsLine: "#e08a3c", // 中枢边框
    buy: "#d94a3d",    // 买点：红（中国市场惯例：涨=红）
    sell: "#1e9e6a",   // 卖点：绿
  };

  // 把后端返回的时间统一成「K 线时间键」：
  //   日线   "2026-11-26"            → "2026-11-26"
  //   分钟线 "2026-11-26 09:35:00"  → "2026-11-26 09:35"
  // 与后端 chan_analysis 的 kl_time_map 格式一致。
  function chanTimeKey(dt) {
    if (!dt) return "";
    const s = String(dt);
    return s.length >= 16 ? s.slice(0, 16) : s.slice(0, 10);
  }

  // 把缠论元素重新映射到「当前图表」的 K 线索引上。
  //
  // 为什么必须这么做：后端给的 start_idx/end_idx 是「缠论自己那批 K 线」的序号，
  // 而图上画的是 /api/analysis 那批 K 线。两者只要有一根之差就会整体错位，
  // 错位来源有三个：
  //   1. 切换周期/切换数量后两次取数的窗口长度不同；
  //   2. 日线会额外合并一根「当日进行中 K 线」（analysis 301 根 / chan 300 根）；
  //   3. K 线缓存 120 秒就过期，分钟线在盘中会滚出一根新的 bar，
  //      导致缠论那批数据的首尾日期和图上对不上。
  // 所以这里按「时间」而不是「序号」对齐：找不到对应 bar 的元素直接丢弃，
  // 保证画出来的每一根笔/线段/中枢都落在图上真实存在的那根 K 线上。
  function alignChan(c) {
    if (!c) return null;
    const dates = (state.series && state.series.datetime) || [];
    if (!dates.length) return c;

    const hasTime = (c.bi || []).some(b => b.start_time)
      || (c.seg || []).some(s => s.start_time)
      || (c.zs || []).some(z => z.start_time)
      || (c.bsp || []).some(b => b.time);
    if (!hasTime) return c;   // 后端没给时间，只能退回原始索引

    const map = new Map();
    const dayMap = new Map();      // 日期 -> 当日第一根 K 线（分钟图上遇到纯日期时间戳时兜底）
    dates.forEach((d, i) => {
      const k = chanTimeKey(d);
      if (!map.has(k)) map.set(k, i);
      const day = k.slice(0, 10);
      if (day && !dayMap.has(day)) dayMap.set(day, i);
    });

    // 返回 null 表示这根 bar 已经不在图上（窗口错位/已滚出），该元素要丢掉
    const pick = (t, fb) => {
      if (t == null) return fb;
      const k = chanTimeKey(t);
      let i = map.get(k);
      if (i === undefined && k.length === 10) i = dayMap.get(k);   // 纯日期 → 当日第一根
      return i === undefined ? null : i;
    };

    // 价格吸附：缠论的价格取自它自己那批 K 线，万一两次取数的快照有细微差别
    // （日线末根盘中被刷新），笔的端点可能落在图上那根 K 线的高低点之外，
    // 画出来就会有一小截错位。这里把价格夹回该 bar 的实际区间内。
    const bars = (state.series && state.series) || null;
    const clampPrice = (price, idx) => {
      if (!bars || idx == null) return price;
      const lo = bars.low[idx], hi = bars.high[idx];
      if (lo == null || hi == null) return price;
      const a = Math.min(lo, hi), b = Math.max(lo, hi);
      if (price >= a - 1e-6 && price <= b + 1e-6) return price;
      return Math.min(b, Math.max(a, price));
    };

    const out = { ...c, bi: [], seg: [], zs: [], bsp: [] };
    for (const b of (c.bi || [])) {
      const si = pick(b.start_time, b.start_idx);
      const ei = pick(b.end_time, b.end_idx);
      if (si === null || ei === null) continue;
      out.bi.push({ ...b, start_idx: si, end_idx: ei,
        start_price: clampPrice(b.start_price, si), end_price: clampPrice(b.end_price, ei) });
    }
    for (const s of (c.seg || [])) {
      const si = pick(s.start_time, s.start_idx);
      const ei = pick(s.end_time, s.end_idx);
      if (si === null || ei === null) continue;
      out.seg.push({ ...s, start_idx: si, end_idx: ei,
        start_price: clampPrice(s.start_price, si), end_price: clampPrice(s.end_price, ei) });
    }
    for (const z of (c.zs || [])) {
      const si = pick(z.start_time, z.start_idx);
      const ei = pick(z.end_time, z.end_idx);
      if (si === null || ei === null) continue;
      out.zs.push({ ...z, start_idx: si, end_idx: ei });
    }
    for (const b of (c.bsp || [])) {
      const i = pick(b.time, b.klu_idx);
      if (i === null) continue;
      out.bsp.push({ ...b, klu_idx: i, price: clampPrice(b.price, i) });
    }
    return out;
  }

  function chanSeries() {
    const c = alignChan(state.chan);
    if (!c) return [];

    // 笔：一条 custom series，一笔一个数据点。
    //
    // 坐标系处理（踩过两个坑）：
    // 1. custom series 的 data 里若混放"索引"和"价格"，ECharts 会把两个维度
    //    都当 y 值参与轴范围计算，索引(0~300)会把 y 轴撑到从 0 起，
    //    K 线被压成一条扁带。→ 用 encode 明确指定 x/y 各取哪个维度。
    // 2. data 不能全填 null：虽然 renderItem 仍会被调用，但图形不会被绘制。
    //    → data 填真实值 [x1, y1, x2, y2]，encode 只认前两个维度参与坐标，
    //      后两个维度供 renderItem 取终点，不参与轴范围。
    //
    // 注意：笔只建「一条」series。早先的写法是每一笔建一条、每条都塞全部数据，
    // 结果 N 笔就画 N×N 条完全重合的线（8 笔 = 56 次重绘），既浪费又容易在
    // 合并 series 时残留。
    const biSeries = (c.bi || []).length ? [{
      type: "custom",
      name: "笔",
      encode: { x: 0, y: 1 },
      renderItem: (params, api) => {
        const b = c.bi[params.dataIndex];
        if (!b) return null;
        const p1 = api.coord([api.value(0), api.value(1)]);
        const p2 = api.coord([api.value(2), api.value(3)]);
        return {
          type: "line",
          shape: { x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1] },
          style: {
            stroke: b.is_sure ? CHAN_COLORS.bi : "#8fb0cf",
            lineWidth: 1.4,
            lineDash: b.is_sure ? undefined : [4, 3],
          },
        };
      },
      data: c.bi.map(b => [b.start_idx, b.start_price, b.end_idx, b.end_price]),
      z: 4,
      silent: true,
    }] : [];

    // 线段：加粗紫色，同样只建一条
    const segSeries = (c.seg || []).length ? [{
      type: "custom",
      name: "线段",
      encode: { x: 0, y: 1 },
      renderItem: (params, api) => {
        const sg = c.seg[params.dataIndex];
        if (!sg) return null;
        const p1 = api.coord([api.value(0), api.value(1)]);
        const p2 = api.coord([api.value(2), api.value(3)]);
        return {
          type: "line",
          shape: { x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1] },
          style: {
            stroke: sg.is_sure ? CHAN_COLORS.seg : "#d3a5db",
            lineWidth: 2.4,
            lineDash: sg.is_sure ? undefined : [6, 4],
          },
        };
      },
      data: c.seg.map(sg => [sg.start_idx, sg.start_price, sg.end_idx, sg.end_price]),
      z: 5,
      silent: true,
    }] : [];

    // 中枢：用一个 custom series 画矩形。
    // 不能用 markArea —— markArea 的 yAxis 值会参与 y 轴范围计算，
    // 把轴撑到 0 起，K 线被压成一条扁带，结构完全看不清。
    const zsSeries = [];
    if ((c.zs || []).length) {
      zsSeries.push({
        name: "中枢", type: "custom", z: 2, silent: true,
        encode: { x: 0, y: 1 },
        data: c.zs.map(z => [z.start_idx, z.zd, z.end_idx, z.zg]),
        renderItem: (params, api) => {
          const z = c.zs[params.dataIndex];
          if (!z) return null;
          const p1 = api.coord([api.value(0), api.value(1)]);
          const p2 = api.coord([api.value(2), api.value(3)]);
          const x = Math.min(p1[0], p2[0]), y = Math.min(p1[1], p2[1]);
          const w = Math.abs(p2[0] - p1[0]), h = Math.abs(p2[1] - p1[1]);
          return {
            type: "group",
            children: [
              {
                type: "rect",
                shape: { x, y, width: Math.max(w, 2), height: Math.max(h, 2) },
                style: { fill: CHAN_COLORS.zsFill, stroke: CHAN_COLORS.zsLine, lineWidth: 1.2 },
              },
              {
                type: "text",
                style: {
                  // 标签放在矩形下沿外侧：放上方容易和 K 线、买卖点标签撞在一起
                  text: `中枢${z.idx + 1}`,
                  x: x + 4, y: y + Math.max(h, 2) + 3,
                  fill: CHAN_COLORS.zsLine, fontSize: 10, fontWeight: 600,
                  // 加白底，否则会和 K 线混在一起看不清
                  backgroundColor: "rgba(255,255,255,.9)",
                  padding: [1, 3], borderRadius: 2,
                },
              },
            ],
          };
        },
      });
    }

    // 买卖点：三角箭头 + 文字标签
    // 同样用 encode 指定 x=索引、y=价格，避免索引污染 y 轴范围
    const bspSeries = [];
    if ((c.bsp || []).length) {
      bspSeries.push({
        name: "买卖点", type: "custom", z: 8, silent: true,
        encode: { x: 0, y: 1 },
        data: c.bsp.map(b => [b.klu_idx, b.price]),
        renderItem: (params, api) => {
          const b = c.bsp[params.dataIndex];
          if (!b) return null;
          const p = api.coord([api.value(0), api.value(1)]);
          const isBuy = b.is_buy;
          const color = isBuy ? CHAN_COLORS.buy : CHAN_COLORS.sell;
          const s = 6.5;
          // 买点：向上三角（顶点在 K 线上方）；卖点：向下三角
          const tri = isBuy
            ? [[p[0], p[1] - s], [p[0] - s, p[1] + s], [p[0] + s, p[1] + s]]
            : [[p[0], p[1] + s], [p[0] - s, p[1] - s], [p[0] + s, p[1] - s]];
          // 标签靠近图表右缘时改为右对齐，否则会被裁掉
          const grid = api.coord([api.value(0), api.value(1)]);
          const rightEdge = api.getWidth() - 4;
          const nearRight = grid[0] > rightEdge - 34;
          const nearLeft = grid[0] < 34;
          const align = nearRight ? "right" : nearLeft ? "left" : "center";
          const textX = nearRight ? grid[0] + 9 : nearLeft ? grid[0] - 9 : grid[0];
          return {
            type: "group",
            children: [
              {
                type: "polygon",
                shape: { points: tri },
                style: { fill: color, stroke: "#fff", lineWidth: 1 },
              },
              {
                type: "text",
                style: {
                  text: b.label,
                  x: textX, y: isBuy ? p[1] + s + 12 : p[1] - s - 6,
                  fill: color, fontSize: 10, fontWeight: 700,
                  align, verticalAlign: "middle",
                  backgroundColor: "rgba(255,255,255,.85)",
                  padding: [1, 3],
                },
              },
            ],
          };
        },
      });
    }

    const out = [...zsSeries, ...biSeries, ...segSeries, ...bspSeries];
    return out;
  }

  async function loadChan() {
    if (!state.meta) return;
    // 请求序号：切换周期/品种时会连续发多次请求，慢的旧响应必须丢弃，
    // 否则会出现「切到 5m 后又被 1d 的缠论盖回来」的错位。
    const req = ++state.chanReq;
    const sym = state.meta.code, iv = state.interval, cnt = state.count;
    const btn = $("chanBtn");
    if (btn) { btn.disabled = true; btn.classList.add("loading"); }
    try {
      const url = `/api/chan?symbol=${sym}&interval=${iv}&count=${cnt}`;
      const data = await api(url);
      if (req !== state.chanReq) return;   // 过期响应，直接丢弃
      if (data.error) {
        state.chan = null;
        $("chanLegend").innerHTML = `<span class="chan-err">缠论计算失败：${esc(data.error)}</span>`;
        renderCharts();
        return;
      }
      state.chan = data;
      const st = data.stats || {};
      $("chanLegend").innerHTML = `
        <span class="lg"><i class="sw sw-chan-bi"></i>笔 ${st.bi_count || 0}</span>
        <span class="lg"><i class="sw sw-chan-seg"></i>线段 ${st.seg_count || 0}</span>
        <span class="lg"><i class="sw sw-chan-zs"></i>中枢 ${st.zs_count || 0}</span>
        <span class="lg"><i class="sw sw-chan-buy"></i>买点 ${st.buy_count || 0}</span>
        <span class="lg"><i class="sw sw-chan-sell"></i>卖点 ${st.sell_count || 0}</span>
        <span class="lg chan-note">虚线 = 未确认元素</span>`;
      $("chanPanel").hidden = false;

      // 统计摘要：中枢区间 + 最近买卖点
      const zsLines = (data.zs || []).map((z, i) => {
        const dir = z.zg > z.zd ? "" : "";
        return `<div class="chan-stat-row"><span class="k">中枢 ${i + 1}</span>
          <span class="v">${z.start_time || "?"} ~ ${z.end_time || "?"}</span>
          <span class="v">区间 ${fmt(z.zd)} ~ ${fmt(z.zg)}</span>
          <span class="v">${z.bi_count} 笔${z.is_sure ? "" : "（未确认）"}</span></div>`;
      }).join("");
      const bspLines = (data.bsp || []).slice(-8).map(b =>
        `<div class="chan-stat-row"><span class="k ${b.is_buy ? "buy" : "sell"}">${b.label}</span>
          <span class="v">${b.time || "?"}</span>
          <span class="v">${fmt(b.price)}</span></div>`).join("");
      $("chanStats").innerHTML =
        `<div class="chan-stat-col">
          <div class="chan-stat-title">中枢</div>${zsLines || '<div class="chan-stat-row"><span class="v">本区间未形成中枢</span></div>'}
        </div>
        <div class="chan-stat-col">
          <div class="chan-stat-title">最近买卖点</div>${bspLines || '<div class="chan-stat-row"><span class="v">本区间未出现买卖点</span></div>'}
        </div>`;

      renderCharts();
    } catch (err) {
      if (req !== state.chanReq) return;
      $("chanLegend").innerHTML = `<span class="chan-err">缠论请求失败：${esc(err.message)}</span>`;
    } finally {
      if (req === state.chanReq && btn) { btn.disabled = false; btn.classList.remove("loading"); }
    }
  }

  function clearChan() {
    // 作废在途的缠论请求，避免旧响应回来又把图层画上去
    state.chanReq++;
    state.chan = null;
    $("chanLegend").innerHTML = "";
    const p = $("chanPanel");
    if (p) p.hidden = true;
    // 立刻把图上残留的缠论图层抹掉，不等下一次 renderCharts
    if (state.series) renderCharts();
  }

  function updateChanBtn() {
    const btn = $("chanBtn");
    if (!btn) return;
    btn.textContent = state.chanOn ? "关闭缠论" : "叠加缠论";
    btn.classList.toggle("on", state.chanOn);
  }

  const axisCommon = {
    axisLine: { lineStyle: { color: "#e4e8ee" } },
    axisLabel: { color: "#8a95a3", fontSize: 10, hideOverlap: true },
    splitLine: { lineStyle: { color: "#eef1f5" } },
  };

  function renderCharts() {
    if (!state.series) return;
    const s = state.series;
    const dates = s.datetime;

    // 主图
    const mainSeries = [
      {
        name: "K线", type: "candlestick", data: s.open.map((o, i) => [o, s.close[i], s.low[i], s.high[i]]),
        itemStyle: {
          color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN,
        },
      },
      { name: "MA5", type: "line", data: s.ma5, smooth: true, symbol: "none", lineStyle: { width: 1.2, color: "#f2a93b" } },
      { name: "MA10", type: "line", data: s.ma10, smooth: true, symbol: "none", lineStyle: { width: 1.2, color: "#4a86c8" } },
      { name: "MA20", type: "line", data: s.ma20, smooth: true, symbol: "none", lineStyle: { width: 1.4, color: "#b45fc0" } },
      { name: "MA60", type: "line", data: s.ma60, smooth: true, symbol: "none", lineStyle: { width: 1.4, color: "#46a08a" } },
      { name: "BOLL上轨", type: "line", data: s.boll_upper, symbol: "none", lineStyle: { width: 1, color: "#b8c1cc", type: "dashed" }, showSymbol: false },
      { name: "BOLL下轨", type: "line", data: s.boll_lower, symbol: "none", lineStyle: { width: 1, color: "#b8c1cc", type: "dashed" }, showSymbol: false },
    ];

    // replaceMerge: "series" —— 必须整组替换 series，不能用默认的 merge。
    // 默认 merge 是按「下标」合并：新数组比旧数组短时，旧数组尾部的 series
    // 会被完整保留。结果就是「关掉缠论后图层还在」「切到 5m 后 1d 的缠论
    // 还盖在上面」，K 线索引和价格双重错位。
    state.charts.main.setOption({
      animation: false,
      grid: [{ left: 58, right: 58, top: 18, height: 298 }],
      xAxis: { type: "category", data: dates, ...axisCommon, axisLabel: { show: false }, axisTick: { show: false } },
      yAxis: [{ scale: true, ...axisCommon, position: "right", splitLine: { show: false } }],
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross", label: { backgroundColor: "#5b6675" } },
        backgroundColor: "rgba(255,255,255,.96)", borderColor: "#e4e8ee", borderWidth: 1,
        textStyle: { color: "#1f2933", fontSize: 11 },
      },
      legend: { show: false },
      dataZoom: [
        { type: "inside", start: Math.max(0, 100 - (state.count > 300 ? 70 : 85)), end: 100 },
        { type: "slider", height: 14, bottom: 4, borderColor: "#e4e8ee", fillerColor: "rgba(44,95,141,.08)",
          handleStyle: { color: "#c8d2de" }, textStyle: { color: "#8a95a3", fontSize: 9 } },
      ],
      series: mainSeries.concat(chanSeries()),
      axisPointer: { link: [{ xAxisIndex: "all" }] },
    }, { replaceMerge: "series" });

    // 成交 + 持仓
    const volColors = s.close.map((c, i) => (i > 0 ? (c >= s.close[i - 1] ? UP : DOWN) : DOWN));
    const oiData = s.open_interest;
    state.charts.vol.setOption({
      animation: false,
      grid: [{ left: 58, right: 58, top: 18, height: 76 }],
      xAxis: { type: "category", data: dates, ...axisCommon, axisLabel: { show: false }, axisTick: { show: false } },
      yAxis: [
        { scale: true, ...axisCommon, position: "right", splitLine: { show: false },
          axisLine: { show: false },
          axisLabel: { color: "#8a95a3", fontSize: 9, showMinLabel: false, hideOverlap: true,
            formatter: (v) => v >= 10000 ? (v / 10000).toFixed(1) + "万" : v } },
        { scale: true, ...axisCommon, position: "left", splitLine: { show: false },
          axisLine: { show: false }, axisLabel: { show: false } },
      ],
      tooltip: { trigger: "axis", backgroundColor: "rgba(255,255,255,.96)", borderColor: "#e4e8ee",
        textStyle: { color: "#1f2933", fontSize: 11 },
        formatter: (params) => {
          const p = params.find(x => x.seriesName === "持仓量");
          const v = params.find(x => x.seriesName === "成交量");
          if (!p) return "";
          const fmtVol = (x) => x >= 10000 ? (x / 10000).toFixed(2) + "万" : x;
          return `${p.axisValue}<br/>成交量：${v ? fmtVol(v.value) : "--"} 手<br/>持仓量：${fmtVol(p.value)} 手`;
        } },
      series: [
        { name: "成交量", type: "bar", data: s.volume.map((v, i) => ({ value: v, itemStyle: { color: volColors[i], opacity: .68 } })) },
        { name: "持仓量", type: "line", yAxisIndex: 0, data: oiData, symbol: "none", smooth: true,
          lineStyle: { width: 1.4, color: "#4a86c8" } },
      ],
      axisPointer: { link: [{ xAxisIndex: "all" }] },
    });

    // 副图
    const sub = state.sub;
    let subSeries = [], subLegend = [];
    if (sub === "macd") {
      subSeries = [
        { name: "DIF", type: "line", data: s.dif, symbol: "none", lineStyle: { width: 1.3, color: "#4a86c8" } },
        { name: "DEA", type: "line", data: s.dea, symbol: "none", lineStyle: { width: 1.3, color: "#e0a83c" } },
        { name: "MACD", type: "bar", data: s.hist.map(v => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) },
      ];
      subLegend = ["DIF", "DEA", "MACD"];
    } else if (sub === "kdj") {
      subSeries = [
        { name: "K", type: "line", data: s.kdj_k, symbol: "none", lineStyle: { width: 1.3, color: "#4a86c8" } },
        { name: "D", type: "line", data: s.kdj_d, symbol: "none", lineStyle: { width: 1.3, color: "#e0a83c" } },
        { name: "J", type: "line", data: s.kdj_j, symbol: "none", lineStyle: { width: 1.1, color: "#b45fc0" } },
      ];
      subLegend = ["K", "D", "J"];
    } else if (sub === "rsi") {
      subSeries = [{ name: "RSI14", type: "line", data: s.rsi14, symbol: "none", lineStyle: { width: 1.5, color: "#b45fc0" } }];
      subLegend = ["RSI14"];
    }

    state.charts.sub.clear();
    if (sub !== "none") {
      state.charts.sub.setOption({
        animation: false,
        grid: [{ left: 58, right: 58, top: 26, height: 72 }],
        xAxis: { type: "category", data: dates, ...axisCommon, axisLabel: { color: "#8a95a3", fontSize: 9 }, axisTick: { show: false } },
        yAxis: { scale: true, ...axisCommon, position: "right", splitLine: { show: false } },
        tooltip: { trigger: "axis", backgroundColor: "rgba(255,255,255,.96)", borderColor: "#e4e8ee",
          textStyle: { color: "#1f2933", fontSize: 11 } },
        legend: { data: subLegend, top: 2, right: 62, itemWidth: 12, itemHeight: 7, itemGap: 14,
          textStyle: { fontSize: 10, color: "#5b6675" } },
        series: subSeries,
        axisPointer: { link: [{ xAxisIndex: "all" }] },
      });
    }
  }

  // ---------------- 技术面面板 ----------------
  function renderTechPanel() {
    const sig = state.signals;
    const score = sig.score;
    const pct = Math.min(100, Math.abs(score) / 60 * 50);
    const isBull = score > 8, isBear = score < -8;
    const color = isBull ? UP : (isBear ? DOWN : "#8a95a3");
    const left = score >= 0 ? 50 : 50 - pct;

    $("signalCard").innerHTML = `
      <div class="signal-head">
        <div class="signal-label ${isBull ? "bull" : isBear ? "bear" : "neutral"}">${sig.label}</div>
        <div class="signal-conf">综合评分 ${score > 0 ? "+" : ""}${score}</div>
      </div>
      <div class="score-bar">
        <div class="score-marker"></div>
        <div class="score-fill" style="left:${left}%; width:${pct}%; background:${color};"></div>
      </div>
      <div class="score-scale"><span>偏空 -60</span><span>中性 0</span><span>偏多 +60</span></div>
      <div class="signal-meta">
        <div>价格分位<b>${sig.stats.percentile !== null ? fmt(sig.stats.percentile, 1) + "%" : "--"}</b></div>
        <div>近20日<b class="${cls(sig.stats.ret20)}">${sig.stats.ret20 !== null ? (sig.stats.ret20 > 0 ? "+" : "") + fmt(sig.stats.ret20) + "%" : "--"}</b></div>
        <div>ATR占比<b>${fmt(sig.stats.atr_pct)}%</b></div>
        <div>K线样本<b>${sig.stats.bars}</b></div>
      </div>
    `;

    $("signalList").innerHTML = sig.items.map(it => {
      const k = it.points > 0 ? "pos" : (it.points < 0 ? "neg" : "neu");
      const sign = it.points > 0 ? "+" : it.points;
      return `<div class="sig-item">
        <div class="sig-dot ${k}">${sign}</div>
        <div class="sig-text"><b>${it.label}</b><span>${it.detail}</span></div>
      </div>`;
    }).join("");

    const L = sig.levels;
    const cur = sig.stats.close;
    const levels = [
      ["60 日压力", L.high60, "res"], ["60 日支撑", L.low60, "sup"],
      ["20 日压力", L.high20, "res"], ["20 日支撑", L.low20, "sup"],
      ["布林上轨", L.boll_upper, "res"], ["布林下轨", L.boll_lower, "sup"],
      ["MA20", L.ma20, ""], ["MA60", L.ma60, ""],
    ];
    $("levelsGrid").innerHTML = levels.map(([k, v, c]) =>
      `<div class="level-item ${c}"><div class="k">${k}</div><div class="v">${v === null ? "--" : fmt(v)}</div></div>`
    ).join("");

    // 研究提示：结合技术面位置 + 期限结构的注意事项
    const notes = [];
    const p = sig.stats.percentile;
    if (p !== null) {
      if (p >= 80) notes.push(`<p><b>价格位于近 ${sig.stats.bars} 根 K 线的 ${fmt(p, 1)}% 分位（高位区）。</b>追涨的赔率下降，但强势品种常在高位延续，需要区分是趋势加速还是情绪冲顶。</p>`);
      else if (p <= 20) notes.push(`<p><b>价格位于近 ${sig.stats.bars} 根 K 线的 ${fmt(p, 1)}% 分位（低位区）。</b>低位不等于便宜，先看是否跌破了否决权变量。</p>`);
      else notes.push(`<p>价格处于近 ${sig.stats.bars} 根 K 线的 <b>${fmt(p, 1)}% 分位</b>，属于中位区域，位置本身不构成优势。</p>`);
    }
    if (sig.stats.oi_delta_pct !== undefined && Math.abs(sig.stats.oi_delta_pct) >= 10) {
      notes.push(`<p><b>持仓单日变动 ${fmt(sig.stats.oi_delta_pct)}%</b>，连续主力合约在此类跳变多为移仓换月，持仓方向信号此时失真，应切换到具体合约看真实持仓结构。</p>`);
    }
    if (sig.stats.atr_pct !== null && sig.stats.atr_pct > 2.5) {
      notes.push(`<p><b>ATR 占价格 ${fmt(sig.stats.atr_pct)}%</b>，日内波动放大，止损距离需相应放宽，单笔风险要按波动率折算而不是固定点数。</p>`);
    }
    if (state.meta && state.meta.anchor) {
      notes.push(`<p>${state.meta.name} 属于外盘强驱动品种，按研究纪律需<b>强制启用外盘锚定层</b>：核对汇率、进口利润与内外价差，避免用外盘涨直接推导内盘多头。</p>`);
    }
    notes.push(`<p>以上全部为盘面行为描述，<b>不能证明供需原因</b>。方向结论需要与基本面的库存、开工、仓单等事实交叉验证。</p>`);
    $("techNote").innerHTML = notes.join("");
  }

  // ---------------- 否决权变量 ----------------
  function renderVetoList() {
    const veto = (state.meta && state.meta.veto) || [];
    const vars = (state.meta && state.meta.vars) || [];
    const saved = (state.research && state.research.veto_status) || {};
    $("vetoList").innerHTML = veto.map(v => {
      const cur = saved[v] || "";
      const opts = [["支持", "support"], ["冲突", "conflict"], ["中性", "neutral"]].map(([label, val]) =>
        `<button data-v="${v}" data-val="${val}" class="${cur === val ? "on-" + val : ""}">${label}</button>`
      ).join("");
      return `<div class="veto-row">
        <div class="name"><span>${v}</span><em>否决权变量</em></div>
        <div class="seg-veto">${opts}</div>
      </div>`;
    }).join("") || `<p class="note-box">该品种未配置否决权变量，请在下方事实表中自行补充。</p>`;

    $("vetoList").querySelectorAll(".seg-veto button").forEach(btn => {
      btn.addEventListener("click", () => {
        const v = btn.dataset.v, val = btn.dataset.val;
        const row = btn.parentElement;
        const was = btn.classList.contains("on-" + val);
        row.querySelectorAll("button").forEach(b => b.className = "");
        if (!was) btn.classList.add("on-" + val);
        collectResearch();
      });
    });
  }

  // ---------------- 事实表 ----------------
  function addFactRow(fact) {
    const tb = $("factBody");
    const tr = document.createElement("tr");
    const tier = (fact && fact.tier) || "A";
    tr.innerHTML = `
      <td><div class="tier">${["S", "A", "B", "C", "D"].map(t =>
        `<button data-t="${t}" class="${t === tier ? "on" : ""}">${t}</button>`).join("")}</div></td>
      <td><input type="text" value="${(fact && fact.content) || ""}" placeholder="事实内容"></td>
      <td><input type="text" value="${(fact && fact.source) || ""}" placeholder="来源"></td>
      <td><input type="text" value="${(fact && fact.date) || ""}" placeholder="日期"></td>
      <td><button class="del-btn" title="删除">×</button></td>`;
    tb.appendChild(tr);
    tr.querySelectorAll(".tier button").forEach(b => b.addEventListener("click", () => {
      tr.querySelectorAll(".tier button").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
    }));
    tr.querySelector(".del-btn").addEventListener("click", () => tr.remove());
    tr.querySelectorAll("input").forEach(inp => inp.addEventListener("change", collectResearch));
  }

  function readFacts() {
    return [...$("factBody").querySelectorAll("tr")].map(tr => ({
      tier: (tr.querySelector(".tier button.on") || {}).dataset?.t || "A",
      content: tr.querySelectorAll("input")[0].value,
      source: tr.querySelectorAll("input")[1].value,
      date: tr.querySelectorAll("input")[2].value,
    })).filter(f => f.content.trim());
  }

  function addUnverified(text) {
    const list = $("unverifiedList");
    const chip = document.createElement("span");
    chip.className = "unv-chip";
    chip.innerHTML = `${text}<button title="移除">×</button>`;
    chip.querySelector("button").addEventListener("click", () => { chip.remove(); collectResearch(); });
    list.appendChild(chip);
  }

  // ---------------- 自动基本面数据 ----------------
  function sparkline(values, opts) {
    const o = opts || {};
    const w = o.width || 260, h = o.height || 44, pad = 4;
    const vals = (values || []).filter(v => typeof v === "number" && isFinite(v));
    if (vals.length < 2) return `<div class="spark-empty">序列不足</div>`;
    const min = Math.min.apply(null, vals);
    const max = Math.max.apply(null, vals);
    const span = (max - min) || 1;
    const step = (w - pad * 2) / (vals.length - 1);
    const pts = vals.map((v, i) => [pad + i * step, h - pad - ((v - min) / span) * (h - pad * 2)]);
    const line = pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const color = o.color || ACCENT;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polygon points="${pad},${h - pad} ${line} ${w - pad},${h - pad}" fill="${color}" opacity="0.13"/>
      <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.7"
        stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
  }

  function fundCard(o) {
    const gradeCls = `grade-${(o.grade || "-").replace(/[^A-Za-z-]/g, "")}`;
    return `<div class="auto-item">
      <div class="auto-item-head">
        <span class="auto-item-title">${o.title}</span>
        <span class="grade ${gradeCls}" title="来源等级">${o.grade || "-"}</span>
      </div>
      <div class="auto-item-body">${o.body}</div>
      ${o.chart || ""}
      <div class="auto-item-foot">
        <span class="src">数据来自于盘立方</span>
        <span class="asof">${o.asof ? "@" + o.asof : ""}</span>
      </div>
      ${o.note ? `<div class="auto-note">${o.note}</div>` : ""}
    </div>`;
  }

  function topDetail(rows) {
    if (!rows || !rows.length) return "";
    const items = rows.slice(0, 4).map(r =>
      `<li><span class="w">${r.name || "—"}</span><span class="q">${fmtInt(r.qty)}</span></li>`
    ).join("");
    return `<ul class="top-list">${items}</ul>`;
  }

  async function loadFundamentals(force) {
    if (!state.meta) return;
    state.fundLoaded = true;
    const code = state.meta.code;
    // 请求序号：快速切换品种时，慢的旧品种响应不能覆盖新品种面板。
    // 之前基本面没有这层保护，旧响应会把 state.fundamentals 换成别的品种，
    // 于是「面板显示 HC、AI 上下文拿到的却是 RB」这类不一致就出现了。
    const req = ++state.fundReq;
    if (state.fundamentals && state.fundamentals.symbol === code && !force) {
      renderFundamentals(state.fundamentals);
      return;
    }
    $("autoFundMeta").textContent = "采集中…（数据来自于盘立方，约 5-20 秒）";
    // 不清空 AI 研判：新数据回来后由 renderFundamentals → syncAIStaleness
    // 按指纹判断是「沿用」还是「标记为过期」，比直接抹掉更可追溯。
    try {
      const r = await fetch(`/api/fundamentals?symbol=${encodeURIComponent(code)}`);
      const d = await r.json();
      if (req !== state.fundReq || !state.meta || state.meta.code !== code) return;
      state.fundamentals = d;
      renderFundamentals(d);
      if (state.signals) renderCombine();
    } catch (err) {
      if (req !== state.fundReq) return;
      $("autoFundMeta").textContent = "采集失败：" + err.message;
    }
  }

  function renderFundamentals(d) {
    const meta = $("autoFundMeta");
    const grid = $("autoFundCards");
    const gaps = $("autoFundGaps");
    if (!d || d.error) {
      meta.textContent = "采集失败：" + ((d && d.error) || "未知错误");
      grid.innerHTML = "";
      gaps.innerHTML = "";
      return;
    }
    // 二次防御：任何串品种的响应都不渲染到当前面板
    if (state.meta && d.symbol && d.symbol !== state.meta.code) return;
    const b = d.blocks || {};
    meta.innerHTML = `<b>${d.name}</b>（${d.symbol} · ${d.exchange}） · 主力合约 <b>${d.main_contract || "—"}</b> · 采集于 ${d.fetched_at}`;

    const cards = [];

    // 客观基本面倾向：由采集到的读数折算，独立于人工判断
    const auto = d.auto_signal;
    if (auto) {
      const bull = auto.score > 0, bear = auto.score < 0;
      const sigBox = document.getElementById("autoFundSignal");
      if (sigBox) {
        sigBox.innerHTML = `
          <span class="sig-tag ${bull ? "up" : bear ? "down" : ""}">客观基本面：${auto.label}</span>
          <span class="sig-score">评分 ${auto.score > 0 ? "+" : ""}${auto.score}</span>
          ${auto.items.length ? `<details class="sig-detail"><summary>${auto.items.length} 条读数</summary><ul>${
            auto.items.map(it => `<li><span class="${it.points > 0 ? "up" : it.points < 0 ? "down" : ""}">[${it.points > 0 ? "+" : ""}${it.points}]</span> ${it.detail}</li>`).join("")
          }</ul></details>` : `<span class="sig-detail dim">读数不足，不参与计分</span>`}`;
      }
    }

    const rc = b.receipt;
    if (rc && rc.ok) {
      const v = rc.value;
      cards.push(fundCard({
        title: "交割仓单", grade: rc.grade, source: rc.source, asof: rc.asof,
        body: `
          <div class="big-num">${fmtInt(v.total)}<small>手</small></div>
          <div class="sub-line">日变化 <span class="${cls(v.delta)}">${v.delta >= 0 ? "+" : ""}${fmtInt(v.delta)}</span></div>
          ${v.forecast != null ? `<div class="sub-line">有效预报 <b>${fmtInt(v.forecast)}</b> 手</div>` : ""}
          ${v.premium != null ? `<div class="sub-line">升贴水 <b>${v.premium}</b></div>` : ""}`,
        note: topDetail(v.detail),
      }));
    }

    const iv = b.inventory;
    if (iv && iv.ok) {
      const v = iv.value;
      const up = v.chg_5d != null && v.chg_5d > 0;
      cards.push(fundCard({
        title: "期货库存", grade: iv.grade, source: iv.source, asof: iv.asof,
        body: `
          <div class="big-num">${fmtInt(v.latest)}<small>手</small></div>
          <div class="sub-line">5 日 <span class="${cls(v.chg_5d)}">${v.chg_5d >= 0 ? "+" : ""}${fmtInt(v.chg_5d)}</span>
            · 20 日 <span class="${cls(v.chg_20d)}">${v.chg_20d >= 0 ? "+" : ""}${fmtInt(v.chg_20d)}</span></div>
          <div class="sub-line dim">${up ? "库存累积 · 偏空压力" : "库存去化 · 偏紧支撑"}</div>`,
        chart: sparkline(v.series.map(s => s.value), { color: up ? DOWN : UP }),
      }));
    }

    const bs = b.basis;
    if (bs && bs.ok) {
      const v = bs.value;
      const rate = v.dom_basis_rate_pct;
      const z = v.basis_z20;
      cards.push(fundCard({
        title: "现货与基差", grade: bs.grade, source: bs.source, asof: bs.asof,
        body: `
          <div class="big-num">${fmt(v.spot, 1)}<small>元/吨 现货</small></div>
          <div class="sub-line">主力 <b>${v.dom_contract || "—"}</b> 基差
            <span class="${cls(v.dom_basis)}">${v.dom_basis >= 0 ? "+" : ""}${fmt(v.dom_basis, 1)}</span>
            <span class="dim">（${rate != null ? (rate >= 0 ? "+" : "") + rate.toFixed(2) + "%" : "—"}）</span></div>
          <div class="sub-line">除税参考基差 <span class="${cls(v.dom_basis_ex_vat)}">${v.dom_basis_ex_vat != null ? (v.dom_basis_ex_vat >= 0 ? "+" : "") + fmt(v.dom_basis_ex_vat, 1) : "—"}</span>
            ${z != null ? ` · z<sub>20</sub> <span class="${cls(z)}">${z >= 0 ? "+" : ""}${z.toFixed(2)}</span>` : ""}</div>`,
        chart: sparkline(v.series.map(s => s.dom_basis_rate), { color: WARM }),
        note: bs.note,
      }));
    }

    const ps = b.position;
    if (ps && ps.ok) {
      const v = ps.value;
      const r = v.long_short_ratio;
      cards.push(fundCard({
        title: "会员持仓结构", grade: ps.grade, source: ps.source, asof: v.asof,
        body: `
          <div class="big-num">${r != null ? r.toFixed(3) : "—"}<small>多空比</small></div>
          <div class="sub-line">前 20 多 <b>${fmtInt(v.long_sum)}</b> · 空 <b>${fmtInt(v.short_sum)}</b></div>
          <div class="sub-line">净持仓 <span class="${cls(v.net)}">${v.net >= 0 ? "+" : ""}${fmtInt(v.net)}</span> 手</div>`,
        note: ps.note,
      }));
    }

    grid.innerHTML = cards.join("");

    const missing = Object.entries(b)
      .filter(([, blk]) => !blk || !blk.ok)
      .map(([k, blk]) => `${kLabel(k)}：${(blk && blk.error) || "不可用"}`);
    gaps.innerHTML = missing.length
      ? `<div class="gap-head">数据缺口（不会用推测值填补）</div>` +
        missing.map(m => `<div class="gap-item">· ${m}</div>`).join("")
      : `<div class="gap-ok">四个数据块均已获取</div>`;

    // 面板数据落地后立即比对 AI 研判指纹：数据变了就明确标记旧研判过期
    syncAIStaleness(d);
  }

  function kLabel(k) {
    return { receipt: "交割仓单", inventory: "期货库存", basis: "现货基差", position: "会员持仓" }[k] || k;
  }

  function fillFactsFromAuto() {
    const d = state.fundamentals;
    if (!d || d.error) { toast("尚未采集到基本面数据"); return; }
    const b = d.blocks || {};
    const facts = [];
    const push = (blk, content) => facts.push({
      tier: blk.grade, content, source: blk.source, date: blk.asof || "",
    });

    if (b.receipt && b.receipt.ok) {
      const v = b.receipt.value;
      push(b.receipt, `交割仓单 ${fmtInt(v.total)} 手，当日变化 ${v.delta >= 0 ? "+" : ""}${fmtInt(v.delta)} 手`);
      if (v.forecast) push(b.receipt, `有效预报 ${fmtInt(v.forecast)} 手`);
    }
    if (b.inventory && b.inventory.ok) {
      const v = b.inventory.value;
      push(b.inventory, `期货库存 ${fmtInt(v.latest)} 手，5 日 ${v.chg_5d >= 0 ? "+" : ""}${fmtInt(v.chg_5d)}，20 日 ${v.chg_20d >= 0 ? "+" : ""}${fmtInt(v.chg_20d)}`);
    }
    if (b.basis && b.basis.ok) {
      const v = b.basis.value;
      push(b.basis, `现货 ${fmt(v.spot, 1)} 元/吨，主力 ${v.dom_contract} 基差 ${v.dom_basis >= 0 ? "+" : ""}${fmt(v.dom_basis, 1)}（${v.dom_basis_rate_pct != null ? (v.dom_basis_rate_pct >= 0 ? "+" : "") + v.dom_basis_rate_pct.toFixed(2) + "%" : "—"}）`);
    }
    if (b.position && b.position.ok) {
      const v = b.position.value;
      push(b.position, `前 20 会员多单 ${fmtInt(v.long_sum)} 手、空单 ${fmtInt(v.short_sum)} 手，多空比 ${v.long_short_ratio != null ? v.long_short_ratio.toFixed(3) : "—"}`);
    }
    if (!facts.length) { toast("没有可写入的事实（数据块均不可用）"); return; }
    facts.forEach(f => addFactRow(f));
    collectResearch();
    toast(`已写入 ${facts.length} 条事实，请核对后填写结论`);
  }

  // ---------------- 研究记录 ----------------
  function collectResearch(render) {
    const veto_status = {};
    document.querySelectorAll(".seg-veto button").forEach(b => {
      const cls = b.className;
      if (cls.startsWith("on-")) veto_status[b.dataset.v] = cls.replace("on-", "");
    });
    const unverified = [...$("unverifiedList").querySelectorAll(".unv-chip")]
      .map(c => c.textContent.replace("×", "").trim()).filter(Boolean);

    const rec = {
      symbol: state.meta ? state.meta.code : "",
      name: state.meta ? state.meta.name : "",
      date: $("fDate").value || new Date().toISOString().slice(0, 10),
      bias: $("fBias").value,
      horizon: $("fHorizon").value,
      confidence: $("fConfidence").value,
      drivers: $("fDrivers").value,
      counter: $("fCounter").value,
      watch: $("fWatch").value,
      veto_status,
      facts: readFacts(),
      unverified,
      warehouse: $("fWarehouse").value,
      forecast: $("fForecast").value,
      ratio: $("fRatio").value,
      risk: $("fRisk").value,
      delivery: $("fDelivery").value,
      tech_label: state.signals ? state.signals.label : "",
      tech_score: state.signals ? state.signals.score : 0,
      data_asof: state.dataAsOf,
    };
    state.research = rec;
    if (render !== false) renderCombine();
    return rec;
  }

  async function saveResearch() {
    const rec = collectResearch();
    if (!rec.symbol) { toast("请先选择品种"); return; }
    try {
      const res = await fetch("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rec),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      $("saveHint").textContent = "已保存 " + (data.record.updated_at || "");
      toast("研究记录已保存");
      loadResearchLog();
    } catch (err) {
      toast("保存失败：" + err.message, 3200);
    }
  }

  async function loadResearch() {
    if (!state.meta) return;
    try {
      const data = await api(`/api/research?symbol=${state.meta.code}`);
      if (!data.items.length) { toast("该品种暂无历史记录"); return; }
      const rec = data.items[0];
      $("fBias").value = rec.bias || "";
      $("fHorizon").value = rec.horizon || "";
      $("fConfidence").value = rec.confidence || "";
      $("fDate").value = rec.date || "";
      $("fDrivers").value = rec.drivers || "";
      $("fCounter").value = rec.counter || "";
      $("fWatch").value = rec.watch || "";
      $("fWarehouse").value = rec.warehouse || "";
      $("fForecast").value = rec.forecast || "";
      $("fRatio").value = rec.ratio || "";
      $("fRisk").value = rec.risk || "";
      $("fDelivery").value = rec.delivery || "";
      $("factBody").innerHTML = "";
      (rec.facts || []).forEach(addFactRow);
      $("unverifiedList").innerHTML = "";
      (rec.unverified || []).forEach(addUnverified);
      renderVetoList();
      const saved = rec.veto_status || {};
      document.querySelectorAll(".seg-veto button").forEach(b => {
        const v = b.dataset.v, val = b.dataset.val;
        b.className = saved[v] === val ? "on-" + val : "";
      });
      collectResearch();
      toast("已载入 " + (rec.date || "") + " 的记录");
      loadResearchLog();
    } catch (err) {
      toast("载入失败：" + err.message, 3000);
    }
  }

  async function loadResearchLog() {
    if (!state.meta) return;
    try {
      const data = await api(`/api/research?symbol=${state.meta.code}`);
      const el = $("researchLog");
      if (!data.items.length) { el.innerHTML = `<p class="note-box">暂无历史记录，录入并保存后会在这里形成时间序列。</p>`; return; }
      el.innerHTML = data.items.map(rec => `
        <div class="log-item">
          <div class="meta"><span>${rec.date || "--"} · ${rec.updated_at || ""}</span>
            <span class="bias" style="color:${rec.bias === "偏多" || rec.bias === "中性偏多" ? UP : (rec.bias === "偏空" || rec.bias === "中性偏空" ? DOWN : "#5b6675")}">${rec.bias || "未判定"}</span></div>
          <div class="drivers">${(rec.drivers || "").replace(/</g, "&lt;") || "—"}</div>
        </div>`).join("");
    } catch (e) { /* 静默 */ }
  }

  // ---------------- 综合诊断 ----------------
  function renderCombine() {
    const sig = state.signals;
    if (!sig) return;
    const rec = state.research || collectResearch(false);
    const techLabel = sig.label;
    const techScore = sig.score;

    $("combineTech").innerHTML = `
      <dl>
        <dt>技术面倾向</dt><dd>${techLabel}（${techScore > 0 ? "+" : ""}${techScore}）</dd>
        <dt>价格分位</dt><dd>${fmt(sig.stats.percentile, 1)}%</dd>
        <dt>近20日涨跌</dt><dd>${sig.stats.ret20 !== null ? (sig.stats.ret20 > 0 ? "+" : "") + fmt(sig.stats.ret20) + "%" : "--"}</dd>
        <dt>波动率(ATR)</dt><dd>${fmt(sig.stats.atr)}（${fmt(sig.stats.atr_pct)}%）</dd>
        <dt>持仓变动</dt><dd>${sig.stats.oi_delta !== null ? (sig.stats.oi_delta > 0 ? "+" : "") + fmtInt(sig.stats.oi_delta) : "--"}</dd>
        <dt>数据截止</dt><dd>${state.dataAsOf || "--"}</dd>
      </dl>`;

    // 本地诊断（与服务端规则一致）
    const conflicts = Object.entries(rec.veto_status || {}).filter(([, v]) => v === "conflict").map(([k]) => k);
    const unverified = rec.unverified || [];
    const bullTech = techScore >= 7, bearTech = techScore <= -7;
    const bullF = ["偏多", "中性偏多"].includes(rec.bias);
    const bearF = ["偏空", "中性偏空"].includes(rec.bias);

    // 客观基本面：自动采集读数折算出的倾向，独立于人工判断
    const fundState = state.fundamentals;
    const auto = fundState && !fundState.error ? fundState.auto_signal : null;
    const autoLabel = auto ? auto.label : "";
    const autoScore = auto ? auto.score : 0;
    const bullAuto = autoScore >= 1, bearAuto = autoScore <= -1;
    const strongAuto = autoScore >= 3 || autoScore <= -3;

    let severity = "ok", title, detail, actions = [];
    if (!rec.bias) {
      severity = "info"; title = "尚未录入基本面判断";
      detail = "技术面已就绪。到「基本面」页填写决策面板（倾向、时间边界、否决权变量状态）后，这里会给出两者的一致性诊断与降级建议。";
    } else if (bullTech && bearF) {
      severity = "warn"; title = "方向冲突：技术面偏多 vs 基本面偏空";
      detail = `盘面信号为${techLabel}（评分 ${techScore}），基本面倾向记为「${rec.bias}」。这种背离通常意味着市场在交易尚未兑现的预期，或存在未被识别的否决权变量，不宜用单一方向下注。`;
      actions = ["核对盘面是否提前交易了未兑现的预期", "检查否决权变量是否真的支持盘面方向",
        "考虑降级为「只看结构」或改用跨期表达", "确认时间边界一致（日内波动与中期供需不可混用）"];
    } else if (bearTech && bullF) {
      severity = "warn"; title = "方向冲突：技术面偏空 vs 基本面偏多";
      detail = `盘面信号为${techLabel}（评分 ${techScore}），基本面倾向记为「${rec.bias}」。通常意味着基本面预期已被价格提前反映，或存在压制盘面的结构性因素（仓单、交割、逼仓）。`;
      actions = ["核对基差与现货：基本面转好但盘面走弱，多半是现货没跟上",
        "检查仓单与交割资源是否压制近月", "确认时间边界是否错配"];
    } else if (strongAuto && ((bullTech && bearAuto) || (bearTech && bullAuto))) {
      severity = "warn"; title = `方向冲突：技术面${techLabel} vs 客观基本面${autoLabel}`;
      detail = `盘面信号为${techLabel}（评分 ${techScore}），而自动采集的客观读数指向${autoLabel}（评分 ${autoScore > 0 ? "+" : ""}${autoScore}）。两者反向时，盘面与现货之间通常存在尚未兑现的预期，或盘面正在交易结构性风险（仓单、交割、逼仓）。`;
      actions = ["先核实客观读数的来源等级与数据日期（S 级官方数据优先）",
        "检查主观判断是否忽略了刚出现的边际变化", "结论降级为「只看结构」，等待盘面与客观数据重新同向"];
    } else if (strongAuto && ((bullF && bearAuto) || (bearF && bullAuto))) {
      severity = "warn"; title = `主观判断与客观数据不一致：基本面${rec.bias} vs 客观${autoLabel}`;
      detail = `决策面板记为「${rec.bias}」，但自动采集的客观读数（库存、基差、仓单、持仓）指向${autoLabel}（评分 ${autoScore > 0 ? "+" : ""}${autoScore}）。要么人工判断引入了尚未入库的信息，要么客观读数尚未反映真实供需——必须先解决这个分歧，不能带着矛盾出结论。`;
      actions = ["逐条核对客观读数，确认是否存在口径或时效问题",
        "若主观判断基于未入库信息，补上来源与日期后重新评估",
        "在分歧消除前，结论按「观望」处理"];
    } else if (conflicts.length) {
      severity = "warn"; title = "否决权变量冲突：结论需要降级";
      detail = `已标记为「冲突」的否决权变量：${conflicts.join("、")}。否决权变量与主叙事冲突时，按纪律必须先降级结论。`;
      actions = ["优先核实冲突变量的最新数据与来源等级", "冲突解除前，结论降级为「观望」或「只看结构」",
        "明确写出失效条件与改判触发点"];
    } else if (unverified.length) {
      severity = "warn"; title = "存在未验证信息，暂不能进入主链";
      detail = `以下信息尚未验证：${unverified.slice(0, 5).join("、")}。未核验信息不得直接进入主结论。`;
      actions = ["先做源头核验与异类交叉验证", "核验完成前只作为线索处理"];
    } else if ((bullTech && bullF) || (bearTech && bearF)) {
      title = "技术面与基本面同向";
      detail = `盘面${techLabel}（评分 ${techScore}）与基本面倾向「${rec.bias}」方向一致。同向时仍需检查时间边界是否一致，以及当前价格是否已充分定价。`;
      actions = ["检查价格分位，避免在情绪极端处加仓", "设定失效条件与止损位置"];
    } else {
      severity = "info"; title = "技术面中性，与基本面无明显冲突";
      detail = `盘面信号为${techLabel}（评分 ${techScore}），未与基本面倾向「${rec.bias}」形成反向。盘面信号弱时由基本面主导方向，但需降低操作频率与置信度。`;
      actions = ["等待技术面信号确认或基本面出现边际变化"];
    }

    if (rec.confidence === "低" && severity === "ok") {
      severity = "warn"; title = "置信度偏低：结论按低置信度处理";
      detail = "基本面记录标记为低置信度，输出应强调条件触发与失效条件，而不是直接给方向。";
    }

    $("diagnoseCard").innerHTML = `
      <div class="diag-head">
        <div class="diag-badge ${severity}">${severity === "warn" ? "!" : severity === "ok" ? "✓" : "i"}</div>
        <div class="diag-title">${title}</div>
      </div>
      <div class="diag-detail">${detail}</div>
      ${actions.length ? `<ul class="diag-actions">${actions.map(a => `<li>${a}</li>`).join("")}</ul>` : ""}
      <div class="diag-cmp">
        <div class="side"><div class="k">技术面</div><div class="v" style="color:${bullTech ? UP : bearTech ? DOWN : "#5b6675"}">${techLabel}</div>
          <div class="k">评分 ${techScore > 0 ? "+" : ""}${techScore} · 分位 ${fmt(sig.stats.percentile, 1)}%</div></div>
        <div class="side"><div class="k">客观基本面</div><div class="v" style="color:${bullAuto ? UP : bearAuto ? DOWN : "#5b6675"}">${autoLabel || "未采集"}</div>
          <div class="k">${auto ? `评分 ${autoScore > 0 ? "+" : ""}${autoScore} · ${auto.items.length} 条读数` : "点「重新采集」获取"}</div></div>
        <div class="side"><div class="k">人工判断</div><div class="v" style="color:${bullF ? UP : bearF ? DOWN : "#5b6675"}">${rec.bias || "未判定"}</div>
          <div class="k">${rec.horizon || "时间边界未界定"} · 置信度${rec.confidence || "未定"}</div></div>
      </div>`;
  }

  // ---------------- 期限结构 ----------------
  // 期限结构曲线：横轴是合约月份，纵轴是价格，连成一条"这条线"
  function buildTermOption(data, opt) {
    const o = opt || {};
    const rows = (data.rows || []).slice().sort((a, b) => a.ym - b.ym);
    const cats = rows.map(r => o.fullLabel ? r.contract : r.contract.replace(/^[A-Za-z]+/, ""));
    const inCurve = r => !r.expired && r.liquid !== false;
    // 只有进入曲线的合约才画点：摘牌陈价动辄偏离几千点（CU2610 停在 109600），
    // 画上去会把 y 轴拉爆、曲线压成一条平线。剔除清单在图下方单独说明。
    const priceData = rows.map(r => (inCurve(r) ? r.close : null));

    const main = data.main || {};
    const spreadData = rows.map(r => {
      if (!inCurve(r) || !main.close) return null;
      return r.close - main.close;
    });

    // 形态配色：升水偏紧=红，贴水偏松=绿，走平=蓝
    const kind = data.shape_kind;
    const lineColor = kind === "backwardation" ? UP : kind === "contango" ? DOWN : ACCENT;

    const idxOf = c => rows.findIndex(r => r.contract === c);
    const marks = [];
    const used = {};
    // 主力优先占位：主力=近月时（常见），若先标近月会把主力标记挤掉
    const mk = (c, name, color, pos) => {
      const i = idxOf(c);
      if (i < 0 || !inCurve(rows[i])) return;
      if (used[i]) return;
      used[i] = true;
      marks.push({
        name, coord: [i, rows[i].close], value: rows[i].close,
        symbol: "circle", symbolSize: 11,
        itemStyle: { color, borderColor: "#fff", borderWidth: 1.5 },
        label: { show: true, formatter: name, position: pos || "top", fontSize: o.fs || 10,
                 color: "#fff", backgroundColor: color, padding: [2, 4], borderRadius: 3 },
      });
    };
    mk(main.contract, "主力", ACCENT, "bottom");
    mk(data.front && data.front.contract, "近月", WARM, "top");
    mk(data.deferred && data.deferred.contract, "远月", "#7b8794", "top");

    const fs = o.fs || 10;
    const left = o.fs === 10 ? 44 : 56;
    const right = o.fs === 10 ? 46 : 60;
    return {
      animation: false,
      grid: [
        { left, right, top: 26, height: o.mainH },
        { left, right, top: o.mainH + 52, height: o.subH },
      ],
      legend: { show: false },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      graphic: [{
        type: "text", left: 4, top: 2,
        style: {
          text: `${shapeTitle(data)}　${shapeHint(data)}`,
          fill: lineColor, fontSize: o.fullLabel ? 12 : 10, fontWeight: 600,
        },
      }],
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross", label: { backgroundColor: "#5b6675" } },
        backgroundColor: "rgba(255,255,255,.96)", borderColor: "#e4e8ee", borderWidth: 1,
        textStyle: { color: "#1f2933", fontSize: 11 },
        formatter: (params) => {
          const p = params[0];
          const i = p.dataIndex;
          const r = rows[i];
          if (!r) return "";
          const tag = r.expired ? "（已摘牌，不参与月差）" : (r.liquid === false ? "（流动性不足，不参与月差）" : "");
          const sp = (inCurve(r) && main.close) ? (r.close - main.close) : null;
          const spTxt = sp === null ? "—" : `${sp > 0 ? "+" : ""}${sp.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
          return `<b>${r.contract}</b> ${tag}<br/>`
            + `收盘：${r.close.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}<br/>`
            + `对主力月差：${spTxt}<br/>`
            + `成交量：${(r.volume || 0).toLocaleString("zh-CN")} 手<br/>`
            + `持仓量：${(r.open_interest || 0).toLocaleString("zh-CN")} 手`;
        },
      },
      xAxis: [
        { type: "category", data: cats, gridIndex: 0, boundaryGap: false,
          axisLine: { lineStyle: { color: "#e4e8ee" } },
          axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        { type: "category", data: cats, gridIndex: 1, boundaryGap: false,
          axisLine: { lineStyle: { color: "#e4e8ee" } },
          axisLabel: { color: "#8a95a3", fontSize: fs, hideOverlap: true, margin: 10 },
          axisTick: { show: false }, splitLine: { show: false } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, scale: true, position: "right",
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: "#8a95a3", fontSize: fs - 1, hideOverlap: true },
          splitLine: { lineStyle: { color: "#eef1f5" } },
          name: "价格", nameTextStyle: { color: "#8a95a3", fontSize: fs - 1 },
          nameGap: 10, nameLocation: "end" },
        { type: "value", gridIndex: 1, scale: false, position: "left",
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: "#8a95a3", fontSize: fs - 1, hideOverlap: true,
            formatter: (v) => (v > 0 ? "+" : "") + v },
          splitLine: { lineStyle: { color: "#eef1f5" } },
          name: "月差", nameTextStyle: { color: "#8a95a3", fontSize: fs - 1 },
          nameGap: 10, nameLocation: "end" },
      ],
      series: [
        {
          name: "期限价格", type: "line", xAxisIndex: 0, yAxisIndex: 0,
          data: priceData, connectNulls: false, smooth: false,
          symbol: "circle", symbolSize: 6,
          lineStyle: { width: 2, color: lineColor },
          itemStyle: { color: lineColor, borderColor: "#fff", borderWidth: 1 },
          areaStyle: { color: "transparent" },
          markPoint: { data: marks, silent: false },
          z: 3,
        },
        {
          name: "对主力月差", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: spreadData.map(v => (v === null ? null : { value: v,
            itemStyle: { color: v > 0 ? UP : v < 0 ? DOWN : "#b8c1cc" } })),
          barMaxWidth: 22, z: 1,
        },
      ],
    };
  }

  function hexToRgba(hex, a) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  // 与后端 term_structure 一致的"进入曲线"判定
  function inTermCurve(r) { return !r.expired && r.liquid !== false; }

  // 图上的形态标题与方向提示
  function shapeTitle(data) {
    const k = data.shape_kind;
    if (k === "backwardation") return "近月升水 Backwardation";
    if (k === "contango") return "近月贴水 Contango";
    if (k === "flat") return "平坦 Flat";
    return "形态不明";
  }
  function shapeHint(data) {
    const ann = data.annualized_pct;
    if (ann === null || ann === undefined) return "";
    const arrow = data.shape_kind === "backwardation" ? "↘ 曲线向下倾斜"
                : data.shape_kind === "contango" ? "↗ 曲线向上倾斜" : "→ 曲线基本走平";
    return `${arrow}　年化 ${ann > 0 ? "+" : ""}${ann}%`;
  }

  async function loadTermStructure() {
    if (!state.meta) return;
    $("termShape").innerHTML = "正在计算月差…";
    try {
      const data = await api(`/api/term-structure?code=${state.meta.code}`);
      if (data.error) { $("termShape").innerHTML = data.error; return; }
      state.term = data;

      const mainC = data.main.contract;
      const frontC = data.front ? data.front.contract : "—";
      const farC = data.deferred ? data.deferred.contract : "—";
      const num = (v, n) => (v === null || v === undefined) ? "--" : fmt(v);
      $("termShape").innerHTML = `
        <div class="ts-shape-main">${esc(data.shape)}</div>
        <div class="sub">近月 ${esc(frontC)} ↔ 远月 ${esc(farC)} ·
          价差 ${num(data.curve_spread)} 元 ·
          幅度 ${num(data.curve_pct)}% ·
          跨 ${data.months_gap || "--"} 个月 ·
          年化 ${num(data.annualized_pct)}%</div>
        <div class="sub">主力 ${esc(mainC)} ·
          主力-远月 ${num(data.far_spread)} 元</div>`;

      // ---- 期限结构曲线（面板内小图）----
      if (!state.charts.term) state.charts.term = echarts.init($("termChart"));
      state.charts.term.setOption(buildTermOption(data, { mainH: 116, subH: 40, fs: 10 }), true);
      state.charts.term.resize();

      // 图例：把"线"讲清楚
      const kindCls = data.shape_kind === "backwardation" ? "sw-up"
                    : data.shape_kind === "contango" ? "sw-down" : "sw-flat";
      const exclRows = data.rows.filter(r => !inTermCurve(r));
      $("termLegend").innerHTML = `
        <span class="lg"><i class="sw ${kindCls}"></i>期限价格曲线</span>
        <span class="lg"><i class="sw sw-main"></i>主力</span>
        <span class="lg"><i class="sw sw-near"></i>近月</span>
        <span class="lg"><i class="sw sw-far"></i>远月</span>
        <span class="lg">下方柱：对主力月差</span>`;
      $("termExcl").innerHTML = exclRows.length
        ? `未入曲线：${exclRows.map(r =>
            `${r.contract}（${r.expired ? "已摘牌" : "流动性不足"}，收盘 ${fmt(r.close, 0)}）`).join("、")}`
        : "";
      $("termExcl").hidden = !exclRows.length;

      // 通俗解释：概念 → 代入数字 → 注意事项
      const ex = $("termExplain");
      if (data.concept) {
        const kindCls2 = data.shape_kind === "backwardation" ? "ts-up"
                      : data.shape_kind === "contango" ? "ts-down" : "ts-flat";
        ex.innerHTML = `
          <div class="ts-ex-block ${kindCls2}">
            <div class="ts-ex-t">这条线是什么意思</div>
            <div class="ts-ex-b">${esc(data.concept)}</div>
          </div>
          <div class="ts-ex-block ts-read">
            <div class="ts-ex-t">这个品种现在说明了什么</div>
            <div class="ts-ex-b">${esc(data.reading)}</div>
          </div>
          <div class="ts-ex-block ts-caution">
            <div class="ts-ex-t">读这条线要注意</div>
            <div class="ts-ex-b">${esc(data.caution)}</div>
          </div>`;
      } else {
        ex.innerHTML = "";
      }

      const rows = data.rows.map(r => {
        let cls2 = "";
        if (r.contract === mainC) cls2 = "is-main";
        else if (data.near && r.contract === data.near.contract) cls2 = "is-near";
        else if (r.contract === farC) cls2 = "is-far";
        const spread = r.close - data.main.close;
        let thin = "";
        if (r.expired) thin = '<span style="color:#a9b3bf;font-size:10px;"> 已摘牌</span>';
        else if (r.liquid === false) thin = '<span style="color:#a9b3bf;font-size:10px;"> 流动性不足</span>';
        return `<tr class="${cls2}">
          <td>${r.contract}${thin}</td>
          <td>${fmt(r.close)}</td>
          <td style="color:${spread > 0 ? UP : spread < 0 ? DOWN : "#8a95a3"}">${spread > 0 ? "+" : ""}${fmt(spread)}</td>
          <td>${fmtInt(r.volume)}</td>
          <td>${fmtInt(r.open_interest)}</td>
        </tr>`;
      }).join("");

      $("termTable").innerHTML = `<table class="term-table">
        <thead><tr><th>合约</th><th>收盘</th><th>对主力月差</th><th>成交量</th><th>持仓量</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
      $("termFoot").textContent = (data.note || "") + " · 数据日期 " + (data.main.date || "--");
    } catch (err) {
      $("termShape").innerHTML = "期限结构获取失败：" + err.message;
    }
  }

  // 期限结构大图弹窗
  function openTermModal() {
    const data = state.term;
    if (!data) { toast("期限结构数据尚未就绪，请先切到「期限结构」标签"); return; }
    $("termModal").hidden = false;
    if (!state.charts.termModal) state.charts.termModal = echarts.init($("termModalChart"));
    state.charts.termModal.setOption(
      buildTermOption(data, { mainH: 276, subH: 88, fs: 12, fullLabel: true }), true);
    state.charts.termModal.resize();
    $("termModalTitle").textContent =
      `${state.meta ? state.meta.name : ""} ${state.meta ? state.meta.code : ""} · 期限结构曲线`;
    const exclRows = data.rows.filter(r => !inTermCurve(r));
    $("termModalFoot").innerHTML =
      (exclRows.length
        ? `<div class="term-excl-line">未入曲线：${exclRows.map(r =>
            `${r.contract}（${r.expired ? "已摘牌" : "流动性不足"}，收盘 ${fmt(r.close, 0)}）`).join("、")}</div>`
        : "")
      + `<div>${(data.note || "") + " · 数据日期 " + (data.main.date || "--")}</div>`;
  }

  function closeTermModal() {
    $("termModal").hidden = true;
  }

  // ---------------- 自选行 ----------------
  async function loadWatchRow() {
    const codes = ["RB", "HC", "I", "CU", "AL", "SC", "M", "Y", "AU", "TA"];
    try {
      const data = await api("/api/quote?codes=" + codes.join(","));
      $("watchRow").innerHTML = data.rows.map(r => {
        if (r.error) return `<div class="watch-item"><div class="k">${r.code}</div><div class="v" style="color:#8a95a3">--</div></div>`;
        return `<div class="watch-item" data-code="${r.code}" style="cursor:pointer">
          <div class="k">${r.name}</div>
          <div class="v">${fmt(r.close)}<span class="d ${cls(r.change_pct)}">${r.change_pct > 0 ? "+" : ""}${fmt(r.change_pct)}%</span></div>
        </div>`;
      }).join("");
      $("watchRow").querySelectorAll(".watch-item[data-code]").forEach(el => {
        el.addEventListener("click", () => {
          const meta = state.symbols.find(s => s.code === el.dataset.code);
          if (meta) { $("symbolSelect").value = meta.code; loadSymbol(meta); }
        });
      });
    } catch (e) { /* 静默 */ }
  }

  // ---------------- AI 研判（云服务 LLM，浏览器直连） ----------------
  // 模型调用必须由前端发起：云服务规范禁止把 /.cloud/llm/* 挂到后端做代理。
  const CLOUD = {
    endpoint: "https://finresearch-90334.app.workbuddy.host",
    publishableKey: "wbpk_Bmg2gymNxbM5tyjmnNrbs1_CCPGTy4vAoxJ8haPwlfHf78aSmE0GBiJ",
  };
  // 优先级：先选推理质量更好的，再退到均衡/自动
  const MODEL_PRIORITY = ["space-bunny", "deepseek-v4-pro", "glm-5.3-flash",
                          "balanced-model", "default", "auto"];

  let cloudClient = null;
  let modelCache = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  async function postJSON(path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function getCloud() {
    if (cloudClient) return cloudClient;
    if (typeof WorkBuddyCloud === "undefined") {
      throw new Error("云服务 SDK 未加载，无法调用大模型");
    }
    cloudClient = WorkBuddyCloud.createWorkBuddyCloud({
      endpoint: CLOUD.endpoint,
      publishableKey: CLOUD.publishableKey,
    });
    return cloudClient;
  }

  async function getModels() {
    if (modelCache) return modelCache;
    const cloud = await getCloud();
    const list = await cloud.llm.models.list();
    modelCache = (list || []).filter(m => m.disabled !== true);
    if (!modelCache.length) throw new Error("模型目录为空，无法调用");
    return modelCache;
  }

  function pickModel(models) {
    for (const id of MODEL_PRIORITY) {
      const m = models.find(x => x.id === id);
      if (m) return m;
    }
    return models[0];
  }

  function aiSection(title, body, kind) {
    if (!body) return "";
    return `<div class="ai-sec ${kind || ""}">
      <div class="ai-sec-t">${esc(title)}</div>
      <div class="ai-sec-b">${body}</div>
    </div>`;
  }

  // 校验模型输出是否与本次数据自洽。模型是概率生成，尤其在数据有缺口时
  // 会把「未获取」臆写成 0 手、把客观评分改写为「中性 0 分」，必须显式提示。
  function validateAIOutput(obj, meta) {
    const warnings = [];
    if (!meta) return warnings;
    const text = [obj.observed, obj.analysis, obj.process, obj.conclusion, obj.caveats]
      .filter(Boolean).join(" ");
    const flat = text.replace(/,/g, "");
    const probes = [...new Set((meta.key_numbers || [])
      .map(k => String(k).replace(/,/g, "").replace(/^-/, ""))
      .filter(p => p.length >= 3))];
    let missing = 0;
    probes.forEach(p => { if (!flat.includes(p)) missing++; });
    if (probes.length >= 3 && missing >= 2) {
      warnings.push(`模型未完整复述关键读数（仅 ${probes.length - missing}/${probes.length} 项命中），请以面板数字为准`);
    }
    const snap = meta.snapshot || {};
    const auto = snap.auto_signal || {};
    if (auto.score != null && auto.score !== 0 && auto.label !== "中性" &&
        /评分\s*为?\s*0|评分\s*0|倾向为?中性|结论.{0,8}中性/.test(text)) {
      warnings.push(`模型给出的倾向/评分与客观评分「${auto.label}（评分 ${auto.score}）」冲突`);
    }
    // 本次采集里若没有任何 0 值读数，模型写「0 手 / 变化为 0」就是臆造。
    // 不能因「本次有数据缺口」而跳过这条检查——数据有缺口时模型最容易把「未获取」补成 0。
    const hasZeroReading = (meta.key_numbers || []).some(k => {
      const s = String(k).replace(/[,\s-]/g, "");
      return /^0+(\.0+)?$/.test(s);
    });
    if (!hasZeroReading &&
        /(?:为|是|均为|变化为)\s*0\s*手|0\s*手.{0,12}(?:库存|仓单|基差)/.test(text)) {
      warnings.push(`模型把读数写成了 0 手，而本次采集没有 0 值读数`);
    }
    return warnings;
  }

  function aiDataBadge(meta) {
    if (!meta) return "";
    const auto = (meta.snapshot || {}).auto_signal || {};
    const score = auto.score == null ? "" : ` ${auto.score > 0 ? "+" : ""}${auto.score}`;
    return `<div class="ai-data-badge">数据基准：采集于 ${esc(meta.fetched_at || "—")}` +
      ` · 客观评分 ${esc(auto.label || "—")}${esc(score)} · 指纹 ${esc(meta.fingerprint || "—")}` +
      `<span>本研判仅允许引用上述采集数据</span></div>`;
  }

  // 模型输出是不可信文本，必须转义后再进 DOM
  function renderAIResult(el, obj, raw, meta) {
    const sec = aiSection;
    let html = "";
    const warnings = validateAIOutput(obj, meta);
    if (warnings.length) {
      html += `<div class="ai-warn"><b>⚠ 研判与本次数据不自洽</b><ul>` +
        warnings.map(w => `<li>${esc(w)}</li>`).join("") +
        `</ul><span>建议点「生成研判」重试；面板读数始终为准。</span></div>`;
    }
    html += aiDataBadge(meta);
    html += sec("我看到的数据", esc(obj.observed), "s-obs");
    html += sec("分析", esc(obj.analysis), "s-ana");
    html += sec("分析过程", esc(obj.process), "s-proc");
    html += sec("结论", esc(obj.conclusion), "s-conc");
    if (Array.isArray(obj.actions) && obj.actions.length) {
      html += sec("行动建议", "<ul class=\"ai-ul\">" +
        obj.actions.map(a => `<li>${esc(a)}</li>`).join("") + "</ul>", "s-act");
    }
    html += sec("口径与局限", esc(obj.caveats), "s-cav");
    if (!html) {
      html = `<div class="ai-sec"><div class="ai-sec-b">模型未返回可解析的内容，原始输出见下方。</div></div>`;
    }
    if (raw) {
      html += `<details class="ai-raw"><summary>原始输出</summary><pre>${esc(raw)}</pre></details>`;
    }
    el.innerHTML = html;
  }

  function parseAIText(raw) {
    let t = (raw || "").trim();
    // 去掉模型偶尔加的 markdown 围栏
    t = t.replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/i, "").trim();
    const s = t.indexOf("{");
    const e = t.lastIndexOf("}");
    if (s >= 0 && e > s) t = t.slice(s, e + 1);
    return JSON.parse(t);
  }

  async function runAI(scope) {
    const btn = $(scope === "diagnose" ? "aiDiagBtn" : "aiFundBtn");
    const body = $(scope === "diagnose" ? "aiDiagBody" : "aiFundBody");
    if (!state.meta) { toast("请先选择品种"); return; }
    if (btn.disabled) return;

    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "生成中…";
    body.classList.remove("ai-stale");
    body.querySelectorAll(".ai-stale-bar").forEach(n => n.remove());
    body.innerHTML = `<div class="ai-loading"><span class="ai-spin"></span>正在读取上下文并调用模型…</div>`;
    let raw = "";

    try {
      const payload = { symbol: state.meta.code, scope };
      // 诊断用前端当前填写的决策面板（未保存也能诊断）
      if (scope === "diagnose") payload.rec = collectResearch(false);
      // 直接把「面板当前显示的那一份」传给后端，避免后端二次采集导致读数漂移
      const fund = state.fundamentals;
      if (fund && !fund.error && fund.symbol === payload.symbol) payload.fund = fund;

      const ctx = await postJSON("/api/ai-context", payload);
      if (ctx.error) throw new Error(ctx.error);
      state.aiMeta[scope] = {
        fingerprint: ctx.data_fingerprint,
        fetched_at: ctx.fetched_at,
        key_numbers: ctx.key_numbers || [],
        snapshot: ctx.data_snapshot || {},
        source: ctx.fundamentals_source,
      };

      const models = await getModels();
      const model = pickModel(models);

      const cloud = await getCloud();
      const stream = cloud.llm.chat.completions.create({
        model: model.id,
        messages: [
          { role: "system", content: ctx.system },
          { role: "user", content: ctx.user },
        ],
        stream: true,
        response_format: { type: "json_object" },
        temperature: 0.3,
      });

      let ticks = 0;
      for await (const chunk of stream) {
        const d = chunk.choices?.[0]?.delta?.content;
        if (!d) continue;
        raw += d;
        // 流式过程：先显示原文，让用户看到生成过程
        if (++ticks % 3 === 0) {
          body.innerHTML = `<div class="ai-stream"><pre>${esc(raw)}</pre></div>`;
        }
      }

      let obj;
      try {
        obj = parseAIText(raw);
      } catch (e) {
        body.innerHTML = `<div class="ai-sec"><div class="ai-sec-b">模型输出不是合法 JSON，已保留原文：</div></div>
          <details class="ai-raw" open><summary>原始输出</summary><pre>${esc(raw)}</pre></details>`;
        toast("模型输出解析失败", 3000);
        return;
      }
      renderAIResult(body, obj, raw, state.aiMeta[scope]);
      // 生成期间面板可能已重新采集，落地后再比对一次
      syncAIStaleness(state.fundamentals);
      const tag = `${esc(model.name || model.id)} · 数据来自于盘立方：${ctx.fundamentals_source === "panel" ? "面板当前数据" : "后端重新采集"}`;
      body.insertAdjacentHTML("afterbegin", `<div class="ai-model">${tag}</div>`);
    } catch (err) {
      const code = err?.error?.code || "";
      let msg = err?.error?.message || err.message || String(err);
      let hint = "";
      let netBtn = "";
      if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
        msg = "浏览器无法连接云服务";
        hint = "多为本机代理对云服务域名的分流问题——点下面的「立即诊断」可看到浏览器实际走的链路与修复指令。";
        netBtn = `<button class="mini-btn ai-net-fix-btn" data-act="netcheck" type="button">立即诊断</button>`;
      } else if (code.startsWith("quota_")) {
        msg = "模型配额不足或被限流：" + msg;
        hint = "稍候重试，或改用其他模型。";
      } else if (code.startsWith("auth_")) {
        msg = "鉴权失败：" + msg;
        hint = "请重新载入页面以刷新云服务凭据。";
      } else if (code.startsWith("gateway_") || code.startsWith("model_")) {
        msg = "模型服务暂时不可用：" + msg;
        hint = "可稍后重试。";
      } else if (code === "request_stream_required") {
        msg = "调用参数不合规：" + msg;
      }
      body.innerHTML = `<div class="ai-err"><b>生成失败</b><br>${esc(msg)}` +
        (hint ? `<div class="ai-err-hint">${esc(hint)}${netBtn}</div>` : "") + `</div>`;
      body.querySelectorAll('[data-act="netcheck"]').forEach(b =>
        b.addEventListener("click", runNetCheck));
      toast("AI 生成失败", 3000);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ---------------- 网络自诊断 ----------------
  // 浏览器到云服务的链路受系统代理影响，而浏览器只会报 "Failed to fetch"。
  // 后端复刻浏览器的代理决策并实测两条链路，给出可直接照做的修复指令。
  async function runNetCheck() {
    const card = $("aiNetCard");
    const btns = [...document.querySelectorAll('[data-act="netcheck"]')];
    if (!card) return;
    btns.forEach(b => { b.disabled = true; });
    card.hidden = false;
    card.innerHTML = '<div class="ai-loading"><span class="ai-spin"></span>正在检测浏览器到云服务的链路…</div>';
    try {
      const res = await fetch("/api/net-check?endpoint=" + encodeURIComponent(CLOUD.endpoint));
      const d = await res.json();
      if (d.error) {
        card.innerHTML = `<div class="ai-err">检测失败：${esc(d.error)}</div>`;
        return;
      }
      const tag = (ok) => `<b class="${ok ? "ok" : "bad"}">${ok ? "通" : "不通"}</b>`;
      card.innerHTML =
        `<div class="ai-net-head">
           <span class="${d.browser_ok ? "ok" : "bad"}">${d.browser_ok ? "链路正常" : "链路不通"}</span>
           <span class="ai-net-route">${esc(d.browser_route)}</span>
           <button class="mini-btn ai-net-close" id="aiNetClose" type="button">关闭</button>
         </div>
         <div class="ai-net-rows">
           <div><span>直连</span>${tag(d.direct_ok)}<i>${esc(d.direct_msg)}</i></div>
           <div><span>系统代理</span>${tag(d.proxy_ok)}<i>${esc(d.proxy_msg)}</i></div>
         </div>` +
        (d.fix ? `<pre class="ai-net-fix">${esc(d.fix)}</pre>` : "");
      $("aiNetClose").addEventListener("click", () => { card.hidden = true; });
    } catch (e) {
      card.innerHTML = `<div class="ai-err">检测请求本身失败：${esc(e.message)}` +
        `<div class="ai-err-hint">请确认后端服务在运行（http://127.0.0.1:8765）。</div></div>`;
    } finally {
      btns.forEach(b => { b.disabled = false; });
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
