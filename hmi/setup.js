// bd — the run-setup screen (declared in launch.yaml as `setup:`).
//
// The platform hosts this in a shadow root inside the Parameters modal,
// keeps the modal chrome and the Start / Launch buttons, and validates
// whatever value() returns against hmi/kwargs.j2. Everything between is
// this project's business: the rack, its geometry, the doses.
//
// JS rather than HTML because the grid is generated and interactive —
// 19 positions, each with its own dose. See hmi-guide §4b.

const SOURCE = "D5";                       // reservoir — dispensed FROM
const COLS = 5;

export default {
  css: `
    .wrap { font-family: var(--font); color: var(--text); }

    .head { display: flex; align-items: baseline; gap: var(--space-3);
            margin-bottom: var(--space-2); }
    .head h2 { margin: 0; font-size: var(--text-md); font-weight: 700; }
    .head .n { color: var(--muted); font-size: var(--text-sm);
               font-variant-numeric: tabular-nums; }
    .hint { color: var(--muted); font-size: var(--text-sm);
            margin: 0 0 var(--space-4); }

    .quick { display: flex; gap: var(--space-2); margin-bottom: var(--space-4); }
    .quick button {
      font: inherit; font-size: var(--text-sm); font-weight: 600;
      color: var(--text); background: var(--surface2);
      border: 1px solid var(--border); border-radius: var(--radius-md);
      padding: 6px 12px; min-height: 34px; cursor: pointer;
    }
    .quick button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
    .quick button:disabled { opacity: 0.5; cursor: default; }

    /* The rack keeps a plain surface — it is the real footprint of the
       thing on the bench, so it reads as an object, not as chrome. */
    .rack {
      display: grid; grid-template-columns: repeat(${COLS}, 1fr);
      gap: var(--space-3);
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius-lg); padding: var(--space-4);
    }

    .cell {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 2px;
      min-height: 56px; border-radius: var(--radius-md);
      border: 1px solid var(--border); background: var(--surface2);
      font: inherit; color: var(--muted); cursor: pointer;
      transition: border-color var(--motion-fast) var(--ease),
                  background var(--motion-fast) var(--ease);
    }
    .cell:hover:not(:disabled) { border-color: var(--accent); }
    .cell .n { font-family: var(--mono); font-size: 11px; font-weight: 700; }
    .cell .v { font-size: 11px; font-variant-numeric: tabular-nums; }

    .cell[aria-pressed="true"] {
      border-color: var(--accent); color: var(--text);
      background: color-mix(in srgb, var(--accent) 10%, transparent);
      box-shadow: var(--shadow-sm);
    }
    .cell.source {
      cursor: default; background: transparent;
      border-style: dashed; color: var(--muted);
    }
    .cell:disabled { cursor: default; opacity: 0.6; }

    .dose { display: flex; align-items: center; gap: var(--space-3);
            margin-top: var(--space-4); flex-wrap: wrap; }
    .dose label { font-size: var(--text-sm); font-weight: 600; }
    .dose input {
      font: inherit; width: 92px; padding: 6px 10px; min-height: 36px;
      color: var(--text); background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius-md);
    }
    .dose .unit { color: var(--muted); font-size: var(--text-sm); }
    .dose .apply {
      font: inherit; font-size: var(--text-sm); font-weight: 600;
      padding: 6px 12px; min-height: 36px; cursor: pointer;
      color: var(--accent); background: transparent;
      border: 1px solid var(--accent); border-radius: var(--radius-md);
    }
    .dose .apply:disabled { opacity: 0.5; cursor: default; border-color: var(--border); color: var(--muted); }
  `,

  mount(root, api) {
    const spec = api.schema.tubes || {};
    const vspec = spec.value || {};
    const step = vspec.step ?? 0.1;
    const fallback = vspec.default ?? 0;
    // The GRID is the schema default's keys — one source of truth, in
    // the project's own kwargs.j2. Current values win when a previous
    // run saved some.
    const grid = Object.keys(spec.default || {});
    const saved = api.values.tubes;
    const start = (saved && typeof saved === "object" && !Array.isArray(saved))
      ? saved : (spec.default || {});

    this.picked = new Map();
    for (const name of grid) {
      if (name in start) this.picked.set(name, Number(start[name]));
    }
    this.frozen = api.frozen;

    const wrap = document.createElement("div");
    wrap.className = "wrap";
    wrap.innerHTML = `
      <div class="head">
        <h2>${spec.label || "Tubes"}</h2><span class="n"></span>
      </div>
      <p class="hint">${spec.hint || ""}</p>
      <div class="quick">
        <button type="button" data-q="all">Select all</button>
        <button type="button" data-q="clear">Clear</button>
      </div>
      <div class="rack"></div>
      <div class="dose">
        <label>${vspec.label || "Value"}</label>
        <input type="number" step="${step}"
               min="${vspec.min ?? 0}" max="${vspec.max ?? 999}" value="${fallback}">
        <span class="unit">${vspec.unit || ""}</span>
        <button type="button" class="apply">Apply to selected</button>
      </div>`;

    const rack = wrap.querySelector(".rack");
    const count = wrap.querySelector(".n");
    const doseInput = wrap.querySelector(".dose input");
    this.cells = new Map();

    // Row-major, with the source cell in its true position.
    const all = [...grid];
    const bySlot = new Set(all);
    if (!bySlot.has(SOURCE)) all.splice(indexOf(SOURCE, all), 0, SOURCE);

    for (const name of all) {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell" + (name === SOURCE ? " source" : "");
      cell.innerHTML = `<span class="n">${name}</span><span class="v"></span>`;
      if (name === SOURCE) {
        cell.disabled = true;
        cell.querySelector(".v").textContent = "SOURCE";
      } else {
        cell.disabled = !!api.frozen;
        cell.addEventListener("click", () => {
          if (this.picked.has(name)) this.picked.delete(name);
          else this.picked.set(name, Number(doseInput.value) || fallback);
          this.sync();
        });
        this.cells.set(name, cell);
      }
      rack.appendChild(cell);
    }

    wrap.querySelector('[data-q="all"]').addEventListener("click", () => {
      const v = Number(doseInput.value) || fallback;
      for (const name of this.cells.keys()) {
        // Select-all applies the current dose to the tubes it ADDS and
        // leaves already-chosen ones on their own value.
        if (!this.picked.has(name)) this.picked.set(name, v);
      }
      this.sync();
    });
    wrap.querySelector('[data-q="clear"]').addEventListener("click", () => {
      this.picked.clear();
      this.sync();
    });
    wrap.querySelector(".apply").addEventListener("click", () => {
      const v = Number(doseInput.value) || fallback;
      for (const name of this.picked.keys()) this.picked.set(name, v);
      this.sync();
    });

    if (api.frozen) {
      for (const el of wrap.querySelectorAll("button,input")) el.disabled = true;
    }

    this.count = count;
    root.appendChild(wrap);
    this.sync();
  },

  sync() {
    for (const [name, cell] of this.cells) {
      const on = this.picked.has(name);
      cell.setAttribute("aria-pressed", on ? "true" : "false");
      cell.querySelector(".v").textContent = on ? String(this.picked.get(name)) : "";
    }
    this.count.textContent = `${this.picked.size} selected`;
  },

  // What the platform sends to the run. print_label is not drawn here,
  // so it keeps its schema default.
  value() {
    return { tubes: Object.fromEntries([...this.picked.entries()].sort()) };
  },

  validate() {
    return this.picked.size ? null : "Select at least one tube";
  },
};

function indexOf(name, all) {
  // Where the source cell belongs in row-major order.
  const row = name[0], col = Number(name.slice(1));
  let i = 0;
  for (const n of all) {
    if (n[0] > row || (n[0] === row && Number(n.slice(1)) > col)) return i;
    i++;
  }
  return all.length;
}
