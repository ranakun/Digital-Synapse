/* A read-only landscape over revision-pinned knowledge. Nothing here saves or investigates. */
(() => {
  "use strict";
  const COLORS = [
    "#8FAED0",
    "#B3A0CA",
    "#C6B182",
    "#CA9A86",
    "#C398AB",
    "#939CBD",
  ];
  const $ = (name) => document.getElementById(`v2-${name}`);
  const S = {
    revision: null,
    organization: null,
    landscape: null,
    payload: null,
    kind: "overview",
    entry: "map",
    view: matchMedia("(max-width:480px)").matches ? "list" : "map",
    selected: null,
    edge: null,
    detail: null,
    exact: null,
    busy: false,
    sceneToken: 0,
    detailToken: 0,
    graph: null,
    graphNodes: [],
    graphLinks: [],
    labels: [],
    channels: new Set(["recorded", "similarity", "overlap"]),
    area: null,
    thread: null,
    threadPage: 0,
    threadTrail: [],
    hoverNode: null,
    query: "",
    hover: null,
    overviewOffset: 0,
    sceneOffset: 0,
    layoutKey: "",
    narrow: matchMedia("(max-width:480px)").matches,
    confirmed: false,
  };
  const clone = (value) =>
    value == null ? value : JSON.parse(JSON.stringify(value));
  const el = (tag, text, css) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (css) node.className = css;
    return node;
  };
  const btn = (text, css, action) => {
    const node = el("button", text, css);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  const hash = (value) => {
    let n = 2166136261;
    for (const c of String(value || ""))
      n = Math.imul(n ^ c.charCodeAt(0), 16777619);
    return n >>> 0;
  };
  const kind = (n) =>
    ["area", "group", "bundle"].includes(n?.kind)
      ? n.kind
      : n?.kind === "source" || n?.ref?.source_id
        ? "source"
        : "record";
  const identity = (n) =>
    n?.ref?.record_id ||
    n?.ref?.source_id ||
    String(n?.id || "").replace(/^record:/, "");
  const label = (n) =>
    n?.display_label ||
    cleanTitle(
      n?.label || n?.name || n?.statement || n?.id || "Untitled material",
    );
  const from = (e) =>
    e.from ||
    e.from_id ||
    (typeof e.source === "object" ? e.source.id : e.source);
  const to = (e) =>
    e.to || e.to_id || (typeof e.target === "object" ? e.target.id : e.target);
  const channel = (e) => e.channel || "recorded";
  const suggested = (n) =>
    n?.availability === "suggestion" ||
    n?.qualification?.availability === "suggestion";
  const areaColor = (id) => {
    const index = directoryData().findIndex((a) => a.id === id);
    return index >= 0 ? COLORS[index % COLORS.length] : "#92909B";
  };
  const color = (n) =>
    n?.color ||
    (["area", "group", "bundle"].includes(kind(n))
      ? kind(n) === "area"
        ? areaColor(n.id)
        : COLORS[hash(n.id) % COLORS.length]
      : areaColor(n?.area_ids?.[0]));
  const coverage = (c) => {
    const r = c?.unique_records || 0,
      s = c?.unique_sources || 0;
    return (
      [
        r ? `${r.toLocaleString()} record${r === 1 ? "" : "s"}` : "",
        s ? `${s.toLocaleString()} source${s === 1 ? "" : "s"}` : "",
      ]
        .filter(Boolean)
        .join(" · ") || "Retained material"
    );
  };
  const shortKind = (n) =>
    kind(n) === "source"
      ? "Source"
      : (
          n.type ||
          (n.kind && !["record", "area"].includes(n.kind) ? n.kind : null) ||
          n.record_kind ||
          "Record"
        ).replaceAll("_", " ");
  const url = (path, args = {}) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(args))
      if (value !== null && value !== undefined && value !== "")
        params.set(key, String(value));
    return path + "?" + params;
  };
  async function api(path) {
    const response = await fetch(path, {
      headers: { Accept: "application/json" },
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error("The viewer received an unreadable response.");
    }
    if (!response.ok || data.error) {
      const e = new Error(
        data.error?.message || "The saved view could not be loaded.",
      );
      e.code = data.error?.code;
      e.status = response.status;
      throw e;
    }
    return data;
  }
  const read = (op, args, revision = S.revision) =>
    api(
      url("/api/v2/read", {
        operation: op,
        arguments: JSON.stringify(args),
        revision,
        budget: 32000,
      }),
    );
  const landscape = (
    revision = S.revision,
    organization = S.organization,
    offset = 0,
    selected = S.selected,
  ) =>
    api(
      url("/api/v2/landscape", {
        revision,
        organization_revision: organization,
        offset,
        selected: identity(selected),
      }),
    );
  const area = (
    id,
    revision = S.revision,
    organization = S.organization,
    offset = 0,
    selected = S.selected,
    groupPath = "",
    query = "",
  ) =>
    api(
      url("/api/v2/browse", {
        area_id: id,
        revision,
        organization_revision: organization,
        offset,
        group_path: groupPath,
        q: query,
        selected: identity(selected),
      }),
    );
  function notice(message, target = "map-status") {
    const node = $(target);
    node.textContent = message || "";
    node.hidden = !message;
  }
  function session(payload) {
    if (payload?.session?.changed)
      notice(
        "New saved knowledge is available. Refresh to explore it.",
        "session",
      );
    else notice("", "session");
  }
  function invalidate() {
    S.detailToken++;
    S.busy = true;
    $("copy").disabled = true;
  }
  function restoreRead() {
    S.busy = false;
    $("copy").disabled = !S.detail;
  }
  function commit(payload, sceneKind, options = {}) {
    S.payload = payload;
    S.kind = sceneKind;
    S.revision = payload.knowledge_revision || S.revision;
    S.organization = payload.organization_revision || S.organization;
    S.sceneOffset = payload.page?.offset || 0;
    if (sceneKind === "overview") S.overviewOffset = S.sceneOffset;
    S.area = sceneKind === "area" ? payload.area : S.area;
    if ("selected" in options) S.selected = options.selected;
    if ("edge" in options) S.edge = options.edge;
    $("boot").hidden = true;
    $("main").hidden = false;
    notice("");
    session(payload);
    render(true);
  }
  async function goOverview(offset = 0) {
    const token = ++S.sceneToken;
    invalidate();
    notice("Opening the map…");
    try {
      const payload = await landscape(S.revision, S.organization, offset, null);
      if (token !== S.sceneToken) return;
      S.landscape = payload;
      S.entry = "map";
      S.thread = null;
      S.threadTrail = [];
      closeDirectory();
      S.selected = null;
      S.edge = null;
      S.detail = null;
      S.exact = null;
      S.busy = false;
      commit(payload, "overview");
    } catch (e) {
      if (token === S.sceneToken) {
        restoreRead();
        notice(e.message + " Your current view is preserved.");
      }
    }
  }
  async function goArea(
    id,
    offset = 0,
    selected = null,
    groupPath = "",
    query = "",
  ) {
    const token = ++S.sceneToken;
    invalidate();
    notice("Opening this area…");
    try {
      const payload = await area(
        id,
        S.revision,
        S.organization,
        offset,
        selected,
        groupPath,
        query,
      );
      if (token !== S.sceneToken) return;
      S.entry = "map";
      S.thread = null;
      S.threadTrail = [];
      closeDirectory();
      S.edge = null;
      S.selected = selected || null;
      S.detail = null;
      S.exact = null;
      S.busy = false;
      $("search").value = query;
      $("search-scope").value = "context";
      if (payload.mode === "collection") S.view = "list";
      else if (!S.narrow) S.view = "map";
      commit(payload, "area");
      if (selected)
        selectNode(
          payload.nodes.find((n) => identity(n) === identity(selected)) ||
            selected,
        );
    } catch (e) {
      if (token === S.sceneToken) {
        restoreRead();
        notice(e.message + " Your current view is preserved.");
      }
    }
  }
  function openNode(n) {
    if (kind(n) === "group")
      return goArea(
        S.area.id,
        0,
        null,
        [S.payload.group_path, n.id].filter(Boolean).join("/"),
      );
    if (kind(n) === "bundle") return loadThreadView(n.id);
    if (kind(n) === "area") return goArea(n.id);
    return selectNode(n);
  }
  function renderTrail() {
    const trail = $("trail");
    trail.replaceChildren();
    if (S.kind === "area") {
      trail.append(
        btn("Whole map", "v2-trail-link", () => goOverview(S.overviewOffset)),
      );
      for (const item of S.payload.trail || []) {
        trail.append(
          el("span", "›", "v2-trail-divider"),
          btn(item.label, "v2-trail-link", () =>
            goArea(S.area.id, 0, null, item.path),
          ),
        );
      }
    } else if (S.kind === "thread") {
      trail.append(btn("Map", "v2-trail-link", returnToMap));
      S.threadTrail.forEach((item, index) => {
        trail.append(
          el("span", "›", "v2-trail-divider"),
          btn(label(item.focus), "v2-trail-link", () => restoreThread(index)),
        );
      });
      trail.append(
        el("span", "›", "v2-trail-divider"),
        btn(label(S.thread.focus), "v2-trail-link", () => loadThreadView("")),
      );
      if (S.payload.bundle)
        trail.append(
          el("span", "›", "v2-trail-divider"),
          el("span", human(S.payload.bundle.split(":")[0])),
        );
    }
    trail.hidden = !trail.childNodes.length;
  }
  function goBack() {
    if (S.kind === "thread") {
      if (S.payload.bundle || S.payload.query) return loadThreadView("");
      if (S.threadTrail.length) return restoreThread(S.threadTrail.length - 1);
      return returnToMap();
    }
    if (S.kind === "area" && (S.payload.group_path || S.payload.query)) {
      const parts = (S.payload.group_path || "").split("/").filter(Boolean);
      if (!S.payload.query) parts.pop();
      return goArea(S.area.id, 0, null, parts.join("/"));
    }
    return goOverview(S.overviewOffset);
  }
  function currentNodes() {
    return S.payload?.nodes || [];
  }
  function currentEdges() {
    return (S.payload?.edges || []).filter((e) => S.channels.has(channel(e)));
  }
  function allNodes() {
    return currentNodes().concat(
      (S.payload?.clouds || []).flatMap((c) => c.nodes),
    );
  }
  function nodeById(id) {
    return allNodes().find((n) => n.id === id || identity(n) === id);
  }
  function edgeName(edge) {
    const a = nodeById(from(edge)),
      b = nodeById(to(edge));
    const separator = ["similarity", "overlap"].includes(channel(edge)) ? "↔" : "→";
    return `${a ? label(a) : "Selected material"} ${separator} ${b ? label(b) : "Related material"}`;
  }
  function directoryData() {
    return S.landscape?.directory || [];
  }
  function render(newScene = false) {
    $("inspector").hidden = !(S.selected || S.edge);
    document
      .querySelectorAll("[data-v2-entry]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.v2Entry === S.entry)),
      );
    document
      .querySelectorAll("[data-view]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.view === S.view)),
      );
    const mapToggle = document.querySelector('[data-view="map"]');
    mapToggle.disabled = S.kind === "search";
    mapToggle.title =
      S.kind === "search" ? "Select a result and follow it in Threads" : "";
    $("map-panel").hidden = S.view !== "map";
    $("list-panel").hidden = S.view !== "list";
    $("back").hidden = S.kind === "overview";
    $("directory-toggle").hidden = !S.landscape;
    const titles = {
      overview: "Knowledge map",
      area: S.payload.trail?.at(-1)?.label || S.area?.label || "Area",
      search: `Results for “${S.query}”`,
      thread: label(S.thread?.focus),
    };
    $("map-title").textContent = titles[S.kind] || "Knowledge map";
    $("map-description").textContent =
      S.kind === "overview"
        ? "Select an area to explore"
        : S.kind === "area"
          ? `${S.payload.total_material?.toLocaleString() || 0} items · ${S.payload.mode === "groups" ? "Choose a group to explore" : S.payload.query ? "Matches within this collection" : S.payload.mode === "collection" ? "Search or browse this collection" : "Explore the material and its connections"}`
          : S.kind === "thread"
            ? `${S.payload.total_neighbors || 0} connected items · ${S.payload.mode === "bundles" ? "Open a connection bundle" : "Follow a connection or inspect its evidence"}`
            : "Records and sources across your knowledge";
    $("map-note").textContent =
      S.kind === "overview"
        ? "Sampled overview · dotted links show similarity, not verified relationships"
        : S.kind === "thread"
          ? "Select a connection to understand why it is here"
          : S.payload.mode === "groups"
            ? "Groups organize material · affiliations may overlap"
            : "Zoom for more labels · select a point or connection";
    $("app").dataset.scene = S.kind;
    $("app").dataset.mode = S.payload?.mode || "map";
    const scoped = ["area", "thread"].includes(S.kind);
    $("search-scope").hidden = !scoped;
    $("search-scope").options[0].textContent =
      S.kind === "thread" ? "Connections" : "This area";
    $("search").placeholder =
      scoped && $("search-scope").value === "context"
        ? "Find a name or connected item"
        : "Search your knowledge";
    $("back").setAttribute(
      "aria-label",
      S.kind === "thread" ? "Back along this thread" : "Back to parent area",
    );
    renderTrail();
    renderList();
    renderPager();
    renderDirectory();
    if (S.view === "map") requestAnimationFrame(() => drawScene(newScene));
  }
  function renderList() {
    const list = $("list");
    list.replaceChildren();
    if (!currentNodes().length) {
      list.append(
        el(
          "p",
          "No material in this view. Try another area or search your knowledge.",
          "v2-empty",
        ),
      );
      return;
    }
    for (const n of currentNodes()) {
      const row = btn(null, "v2-list-item", () => openNode(n));
      row.dataset.nodeId = n.id;
      row.style.setProperty("--cluster", color(n));
      const copy = el("span");
      copy.append(
        el("strong", label(n)),
        el(
          "small",
          ["group", "bundle"].includes(kind(n))
            ? `${n.count.toLocaleString()} item${n.count === 1 ? "" : "s"}${n.description === "Existing material type" ? "" : " · " + n.description}`
            : kind(n) === "area"
              ? coverage(n.coverage)
              : `${shortKind(n)}${suggested(n) ? " · Synapse suggestion" : ""}`,
        ),
      );
      row.append(
        el("i", null, "v2-list-dot"),
        copy,
        el("span", "↗", "v2-arrow"),
      );
      list.append(row);
    }
    const edges = currentEdges();
    if (edges.length) {
      list.append(el("h2", "Connections", "v2-list-heading"));
      for (const edge of edges) {
        const row = btn(null, "v2-list-item", () => selectEdge(edge));
        const text = el("span");
        text.append(
          el("strong", edgeName(edge)),
          el("small", connectionType(edge)),
        );
        row.append(text, el("span", "↗", "v2-arrow"));
        list.append(row);
      }
    }
  }
  function renderPager() {
    const pager = $("scene-pager");
    pager.replaceChildren();
    const page = S.payload?.page;
    if (!page?.total) return;
    const contextual = S.payload.focus || S.payload.anchor ? 1 : 0;
    const count = currentNodes().length - contextual;
    const unit = page.unit || (S.kind === "overview" ? "areas" : "items");
    pager.append(
      el(
        "span",
        `${page.offset + 1}–${Math.min(page.offset + count, page.total)} of ${page.total.toLocaleString()} ${unit}`,
      ),
    );
    const move = (offset) => {
      if (S.kind === "overview") return goOverview(offset);
      if (S.kind === "thread")
        return loadThreadView(
          S.payload.bundle || "",
          offset,
          S.payload.query || "",
        );
      return goArea(
        S.area.id,
        offset,
        S.selected,
        S.payload.group_path || "",
        S.payload.query || "",
      );
    };
    if (page.offset > 0 || page.next_offset != null) {
      const previous = btn("←", "", () =>
        move(Math.max(0, page.offset - (page.size || 6))),
      );
      previous.disabled = page.offset === 0;
      previous.setAttribute("aria-label", `Previous ${unit}`);
      const next = btn("→", "", () => move(page.next_offset));
      next.disabled = page.next_offset == null;
      next.setAttribute("aria-label", `Next ${unit}`);
      pager.append(previous, next);
    }
    if (S.payload.omissions?.edges)
      pager.append(el("small", "Some connections hidden for readability"));
  }
  function renderDirectory() {
    const list = $("area-list");
    list.replaceChildren();
    const q = $("area-search").value.trim().toLowerCase();
    const areas = directoryData().filter((a) =>
      a.label.toLowerCase().includes(q),
    );
    for (const a of areas) {
      const row = btn(null, "v2-related", () => openNode(a));
      row.append(el("span", a.label), el("small", coverage(a.coverage)));
      list.append(row);
    }
    if (!areas.length) list.append(el("p", "No matching areas.", "v2-empty"));
    if (S.landscape?.directory_truncated)
      list.append(
        el(
          "p",
          "Showing the first 256 areas. Use the map page controls to reach the remaining areas.",
          "v2-detail-meta",
        ),
      );
    const loose = S.landscape?.loose;
    if (loose?.count && !q) {
      const b = btn(null, "v2-related", () => goArea("loose"));
      b.append(
        el("span", "Ungrouped material"),
        el("small", "Still available to search and read"),
      );
      list.append(b);
    }
  }
  function closeDirectory() {
    $("directory").hidden = true;
    $("directory-toggle").setAttribute("aria-expanded", "false");
  }
  function dimensions() {
    return { w: $("map").clientWidth, h: $("map").clientHeight };
  }
  function selectedId() {
    return identity(S.selected);
  }
  function activeNode(node) {
    return identity(node.__node || node) === selectedId();
  }
  function arrangeAreas(areas) {
    if (areas.length !== 6) return areas;
    // Six bounded clusters are placed to shorten actual cross-area connections.
    // Slot geometry keeps readable space; no fixed topic occupies a slot.
    const slots = [
      [-0.97, -0.8],
      [0.99, -0.84],
      [0.01, 0.04],
      [-1.05, 0.88],
      [1.07, 0.84],
      [0.02, 1.05],
    ];
    const edges = currentEdges(),
      original = areas.map((a) => a.id);
    let best = areas,
      bestCost = Infinity;
    const visit = (prefix, rest) => {
      if (rest.length) {
        for (let i = 0; i < rest.length; i++)
          visit(
            [...prefix, rest[i]],
            rest.filter((_, j) => i !== j),
          );
        return;
      }
      const positions = new Map(prefix.map((a, i) => [a.id, slots[i]]));
      let cost = 0;
      for (const edge of edges) {
        const a = positions.get(from(edge)),
          b = positions.get(to(edge));
        if (a && b)
          cost +=
            Math.hypot(a[0] - b[0], a[1] - b[1]) *
            Math.log2(2 + Math.min(100, edge.count || 1));
      }
      cost += prefix.reduce(
        (sum, a, i) => sum + (a.id === original[i] ? 0 : 0.0001),
        0,
      );
      if (cost < bestCost) {
        bestCost = cost;
        best = prefix;
      }
    };
    visit([], areas);
    return best;
  }
  function sparseConnections(data, edges) {
    const parent = new Map(data.map((n) => [n.id, n.id]));
    const root = (id) => {
      while (parent.get(id) !== id) id = parent.get(id);
      return id;
    };
    const chosen = new Set();
    for (const e of [...edges].sort(
      (a, b) =>
        Number(channel(a) !== "recorded") - Number(channel(b) !== "recorded") ||
        String(a.id).localeCompare(String(b.id)),
    )) {
      if (!parent.has(from(e)) || !parent.has(to(e))) continue;
      const a = root(from(e)),
        b = root(to(e));
      if (a !== b) {
        parent.set(a, b);
        chosen.add(e.id);
      } else if (channel(e) === "recorded") chosen.add(e.id);
    }
    return chosen;
  }
  function materialPositions(data, edges, w, h, focusId, allLinks = false) {
    const positions = new Map();
    if (S.payload.mode === "bundles") {
      const peers = data.filter((n) => n.id !== focusId);
      data.forEach((n) => {
        const i = peers.indexOf(n),
          angle = -Math.PI / 2 + (i * Math.PI * 2) / Math.max(1, peers.length);
        positions.set(
          n.id,
          n.id === focusId
            ? { x: 0, y: 0, degree: 0 }
            : peers.length <= 3
              ? {
                  x: Math.min(245, w * 0.27),
                  y: (i - (peers.length - 1) / 2) * 110,
                  degree: 0,
                }
              : {
                  x: Math.cos(angle) * Math.min(280, w * 0.3),
                  y: Math.sin(angle) * Math.min(190, h * 0.28),
                  degree: 0,
                },
        );
      });
      return positions;
    }
    data.forEach((n, i) => {
      const angle = i * 2.39996 + (hash(n.id) % 100) / 300;
      const radius = 40 + 150 * Math.sqrt((i + 1) / Math.max(1, data.length));
      positions.set(n.id, {
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
        degree: 0,
      });
    });
    const preview = sparseConnections(data, edges);
    const links = edges.filter(
      (e) =>
        (allLinks || preview.has(e.id)) &&
        positions.has(from(e)) &&
        positions.has(to(e)),
    );
    links.forEach((e) => {
      positions.get(from(e)).degree++;
      positions.get(to(e)).degree++;
    });
    const values = [...positions.entries()];
    for (let step = 0; step < 150; step++) {
      const force = new Map(
        values.map(([id, p]) => [id, { x: -p.x * 0.004, y: -p.y * 0.004 }]),
      );
      for (let i = 0; i < values.length; i++)
        for (let j = i + 1; j < values.length; j++) {
          const [a, p] = values[i],
            [b, q] = values[j];
          const dx = p.x - q.x,
            dy = p.y - q.y,
            d = Math.max(16, Math.hypot(dx, dy));
          const amount = Math.min(14, 1800 / (d * d));
          force.get(a).x += (dx / d) * amount;
          force.get(a).y += (dy / d) * amount;
          force.get(b).x -= (dx / d) * amount;
          force.get(b).y -= (dy / d) * amount;
        }
      for (const e of links) {
        const a = positions.get(from(e)),
          b = positions.get(to(e)),
          dx = b.x - a.x,
          dy = b.y - a.y,
          d = Math.max(1, Math.hypot(dx, dy));
        const amount =
          ((d - 105) * 0.018) /
          (1 + Math.sqrt(Math.max(a.degree, b.degree)) * 0.2);
        force.get(from(e)).x += (dx / d) * amount;
        force.get(from(e)).y += (dy / d) * amount;
        force.get(to(e)).x -= (dx / d) * amount;
        force.get(to(e)).y -= (dy / d) * amount;
      }
      for (const [id, p] of values) {
        if (id === focusId) {
          p.x = 0;
          p.y = 0;
          continue;
        }
        p.x += Math.max(-8, Math.min(8, force.get(id).x));
        p.y += Math.max(-8, Math.min(8, force.get(id).y));
      }
    }
    const maxX = Math.max(100, ...values.map(([, p]) => Math.abs(p.x))),
      maxY = Math.max(100, ...values.map(([, p]) => Math.abs(p.y)));
    const sx = Math.min(Math.max(70, w / 2 - 125) / maxX, 3.5),
      sy = Math.min(Math.max(60, h / 2 - 95) / maxY, 2.8);
    for (const [, p] of values) {
      p.x *= sx;
      p.y *= sy;
    }
    return positions;
  }
  function layout() {
    const { w, h } = dimensions(),
      nodes = [],
      links = [],
      labels = [];
    if (S.kind === "overview" || S.payload.mode === "groups") {
      const grouped = S.payload.mode === "groups";
      const areas = grouped ? currentNodes() : arrangeAreas(currentNodes()),
        clouds = S.payload.clouds || [],
        cols = w < 700 ? 2 : 3,
        rows = Math.ceil(areas.length / cols);
      const cw = Math.min(390, w / cols),
        ch = Math.min(300, (h - 65) / rows),
        left = (-cw * (cols - 1)) / 2,
        top = (-ch * (rows - 1)) / 2 + 14;
      const anchors = new Map();
      areas.forEach((a, index) => {
        const placements =
          !grouped && cols === 3 && areas.length === 6 && h >= 540
            ? [
                [-0.97, -0.8],
                [0.99, -0.84],
                [0.01, 0.04],
                [-1.05, 0.88],
                [1.07, 0.84],
                [0.02, 1.05],
              ]
            : null;
        const x =
          placements
            ? placements[index][0] * cw
            : left + (index % cols) * cw;
        const minY = -h / 2 + (grouped ? 110 : 185),
          maxY = h / 2 - (grouped ? 100 : 165);
        const y = grouped
          ? -h / 2 +
            155 +
            (Math.floor(index / cols) * Math.max(0, h - 265)) /
              Math.max(1, rows - 1)
          : placements
            ? minY +
              ((placements[index][1] + 0.84) / 1.89) * Math.max(0, maxY - minY)
            : top + Math.floor(index / cols) * ch;
        const col = grouped ? color(a) : areaColor(a.id);
        anchors.set(a.id, { x, y });
        nodes.push({
          id: a.id,
          x,
          y,
          fx: x,
          fy: y,
          anchor: true,
          color: col,
          __node: a,
        });
        const cloud = clouds.find((c) => c.area_id === a.id),
          radius = Math.min(
            190,
            cw * 0.46,
            placements ? 190 : Math.max(45, ch - 96),
          ),
          ids = new Map();
        // Reuse the bounded connection layout; all sampled edges influence
        // the shape. Positions remain navigation, never an evidence score.
        const local = materialPositions(
          cloud?.nodes || [],
          cloud?.edges || [],
          radius * 2 + 250,
          radius * 0.96 + 190,
          null,
          true,
        );
        (cloud?.nodes || []).forEach((n) => {
          const point = local.get(n.id),
            xx = x + point.x,
            yy = y + point.y;
          const id = a.id + "::" + n.id;
          ids.set(n.id, id);
          nodes.push({
            id,
            x: xx,
            y: yy,
            fx: xx,
            fy: yy,
            r: 3,
            color: col,
            areaId: a.id,
            __node: n,
          });
        });
        for (const e of cloud?.edges || [])
          if (S.channels.has(channel(e)))
            links.push({
              ...e,
              id: a.id + "::" + e.id,
              source: ids.get(from(e)),
              target: ids.get(to(e)),
              texture: true,
              __edge: e,
            });
        const title = btn(null, "v2-area-label", () => openNode(a));
        title.dataset.areaId = a.id;
        title.style.setProperty("--cluster", col);
        title.append(
          el("strong", label(a)),
          el(
            "small",
            grouped
              ? `${a.count.toLocaleString()} item${a.count === 1 ? "" : "s"}`
              : `${cloud?.shown_material || 0} of ${(cloud?.total_material || 0).toLocaleString()} items shown`,
          ),
        );
        title.addEventListener("mouseenter", () => (S.hover = a.id));
        title.addEventListener("mouseleave", () => (S.hover = null));
        title.title = `${cloud?.shown_material || 0} of ${cloud?.total_material || 0} items shown. Open to browse.`;
        if (placements && index === 5) {
          title.style.transform = "translate(-50%,0)";
          labels.push({
            element: title,
            x,
            y: y + radius * 0.48 + 8,
            bottom: true,
          });
        } else labels.push({ element: title, x, y: y - radius * 0.48 - 22 });
      });
      for (const e of currentEdges())
        if (anchors.has(from(e)) && anchors.has(to(e)))
          links.push({
            ...e,
            source: from(e),
            target: to(e),
            bridge: true,
            __edge: e,
          });
    } else {
      const data = currentNodes();
      const focusId =
        S.kind === "thread"
          ? identity(S.thread?.focus)
          : identity(S.payload.anchor);
      const positions = materialPositions(data, currentEdges(), w, h, focusId);
      const preview = sparseConnections(data, currentEdges());
      data.forEach((n) => {
        const position = positions.get(n.id),
          center = identity(n) === focusId;
        const nav = kind(n) === "bundle";
        nodes.push({
          ...clone(n),
          ...position,
          fx: position.x,
          fy: position.y,
          r: nav ? 11 : center ? 6 : kind(n) === "source" ? 3.5 : 4.5,
          color: color(n),
          center,
          __node: n,
        });
        const title = btn(
          null,
          "v2-node-label" + (center ? " focus" : "") + (nav ? " bundle" : ""),
          () => openNode(n),
        );
        title.dataset.nodeId = n.id;
        title.append(el("span", label(n)));
        if (nav) title.append(el("small", n.description));
        title.title = label(n);
        title.style.setProperty("--cluster", color(n));
        labels.push({
          element: title,
          x: position.x,
          y: position.y + 12,
          nodeId: n.id,
          priority:
            (center ? 10000 : kind(n) === "record" ? 200 : 0) + position.degree,
          artifact:
            kind(n) === "source" &&
            (/\.(jsonl?|ya?ml)$/.test(n.label) ||
              /\/(?:review|integration-review)\//.test(n.label)),
          nav,
        });
      });
      for (const e of currentEdges())
        if (
          nodes.some((n) => n.id === from(e)) &&
          nodes.some((n) => n.id === to(e))
        )
          links.push({
            ...e,
            source: from(e),
            target: to(e),
            preview: preview.has(e.id),
            __edge: e,
          });
    }
    const pairs = new Map();
    for (const e of links) {
      const key = [e.source, e.target].sort().join("|");
      if (!pairs.has(key)) pairs.set(key, []);
      pairs.get(key).push(e);
    }
    for (const values of pairs.values())
      values.forEach(
        (e, i) =>
          (e.lane =
            (i - (values.length - 1) / 2) *
            20 *
            (String(e.source) < String(e.target) ? 1 : -1)),
      );
    return { nodes, links, labels };
  }
  function curve(ctx, a, b, lane) {
    const dx = b.x - a.x,
      dy = b.y - a.y,
      d = Math.hypot(dx, dy) || 1,
      cx = (a.x + b.x) / 2 - (dy / d) * lane,
      cy = (a.y + b.y) / 2 + (dx / d) * lane;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo(cx, cy, b.x, b.y);
    return { cx, cy };
  }
  function initGraph() {
    if (S.graph) return true;
    if (typeof ForceGraph !== "function") {
      notice(
        "The map renderer is unavailable. The same material is available in List.",
      );
      S.view = "list";
      return false;
    }
    try {
      S.graph = ForceGraph()($("map"))
        .backgroundColor("#151517")
        .nodeId("id")
        .nodeLabel(() => false)
        .linkLabel(() => false)
        .enableNodeDrag(false)
        .cooldownTicks(0)
        .autoPauseRedraw(false)
        .onRenderFramePost(() => positionLabels())
        .nodeCanvasObjectMode(() => "replace")
        .nodeCanvasObject((n, ctx, scale) => {
          if (n.anchor) return;
          const active = activeNode(n),
            r = (active ? Math.max(7, n.r) : n.r) / Math.max(0.6, scale);
          ctx.save();
          ctx.globalAlpha =
            S.hover &&
            ["overview", "groups"].includes(
              S.kind === "overview" ? "overview" : S.payload.mode,
            ) &&
            S.hover !== n.areaId
              ? 0.4
              : 0.96;
          ctx.fillStyle = n.color;
          ctx.beginPath();
          ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
          if (["bundle"].includes(kind(n))) {
            ctx.strokeStyle = n.color;
            ctx.lineWidth = 1.2 / scale;
            ctx.stroke();
          } else ctx.fill();
          if (active || n.center || S.hoverNode === n.id) {
            ctx.strokeStyle = "#DEDEF0";
            ctx.lineWidth = 1 / scale;
            ctx.beginPath();
            ctx.arc(n.x, n.y, r + 4 / scale, 0, Math.PI * 2);
            ctx.stroke();
          }
          if (suggested(n.__node)) {
            ctx.setLineDash([2 / scale, 2 / scale]);
            ctx.strokeStyle = n.color;
            ctx.beginPath();
            ctx.arc(n.x, n.y, r + 3 / scale, 0, Math.PI * 2);
            ctx.stroke();
          }
          ctx.restore();
        })
        .nodePointerAreaPaint((n, c, ctx) => {
          if (n.anchor) return;
          ctx.fillStyle = c;
          ctx.beginPath();
          ctx.arc(n.x, n.y, Math.max(9, n.r || 4), 0, Math.PI * 2);
          ctx.fill();
        })
        .linkCanvasObjectMode(() => "replace")
        .linkCanvasObject((e, ctx, scale) => {
          const a = e.source,
            b = e.target;
          if (typeof a !== "object" || typeof b !== "object") return;
          if (
            e.preview === false &&
            scale < 1.6 &&
            ![selectedId(), S.hoverNode].some(
              (id) => id && [a.id, b.id].includes(id),
            )
          )
            return;
          const sel = S.edge && S.edge.id === e.__edge?.id,
            derived = channel(e) !== "recorded";
          ctx.save();
          const relevant =
            !S.selected ||
            [identity(S.selected), S.selected.id].some((id) =>
              [a.id, b.id].includes(id),
            );
          ctx.strokeStyle = sel ? "#DEDEF0" : e.texture ? a.color : "#B1AFB9";
          ctx.globalAlpha = sel
            ? 1
            : e.texture
              ? 0.28
              : e.bridge
                ? 0.46
                : relevant
                  ? 0.48
                  : 0.12;
          ctx.lineWidth = (sel ? 1.6 : e.texture ? 0.55 : 0.9) / scale;
          ctx.setLineDash(
            suggested(e)
              ? [5 / scale, 4 / scale]
              : derived
                ? [2 / scale, 4 / scale]
                : [],
          );
          const c = curve(ctx, a, b, e.lane || 0);
          ctx.stroke();
          if (!derived && !e.texture) {
            const t = Math.atan2(b.y - c.cy, b.x - c.cx),
              r = (b.r || 5) / scale + 3 / scale,
              x = b.x - Math.cos(t) * r,
              y = b.y - Math.sin(t) * r;
            ctx.setLineDash([]);
            ctx.fillStyle = ctx.strokeStyle;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(
              x - (Math.cos(t - 0.45) * 6) / scale,
              y - (Math.sin(t - 0.45) * 6) / scale,
            );
            ctx.lineTo(
              x - (Math.cos(t + 0.45) * 6) / scale,
              y - (Math.sin(t + 0.45) * 6) / scale,
            );
            ctx.fill();
          }
          ctx.restore();
        })
        .linkPointerAreaPaint((e, c, ctx) => {
          if (
            e.preview === false &&
            S.graph.zoom() < 1.6 &&
            ![selectedId(), S.hoverNode].some(
              (id) => id && [e.source.id, e.target.id].includes(id),
            )
          )
            return;
          ctx.strokeStyle = c;
          ctx.lineWidth = 12;
          curve(ctx, e.source, e.target, e.lane || 0);
          ctx.stroke();
        })
        .onNodeClick((n) => {
          if (n.anchor) return;
          if (S.kind === "overview") goArea(n.areaId, 0, n.__node);
          else if (S.payload.mode === "groups") openNode(nodeById(n.areaId));
          else openNode(n.__node);
        })
        .onNodeHover((n) => {
          S.hover = n?.areaId || null;
          S.hoverNode = n?.id || null;
          $("map").style.cursor = n && !n.anchor ? "pointer" : "grab";
        })
        .onLinkClick((e) => {
          selectEdge(e.__edge);
        })
        .onZoom(({ k }) => {
          $("zoom-label").textContent = `${Math.round(k * 100)}%`;
          positionLabels();
        });
      return true;
    } catch (e) {
      notice(
        "The visual map could not open. Use List to explore the same material.",
      );
      S.view = "list";
      return false;
    }
  }
  function drawScene(force = false) {
    const { w, h } = dimensions();
    if (!w || !h || S.view !== "map") return;
    if (!initGraph()) {
      renderList();
      $("map-panel").hidden = true;
      $("list-panel").hidden = false;
      return;
    }
    const key = [
      S.revision,
      S.organization,
      S.kind,
      S.sceneOffset,
      S.threadPage,
      S.payload?.area?.id,
      S.payload?.group_path,
      S.payload?.bundle,
      S.payload?.query,
      w,
      h,
      [...S.channels].join(","),
    ].join("|");
    if (!force && key === S.layoutKey) {
      for (const l of S.labels)
        if (l.element.dataset.nodeId)
          l.element.classList.toggle(
            "selected",
            identity(nodeById(l.element.dataset.nodeId)) === selectedId(),
          );
      return;
    }
    S.layoutKey = key;
    const data = layout();
    S.graphNodes = data.nodes;
    S.graphLinks = data.links;
    S.labels = data.labels;
    $("labels").replaceChildren(...data.labels.map((x) => x.element));
    if (!data.nodes.length && S.kind === "overview") {
      const empty = el("div", null, "v2-map-empty");
      empty.append(
        el("h2", "A place for your knowledge"),
        el(
          "p",
          S.payload.loose?.count
            ? "Your material is available. Areas appear when enough related material gathers."
            : "No grouped material is available in this saved view.",
        ),
      );
      if (S.payload.loose?.count)
        empty.append(
          btn("Browse ungrouped material", "v2-button", () => goArea("loose")),
        );
      $("labels").append(empty);
    }

    S.graph
      .width(w)
      .height(h)
      .graphData({ nodes: data.nodes, links: data.links });
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (S.layoutKey !== key) return;
        fit();
        $("map").dataset.ready = key;
      }),
    );
  }
  function positionLabels() {
    if (!S.graph) return;
    const { w, h } = dimensions(),
      zoom = S.graph.zoom(),
      occupied = [];
    const material = !(S.kind === "overview" || S.payload.mode === "groups");
    const order = [...S.labels].sort(
      (a, b) =>
        Number(b.nodeId === selectedId() || b.nodeId === S.hoverNode) * 100000 +
        (b.priority || 0) -
        (Number(a.nodeId === selectedId() || a.nodeId === S.hoverNode) *
          100000 +
          (a.priority || 0)),
    );
    let shown = 0;
    for (const item of order) {
      const p = S.graph.graph2ScreenCoords(item.x, item.y),
        selected =
          item.nodeId &&
          (item.nodeId === selectedId() || item.nodeId === S.hoverNode);
      const width = material
          ? Math.min(item.nav ? 205 : 180, w - 30)
          : item.element.offsetWidth || 225,
        height = material
          ? item.nav
            ? 66
            : 55
          : item.element.offsetHeight || 60;
      const box = {
        l: p.x - width / 2,
        r: p.x + width / 2,
        t: material || item.bottom ? p.y : p.y - height,
        b: material || item.bottom ? p.y + height : p.y,
      };
      const overlap = occupied.some(
        (q) =>
          box.l < q.r + 10 &&
          box.r > q.l - 10 &&
          box.t < q.b + 8 &&
          box.b > q.t - 8,
      );
      const budget = zoom >= 1.6 ? 40 : zoom >= 1.15 ? 20 : 12;
      const visible =
        p.x > width / 2 + 8 &&
        p.x < w - width / 2 - 8 &&
        box.t > 8 &&
        box.b < h - (material ? 50 : 2) &&
        (!material ||
          selected ||
          ((!item.artifact || zoom >= 1.6) &&
            (item.nav || shown < budget) &&
            !overlap));
      item.element.style.left = p.x + "px";
      item.element.style.top = p.y + "px";
      item.element.hidden = !visible;
      if (visible) {
        occupied.push(box);
        shown++;
      }
      item.element.classList.toggle(
        "selected",
        Boolean(item.nodeId && item.nodeId === selectedId()),
      );
    }
  }
  function fit() {
    if (!S.graph) return;
    S.graph.centerAt(0, 0, 0).zoom(1, 0);
    positionLabels();
  }
  function inspectorShell(node, isEdge = false) {
    $("inspector").hidden = false;
    $("inspector").scrollTop = 0;
    $("detail-kind").textContent = isEdge
      ? connectionType(node)
      : kind(node) === "area"
        ? "Area"
        : shortKind(node);
    $("detail-name").textContent = isEdge ? edgeName(node) : label(node);
    $("detail-meta").textContent = isEdge
      ? ""
      : kind(node) === "area"
        ? coverage(node.coverage)
        : "";
    for (const id of [
      "detail-flags",
      "detail-body",
      "detail-actions",
      "detail-links",
      "detail-status",
    ])
      $(id).replaceChildren();
    $("detail-provenance").open = false;
    $("copy-status").textContent = "";
    $("copy").disabled = true;
    render(false);
  }
  function human(value) {
    if (value == null) return "Not recorded";
    if (typeof value === "string")
      return value.replaceAll("_", " ").replaceAll("-", " ");
    if (Array.isArray(value))
      return value.length ? value.map(human).join(" · ") : "None recorded";
    return Object.entries(value)
      .map(([k, v]) => `${human(k)}: ${human(v)}`)
      .join(" · ");
  }
  function status(labelText, value) {
    if (value === undefined) return;
    const row = el("div", null, "v2-status-row");
    row.append(el("span", labelText), el("strong", human(value)));
    $("detail-status").append(row);
  }
  function structured(value, title = "Complete qualified context") {
    const details = el("details");
    details.append(
      el("summary", title),
      el("pre", JSON.stringify(value, null, 2)),
    );
    $("detail-status").append(details);
  }
  function qualification(node, data) {
    $("detail-status").replaceChildren();
    $("detail-flags").replaceChildren();
    const units = data?.context?.items || data?.items || [];
    const records = units.flatMap((u) => u.records || []);
    const record = records.find((r) => r.id === identity(node)) || node;
    const q = node.qualification || {};
    if (suggested(record) || suggested(node))
      $("detail-flags").append(
        el(
          "p",
          "Synapse suggestion · not adopted by you",
          "v2-flag suggestion",
        ),
      );
    if (q.requires_revalidation || units.some((u) => u.requires_revalidation))
      $("detail-flags").append(
        el(
          "p",
          "Needs revalidation: a premise has changed or is unavailable.",
          "v2-flag",
        ),
      );
    if (units.some((u) => u.qualified || u.notices?.length))
      $("detail-flags").append(
        el(
          "p",
          "This record has qualifying context. Open Evidence & review details to see the corrections or conditions.",
          "v2-flag",
        ),
      );
    if (units.some((u) => u.provisional_dependencies?.length))
      $("detail-flags").append(
        el(
          "p",
          "Uses unreviewed premises. Read the evidence details before relying on this.",
          "v2-flag",
        ),
      );
    if (units.some((u) => u.withheld) || q.withheld)
      $("detail-flags").append(
        el(
          "p",
          "Complete supporting context is not available for this selection.",
          "v2-flag",
        ),
      );
    if (
      q.extraction_state === "partial" ||
      data?.source?.extraction?.completeness === "partial"
    )
      $("detail-flags").append(
        el(
          "p",
          "This source was only partly extracted. Some material may be missing.",
          "v2-flag",
        ),
      );
    const fields = [
      ["Availability", "availability"],
      ["Your review", "owner_review"],
      ["Your position", "owner_position"],
      ["Evidence basis", "epistemic_basis"],
      ["Support", "support"],
      ["Lifecycle", "lifecycle"],
      ["Attestation", "review_status"],
    ];
    for (const [title, key] of fields) status(title, record[key] ?? q[key]);
    status("Original title or path", node.label || node.name);
    status("Knowledge revision", S.revision);
    if (node.ref)
      status(
        "Material version",
        node.ref.record_version || node.ref.source_version,
      );
    if (q.extraction) status("Extraction", q.extraction);
    if (records.some((r) => r.conditions_and_limits))
      $("detail-flags").append(
        el(
          "p",
          records
            .filter((r) => r.conditions_and_limits)
            .map((r) => r.conditions_and_limits)
            .join("\n"),
          "v2-flag",
        ),
      );
    structured(data?.context || data || q);
    if (node.qualification)
      structured(node.qualification, "Navigation qualifications");
    $("copy").disabled = !!(
      node.state === "unavailable" || units.some((u) => u.withheld)
    );
  }
  function prose(text) {
    const wrap = el("div");
    for (const part of String(text || "").split(/\n\s*\n/)) {
      const value = part.trim();
      if (!value) continue;
      const p = el(
        /^#{1,6}\s/.test(value) ? "h4" : "p",
        value
          .replace(/^#{1,6}\s/, "")
          .replace(/\[\[[^\]|]+\|([^\]]+)\]\]/g, "$1")
          .replace(/\[\[([^\]]+)\]\]/g, "$1")
          .replace(/\*\*([^*]+)\*\*/g, "$1"),
      );
      wrap.append(p);
    }
    return wrap;
  }
  function bodyText(raw) {
    let text = String(raw || "")
      .replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "")
      .trim();
    text = text.replace(/^# [^\n]+\n+/, "");
    return text;
  }
  function summaryText(text) {
    const blocks = String(text || "")
      .split(/\n\s*\n/)
      .filter((x) => x.trim() && !/^#+\s/.test(x));
    let result = blocks.slice(0, 2).join("\n\n");
    if (result.length > 340) {
      result = result.slice(0, 340).replace(/\s+\S*$/, "");
      const end = result.lastIndexOf(". ");
      result = (end > 150 ? result.slice(0, end + 1) : result) + "…";
    }
    return result;
  }
  function evidenceRefs(data) {
    const refs = [];
    for (const unit of data?.context?.items || data?.items || [])
      for (const r of unit.records || [])
        for (const ref of r.evidence || [])
          if (!refs.some((x) => JSON.stringify(x) === JSON.stringify(ref)))
            refs.push(ref);
    return refs;
  }
  async function detailData(node, revision = S.revision) {
    if (kind(node) === "area") return { area: node };
    if (kind(node) === "source")
      return {
        source: await read(
          "source",
          {
            id: node.ref.source_id,
            version: node.ref.source_version,
            offset: 0,
            limit: 4000,
          },
          revision,
        ),
      };
    const id = identity(node);
    const [context, record] = await Promise.all([
      read(
        "context",
        { ids: [id], knowledge_policy: "mixed", supports_qualifications: true },
        revision,
      ),
      read("record", { id, offset: 0, limit: 4000 }, revision),
    ]);
    return { context, record };
  }
  async function selectNode(node) {
    S.sceneToken++;
    S.selected = clone(node);
    S.edge = null;
    S.detail = null;
    S.exact = null;
    const token = ++S.detailToken;
    S.busy = true;
    inspectorShell(node);
    $("detail-body").append(el("p", "Reading this material…"));
    try {
      const data = await detailData(node);
      if (token !== S.detailToken) return;
      S.detail = data;
      S.busy = false;
      renderDetail(node, data);
      session(data.context || data.source);
    } catch (e) {
      if (token === S.detailToken) {
        S.busy = false;
        $("detail-body").replaceChildren(el("p", e.message));
        $("detail-actions").append(
          btn("Try again", "v2-link-button", () => selectNode(node)),
        );
      }
    }
  }
  function renderDetail(node, data) {
    $("detail-body").replaceChildren();
    $("detail-actions").replaceChildren();
    $("detail-links").replaceChildren();
    qualification(node, data);
    if (node.state === "unavailable") {
      $("detail-body").append(
        el("p", "This material is unavailable in this saved version."),
      );
      return;
    }
    const source = data.source,
      record = data.record,
      units = data.context?.items || [],
      subject = units
        .flatMap((u) => u.records || [])
        .find((r) => r.id === identity(node));
    const text =
      source?.text ||
      subject?.statement ||
      bodyText(record?.text) ||
      "No readable summary is available. Open the exact material for its retained content.";
    $("detail-meta").textContent = source
      ? "Excerpt from the saved source"
      : "Excerpt from the saved record";
    $("detail-body").append(prose(summaryText(text)));
    const refArgs = source
      ? {
          id: node.ref.source_id,
          version: node.ref.source_version,
          offset: 0,
          limit: 4000,
        }
      : { id: identity(node), offset: 0, limit: 4000 };
    $("detail-actions").append(
      btn(source ? "Read source" : "Read full record", "v2-link-button", () =>
        readExact(source ? "source" : "record", refArgs),
      ),
    );
    const refs = evidenceRefs(data);
    if (refs.length) {
      $("detail-body").append(el("h4", "Source evidence"));
      const b = btn("Read exact passage", "v2-link-button", () =>
        readExact("passage", { evidence: refs[0], context_characters: 200 }),
      );
      $("detail-body").append(b);
      if (refs.length > 1)
        $("detail-body").append(
          el(
            "p",
            `${refs.length} supporting passages · all references are in Evidence & review details.`,
            "v2-detail-meta",
          ),
        );
    }
    if (
      source?.extraction?.completeness === "unavailable" ||
      (source && !source.text)
    )
      $("detail-flags").append(
        el(
          "p",
          "No readable text was extracted. The retained original is still preserved.",
          "v2-flag",
        ),
      );
    $("detail-actions").append(
      btn(
        S.kind === "thread" ? "Continue this thread" : "Follow in Threads",
        "v2-link-button",
        () => enterThreads(node),
      ),
    );
    const rel = currentEdges().filter((e) =>
      [from(e), to(e)].some((id) => id === node.id || id === identity(node)),
    );
    if (rel.length) {
      $("detail-links").append(el("h4", "Connections", "v2-list-heading"));
      for (const e of rel.slice(0, 1)) {
        const other = nodeById(
          from(e) === node.id || from(e) === identity(node) ? to(e) : from(e),
        );
        const b = btn(null, "v2-related", () => selectEdge(e));
        b.append(
          el("span", other ? label(other) : "Related material"),
          el("small", connectionType(e)),
        );
        $("detail-links").append(b);
      }
    }
    $("copy-status").textContent =
      "Copies context for your agent. Nothing is sent or saved.";
  }
  function connectionType(e) {
    return suggested(e)
      ? "Synapse suggestion"
      : channel(e) === "similarity"
        ? "Computed similarity"
        : channel(e) === "overlap"
          ? "Shared material"
          : e.qualification?.state === "legacy-edge"
            ? "Recorded · legacy evidence"
            : "Recorded connection";
  }
  async function selectEdge(edge) {
    S.sceneToken++;
    S.edge = clone(edge);
    S.selected = null;
    S.detail = null;
    S.exact = null;
    const token = ++S.detailToken;
    S.busy = !!edge.assertion_id;
    inspectorShell(edge, true);
    renderEdge(edge);
    if (edge.assertion_id) {
      try {
        const context = await read("context", {
          ids: [edge.assertion_id],
          knowledge_policy: "mixed",
          supports_qualifications: true,
        });
        if (token !== S.detailToken) return;
        S.detail = { context };
        S.busy = false;
        renderEdge(edge, S.detail);
      } catch (e) {
        if (token === S.detailToken) {
          S.busy = false;
          notice(e.message);
        }
      }
    } else {
      S.detail = { connection: clone(edge) };
      S.busy = false;
      $("copy").disabled = false;
    }
  }
  function renderEdge(edge, data) {
    $("detail-body").replaceChildren();
    $("detail-actions").replaceChildren();
    $("detail-links").replaceChildren();
    qualification(edge, data);
    const relation = edge.relation || edge.type || edge.metadata?.type;
    $("detail-body").append(
      el(
        "p",
        edge.explanation ||
          edge.interpretation ||
          (relation
            ? `The retained relationship is “${human(relation)}”.`
            : "This recorded connection links the selected material."),
      ),
    );
    if (channel(edge) === "similarity" || channel(edge) === "overlap")
      $("detail-body").append(
        el(
          "p",
          channel(edge) === "overlap"
            ? "These areas contain shared material. This does not imply that one causes or supports the other."
            : edge.method === "lexical"
              ? "Computed from shared words and phrases. This does not establish a substantive relationship or support a claim."
              : edge.method === "vector"
                ? "Computed from local text-vector similarity. This does not establish a substantive relationship or support a claim."
                : "Computed from text similarity across retained material. This does not establish a substantive relationship or support a claim.",
        ),
      );
    if (edge.qualification?.state === "legacy-edge" || edge.legacy_edge_id)
      $("detail-flags").append(
        el(
          "p",
          "Legacy connection: passage-level evidence may be incomplete.",
          "v2-flag",
        ),
      );
    for (const id of [from(edge), to(edge)]) {
      const n = nodeById(id);
      if (n)
        $("detail-actions").append(
          btn(`Open ${label(n)}`, "v2-link-button", () => openNode(n)),
        );
    }
    if (edge.expansion?.area_id)
      $("detail-actions").append(
        btn(channel(edge) === "overlap" ? "Explore shared material" : "Explore related material", "v2-link-button", () =>
          goArea(edge.expansion.area_id),
        ),
      );
    const refs = evidenceRefs(data);
    for (const [i, ref] of refs.entries())
      $("detail-actions").append(
        btn(
          `Read exact passage${refs.length > 1 ? " " + (i + 1) : ""}`,
          "v2-link-button",
          () =>
            readExact("passage", { evidence: ref, context_characters: 200 }),
        ),
      );
    if (data?.context && !refs.length)
      $("detail-body").append(
        el(
          "p",
          "No passage-level evidence was returned for this connection.",
          "v2-detail-meta",
        ),
      );
    $("copy").disabled = !!S.busy;
  }
  async function readExact(operation, args) {
    const token = ++S.detailToken;
    S.busy = true;
    $("copy").disabled = true;
    try {
      const payload = await read(operation, args);
      if (token !== S.detailToken) return;
      S.exact = { operation, arguments: clone(args), response: payload };
      S.busy = false;
      renderExact();
      session(payload);
    } catch (e) {
      if (token === S.detailToken) {
        S.busy = false;
        notice(e.message + " The previous selection is preserved.");
        $("copy").disabled = !S.detail;
      }
    }
  }
  function renderExact() {
    const { operation, arguments: args, response: p } = S.exact;
    $("detail-body").replaceChildren();
    $("detail-actions").replaceChildren();
    $("detail-links").replaceChildren();
    $("detail-meta").textContent =
      operation === "passage"
        ? "Exact source passage"
        : operation === "source"
          ? "Original source · saved text"
          : "Exact retained record";
    $("detail-body").append(
      el(
        operation === "passage" ? "blockquote" : "pre",
        p.excerpt || p.text || "No readable text is available.",
      ),
    );
    if (operation === "passage" && p.context && p.context !== p.excerpt) {
      const d = el("details");
      d.append(el("summary", "Surrounding context"), el("p", p.context));
      $("detail-body").append(d);
    }
    if (p.next_offset != null)
      $("detail-actions").append(
        btn("Read next page", "v2-link-button", () =>
          readExact(operation, { ...args, offset: p.next_offset }),
        ),
      );
    if (args.offset > 0)
      $("detail-actions").append(
        btn("Start of source", "v2-link-button", () =>
          readExact(operation, { ...args, offset: 0 }),
        ),
      );
    if (operation === "passage" && p.source_id)
      $("detail-actions").append(
        btn("Read full source", "v2-link-button", () =>
          readExact("source", {
            id: p.source_id,
            version: p.source_version,
            offset: 0,
            limit: 4000,
          }),
        ),
      );
    $("detail-actions").append(
      btn("Back to context", "v2-link-button", () => {
        S.exact = null;
        if (S.edge) renderEdge(S.edge, S.detail);
        else renderDetail(S.selected, S.detail);
        $("detail-meta").textContent = "";
      }),
    );
    status("Displayed source version", p.source_version);
    if (p.byte_start != null)
      status("Exact byte range", `${p.byte_start}–${p.byte_end}`);
    $("copy").disabled = !S.detail;
  }
  async function threadData(
    focus,
    revision = S.revision,
    organization = S.organization,
    bundle = "",
    offset = 0,
    query = "",
  ) {
    return api(
      url("/api/v2/threads", {
        id: identity(focus),
        revision,
        organization_revision: organization,
        bundle,
        offset,
        q: query,
      }),
    );
  }
  function commitThread(data) {
    S.thread = { focus: clone(data.focus), data };
    S.entry = "threads";
    S.threadPage = data.page?.offset || 0;
    if (!S.narrow) S.view = "map";
    commit(data, "thread");
    if (!data.total_neighbors)
      notice(
        "No eligible connections are saved for this item. Its original text is still available.",
      );
    else if (data.mode === "bundles")
      notice(
        "Bundles organize existing connections. Open one to inspect the individual relationships and evidence.",
      );
  }
  async function enterThreads(focus = S.selected) {
    if (!focus || ["area", "group", "bundle"].includes(kind(focus))) {
      notice(
        "Choose a note, person or source first, then follow its connections.",
      );
      return;
    }
    const token = ++S.sceneToken;
    invalidate();
    notice("Following these connections…");
    try {
      const data = await threadData(focus);
      if (token !== S.sceneToken) return;
      if (S.kind !== "thread") {
        S.returnScene = {
          payload: S.payload,
          kind: S.kind,
          area: S.area,
          view: S.view,
        };
        S.threadTrail = [];
      } else if (identity(S.thread.focus) !== identity(focus))
        S.threadTrail.push({ focus: S.thread.focus, data: S.thread.data });
      S.edge = null;
      S.detail = null;
      S.exact = null;
      S.selected = data.focus;
      S.busy = false;
      $("search").value = "";
      $("search-scope").value = "context";
      closeDirectory();
      commitThread(data);
      selectNode(data.focus);
    } catch (e) {
      if (token === S.sceneToken) {
        restoreRead();
        notice(e.message + " Your current view is preserved.");
      }
    }
  }
  async function loadThreadView(bundle = "", offset = 0, query = "") {
    if (!S.thread) return;
    const token = ++S.sceneToken;
    invalidate();
    try {
      const data = await threadData(
        S.thread.focus,
        S.revision,
        S.organization,
        bundle,
        offset,
        query,
      );
      if (token !== S.sceneToken) return;
      restoreRead();
      $("search").value = query;
      commitThread(data);
    } catch (e) {
      if (token === S.sceneToken) {
        restoreRead();
        notice(e.message + " Your current view is preserved.");
      }
    }
  }
  function restoreThread(index) {
    const previous = S.threadTrail[index];
    if (!previous) return;
    S.sceneToken++;
    S.detailToken++;
    S.threadTrail = S.threadTrail.slice(0, index);
    S.selected = previous.focus;
    S.edge = null;
    S.detail = null;
    S.exact = null;
    S.busy = false;
    $("search").value = previous.data.query || "";
    commitThread(previous.data);
    selectNode(previous.focus);
  }
  async function searchData(query, revision = S.revision) {
    const [records, sources] = await Promise.all([
      read(
        "context",
        {
          query,
          knowledge_policy: "mixed",
          limit: 20,
          supports_qualifications: true,
        },
        revision,
      ),
      read("search_sources", { query, offset: 0, limit: 20 }, revision),
    ]);
    const nodes = [],
      seen = new Set();
    for (const unit of records.items || [])
      for (const r of unit.records || []) {
        if (seen.has(r.id) || unit.withheld) continue;
        seen.add(r.id);
        nodes.push({
          ...r,
          id: r.id,
          kind: "record",
          label: r.name || r.statement || r.id,
          ref: { record_id: r.id, record_version: r.version },
          qualification: unit,
        });
      }
    for (const s of sources.items || []) {
      const id = s.source_id || s.id;
      if (seen.has(id)) continue;
      seen.add(id);
      nodes.push({
        id,
        kind: "source",
        label: s.origin || id,
        display_label: cleanTitle(s.origin || id),
        ref: {
          source_id: id,
          source_version: s.source_version || s.version,
          text_version: s.text_version,
        },
        qualification: s,
      });
    }
    return {
      knowledge_revision: revision,
      query,
      nodes,
      edges: [],
      page: { offset: 0, total: nodes.length, next_offset: null },
      session: records.session,
      search_context: records,
    };
  }
  function cleanTitle(text) {
    return String(text)
      .split("/")
      .pop()
      .split(" — ")
      .pop()
      .replace(/\.(md|txt|csv|json|pdf)$/i, "")
      .replace(/\b\d{4}-\d{2}-\d{2}\b/g, "")
      .replace(/[-_]+/g, " ")
      .trim();
  }
  async function search() {
    const q = $("search").value.trim();
    if (!q) return;
    const token = ++S.sceneToken;
    invalidate();
    notice("Searching your knowledge…");
    try {
      const p = await searchData(q);
      if (token !== S.sceneToken) return;
      S.query = q;
      S.entry = "map";
      S.view = "list";
      S.selected = null;
      S.edge = null;
      S.detail = null;
      S.exact = null;
      S.busy = false;
      closeDirectory();
      commit(p, "search");
      notice(
        "Showing initial matches from records and sources, up to 20 of each. Refine the search to find more.",
      );
    } catch (e) {
      if (token === S.sceneToken) {
        restoreRead();
        notice(e.message + " Your current view is preserved.");
      }
    }
  }
  async function copyContext() {
    const target = S.selected || S.edge;
    if (!target || !S.detail || S.busy || target.state === "unavailable")
      return;
    const text = JSON.stringify(
      {
        kind: "synapse-context",
        knowledge_revision: S.revision,
        organization_revision: S.organization,
        selection: target,
        qualified_context: S.detail,
        displayed_read: S.exact,
        meaning:
          "Selection and retained evidence for a conversation. Navigation similarity is not a fact; availability is independent of owner attestation.",
      },
      null,
      2,
    );
    try {
      await navigator.clipboard.writeText(text);
      $("copy-status").textContent =
        "Copied. Paste it into your conversation with an agent.";
    } catch {
      const box = el("textarea");
      box.value = text;
      box.style.width = "100%";
      box.style.minHeight = "130px";
      box.setAttribute("aria-label", "Context to copy manually");
      $("copy-status").replaceChildren(
        el("span", "Clipboard unavailable. Select and copy this context:"),
        box,
      );
      box.focus();
      box.select();
    }
  }
  async function refresh() {
    const token = ++S.sceneToken;
    const prior = {
      revision: S.revision,
      organization: S.organization,
      busy: S.busy,
      kind: S.kind,
      selected: clone(S.selected),
      edge: clone(S.edge),
      area: S.area,
      thread: S.thread,
      query: S.query,
      entry: S.entry,
    };
    S.detailToken++;
    notice("Refreshing your saved knowledge…");
    try {
      const next = await landscape(null, null, 0, prior.selected);
      const rev = next.knowledge_revision,
        org = next.organization_revision;
      if (rev === prior.revision && org === prior.organization && !prior.busy) {
        session(next);
        notice("You are viewing the latest saved knowledge.");
        return;
      }
      let p = next,
        k = "overview",
        selected = null,
        thread = null;
      if (prior.selected) {
        selected = next.selection?.member || null;
        if (next.selection?.state === "unavailable")
          selected = { ...prior.selected, state: "unavailable" };
      }
      if (
        prior.kind === "area" &&
        next.directory.some((a) => a.id === prior.area?.id)
      ) {
        p = await area(prior.area.id, rev, org, 0, selected);
        k = "area";
      }
      if (prior.kind === "search") {
        p = await searchData(prior.query, rev);
        k = "search";
      }
      if (
        prior.kind === "thread" &&
        selected &&
        selected.state !== "unavailable"
      ) {
        const data = await threadData(selected, rev, org);
        thread = { focus: selected, data };
      }
      const detail =
        selected && selected.state !== "unavailable"
          ? await detailData(selected, rev)
          : null;
      if (token !== S.sceneToken) return;
      S.returnScene = null;
      S.landscape = next;
      S.selected = selected;
      S.edge = null;
      S.detail = detail;
      S.exact = null;
      S.thread = thread;
      S.entry = thread ? "threads" : "map";
      S.revision = rev;
      S.organization = org;
      S.busy = false;
      S.threadTrail = [];
      if (thread) commitThread(thread.data);
      else commit(p, k);
      if (selected) {
        inspectorShell(selected);
        if (detail) renderDetail(selected, detail);
        else {
          $("detail-body").append(
            el(
              "p",
              "This selection is no longer available in the current saved knowledge.",
            ),
          );
          $("copy").disabled = true;
        }
      }
      if (prior.edge)
        notice(
          "The map is refreshed. Select the connection again to inspect its current evidence.",
        );
      else if (prior.kind === "area" && k === "overview")
        notice(
          "The areas have regrouped. Your underlying material is still searchable.",
        );
    } catch (e) {
      if (token === S.sceneToken)
        notice(e.message + " Your previous map and evidence are preserved.");
    }
  }
  function closeInspector() {
    S.detailToken++;
    S.selected = null;
    S.edge = null;
    S.detail = null;
    S.exact = null;
    S.busy = false;
    $("inspector").hidden = true;
    render(false);
  }
  function returnToMap() {
    S.sceneToken++;
    if (
      S.entry === "threads" &&
      S.returnScene?.payload.knowledge_revision === S.revision
    ) {
      S.entry = "map";
      S.area = S.returnScene.area;
      S.view = S.narrow ? "list" : S.returnScene.view || "map";
      $("search").value = S.returnScene.payload.query || "";
      commit(S.returnScene.payload, S.returnScene.kind);
      if (S.selected && S.detail) renderDetail(S.selected, S.detail);
    } else if (S.entry === "map") return;
    else goOverview(S.overviewOffset);
  }
  function bind() {
    $("home").addEventListener("click", (e) => {
      e.preventDefault();
      goOverview();
    });
    $("back").addEventListener("click", goBack);
    $("search-form").addEventListener("submit", (e) => {
      e.preventDefault();
      if (
        ["area", "thread"].includes(S.kind) &&
        $("search-scope").value === "context"
      ) {
        const q = $("search").value.trim();
        if (S.kind === "thread") loadThreadView(S.payload.bundle || "", 0, q);
        else goArea(S.area.id, 0, null, S.payload.group_path || "", q);
      } else search();
    });
    $("refresh").addEventListener("click", refresh);
    $("copy").addEventListener("click", copyContext);
    $("search-scope").addEventListener("change", () => render(false));
    $("detail-close").addEventListener("click", closeInspector);
    document
      .querySelectorAll("[data-v2-entry]")
      .forEach((b) =>
        b.addEventListener("click", () =>
          b.dataset.v2Entry === "map" ? returnToMap() : enterThreads(),
        ),
      );
    document.querySelectorAll("[data-view]").forEach((b) =>
      b.addEventListener("click", () => {
        S.view = b.dataset.view;
        render(true);
      }),
    );
    document.querySelectorAll("[data-channel]").forEach((b) =>
      b.addEventListener("change", () => {
        if (b.checked) S.channels.add(b.dataset.channel);
        else S.channels.delete(b.dataset.channel);
        render(true);
      }),
    );
    $("directory-toggle").addEventListener("click", () => {
      const open = $("directory").hidden;
      $("directory").hidden = !open;
      $("directory-toggle").setAttribute("aria-expanded", String(open));
      if (open) {
        renderDirectory();
        $("area-search").focus();
      }
    });
    $("directory-close").addEventListener("click", closeDirectory);
    $("area-search").addEventListener("input", renderDirectory);
    $("zoom-in").addEventListener("click", () =>
      S.graph?.zoom(Math.min(3, S.graph.zoom() * 1.2), 0),
    );
    $("zoom-out").addEventListener("click", () =>
      S.graph?.zoom(Math.max(0.4, S.graph.zoom() / 1.2), 0),
    );
    $("fit").addEventListener("click", fit);
    $("about").addEventListener("click", () => {
      $("about-state").replaceChildren(
        el(
          "pre",
          JSON.stringify(
            {
              knowledge_revision: S.revision,
              organization_revision: S.organization,
              method: S.landscape?.method,
              coverage: S.landscape?.coverage,
              limits: S.landscape?.limits,
            },
            null,
            2,
          ),
        ),
      );
      $("about-dialog").showModal();
    });
    $("about-close").addEventListener("click", () => $("about-dialog").close());
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(e.target.tagName)) {
        e.preventDefault();
        $("search").focus();
      }
      if (e.key === "Escape") {
        if (!$("directory").hidden) closeDirectory();
        else if (!$("inspector").hidden) closeInspector();
      }
    });
    let resizeTimer;
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const narrow = matchMedia("(max-width:480px)").matches;
        if (narrow && !S.narrow) S.view = "list";
        S.narrow = narrow;
        if (S.payload) render(false);
      }, 60);
    }).observe($("map"));
  }
  function showBoot(title, message, retry) {
    $("app").hidden = false;
    document.querySelector(".app-shell").hidden = true;
    $("boot").hidden = false;
    $("main").hidden = true;
    $("boot-title").textContent = title;
    $("boot-message").textContent = message;
    $("boot-retry").hidden = !retry;
  }
  async function start() {
    showBoot(
      "Opening your knowledge",
      "Preparing the saved map. A fresh grouping can take a moment.",
      false,
    );
    try {
      const s = await api("/api/v2/session");
      if (s.mode === "legacy") {
        $("app").hidden = true;
        document.querySelector(".app-shell").hidden = false;
        return false;
      }
      if (s.mode !== "v2")
        throw new Error("The saved knowledge could not be identified.");
      S.confirmed = true;
      S.revision = s.knowledge_revision;
      const p = await landscape(S.revision, null, 0, null);
      S.landscape = p;
      commit(p, "overview");
      return true;
    } catch (e) {
      showBoot(
        "The map could not open",
        e.message + " Your saved knowledge has not been changed.",
        true,
      );
      return true;
    }
  }
  bind();
  $("boot-retry").addEventListener("click", start);
  /* Truthy means v2 owns the page, including a recoverable v2 load failure. */
  window.__synapseV2Probe = start();
})();
