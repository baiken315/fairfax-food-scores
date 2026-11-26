// Fills the method page's tables from data/meta.json.
"use strict";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 1) => (n == null ? "–" : Number(n).toFixed(d).replace(/\.0+$/, ""));
const fmtDate = (iso) => {
  if (!iso) return "?";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
};

(async function () {
  const meta = await fetch("data/meta.json").then((r) => r.json());

  document.getElementById("updated").textContent =
    `Data updated ${fmtDate(meta.generated_at)}. ${meta.counts.locations.toLocaleString()} locations, ` +
    `${meta.counts.inspections.toLocaleString()} inspections, ${meta.counts.complaints.toLocaleString()} complaints.`;
  document.getElementById("unit").textContent = fmt(meta.violation_unit);
  document.getElementById("repeat").textContent = fmt(meta.points.repeat_multiplier);
  document.getElementById("window").textContent =
    `${fmtDate(meta.inspection_window[0])} to ${fmtDate(meta.inspection_window[1])}`;

  const letters = meta.letters;
  document.querySelector("#letters tbody").innerHTML = letters.map((l, i) => {
    const hi = i === 0 ? "100" : fmt(letters[i - 1].min_score - 0.1);
    const range = l.letter === "F" ? `below ${fmt(letters[i - 1].min_score)}` : `${fmt(l.min_score)}–${hi}`;
    return `<tr><td><span class="grade sm g-${esc(l.letter)}">${esc(l.letter)}</span></td><td class="num">${range}</td>
      <td>${esc(l.meaning)}</td><td class="num">${(meta.letter_counts[l.letter] || 0).toLocaleString()}</td></tr>`;
  }).join("") + `<tr><td><span class="grade sm">?</span></td><td class="num">–</td><td>Not enough recent inspections to grade</td>
      <td class="num">${(meta.letter_counts.none || 0).toLocaleString()}</td></tr>`;

  const p = meta.points;
  const row = (label, t) => `<tr><td>${label}</td><td class="num">${fmt(t.priority)}</td>
    <td class="num">${fmt(t["priority foundation"])}</td><td class="num">${fmt(t.core)}</td></tr>`;
  document.querySelector("#points tbody").innerHTML =
    row("Risk factor (items 1–29)", p.risk_factor) + row("Good retail practice (items 30–56)", p.good_retail);

})();
