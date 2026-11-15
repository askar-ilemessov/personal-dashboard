/* Client behaviour: charts, command palette, undo toast, shortcuts, theme.
   Loaded once; pages are swapped in by HTMX (hx-boost), never fully reloaded. */

// --- formatting (mirrors tracking/fmt.py) -----------------------------------

function fmtValue(kind, v, short) {
  if (v === null || v === undefined) return "not logged";
  if (kind === "minutes") {
    const total = Math.round(v), h = Math.floor(total / 60), m = total % 60;
    if (short) return h ? (m ? (total / 60).toFixed(1) + "h" : h + "h") : m + "m";
    return h && m ? `${h}h ${String(m).padStart(2, "0")}m` : h ? `${h}h` : `${m}m`;
  }
  if (kind === "money") {
    return "$" + v.toLocaleString("en-CA", { minimumFractionDigits: short ? 0 : 2, maximumFractionDigits: short ? 0 : 2 });
  }
  if (kind === "percent") return Math.round(v * 100) + "%";
  if (kind === "clock" || kind === "bedtime") {  // minutes after midnight, or after noon for bedtimes
    const total = Math.round(v) + (kind === "bedtime" ? 720 : 0);
    return String(Math.floor(total / 60) % 24).padStart(2, "0") + ":" + String(((total % 60) + 60) % 60).padStart(2, "0");
  }
  return String(Math.round(v * 100) / 100);
}

// --- charts ------------------------------------------------------------------

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const goalLine = {
  id: "goalLine",
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || opts.value == null) return;
    const { ctx, chartArea: area, scales } = chart;
    const y = scales.y.getPixelForValue(opts.value);
    if (y < area.top || y > area.bottom) return;
    ctx.save();
    ctx.strokeStyle = opts.color;
    ctx.fillStyle = opts.color;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(area.left, y);
    ctx.lineTo(area.right, y);
    ctx.stroke();
    ctx.font = "11px " + cssVar("--font");
    ctx.textAlign = "right";
    ctx.fillText("Goal: " + opts.label, area.right - 4, y - 5);
    ctx.restore();
  },
};

function buildChart(canvas) {
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();
  const c = JSON.parse(canvas.dataset.chart);
  const accent = cssVar("--accent"), previous = cssVar("--series-previous");
  const text2 = cssVar("--text-2"), border = cssVar("--border"), surface = cssVar("--surface");
  if (c.type === "scatter") return buildScatter(canvas, c, { accent, text2, border, surface });
  const isLine = c.type === "line";

  // A logged day between two unlogged ones would vanish on a line with no
  // points, so isolated values get a visible marker.
  const lonely = (ctx) => {
    const d = ctx.dataset.data, i = ctx.dataIndex;
    return d[i] != null && d[i - 1] == null && d[i + 1] == null ? 3 : 0;
  };

  const datasets = [
    isLine
      ? { label: c.name, data: c.data, borderColor: accent, backgroundColor: accent, borderWidth: 2, tension: 0.25,
          pointRadius: lonely, pointHoverRadius: 5, pointHoverBorderColor: surface, pointHoverBorderWidth: 2, spanGaps: false, order: 1 }
      : { label: c.name, data: c.data, backgroundColor: accent, borderRadius: { topLeft: 4, topRight: 4 },
          maxBarThickness: 22, categoryPercentage: 0.82, barPercentage: 0.9, order: 1 },
  ];
  if (c.previous) {
    datasets[0].label = "This period";
    datasets.push({ type: "line", label: "Previous period", data: c.previous, borderColor: previous, backgroundColor: previous,
      borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 4, tension: 0.25, spanGaps: false, order: 0 });
  }

  const goalMax = c.goal ? c.goal.value * 1.15 : undefined;
  // Durations get whole-hour gridlines instead of 6.7h, 3.3h.
  let step;
  if (c.fmt === "minutes") {
    const top = Math.max(goalMax || 0, ...c.data.filter((v) => v != null), ...(c.previous || []).filter((v) => v != null));
    step = top <= 90 ? 15 : top <= 240 ? 60 : top <= 720 ? 120 : 240;
  }
  new Chart(canvas, {
    type: c.type,
    data: { labels: c.labels, datasets },
    plugins: [goalLine],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: !!c.previous, position: "top", align: "end", reverse: true,
                  labels: { color: text2, boxWidth: 18, boxHeight: 2, font: { size: 11 } } },
        tooltip: {
          callbacks: { label: (item) => `${item.dataset.label}: ${fmtValue(c.fmt, item.parsed.y)}` },
        },
        goalLine: c.goal ? { value: c.goal.value, label: c.goal.label, color: text2 } : {},
      },
      scales: {
        x: { grid: { display: false }, border: { color: border },
             ticks: { color: text2, maxRotation: 0, autoSkip: true, maxTicksLimit: c.compact ? 4 : 9, font: { size: 11 } } },
        y: { beginAtZero: !isLine || c.fmt === "minutes", max: c.max || undefined, suggestedMax: goalMax,
             grid: { color: border }, border: { display: false },
             ticks: { color: text2, maxTicksLimit: c.compact ? 4 : 8, stepSize: step, font: { size: 11 },
                      callback: (v) => fmtValue(c.fmt, v, true) } },
      },
    },
  });
}

function buildScatter(canvas, c, color) {
  // Time-based axes get gridlines on whole hours (or half hours for short spans).
  const step = (kind, key) => {
    if (!["minutes", "clock", "bedtime"].includes(kind)) return undefined;
    const values = c.points.map((p) => p[key]);
    const span = Math.max(...values) - Math.min(...values);
    return span <= 120 ? 30 : span <= 480 ? 60 : 120;
  };
  const axis = (name, kind, key) => ({
    title: { display: true, text: name, color: color.text2, font: { size: 11 } },
    grid: { color: color.border }, border: { display: false },
    ticks: { color: color.text2, maxTicksLimit: 8, stepSize: step(kind, key), font: { size: 11 },
             callback: (v) => fmtValue(kind, v, true) },
  });
  new Chart(canvas, {
    type: "scatter",
    data: { datasets: [{ label: c.name, data: c.points, backgroundColor: color.accent, borderColor: color.surface,
                         borderWidth: 1.5, pointRadius: 4.5, pointHoverRadius: 6 }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (item) =>
          `${c.xname}: ${fmtValue(c.xfmt, item.parsed.x)} · ${c.yname}: ${fmtValue(c.yfmt, item.parsed.y)}` } },
      },
      scales: { x: axis(c.xname, c.xfmt, "x"), y: axis(c.yname, c.yfmt, "y") },
    },
  });
}

function buildCharts(root) {
  if (!window.Chart) return;
  for (const chart of Object.values(Chart.instances)) {
    if (!chart.canvas || !chart.canvas.isConnected) chart.destroy();
  }
  (root || document).querySelectorAll("canvas[data-chart]").forEach(buildChart);
}

// --- theme -------------------------------------------------------------------

function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* private mode */ }
  buildCharts(document);
}

// --- events from the server --------------------------------------------------

function csrfToken() {
  try { return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRFToken"]; } catch (e) { return ""; }
}

/** POST outside HTMX (toast buttons) and replay the HX-Trigger events. */
async function post(url) {
  const response = await fetch(url, { method: "POST", headers: { "X-CSRFToken": csrfToken() } });
  const header = response.headers.get("HX-Trigger");
  if (header) {
    for (const [name, detail] of Object.entries(JSON.parse(header))) {
      document.body.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
    }
  }
}

document.addEventListener("alpine:init", () => {
  Alpine.data("palette", () => ({
    open: false,
    show() {
      this.open = true;
      this.$nextTick(() => { this.$refs.input.focus(); this.$refs.input.select(); });
    },
    hide() {
      if (!this.open) return;
      this.open = false;
      this.$refs.input.value = "";
      htmx.trigger(this.$refs.input, "palette-reset");
    },
  }));

  Alpine.data("toast", () => ({
    visible: false, message: "", error: false, op: null, timer: null,
    show(detail) {
      this.message = detail.message;
      this.error = !!detail.error;
      this.op = detail.op || null;
      this.visible = true;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => { this.visible = false; }, this.op ? 8000 : 4500);
    },
    undo() {
      const op = this.op;
      this.op = null;
      post(`/undo/${op}/`);
    },
  }));
});

// --- keyboard ----------------------------------------------------------------

let pendingG = 0;
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    window.dispatchEvent(new CustomEvent("palette-open"));
    return;
  }
  const el = e.target;
  const typing = el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") {
    e.preventDefault();
    window.dispatchEvent(new CustomEvent("palette-open"));
  } else if (e.key === "g") {
    pendingG = Date.now();
  } else if (Date.now() - pendingG < 1200) {
    pendingG = 0;
    const link = document.querySelector(`.sidebar nav a[data-key="${CSS.escape(e.key)}"]`);
    if (link) link.click();
  }
});

// --- boot --------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  buildCharts(document);
  htmx.onLoad((el) => buildCharts(el.querySelector ? el.parentNode || el : document));
});
