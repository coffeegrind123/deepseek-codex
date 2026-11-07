// codex-workspace: the DeepSeek balance beside a DeepSeek model in the composer's pill,
// "deepseek-flash · High · $12.34" (README "DeepSeek balance"). Loaded from dist/index.html
// (scripts/ovis_patch.py); a file of its own because Ovis's CSP is script-src 'self'.
//
// The pill is React's. This adds one span after its `.composer-reasoning` and puts it back
// whenever React re-renders the pill, so nothing in the bundle is edited. The span is shown only
// where the pill's model (the `.composer-model` title, the full model id) names DeepSeek.
// Values come from server/deepseek-balance.ts, which keeps the API key to itself.
(function () {
  "use strict";

  const ENDPOINT = "/api/deepseek/balance";
  const POLL_MS = 60000;
  /** Below this the amount turns the warning colour, as Ovis's usage meter does at 80%. */
  const LOW_USD = 2;
  const CLASS = "cw-balance";
  const DEEPSEEK = /deepseek/i;

  /** "12.3456" -> "$12.35"; anything not a plain decimal is refused. */
  function formatAmount(total) {
    if (typeof total !== "string" || !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(total)) return null;
    const value = Number(total);
    if (!Number.isFinite(value)) return null;
    return "$" + value.toFixed(2);
  }

  /** What the span shows for one server reading, or null when it should not be shown. */
  function describe(reading, nowMs) {
    if (!reading || !reading.balance) return null;
    const b = reading.balance;
    const text = formatAmount(b.total_usd);
    if (text === null) return null;
    const low = !b.available || Number(b.total_usd) < LOW_USD;
    const lines = ["DeepSeek balance: " + text + " USD (granted $" + b.granted_usd + ", topped up $" + b.topped_up_usd + ")"];
    if (!b.available) lines.push("API access unavailable");
    if (reading.fetched_at) {
      const age = Math.max(0, Math.round(((nowMs || Date.now()) - Date.parse(reading.fetched_at)) / 60000));
      lines.push(reading.stale ? "Stale: last read " + age + " min ago" : age === 0 ? "Updated just now" : "Updated " + age + " min ago");
    }
    return { text: text, title: lines.join("\n"), low: low, stale: Boolean(reading.stale) };
  }

  function isDeepSeek(model) {
    return typeof model === "string" && DEEPSEEK.test(model);
  }

  globalThis.__codexWorkspaceBalance = { formatAmount, describe, isDeepSeek, LOW_USD };
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---- browser wiring -------------------------------------------------------------------

  const STYLE = [
    "." + CLASS + "{flex:none;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--text-dim)}",
    "." + CLASS + "-dot{margin-right:var(--space-2)}",
    "." + CLASS + ".is-low{color:var(--status-blocked)}",
    "." + CLASS + ".is-stale{opacity:.6}",
  ].join("");

  /** An unforced read (a pill appearing, the tab returning) waits at least this long after the last. */
  const NUDGE_MS = 10000;

  let reading = null;
  let scheduled = false;
  let polling = false;
  let lastPoll = 0;

  function modelOf(info) {
    const model = info.querySelector(".composer-model");
    return model ? model.getAttribute("title") : null;
  }

  function render(info, view) {
    let span = info.querySelector(":scope > ." + CLASS);
    if (!view || !isDeepSeek(modelOf(info))) {
      if (span) span.remove();
      return;
    }
    if (!span) {
      span = document.createElement("span");
      span.className = CLASS;
      const dot = document.createElement("span");
      dot.className = CLASS + "-dot";
      dot.setAttribute("aria-hidden", "true");
      dot.textContent = "·";
      const amount = document.createElement("span");
      amount.className = CLASS + "-amount";
      span.append(dot, amount);
    }
    // after the reasoning span, wherever React has put it
    const reasoning = info.querySelector(":scope > .composer-reasoning");
    const anchor = reasoning ? reasoning.nextSibling : null;
    if (span.parentNode !== info || span.previousSibling !== reasoning) info.insertBefore(span, anchor);
    const amount = span.lastChild;
    if (amount.textContent !== view.text) amount.textContent = view.text;
    if (span.title !== view.title) span.title = view.title;
    span.setAttribute("aria-label", "DeepSeek balance " + view.text + (view.stale ? ", stale" : ""));
    span.classList.toggle("is-low", view.low);
    span.classList.toggle("is-stale", view.stale);
  }

  function place() {
    scheduled = false;
    const view = describe(reading);
    for (const info of document.querySelectorAll(".composer-model-info")) render(info, view);
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(place);
  }

  function wanted() {
    for (const info of document.querySelectorAll(".composer-model-info")) {
      if (isDeepSeek(modelOf(info))) return true;
    }
    return false;
  }

  async function poll(forced) {
    if (polling || document.visibilityState === "hidden" || !wanted()) return;
    if (forced !== true && Date.now() - lastPoll < NUDGE_MS) return;
    polling = true;
    lastPoll = Date.now();
    try {
      const response = await fetch(ENDPOINT, { credentials: "same-origin", cache: "no-store" });
      if (response.ok) reading = await response.json();
    } catch (_) {
      // offline: keep showing the last value, its tooltip says how old
    } finally {
      polling = false;
    }
    schedule();
  }

  function start() {
    const style = document.createElement("style");
    style.id = "codex-workspace-balance-style";
    style.textContent = STYLE;
    document.head.appendChild(style);

    // A pill appearing or its model changing re-renders it: put the span back, and read the
    // balance at once the first time a DeepSeek pill shows.
    new MutationObserver(function (records) {
      for (const record of records) {
        const target = record.target;
        if (target.nodeType === 1 && target.closest && target.closest("." + CLASS)) continue;
        if (reading === null) poll();
        schedule();
        return;
      }
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["title", "class"] });

    setInterval(function () { poll(true); }, POLL_MS);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") poll();
    });
    poll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
