(function () {
  "use strict";

  var DEFAULT_GRAPH = {
    nodes: [
      {
        id: "01HZXA00000000000000000001",
        type: "person",
        name: "Ari Mercer",
        aliases: ["A. Mercer", "Ari"],
        review_status: "verified",
        tags: ["network", "ops"],
        properties: { location: "Remote", role: "Coordinator" },
        provenance: { source_file: "contacts.csv" },
      },
      {
        id: "01HZXA00000000000000000002",
        type: "company",
        name: "Northstar Labs",
        aliases: ["Northstar"],
        review_status: "verified",
        tags: ["research"],
        properties: { domain: "northstar.example" },
        provenance: { source_file: "linkedin_export.json" },
      },
      {
        id: "01HZXA00000000000000000003",
        type: "project",
        name: "Synapse Serve",
        aliases: ["serve"],
        review_status: "proposed",
        tags: ["ui", "local"],
        properties: { status: "active" },
        provenance: { source_file: "notes.md" },
      },
      {
        id: "01HZXA00000000000000000004",
        type: "goal",
        name: "Offline graph viewer",
        aliases: ["graph ui"],
        review_status: "verified",
        tags: ["product"],
        properties: { priority: "high" },
        provenance: { source_file: "roadmap.md" },
      },
      {
        id: "01HZXA00000000000000000005",
        type: "person",
        name: "Mina Patel",
        aliases: ["Mina"],
        review_status: "proposed",
        tags: ["review"],
        properties: { location: "London", role: "Advisor" },
        provenance: { source_file: "whatsapp.txt" },
      },
      {
        id: "01HZXA00000000000000000006",
        type: "finance",
        name: "Operating Account",
        aliases: ["main account"],
        review_status: "verified",
        tags: ["bank"],
        properties: { currency: "USD" },
        provenance: { source_file: "ledger.csv" },
      },
      {
        id: "01HZXA00000000000000000007",
        type: "company",
        name: "Vector Forge",
        aliases: ["Forge"],
        review_status: "verified",
        tags: ["partner"],
        properties: { region: "EU" },
        provenance: { source_file: "inbox/export.json" },
      },
    ],
    edges: [
      { id: "e1", source: "01HZXA00000000000000000001", target: "01HZXA00000000000000000002", type: "works_at", weak: 0, properties: { role: "Coordinator" } },
      { id: "e2", source: "01HZXA00000000000000000001", target: "01HZXA00000000000000000003", type: "leads", weak: 0, properties: {} },
      { id: "e3", source: "01HZXA00000000000000000003", target: "01HZXA00000000000000000004", type: "supports", weak: 0, properties: {} },
      { id: "e4", source: "01HZXA00000000000000000005", target: "01HZXA00000000000000000003", type: "contributes_to", weak: 1, properties: { note: "from note body" } },
      { id: "e5", source: "01HZXA00000000000000000001", target: "01HZXA00000000000000000006", type: "owns_account", weak: 0, properties: {} },
      { id: "e6", source: "01HZXA00000000000000000002", target: "01HZXA00000000000000000007", type: "partner_of", weak: 0, properties: { since: 2024 } },
      { id: "e7", source: "01HZXA00000000000000000005", target: "01HZXA00000000000000000001", type: "introduced_by", weak: 0, properties: {} },
    ],
  };

  var dom = {};
  var state = {
    rawGraph: null,
    graph: null,
    nodesById: new Map(),
    relationTypes: [],
    relationFilter: new Set(),
    searchQuery: "",
    selectedId: null,
    highlightedPathNodes: new Set(),
    highlightedPathEdges: new Set(),
    pathSource: "",
    pathTarget: "",
    focusNodeId: null,
    lastRender: null,
  };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    cacheDom();
    state.rawGraph = normalizeGraph(await loadInitialGraph());
    state.graph = cloneGraph(state.rawGraph);
    state.nodesById = indexNodes(state.graph.nodes);
    state.relationTypes = uniqueRelationTypes(state.graph.edges);
    state.relationFilter = new Set(state.relationTypes);
    populateSuggestions();
    renderRelationFilters();
    wireEvents();
    setGraphMessage("");
    if (state.graph.nodes.length) {
      selectNode(state.graph.nodes[0].id, true);
    } else {
      renderNodeDetail(null);
    }
    refreshAll();
  }

  async function loadInitialGraph() {
    if (window.SYNAPSE_GRAPH || window.__SYNAPSE_GRAPH__) {
      return window.SYNAPSE_GRAPH || window.__SYNAPSE_GRAPH__;
    }
    if (typeof fetch !== "function") {
      return DEFAULT_GRAPH;
    }
    try {
      var response = await fetch("/api/graph", { headers: { Accept: "application/json" } });
      if (!response.ok) {
        return DEFAULT_GRAPH;
      }
      var graph = await response.json();
      if (graph && Array.isArray(graph.nodes) && Array.isArray(graph.edges)) {
        return graph;
      }
    } catch (error) {
      return DEFAULT_GRAPH;
    }
    return DEFAULT_GRAPH;
  }

  function cacheDom() {
    dom.searchInput = document.getElementById("search-input");
    dom.searchClear = document.getElementById("search-clear");
    dom.searchStatus = document.getElementById("search-status");
    dom.relationFilters = document.getElementById("relation-filters");
    dom.relationsReset = document.getElementById("relations-reset");
    dom.pathSource = document.getElementById("path-source");
    dom.pathTarget = document.getElementById("path-target");
    dom.findPath = document.getElementById("find-path");
    dom.clearPath = document.getElementById("clear-path");
    dom.pathStatus = document.getElementById("path-status");
    dom.pathList = document.getElementById("path-list");
    dom.nodeSuggestions = document.getElementById("node-suggestions");
    dom.graphShell = document.getElementById("graph-shell");
    dom.graphSvg = document.getElementById("graph-svg");
    dom.graphEmpty = document.getElementById("graph-empty");
    dom.graphCounts = document.getElementById("graph-counts");
    dom.fitView = document.getElementById("fit-view");
    dom.resetView = document.getElementById("reset-view");
    dom.nodeEmpty = document.getElementById("node-empty");
    dom.nodeDetail = document.getElementById("node-detail");
    dom.nodeName = document.getElementById("node-name");
    dom.nodeId = document.getElementById("node-id");
    dom.nodeType = document.getElementById("node-type");
    dom.nodeReview = document.getElementById("node-review");
    dom.nodeAliases = document.getElementById("node-aliases");
    dom.nodeTags = document.getElementById("node-tags");
    dom.nodeProperties = document.getElementById("node-properties");
    dom.nodeRelations = document.getElementById("node-relations");
    dom.expandNeighbors = document.getElementById("expand-neighbors");
  }

  function wireEvents() {
    dom.searchInput.addEventListener("input", function () {
      state.searchQuery = dom.searchInput.value;
      refreshAll();
    });

    dom.searchInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        var matches = searchMatches();
        if (matches.length) {
          state.focusNodeId = null;
          selectNode(matches[0].id, true);
        }
      }
    });

    dom.searchClear.addEventListener("click", function () {
      dom.searchInput.value = "";
      state.searchQuery = "";
      refreshAll();
    });

    dom.relationsReset.addEventListener("click", function () {
      state.relationFilter = new Set(state.relationTypes);
      renderRelationFilters();
      refreshAll();
    });

    dom.pathSource.addEventListener("input", function () {
      state.pathSource = dom.pathSource.value;
    });

    dom.pathTarget.addEventListener("input", function () {
      state.pathTarget = dom.pathTarget.value;
    });

    dom.findPath.addEventListener("click", function () {
      computePathAndRender();
    });

    dom.clearPath.addEventListener("click", function () {
      state.highlightedPathNodes = new Set();
      state.highlightedPathEdges = new Set();
      state.pathSource = "";
      state.pathTarget = "";
      dom.pathSource.value = "";
      dom.pathTarget.value = "";
      dom.pathList.innerHTML = "";
      dom.pathStatus.textContent = "";
      refreshAll();
    });

    dom.fitView.addEventListener("click", function () {
      fitGraph();
    });

    dom.resetView.addEventListener("click", function () {
      dom.searchInput.value = "";
      dom.pathSource.value = "";
      dom.pathTarget.value = "";
      dom.pathList.innerHTML = "";
      dom.pathStatus.textContent = "";
      state.searchQuery = "";
      state.pathSource = "";
      state.pathTarget = "";
      state.relationFilter = new Set(state.relationTypes);
      state.highlightedPathNodes = new Set();
      state.highlightedPathEdges = new Set();
      state.selectedId = null;
      state.focusNodeId = null;
      renderRelationFilters();
      if (state.graph.nodes.length) {
        selectNode(state.graph.nodes[0].id, true);
      }
      refreshAll();
    });

    dom.expandNeighbors.addEventListener("click", function () {
      if (!state.selectedId) {
        return;
      }
      state.focusNodeId = state.selectedId;
      refreshAll();
      fitGraph();
    });

    dom.relationFilters.addEventListener("change", function (event) {
      var checkbox = event.target;
      if (!(checkbox instanceof HTMLInputElement)) {
        return;
      }
      var type = checkbox.value;
      if (checkbox.checked) {
        state.relationFilter.add(type);
      } else {
        state.relationFilter.delete(type);
      }
      refreshAll();
    });
  }

  function normalizeGraph(input) {
    var nodes = Array.isArray(input.nodes) ? input.nodes : [];
    var edges = Array.isArray(input.edges) ? input.edges : [];

    return {
      nodes: nodes.map(normalizeNode),
      edges: edges.map(normalizeEdge),
    };
  }

  function normalizeNode(node) {
    return {
      id: String(node.id),
      type: node.type || "entity",
      name: node.name || String(node.id),
      aliases: Array.isArray(node.aliases) ? node.aliases.slice() : [],
      review_status: node.review_status || "proposed",
      tags: Array.isArray(node.tags) ? node.tags.slice() : [],
      properties: node.properties && typeof node.properties === "object" ? clonePlain(node.properties) : {},
      provenance: node.provenance && typeof node.provenance === "object" ? clonePlain(node.provenance) : {},
    };
  }

  function normalizeEdge(edge) {
    var source = edge.source || edge.from_id;
    var target = edge.target || edge.to_id;
    return {
      id: String(edge.id || source + ":" + target + ":" + edge.type),
      source: String(source),
      target: String(target),
      type: edge.type || "related_to",
      weak: edge.weak ? 1 : 0,
      properties: edge.properties && typeof edge.properties === "object" ? clonePlain(edge.properties) : {},
    };
  }

  function cloneGraph(graph) {
    return {
      nodes: graph.nodes.map(function (node) {
        return Object.assign({}, node, {
          aliases: node.aliases.slice(),
          tags: node.tags.slice(),
          properties: clonePlain(node.properties),
          provenance: clonePlain(node.provenance),
        });
      }),
      edges: graph.edges.map(function (edge) {
        return Object.assign({}, edge, {
          properties: clonePlain(edge.properties),
        });
      }),
    };
  }

  function clonePlain(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  function indexNodes(nodes) {
    var map = new Map();
    nodes.forEach(function (node) {
      map.set(node.id, node);
    });
    return map;
  }

  function uniqueRelationTypes(edges) {
    var types = new Set();
    edges.forEach(function (edge) {
      types.add(edge.type);
    });
    return Array.from(types).sort();
  }

  function populateSuggestions() {
    var fragment = document.createDocumentFragment();
    state.graph.nodes.forEach(function (node) {
      var option = document.createElement("option");
      option.value = node.name;
      fragment.appendChild(option);
      var idOption = document.createElement("option");
      idOption.value = node.id;
      fragment.appendChild(idOption);
      node.aliases.forEach(function (alias) {
        var aliasOption = document.createElement("option");
        aliasOption.value = alias;
        fragment.appendChild(aliasOption);
      });
    });
    dom.nodeSuggestions.replaceChildren(fragment);
  }

  function renderRelationFilters() {
    var fragment = document.createDocumentFragment();
    var counts = relationCounts();

    state.relationTypes.forEach(function (type) {
      var item = document.createElement("div");
      item.className = "filter-item";

      var label = document.createElement("label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = type;
      checkbox.checked = state.relationFilter.has(type);
      checkbox.setAttribute("aria-label", type);

      var text = document.createElement("span");
      text.textContent = type;

      var count = document.createElement("span");
      count.className = "filter-count";
      count.textContent = String(counts.get(type) || 0);

      label.appendChild(checkbox);
      label.appendChild(text);
      item.appendChild(label);
      item.appendChild(count);
      fragment.appendChild(item);
    });

    dom.relationFilters.replaceChildren(fragment);
  }

  function relationCounts() {
    var counts = new Map();
    state.graph.edges.forEach(function (edge) {
      counts.set(edge.type, (counts.get(edge.type) || 0) + 1);
    });
    return counts;
  }

  function searchMatches() {
    var query = normalizeText(state.searchQuery);
    if (!query) {
      return state.graph.nodes.slice();
    }
    return state.graph.nodes.filter(function (node) {
      return searchableText(node).indexOf(query) !== -1;
    });
  }

  function searchableText(node) {
    var parts = [node.id, node.name, node.type];
    parts = parts.concat(node.aliases);
    parts = parts.concat(node.tags);
    Object.keys(node.properties || {}).forEach(function (key) {
      parts.push(key);
      var value = node.properties[key];
      if (value !== null && value !== undefined) {
        parts.push(String(value));
      }
    });
    return normalizeText(parts.join(" "));
  }

  function normalizeText(value) {
    return String(value || "").toLowerCase().trim();
  }

  function relationVisible(edge) {
    if (!state.relationFilter.size) {
      return true;
    }
    return state.relationFilter.has(edge.type);
  }

  function refreshAll() {
    var matches = searchMatches();
    var matchIds = new Set(matches.map(function (node) {
      return node.id;
    }));
    var visibleEdges = state.graph.edges.filter(function (edge) {
      return relationVisible(edge);
    });
    var connectedIds = new Set();
    visibleEdges.forEach(function (edge) {
      connectedIds.add(edge.source);
      connectedIds.add(edge.target);
    });

    var visibleNodeIds = new Set();
    var focusedIds = new Set();
    if (state.focusNodeId && state.nodesById.has(state.focusNodeId)) {
      focusedIds.add(state.focusNodeId);
      visibleEdges.forEach(function (edge) {
        if (edge.source === state.focusNodeId) {
          focusedIds.add(edge.target);
        }
        if (edge.target === state.focusNodeId) {
          focusedIds.add(edge.source);
        }
      });
    }
    state.graph.nodes.forEach(function (node) {
      if (!state.searchQuery || matchIds.has(node.id) || connectedIds.has(node.id) || state.highlightedPathNodes.has(node.id) || node.id === state.selectedId || focusedIds.has(node.id)) {
        visibleNodeIds.add(node.id);
      }
    });

    if (!state.searchQuery && !state.highlightedPathNodes.size && !state.selectedId && !state.focusNodeId) {
      state.graph.nodes.forEach(function (node) {
        visibleNodeIds.add(node.id);
      });
    }

    if (!visibleNodeIds.size && state.graph.nodes.length) {
      state.graph.nodes.forEach(function (node) {
        visibleNodeIds.add(node.id);
      });
    }

    visibleEdges = visibleEdges.filter(function (edge) {
      return visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target);
    });

    var visibleNodes = state.graph.nodes.filter(function (node) {
      return visibleNodeIds.has(node.id);
    });

    updateStatus(matches, visibleNodes, visibleEdges);
    renderGraph(visibleNodes, visibleEdges, matchIds);
    renderNodeDetail(state.selectedId ? state.nodesById.get(state.selectedId) || null : null);
    dom.graphEmpty.hidden = visibleNodes.length > 0;
  }

  function updateStatus(matches, visibleNodes, visibleEdges) {
    dom.searchStatus.textContent = state.searchQuery
      ? String(matches.length) + " match" + (matches.length === 1 ? "" : "es")
      : "All nodes";
    dom.graphCounts.textContent = String(visibleNodes.length) + " nodes, " + String(visibleEdges.length) + " edges";
  }

  function renderGraph(nodes, edges, matchIds) {
    var width = Math.max(dom.graphShell.clientWidth, 320);
    var height = Math.max(dom.graphShell.clientHeight, 420);
    var layout = layoutGraph(nodes, edges, width, height);
    state.lastRender = layout;

    dom.graphSvg.setAttribute("viewBox", "0 0 " + width + " " + height);
    dom.graphSvg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    dom.graphSvg.innerHTML = "";

    if (!nodes.length) {
      return;
    }

    var defs = svgEl("defs");
    var marker = svgEl("marker");
    marker.setAttribute("id", "arrow-head");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9");
    marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    var arrow = svgEl("path");
    arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    arrow.setAttribute("fill", "#5b6674");
    marker.appendChild(arrow);
    defs.appendChild(marker);
    dom.graphSvg.appendChild(defs);

    edges.forEach(function (edge) {
      var source = layout.positions.get(edge.source);
      var target = layout.positions.get(edge.target);
      if (!source || !target) {
        return;
      }
      var line = svgEl("line");
      line.setAttribute("x1", source.x);
      line.setAttribute("y1", source.y);
      line.setAttribute("x2", target.x);
      line.setAttribute("y2", target.y);
      line.setAttribute("marker-end", "url(#arrow-head)");
      line.classList.add("graph-edge");
      if (state.highlightedPathEdges.has(edge.id)) {
        line.classList.add("path");
      }
      if (state.searchQuery && !(matchIds.has(edge.source) || matchIds.has(edge.target))) {
        line.classList.add("muted");
      }
      dom.graphSvg.appendChild(line);
    });

    nodes.forEach(function (node) {
      var position = layout.positions.get(node.id);
      if (!position) {
        return;
      }
      var group = svgEl("g");
      group.classList.add("graph-node");
      if (matchIds.has(node.id)) {
        group.classList.add("match");
      }
      if (node.id === state.selectedId) {
        group.classList.add("selected");
      }
      if (state.highlightedPathNodes.has(node.id)) {
        group.classList.add("path");
      }
      group.setAttribute("transform", "translate(" + position.x + " " + position.y + ")");
      group.dataset.nodeId = node.id;

      var circle = svgEl("circle");
      circle.setAttribute("r", String(radiusFor(node)));
      group.appendChild(circle);

      var label = svgEl("text");
      label.setAttribute("class", "graph-node-label");
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("y", String(radiusFor(node) + 16));
      label.textContent = shortenLabel(node.name, 18);
      group.appendChild(label);

      var title = svgEl("title");
      title.textContent = node.name + "\n" + node.id + "\n" + node.type;
      group.appendChild(title);

      group.addEventListener("click", function () {
        selectNode(node.id, false);
      });

      group.addEventListener("dblclick", function () {
        state.focusNodeId = node.id;
        selectNode(node.id, true);
        refreshAll();
      });

      dom.graphSvg.appendChild(group);
    });
  }

  function radiusFor(node) {
    if (node.type === "person") return 19;
    if (node.type === "company") return 21;
    if (node.type === "project") return 20;
    if (node.type === "goal") return 18;
    if (node.type === "finance") return 18;
    return 17;
  }

  function shortenLabel(text, maxLength) {
    var value = String(text || "");
    if (value.length <= maxLength) {
      return value;
    }
    return value.slice(0, Math.max(1, maxLength - 3)) + "...";
  }

  function svgEl(name) {
    return document.createElementNS("http://www.w3.org/2000/svg", name);
  }

  function layoutGraph(nodes, edges, width, height) {
    var positions = new Map();
    if (!nodes.length) {
      return { positions: positions, bounds: { width: width, height: height } };
    }

    var cx = width / 2;
    var cy = height / 2;
    var radius = Math.max(100, Math.min(width, height) * 0.32);

    nodes.forEach(function (node, index) {
      var angle = (Math.PI * 2 * index) / nodes.length;
      var seed = hashCode(node.id);
      var jitterX = ((seed % 31) - 15) * 1.6;
      var jitterY = (((seed / 31) | 0) % 31 - 15) * 1.6;
      positions.set(node.id, {
        x: cx + Math.cos(angle) * radius + jitterX,
        y: cy + Math.sin(angle) * radius + jitterY,
        vx: 0,
        vy: 0,
      });
    });

    var nodeById = new Map(nodes.map(function (node) {
      return [node.id, node];
    }));
    var filteredEdges = edges.filter(function (edge) {
      return nodeById.has(edge.source) && nodeById.has(edge.target);
    });

    for (var step = 0; step < 140; step += 1) {
      nodes.forEach(function (a) {
        var pa = positions.get(a.id);
        nodes.forEach(function (b) {
          if (a.id === b.id) {
            return;
          }
          var pb = positions.get(b.id);
          var dx = pa.x - pb.x;
          var dy = pa.y - pb.y;
          var dist2 = Math.max(dx * dx + dy * dy, 36);
          var force = 1800 / dist2;
          pa.vx += dx * force * 0.001;
          pa.vy += dy * force * 0.001;
        });
      });

      filteredEdges.forEach(function (edge) {
        var source = positions.get(edge.source);
        var target = positions.get(edge.target);
        var dx = target.x - source.x;
        var dy = target.y - source.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        var desired = 150;
        var force = (dist - desired) * 0.01;
        var fx = (dx / dist) * force;
        var fy = (dy / dist) * force;
        source.vx += fx;
        source.vy += fy;
        target.vx -= fx;
        target.vy -= fy;
      });

      nodes.forEach(function (node) {
        var point = positions.get(node.id);
        point.vx += (cx - point.x) * 0.0015;
        point.vy += (cy - point.y) * 0.0015;
        point.x += point.vx;
        point.y += point.vy;
        point.vx *= 0.82;
        point.vy *= 0.82;
      });
    }

    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    nodes.forEach(function (node) {
      var point = positions.get(node.id);
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x);
      maxY = Math.max(maxY, point.y);
    });

    var pad = 60;
    var spanX = Math.max(1, maxX - minX);
    var spanY = Math.max(1, maxY - minY);
    var scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY, 1.2);
    var offsetX = (width - spanX * scale) / 2 - minX * scale;
    var offsetY = (height - spanY * scale) / 2 - minY * scale;

    positions.forEach(function (point) {
      point.x = point.x * scale + offsetX;
      point.y = point.y * scale + offsetY;
    });

    return {
      positions: positions,
      bounds: { width: width, height: height },
    };
  }

  function hashCode(text) {
    var hash = 0;
    for (var i = 0; i < text.length; i += 1) {
      hash = (hash << 5) - hash + text.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash);
  }

  function selectNode(id, centerView) {
    if (!state.nodesById.has(id)) {
      return;
    }
    state.selectedId = id;
    renderNodeDetail(state.nodesById.get(id));
    if (centerView) {
      fitGraph(id);
    } else {
      refreshAll();
    }
  }

  function fitGraph(anchorId) {
    if (!state.lastRender || !state.lastRender.positions.size) {
      return;
    }

    var width = Math.max(dom.graphShell.clientWidth, 320);
    var height = Math.max(dom.graphShell.clientHeight, 420);
    var nodes = state.graph.nodes.filter(function (node) {
      return state.lastRender.positions.has(node.id);
    });

    if (!nodes.length) {
      return;
    }

    if (anchorId && state.lastRender.positions.has(anchorId)) {
      var anchor = state.lastRender.positions.get(anchorId);
      var viewSize = Math.min(width, height) * 0.72;
      dom.graphSvg.setAttribute("viewBox", (anchor.x - viewSize / 2) + " " + (anchor.y - viewSize / 2) + " " + viewSize + " " + viewSize);
      return;
    }

    var positions = nodes.map(function (node) {
      return state.lastRender.positions.get(node.id);
    });
    var minX = positions.reduce(function (min, point) {
      return Math.min(min, point.x);
    }, Infinity);
    var minY = positions.reduce(function (min, point) {
      return Math.min(min, point.y);
    }, Infinity);
    var maxX = positions.reduce(function (max, point) {
      return Math.max(max, point.x);
    }, -Infinity);
    var maxY = positions.reduce(function (max, point) {
      return Math.max(max, point.y);
    }, -Infinity);
    var padding = 50;
    dom.graphSvg.setAttribute("viewBox", (minX - padding) + " " + (minY - padding) + " " + (maxX - minX + padding * 2) + " " + (maxY - minY + padding * 2));
  }

  function renderNodeDetail(node) {
    if (!node) {
      dom.nodeEmpty.hidden = false;
      dom.nodeDetail.hidden = true;
      dom.nodeName.textContent = "";
      dom.nodeId.textContent = "";
      dom.nodeType.textContent = "";
      dom.nodeReview.textContent = "";
      dom.nodeAliases.textContent = "";
      dom.nodeTags.textContent = "";
      dom.nodeProperties.textContent = "";
      dom.nodeRelations.innerHTML = "";
      return;
    }

    dom.nodeEmpty.hidden = true;
    dom.nodeDetail.hidden = false;
    dom.nodeName.textContent = node.name;
    dom.nodeId.textContent = node.id;
    dom.nodeType.textContent = node.type;
    dom.nodeReview.textContent = node.review_status;
    dom.nodeAliases.textContent = node.aliases.length ? node.aliases.join(", ") : "None";
    dom.nodeTags.textContent = node.tags.length ? node.tags.join(", ") : "None";
    dom.nodeProperties.textContent = JSON.stringify(node.properties, null, 2);

    var connected = state.graph.edges.filter(function (edge) {
      return edge.source === node.id || edge.target === node.id;
    });
    var fragment = document.createDocumentFragment();

    if (!connected.length) {
      var empty = document.createElement("div");
      empty.className = "relation-item";
      empty.textContent = "No relations in the current graph.";
      fragment.appendChild(empty);
    } else {
      connected.forEach(function (edge) {
        var otherId = edge.source === node.id ? edge.target : edge.source;
        var other = state.nodesById.get(otherId);
        var item = document.createElement("div");
        item.className = "relation-item";

        var line = document.createElement("div");
        line.innerHTML = "";
        var relationButton = document.createElement("button");
        relationButton.type = "button";
        relationButton.className = "relation-link";
        relationButton.textContent = other ? other.name : otherId;
        relationButton.addEventListener("click", function () {
          if (other) {
            selectNode(other.id, false);
          }
        });

        var direction = edge.source === node.id ? "→" : "←";
        line.appendChild(document.createTextNode(direction + " " + edge.type + " "));
        line.appendChild(relationButton);
        item.appendChild(line);

        var meta = document.createElement("div");
        meta.className = "relation-meta";
        meta.textContent = edge.weak ? "weak edge" : "typed edge";
        if (Object.keys(edge.properties || {}).length) {
          meta.textContent += " · " + JSON.stringify(edge.properties);
        }
        item.appendChild(meta);
        fragment.appendChild(item);
      });
    }

    dom.nodeRelations.replaceChildren(fragment);
  }

  function computePathAndRender() {
    var sourceRef = normalizeText(dom.pathSource.value);
    var targetRef = normalizeText(dom.pathTarget.value);
    state.pathSource = dom.pathSource.value;
    state.pathTarget = dom.pathTarget.value;
    var source = resolveNodeRef(sourceRef);
    var target = resolveNodeRef(targetRef);

    if (!source || !target) {
      state.highlightedPathNodes = new Set();
      state.highlightedPathEdges = new Set();
      dom.pathStatus.textContent = "Resolve both source and target first.";
      dom.pathList.innerHTML = "";
      refreshAll();
      return;
    }

    var path = shortestPath(source.id, target.id);
    if (!path) {
      state.highlightedPathNodes = new Set();
      state.highlightedPathEdges = new Set();
      dom.pathStatus.textContent = "No path found on the current filtered graph.";
      dom.pathList.innerHTML = "";
      refreshAll();
      return;
    }

    state.highlightedPathNodes = new Set(path.nodes);
    state.highlightedPathEdges = new Set(path.edges);
    dom.pathStatus.textContent = "Path length: " + String(path.nodes.length - 1) + " hop" + (path.nodes.length - 1 === 1 ? "" : "s");
    renderPathList(path);
    refreshAll();
    selectNode(path.nodes[path.nodes.length - 1], false);
  }

  function renderPathList(path) {
    var fragment = document.createDocumentFragment();
    path.nodes.forEach(function (nodeId, index) {
      var item = document.createElement("li");
      var node = state.nodesById.get(nodeId);
      item.className = "path-step";
      item.textContent = node ? node.name + " (" + node.type + ")" : nodeId;
      if (index < path.edges.length) {
        var edge = state.graph.edges.find(function (candidate) {
          return candidate.id === path.edges[index];
        });
        if (edge) {
          item.appendChild(document.createTextNode(" via " + edge.type));
        }
      }
      fragment.appendChild(item);
    });
    dom.pathList.replaceChildren(fragment);
  }

  function resolveNodeRef(ref) {
    if (!ref) {
      return null;
    }
    var exact = state.graph.nodes.find(function (node) {
      return normalizeText(node.id) === ref || normalizeText(node.name) === ref;
    });
    if (exact) {
      return exact;
    }
    return state.graph.nodes.find(function (node) {
      return node.aliases.some(function (alias) {
        return normalizeText(alias) === ref;
      });
    }) || null;
  }

  function shortestPath(startId, endId) {
    if (startId === endId) {
      return { nodes: [startId], edges: [] };
    }

    var adj = new Map();
    state.graph.edges.forEach(function (edge) {
      if (!relationVisible(edge)) {
        return;
      }
      if (!adj.has(edge.source)) {
        adj.set(edge.source, []);
      }
      if (!adj.has(edge.target)) {
        adj.set(edge.target, []);
      }
      adj.get(edge.source).push({ node: edge.target, edge: edge.id });
      adj.get(edge.target).push({ node: edge.source, edge: edge.id });
    });

    var queue = [startId];
    var visited = new Set([startId]);
    var previous = new Map();
    while (queue.length) {
      var current = queue.shift();
      var neighbors = adj.get(current) || [];
      for (var i = 0; i < neighbors.length; i += 1) {
        var next = neighbors[i];
        if (visited.has(next.node)) {
          continue;
        }
        visited.add(next.node);
        previous.set(next.node, { node: current, edge: next.edge });
        if (next.node === endId) {
          return unwindPath(previous, startId, endId);
        }
        queue.push(next.node);
      }
    }

    return null;
  }

  function unwindPath(previous, startId, endId) {
    var nodes = [endId];
    var edges = [];
    var cursor = endId;
    while (cursor !== startId) {
      var prev = previous.get(cursor);
      if (!prev) {
        break;
      }
      edges.unshift(prev.edge);
      nodes.unshift(prev.node);
      cursor = prev.node;
    }
    return { nodes: nodes, edges: edges };
  }

  function setGraphMessage(text) {
    dom.graphEmpty.textContent = text || "No nodes match the current view.";
  }
})();
