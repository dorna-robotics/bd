// bd — the run-setup screen (declared in launch.yaml as `setup:`).
//
// The platform hosts this in a shadow root inside the Parameters modal,
// keeps the modal chrome and the Start / Launch buttons, and validates
// whatever value() returns against hmi/default.j2. Everything between is
// this project's business: the rack, its geometry, the doses.
//
// JS rather than HTML because the grid is generated and interactive —
// 19 positions, each with its own dose. See hmi-guide §4b.

const SOURCE = "D5";                       // reservoir — dispensed FROM
const COLS = 5;

// Presentation lives HERE, not in the schema — kwargs.j2 is bare
// defaults only ("the kwargs themselves").
const LABEL = "Tubes to process";
const HINT = "Tap the rack positions you loaded. Each selected tube runs the full chain.";
const DOSE = { label: "Dispense", unit: "mL", default: 0.4, min: 0.1, max: 5.0, step: 0.1 };

export default {
  css: `
    .wrap { font-family: var(--font); color: var(--text); }

    .head { display: flex; align-items: baseline; gap: var(--space-3);
            margin-bottom: var(--space-2); }
    .head h2 { margin: 0; font-size: var(--text-md); font-weight: 700; }
    .head .n { color: var(--muted); font-family: var(--mono);
               font-size: 12px; font-variant-numeric: tabular-nums; }
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

    /* The rack draws as the physical object: circular wells on a plain
       card, row letters + column numbers as axes, the dose under each
       well, and the load edge marked. Empty wells are recessed (dashed
       ring + inner shadow); selected wells fill with the accent tint;
       the reservoir carries the amber ramp. */
    .rack {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius-lg); padding: 14px 14px 6px;
      display: flex; flex-direction: column; gap: 8px;
    }
    .rrow { display: grid; grid-template-columns: 24px repeat(${COLS}, 1fr); gap: 8px; }
    .colh { text-align: center; font-family: var(--mono); font-size: 11px; color: var(--muted); }
    .rowh { height: 50px; display: flex; align-items: center; justify-content: center;
            font-family: var(--mono); font-size: 11px; color: var(--muted); }

    .cell {
      display: flex; flex-direction: column; align-items: center; gap: 4px;
      padding: 3px 2px 5px; border: none; background: transparent;
      border-radius: 7px; font: inherit; cursor: pointer;
    }
    .cell .well {
      width: 44px; height: 44px; border-radius: 999px;
      display: grid; place-items: center;
      font-weight: 700; font-size: 12px;
      background: var(--bg); border: 1px dashed var(--border);
      color: var(--muted);
      box-shadow: inset 0 2px 5px rgb(0 0 0 / 0.08);
      transition: background var(--motion-fast) var(--ease),
                  border-color var(--motion-fast) var(--ease),
                  box-shadow var(--motion-fast) var(--ease);
    }
    .cell:hover:not(:disabled) .well { border-color: var(--accent); }
    .cell .v { font-family: var(--mono); font-size: 10.5px; min-height: 14px;
               color: var(--muted); font-variant-numeric: tabular-nums; }

    .cell[aria-pressed="true"] .well {
      background: var(--accent-dim); border: 1.5px solid var(--accent);
      color: var(--text); box-shadow: inset 0 0 0 2px var(--surface);
    }
    .cell.source { cursor: default; }
    .cell.source .well {
      background: var(--amber-dim); border: 1.5px solid var(--amber);
      color: var(--amber); box-shadow: inset 0 0 0 2px var(--surface);
    }
    .cell.source .v { color: var(--amber); }
    .cell:disabled:not(.source) { cursor: default; opacity: 0.6; }

    .edge { display: flex; align-items: center; gap: 10px; padding: 8px 4px 2px; }
    .edge::before, .edge::after { content: ""; flex: 1; height: 1px; background: var(--border); }
    .edge span { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; color: var(--muted); }

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
    const vspec = DOSE;
    const step = vspec.step;
    const fallback = vspec.default;
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
        <h2>${LABEL}</h2><span class="n"></span>
      </div>
      <p class="hint">${HINT}</p>
      <div class="quick">
        <button type="button" data-q="all">Select all</button>
        <button type="button" data-q="clear">Clear</button>
      </div>
      <div class="rack">
        <div class="rrow head"></div>
        <div class="edge"><span>FRONT · LOAD FROM THIS EDGE</span></div>
      </div>
      <div class="dose">
        <label>${vspec.label}</label>
        <input type="number" step="${step}"
               min="${vspec.min}" max="${vspec.max}" value="${fallback}">
        <span class="unit">${vspec.unit}</span>
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

    // Column-number header row (blank corner over the row letters).
    const head = rack.querySelector(".rrow.head");
    head.appendChild(document.createElement("span"));
    for (let c = 1; c <= COLS; c++) {
      const h = document.createElement("span");
      h.className = "colh";
      h.textContent = String(c);
      head.appendChild(h);
    }

    const edge = rack.querySelector(".edge");
    let row = null, rowLetter = null;
    for (const name of all) {
      if (name[0] !== rowLetter) {
        rowLetter = name[0];
        row = document.createElement("div");
        row.className = "rrow";
        const rh = document.createElement("span");
        rh.className = "rowh";
        rh.textContent = rowLetter;
        row.appendChild(rh);
        rack.insertBefore(row, edge);
      }
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell" + (name === SOURCE ? " source" : "");
      cell.innerHTML = `<span class="well">${name}</span><span class="v"></span>`;
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
      row.appendChild(cell);
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
      cell.querySelector(".v").textContent = on ? this.picked.get(name).toFixed(1) + " mL" : "";
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
