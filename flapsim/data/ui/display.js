"use strict";
// Shared virtual-display rendering for the live panel and the run replay. Everything is built
// with DOM calls and textContent, so module text from the firmware is never parsed as markup.
const FlapUI = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const BETWEEN = 0.25; // landing error (in flap pitches) beyond which a tile is between flaps
  const COLUMNS = ["module", "char", "angle", "error", "hall", "coils", "energized", "steps",
    "stalls", "anomalies", "faults", "mechanics"];
  const HEADINGS = ["Module", "Shows", "Angle", "Flap error", "Hall", "Coils", "Energized",
    "Steps (fwd/back)", "Stalls", "Anomalies", "Faults", "Mechanics"];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function svg(tag, attrs, cls) {
    const e = document.createElementNS(SVG, tag);
    for (const name in attrs) e.setAttribute(name, attrs[name]);
    if (cls) e.setAttribute("class", cls);
    return e;
  }
  function hex(n) { return "0x" + n.toString(16); }
  function key(m) { return m.bus + ":" + m.addr; }
  function label(m) { return "bus" + m.bus + " " + hex(m.addr); }

  // A drum-angle dial: the needle is the drum, the dot is the magnet.
  function makeDial() {
    const root = svg("svg", { viewBox: "0 0 34 34" }, "dial");
    root.setAttribute("aria-hidden", "true");
    const needle = svg("line", { x1: 17, y1: 17, x2: 17, y2: 5 }, "needle");
    const magnet = svg("circle", { cx: 29, cy: 17, r: 2.5 }, "magnet");
    root.append(svg("circle", { cx: 17, cy: 17, r: 13 }, "ring"), needle, magnet);
    return { root, needle, magnet };
  }
  function pointAt(position, steps, radius) {
    const a = (position / steps) * 2 * Math.PI;
    return [17 + radius * Math.sin(a), 17 - radius * Math.cos(a)];
  }
  function setDial(dial, angle, steps, magnetPosition) {
    const known = Number.isFinite(steps) && steps > 0 && Number.isFinite(angle);
    dial.needle.style.display = known ? "" : "none";
    dial.magnet.style.display = known && Number.isFinite(magnetPosition) ? "" : "none";
    if (!known) return;
    const [x, y] = pointAt(angle, steps, 12);
    dial.needle.setAttribute("x2", x.toFixed(1));
    dial.needle.setAttribute("y2", y.toFixed(1));
    if (Number.isFinite(magnetPosition)) {
      const [mx, my] = pointAt(magnetPosition, steps, 13);
      dial.magnet.setAttribute("cx", mx.toFixed(1));
      dial.magnet.setAttribute("cy", my.toFixed(1));
    }
  }

  function legend() {
    const box = el("div", "legend");
    const coil = el("span");
    coil.append(el("i", "led coil on"), " motor coils energized");
    const hall = el("span");
    hall.append(el("i", "led hall on"), " hall sensor sees magnet");
    const drum = el("span");
    const dial = makeDial();
    setDial(dial, 0, 12, 3);
    drum.append(dial.root, " drum angle (dot = magnet)");
    const between = el("span");
    between.append(el("i", "swatch"), " between flaps (landing error over " + BETWEEN + ")");
    box.append(coil, hall, drum, between);
    return box;
  }

  // The tile display. update() takes the modules of one state or replay snapshot.
  function display(container) {
    const tiles = new Map();
    let signature = "";
    function layout(modules) {
      container.replaceChildren();
      tiles.clear();
      const buses = [...new Set(modules.map(m => m.bus))].sort((a, b) => a - b);
      for (const bus of buses) {
        const row = el("div", "row");
        row.setAttribute("aria-label", "Bus " + bus);
        for (const m of modules.filter(m => m.bus === bus)) {
          const mod = el("div", "mod");
          const tile = el("div", "tile");
          const ch = el("div", "ch");
          const err = el("span", "err");
          err.hidden = true;
          tile.append(ch, err);
          const dial = makeDial();
          const coil = el("i", "led coil");
          const hall = el("i", "led hall");
          const status = el("div", "info");
          status.append(dial.root, coil, hall);
          mod.append(tile, status, el("div", "info", label(m)));
          row.append(mod);
          tiles.set(key(m), { tile, ch, err, dial, coil, hall, mod, text: null });
        }
        container.append(row);
      }
    }
    function update(modules, animate) {
      const next = modules.map(key).join();
      if (next !== signature) {
        layout(modules);
        signature = next;
      }
      for (const m of modules) {
        const t = tiles.get(key(m));
        const shown = m.char === " " ? "\u00a0" : m.char;
        if (t.text !== shown) {
          const first = t.text === null;
          t.text = shown;
          t.ch.textContent = shown;
          if (animate && !first) {
            t.ch.classList.remove("flip");
            void t.ch.offsetWidth;
            t.ch.classList.add("flip");
          }
        }
        const mech = m.mechanics || {};
        setDial(t.dial, m.angle, mech.steps_per_rotation, mech.magnet_position);
        t.coil.classList.toggle("on", !!m.energized);
        t.hall.classList.toggle("on", !!m.hall);
        const between = Number.isFinite(m.flap_error) && Math.abs(m.flap_error) > BETWEEN;
        t.tile.classList.toggle("between", between);
        t.err.hidden = !between;
        if (between) t.err.textContent = (m.flap_error > 0 ? "+" : "") + m.flap_error.toFixed(2);
        t.mod.title = label(m) + ", angle " + m.angle;
      }
      if (!modules.length && !container.firstChild) {
        container.append(el("p", "empty", "No modules."));
        signature = "";
      }
    }
    return { update };
  }

  // The per-module data table, restyled by the shared theme.
  function table(wrap) {
    const tbl = el("table");
    const head = el("tr");
    for (const text of HEADINGS) head.append(el("th", "", text));
    const thead = el("thead");
    thead.append(head);
    const body = el("tbody");
    tbl.append(thead, body);
    wrap.replaceChildren(tbl);
    const rows = new Map();
    let signature = "";
    function update(modules) {
      const next = modules.map(key).join();
      if (next !== signature) {
        body.replaceChildren();
        rows.clear();
        for (const m of modules) {
          const tr = el("tr");
          const cells = {};
          for (const name of COLUMNS) {
            cells[name] = el("td");
            tr.append(cells[name]);
          }
          body.append(tr);
          rows.set(key(m), cells);
        }
        signature = next;
      }
      for (const m of modules) {
        const c = rows.get(key(m));
        const mech = m.mechanics || {};
        const counters = m.counters || {};
        const coils = (m.latch >> 1) & 0xf;
        c.module.textContent = "bus " + m.bus + " " + hex(m.addr);
        c.char.textContent = JSON.stringify(m.char);
        c.angle.textContent = mech.steps_per_rotation ? m.angle + " / " + mech.steps_per_rotation
          : String(m.angle);
        const known = Number.isFinite(m.flap_error);
        c.error.textContent = known ? m.flap_error.toFixed(3) : "";
        c.error.className = known && Math.abs(m.flap_error) > BETWEEN ? "warn" : "";
        c.hall.textContent = m.hall ? "high" : "low";
        c.coils.className = "coils";
        c.coils.textContent = [8, 4, 2, 1].map(b => (coils & b ? "■" : "□")).join("");
        c.energized.textContent = m.energized ? "yes" : "no";
        c.steps.textContent = counters.forward + " / " + counters.backward;
        c.stalls.textContent = counters.stalls;
        c.stalls.className = counters.stalls ? "bad" : "";
        c.anomalies.textContent = counters.anomalies;
        c.anomalies.className = counters.anomalies ? "bad" : "";
        // Injected faults (#19): only modes that are not "ok", then how often they acted.
        const injected = Object.entries(m.faults || {})
          .filter(([kind, mode]) => ["sensor", "motor", "i2c"].includes(kind) && mode !== "ok")
          .map(([kind, mode]) => kind + " " + mode);
        const acted = m.fault_counters && m.fault_counters.events;
        if (acted) injected.push(acted + " event" + (acted === 1 ? "" : "s"));
        c.faults.textContent = injected.join(", ");
        c.mechanics.textContent = [
          mech.steps_per_rotation !== undefined && mech.steps_per_rotation + " steps",
          mech.magnet_position !== undefined && "magnet " + mech.magnet_position,
          mech.flap_offset !== undefined && "offset " + mech.flap_offset,
        ].filter(Boolean).join(" · ");
        c.mechanics.title = Object.entries(mech).map(([k, v]) => k + " " + v).join(", ");
      }
    }
    return { update };
  }

  return { display, table, legend, el };
})();
