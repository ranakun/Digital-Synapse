/* Digital Synapse – force-graph HTML5 Canvas navigational explorer
 * Vanilla JS, no build step. Depends only on vendor/force-graph.min.js.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Color maps — with explicit DEFAULT fallbacks for unknown future types
  // ---------------------------------------------------------------------------

  /** Node background color keyed by entity type. */
  var NODE_TYPE_COLORS = {
    person:      "#3b6fa0",   // steel blue
    company:     "#2e6b4f",   // forest green
    skill:       "#7a4a9b",   // purple
    school:      "#8a5c2e",   // amber-brown
    role:        "#4a5568",   // slate
    project:     "#2d6b6b",   // teal
    goal:        "#4d6b2d",   // olive
    finance:     "#6b4d2d",   // brown
    event:       "#6b2d4d",   // rose
    location:    "#2d4d6b",   // navy
    document:    "#5a5a3a",   // khaki
    technology:  "#3a5a7a",   // cerulean
    DEFAULT:     "#3a3f4a"    // neutral dark — for any unknown type
  };

  /** Node border/stroke color (lighter accent per type). */
  var NODE_TYPE_BORDER = {
    person:      "#7ab8e0",
    company:     "#5eb88a",
    skill:       "#c08de8",
    school:      "#d4a060",
    role:        "#8090a8",
    project:     "#5eb8b8",
    goal:        "#8ab85e",
    finance:     "#b8905e",
    event:       "#b85e8a",
    location:    "#5e8ab8",
    document:    "#a8a86a",
    technology:  "#6aaad0",
    DEFAULT:     "#7a8494"
  };

  /** Edge line color keyed by relation type. */
  var EDGE_TYPE_COLORS = {
    knows:               "rgba(124, 199, 255, 0.16)",
    works_at:            "#5eb88a",
    former_employee_of:  "#d4a060",
    recruits_for:        "#f87171",
    demonstrates_skill:  "#c084fc",
    mentioned_in:        "#6b7280",
    leads:               "#fb923c",
    supports:            "#34d399",
    partner_of:          "#f59e0b",
    owns_account:        "#60a5fa",
    contributes_to:      "#a78bfa",
    introduced_by:       "#f472b6",
    related_to:          "#94a3b8",
    DEFAULT:             "#5b6674"   // neutral — for any unknown relation type
  };

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  var state = {
    graph: null,                // force-graph instance
    ownerNodeId: null,          // from /api/meta
    metaRelationTypes: [],      // [{type, count, weak}] from /api/meta
    metaEntityTypes: [],        // ['person', 'company', ...] from /api/meta
    activeRelTypes: new Set(),  // checked relation types in facet panel
    activeEntityTypes: new Set(), // checked entity types in facet panel
    includeWeak: false,         // master weak-edges toggle
    selectedNodeId: null,       // currently selected node id
    hoverNodeId: null,          // currently hovered node id (for dimming/highlights)
    launcherEntities: [],       // [{id, type, name, review_status}] from /api/launcher
    rawNodes: [],               // all loaded node objects in memory
    rawEdges: [],               // all loaded edge objects in memory
    contextNodes: [],           // temporary nodes for the selected person's work context
    contextEdges: [],           // canonical context edges plus derived selected-person links
    contextFocusId: null,
    pathHighlightedNodes: new Set(),
    pathHighlightedEdges: new Set(),
    highlightNodes: new Set(),  // currently highlighted nodes (for zoom/focus/hover)
    highlightLinks: new Set(),  // currently highlighted links
    totalNeighborsAtLoad: 0,    // total_neighbors from last neighbors call
    physicsEnabled: true,       // play/pause simulation toggle
    lastClickNodeId: null,      // used to emulate double-click on force-graph builds without onNodeDoubleClick
    lastClickAt: 0,
  };

  // ---------------------------------------------------------------------------
  // DOM cache
  // ---------------------------------------------------------------------------

  var dom = {};

  function cacheDom() {
    dom.searchInput      = document.getElementById("search-input");
    dom.searchResults    = document.getElementById("search-results");
    dom.searchStatus     = document.getElementById("search-status");
    dom.relationFilters  = document.getElementById("relation-filters");
    dom.relationsReset   = document.getElementById("relations-reset");
    dom.entityFilters    = document.getElementById("entity-filters");
    dom.entitiesReset    = document.getElementById("entities-reset");
    dom.includeWeak      = document.getElementById("include-weak");
    dom.pathSource       = document.getElementById("path-source");
    dom.pathTarget       = document.getElementById("path-target");
    dom.findPath         = document.getElementById("find-path");
    dom.clearPath        = document.getElementById("clear-path");
    dom.pathStatus       = document.getElementById("path-status");
    dom.pathList         = document.getElementById("path-list");
    dom.nodeSuggestions  = document.getElementById("node-suggestions");
    dom.cyContainer      = document.getElementById("cy-container"); // keeping container ID for style compatibility
    dom.graphCounts      = document.getElementById("graph-counts");
    dom.graphOverlay     = document.getElementById("graph-overlay");
    dom.graphSpinner     = document.getElementById("graph-spinner");
    dom.spinnerText      = document.getElementById("spinner-text");
    dom.graphError       = document.getElementById("graph-error");
    dom.fitView          = document.getElementById("fit-view");
    dom.resetView        = document.getElementById("reset-view");
    dom.togglePhysics    = document.getElementById("toggle-physics");
    dom.nodeEmpty        = document.getElementById("node-empty");
    dom.nodeLoading      = document.getElementById("node-loading");
    dom.nodeDetail       = document.getElementById("node-detail");
    dom.nodeName         = document.getElementById("node-name");
    dom.nodeId           = document.getElementById("node-id");
    dom.nodeType         = document.getElementById("node-type");
    dom.nodeReview       = document.getElementById("node-review");
    dom.nodeAliases      = document.getElementById("node-aliases");
    dom.nodeTags         = document.getElementById("node-tags");
    dom.nodeProperties   = document.getElementById("node-properties");
    dom.nodePropertiesBlock = document.getElementById("node-properties-block");
    dom.nodeProvenanceBlock = document.getElementById("node-provenance-block");
    dom.nodeProvenance   = document.getElementById("node-provenance");
    dom.nodeRelations    = document.getElementById("node-relations");
    dom.showContext      = document.getElementById("show-context");
    dom.contextStatus    = document.getElementById("context-status");
    dom.expandNeighbors  = document.getElementById("expand-neighbors");
    dom.errorBanner      = document.getElementById("error-banner");
  }

  // ---------------------------------------------------------------------------
  // Utility
  // ---------------------------------------------------------------------------

  function esc(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function nodeColor(type) {
    return NODE_TYPE_COLORS[type] || NODE_TYPE_COLORS.DEFAULT;
  }

  function nodeBorder(type) {
    return NODE_TYPE_BORDER[type] || NODE_TYPE_BORDER.DEFAULT;
  }

  function edgeColor(type) {
    return EDGE_TYPE_COLORS[type] || EDGE_TYPE_COLORS.DEFAULT;
  }

  // ---------------------------------------------------------------------------
  // API helpers
  // ---------------------------------------------------------------------------

  async function apiFetch(url) {
    var res = await fetch(url, { headers: { Accept: "application/json" } });
    var data = await res.json();
    if (!res.ok || (data && data.error)) {
      throw new Error(data && data.error ? data.error : "HTTP " + res.status + " for " + url);
    }
    return data;
  }

  // ---------------------------------------------------------------------------
  // Error / loading UI
  // ---------------------------------------------------------------------------

  function showError(msg) {
    dom.errorBanner.textContent = "Error: " + msg;
    dom.errorBanner.hidden = false;
    console.error("[synapse]", msg);
  }

  function clearError() {
    dom.errorBanner.hidden = true;
    dom.errorBanner.textContent = "";
  }

  function showSpinner(msg) {
    dom.spinnerText.textContent = msg || "Loading…";
    dom.graphSpinner.hidden = false;
  }

  function hideSpinner() {
    dom.graphSpinner.hidden = true;
  }

  function setGraphOverlay(msg) {
    if (msg) {
      dom.graphOverlay.textContent = msg;
      dom.graphOverlay.hidden = false;
    } else {
      dom.graphOverlay.hidden = true;
    }
  }

  // ---------------------------------------------------------------------------
  // ForceGraph Initialisation
  // ---------------------------------------------------------------------------

  function initForceGraph() {
    const rect = dom.cyContainer.getBoundingClientRect();
    state.graph = ForceGraph()(dom.cyContainer)
      .width(rect.width)
      .height(rect.height || 500)
      .backgroundColor("#0d1117")
      .nodeId("id")
      .nodeVal(getNodeValue)
      .nodeCanvasObject(drawNode)
      .nodeCanvasObjectMode(() => "replace")
      .linkWidth(getLinkWidth)
      .linkColor(getLinkColor)
      .linkLineDash(getLinkLineDash)
      .linkLabel(link => link.derived ? "Shared employer/project context (derived)" : link.type)
      .linkDirectionalArrowLength(link => link.derived ? 0 : 3.5)
      .linkDirectionalArrowRelPos(1.0)
      .onNodeClick(node => {
        handleNodeClick(node.id);
      })
      .onNodeHover(node => {
        state.hoverNodeId = node ? node.id : null;
        updateHighlights();
        redrawGraph();
      })
      .onBackgroundClick(() => {
        deselectNode();
      });

    // Make canvas responsive on window resize
    window.addEventListener("resize", () => {
      const newRect = dom.cyContainer.getBoundingClientRect();
      state.graph.width(newRect.width).height(newRect.height || 500);
    });
  }

  function redrawGraph() {
    if (!state.graph) return;
    var data = state.graph.graphData();
    state.graph.graphData(data);
  }

  // ---------------------------------------------------------------------------
  // Custom Node and Link Canvas Drawing
  // ---------------------------------------------------------------------------

  function getNodeValue(node) {
    return Math.max(1, Math.sqrt(Number(node.degree || 0) + 1));
  }

  function getNodeRadius(node) {
    var degree = Number(node.degree || 0);
    return Math.min(18, 4 + Math.sqrt(degree) * 0.7);
  }

  function drawNode(node, ctx, globalScale) {
    const isFocusMode = (state.hoverNodeId || state.selectedNodeId || state.pathHighlightedNodes.size > 0);
    
    // Set node opacity (fade out if focus mode is active and this node is not highlighted)
    if (isFocusMode && !state.highlightNodes.has(node.id)) {
      ctx.globalAlpha = 0.2;
    } else {
      ctx.globalAlpha = 1.0;
    }

    const r = getNodeRadius(node) / globalScale;

    // Color node circle and border
    const fillCol = nodeColor(node.type);
    const borderCol = (node.id === state.selectedNodeId) ? "#ffe07a" : (node.id === "me" ? "#7cc7ff" : nodeBorder(node.type));
    const borderWidth = (node.id === state.selectedNodeId) ? 3 : (node.id === "me" ? 3.5 : 1.5);

    // Draw main circle
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = fillCol;
    ctx.fill();

    // Draw border
    ctx.lineWidth = borderWidth / globalScale;
    ctx.strokeStyle = borderCol;
    ctx.stroke();

    // Draw pulsing highlight ring for selected node
    if (node.id === state.selectedNodeId) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 4 / globalScale, 0, 2 * Math.PI, false);
      ctx.lineWidth = 1 / globalScale;
      ctx.strokeStyle = "rgba(255, 224, 122, 0.4)";
      ctx.stroke();
    }

    // Draw labels only when they help. The owner ego graph can include hundreds
    // of nodes, so showing every label at fit-to-screen makes the canvas unreadable.
    const visibleNodeCount = state.graph ? state.graph.graphData().nodes.length : 0;
    const isDenseView = visibleNodeCount > 180;
    const isLabelVisible = (node.id === state.selectedNodeId)
      || (node.id === state.hoverNodeId)
      || (node.id === "me")
      || state.pathHighlightedNodes.has(node.id)
      || (isDenseView
        ? (globalScale > 4.5 && node.degree >= 12)
        : ((globalScale > 1.35 && node.degree >= 8) || globalScale > 2.4));
    if (isLabelVisible) {
      const label = node.name || node.id;
      const fontSize = 10 / globalScale;
      ctx.font = `${fontSize}px Inter, ui-sans-serif, system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";

      // Draw background text outline to make it readable on dark canvas
      ctx.strokeStyle = "rgba(8, 10, 14, 0.95)";
      ctx.lineWidth = 3 / globalScale;
      ctx.strokeText(label, node.x, node.y + r + 3 / globalScale);

      ctx.fillStyle = (node.id === "me") ? "#7cc7ff" : "#e5edf6";
      ctx.fillText(label, node.x, node.y + r + 3 / globalScale);
    }

    // Reset alpha
    ctx.globalAlpha = 1.0;
  }

  function getLinkWidth(link) {
    const isFocusMode = (state.hoverNodeId || state.selectedNodeId || state.pathHighlightedNodes.size > 0);
    if (isFocusMode && state.highlightLinks.has(link.id)) {
      return link.type === "knows" ? 1 : (link.derived ? 1.8 : 2.5);
    }
    return link.type === "knows" ? 0.45 : (link.derived ? 0.9 : 1.2);
  }

  function getLinkColor(link) {
    const isFocusMode = (state.hoverNodeId || state.selectedNodeId || state.pathHighlightedNodes.size > 0);
    if (link.derived) {
      return isFocusMode && state.highlightLinks.has(link.id)
        ? "rgba(190, 205, 220, 0.75)"
        : "rgba(148, 163, 184, 0.28)";
    }
    if (isFocusMode) {
      if (state.highlightLinks.has(link.id)) {
        if (state.pathHighlightedEdges.size > 0) {
          return state.pathHighlightedEdges.has(link.id) ? "#ffcb6b" : "rgba(255, 203, 107, 0.3)";
        }
        return edgeColor(link.type);
      }
      return "rgba(91, 102, 116, 0.08)"; // faded color
    }
    return edgeColor(link.type);
  }

  function getLinkLineDash(link) {
    return (link.weak || link.derived) ? [4, 4] : null;
  }

  // ---------------------------------------------------------------------------
  // Highlight Engine
  // ---------------------------------------------------------------------------

  function updateHighlights() {
    state.highlightNodes.clear();
    state.highlightLinks.clear();

    const focusId = state.hoverNodeId || state.selectedNodeId;
    
    // Path finding highlights win if active
    if (state.pathHighlightedNodes.size > 0) {
      state.pathHighlightedNodes.forEach(id => state.highlightNodes.add(id));
      state.pathHighlightedEdges.forEach(id => state.highlightLinks.add(id));
      return;
    }

    if (focusId) {
      state.highlightNodes.add(focusId);
      // Highlight adjacent links and nodes
      state.graph.graphData().links.forEach(l => {
        // Handle standard D3 force-graph node mapping (could be string ID or node object reference)
        const sourceId = (typeof l.source === "object") ? l.source.id : l.source;
        const targetId = (typeof l.target === "object") ? l.target.id : l.target;

        if (sourceId === focusId) {
          state.highlightNodes.add(targetId);
          state.highlightLinks.add(l.id);
        } else if (targetId === focusId) {
          state.highlightNodes.add(sourceId);
          state.highlightLinks.add(l.id);
        }
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Dynamic Client-side Filtering
  // ---------------------------------------------------------------------------

  function applyGraphFilters() {
    if (!state.graph) return;

    const rawNodeIds = new Set(state.rawNodes.map(n => n.id));
    const allNodes = state.rawNodes.concat(state.contextNodes.filter(n => !rawNodeIds.has(n.id)));
    const rawEdgeIds = new Set(state.rawEdges.map(e => e.id));
    const allEdges = state.rawEdges.concat(state.contextEdges.filter(e => !rawEdgeIds.has(e.id)));

    // 1. Filter Nodes in memory
    const visibleNodes = allNodes.filter(n => {
      return state.activeEntityTypes.has(n.type);
    });

    const visibleNodeIds = new Set(visibleNodes.map(n => n.id));

    // 2. Filter Links
    const visibleEdges = allEdges.filter(e => {
      // Check relation checked
      if (!e.derived && !state.activeRelTypes.has(e.type)) return false;
      // Check weak edges toggle
      if (e.weak && !state.includeWeak) return false;
      // Ensure endpoints exist in visible nodes
      return visibleNodeIds.has(e.from_id) && visibleNodeIds.has(e.to_id);
    });

    // Translate to D3 links
    const links = visibleEdges.map(e => ({
      id: e.id || `${e.from_id}-${e.to_id}-${e.type}`,
      source: e.from_id,
      target: e.to_id,
      type: e.type,
      weak: e.weak,
      derived: Boolean(e.derived)
    }));

    // 3. Recompute Degrees dynamically
    const degrees = {};
    links.forEach(l => {
      degrees[l.source] = (degrees[l.source] || 0) + 1;
      degrees[l.target] = (degrees[l.target] || 0) + 1;
    });
    visibleNodes.forEach(n => {
      n.degree = degrees[n.id] || 0;
    });

    // 4. Update force graph representation
    state.graph.graphData({ nodes: visibleNodes, links: links });

    // Refresh highlighting maps
    updateHighlights();
    redrawGraph();

    updateCountsOverlay();
    renderEntityFilters(); // refresh counts displayed in filter list
    renderRelationFilters();
  }

  // ---------------------------------------------------------------------------
  // Merge and Replace Memory Helpers
  // ---------------------------------------------------------------------------

  function mergeIntoState(apiNodes, apiEdges) {
    var addedNodes = 0;
    var addedEdges = 0;

    const existingNodeMap = new Map();
    state.rawNodes.forEach(n => existingNodeMap.set(n.id, n));

    apiNodes.forEach(function (n) {
      if (!existingNodeMap.has(n.id)) {
        state.rawNodes.push({
          id:            n.id,
          name:          n.name || n.id,
          type:          n.type || "entity",
          review_status: n.review_status || "proposed",
          aliases:       n.aliases || [],
          tags:          n.tags || [],
          properties:    n.properties || {},
        });
        addedNodes++;
      }
    });

    const existingEdgeIds = new Set(state.rawEdges.map(e => e.id));

    apiEdges.forEach(function (e) {
      // construct edge ID if missing
      const edgeId = e.id || `${e.from_id}-${e.to_id}-${e.type}`;
      if (!existingEdgeIds.has(edgeId)) {
        state.rawEdges.push({
          id:      edgeId,
          from_id:  e.from_id,
          to_id:    e.to_id,
          type:    e.type || "related_to",
          weak:    e.weak ? 1 : 0,
        });
        addedEdges++;
      }
    });

    return { addedNodes: addedNodes, addedEdges: addedEdges };
  }

  function replaceStateWith(apiNodes, apiEdges) {
    clearContext();
    state.rawNodes = apiNodes.map(n => ({
      id:            n.id,
      name:          n.name || n.id,
      type:          n.type || "entity",
      review_status: n.review_status || "proposed",
      aliases:       n.aliases || [],
      tags:          n.tags || [],
      properties:    n.properties || {},
    }));

    state.rawEdges = apiEdges.map(e => ({
      id:      e.id || `${e.from_id}-${e.to_id}-${e.type}`,
      from_id:  e.from_id,
      to_id:    e.to_id,
      type:    e.type || "related_to",
      weak:    e.weak ? 1 : 0,
    }));
  }

  function clearContext() {
    state.contextNodes = [];
    state.contextEdges = [];
    state.contextFocusId = null;
    if (dom.contextStatus) {
      dom.contextStatus.hidden = true;
      dom.contextStatus.textContent = "";
    }
  }

  // ---------------------------------------------------------------------------
  // Counts overlay
  // ---------------------------------------------------------------------------

  function updateCountsOverlay() {
    if (!state.graph) return;
    var visNodes = state.graph.graphData().nodes.length;
    var visEdges = state.graph.graphData().links.length;
    var total = state.totalNeighborsAtLoad;
    var msg = visNodes + " nodes, " + visEdges + " edges";
    if (total > visNodes) {
      msg += " — showing " + visNodes + " of " + (total + 1) + " (double-click node or expand to load)";
    }
    dom.graphCounts.textContent = msg;
  }

  // ---------------------------------------------------------------------------
  // Initial load — owner ego network
  // ---------------------------------------------------------------------------

  async function loadOwnerEgo() {
    showSpinner("Loading graph…");
    clearError();
    setGraphOverlay(null);

    try {
      var meta = await apiFetch("/api/meta");
      state.ownerNodeId = meta.owner_id || null;
      state.metaRelationTypes = meta.relation_types || [];
      state.metaEntityTypes = meta.entity_types || [];

      // Build default active relation set (non-weak)
      state.activeRelTypes = new Set();
      state.metaRelationTypes.forEach(function (rt) {
        if (!rt.weak) {
          state.activeRelTypes.add(rt.type);
        }
      });
      state.includeWeak = false;
      dom.includeWeak.checked = false;

      // Build default active entities set (all)
      state.activeEntityTypes = new Set(state.metaEntityTypes);

      // Fetch launcher list
      var launcher = await apiFetch("/api/launcher");
      state.launcherEntities = launcher.entities || [];
      populateSuggestionsDatalist();

      var focusId = state.ownerNodeId;
      if (!focusId && state.launcherEntities.length) {
        focusId = state.launcherEntities[0].id;
      }

      if (!focusId) {
        hideSpinner();
        setGraphOverlay("No entities in the vault yet.");
        return;
      }

      // The owner ego is still server-capped, but the current vault fits under the hard 2,000-node limit.
      var url = "/api/neighbors?id=" + encodeURIComponent(focusId)
        + "&undirected=true&include_weak=false&limit=2000";

      var nbData = await apiFetch(url);
      state.totalNeighborsAtLoad = nbData.total_neighbors || 0;

      replaceStateWith(nbData.nodes, nbData.edges);
      clearPathHighlight();
      state.selectedNodeId = focusId;
      applyGraphFilters();
      loadNodeDetail(focusId);

      // Fit graph on initial load after physics settles
      setTimeout(() => {
        state.graph.zoomToFit(300);
      }, 500);

      hideSpinner();
    } catch (err) {
      hideSpinner();
      showError(err.message || String(err));
      setGraphOverlay("Failed to load graph. Is the server running?");
    }
  }

  // ---------------------------------------------------------------------------
  // Focus a node: replace canvas with its ego network
  // ---------------------------------------------------------------------------

  async function focusNode(nodeId) {
    showSpinner("Loading " + nodeId + "…");
    clearError();

    try {
      var url = "/api/neighbors?id=" + encodeURIComponent(nodeId)
        + "&undirected=true&limit=500"
        + "&include_weak=" + (state.includeWeak ? "true" : "false");

      var nbData = await apiFetch(url);
      state.totalNeighborsAtLoad = nbData.total_neighbors || 0;

      replaceStateWith(nbData.nodes, nbData.edges);
      state.selectedNodeId = nodeId;
      clearPathHighlight();
      applyGraphFilters();

      hideSpinner();

      await loadNodeDetail(nodeId);
      state.graph.zoomToFit(400);
    } catch (err) {
      hideSpinner();
      showError(err.message || String(err));
    }
  }

  // ---------------------------------------------------------------------------
  // Expand a node: MERGE its neighbors into the current canvas
  // ---------------------------------------------------------------------------

  async function expandNode(nodeId) {
    showSpinner("Expanding " + nodeId + "…");
    clearError();

    try {
      var url = "/api/neighbors?id=" + encodeURIComponent(nodeId)
        + "&undirected=true&limit=500"
        + "&include_weak=" + (state.includeWeak ? "true" : "false");

      var nbData = await apiFetch(url);

      var counts = mergeIntoState(nbData.nodes, nbData.edges);
      clearPathHighlight();
      applyGraphFilters();

      hideSpinner();
      
      // Auto-fit if new items are added
      if (counts.addedNodes > 0 || counts.addedEdges > 0) {
        state.graph.zoomToFit(300);
      }
    } catch (err) {
      hideSpinner();
      showError(err.message || String(err));
    }
  }

  async function showWorkContext(nodeId) {
    showSpinner("Loading work context…");
    clearError();
    dom.showContext.disabled = true;

    try {
      var url = "/api/neighbors?id=" + encodeURIComponent(nodeId)
        + "&depth=2&types=works_at,former_employee_of,contributes_to"
        + "&undirected=true&include_weak=false&limit=500";
      var data = await apiFetch(url);
      var nodesById = new Map((data.nodes || []).map(n => [n.id, n]));
      var memberships = new Map();

      (data.edges || []).forEach(function (edge) {
        var from = nodesById.get(edge.from_id);
        var to = nodesById.get(edge.to_id);
        if (!from || !to) return;

        var person = from.type === "person" ? from : (to.type === "person" ? to : null);
        var context = (from.type === "company" || from.type === "project") ? from
          : ((to.type === "company" || to.type === "project") ? to : null);
        if (!person || !context) return;
        if (!memberships.has(context.id)) memberships.set(context.id, new Set());
        memberships.get(context.id).add(person.id);
      });

      var peerContexts = new Map();
      memberships.forEach(function (people, contextId) {
        if (!people.has(nodeId)) return;
        people.forEach(function (personId) {
          if (personId === nodeId) return;
          if (!peerContexts.has(personId)) peerContexts.set(personId, []);
          peerContexts.get(personId).push(contextId);
        });
      });

      state.contextFocusId = nodeId;
      state.contextNodes = (data.nodes || []).map(n => ({
        id: n.id,
        name: n.name || n.id,
        type: n.type || "entity",
        review_status: n.review_status || "proposed",
        aliases: n.aliases || [],
        tags: n.tags || [],
        properties: n.properties || {},
      }));
      state.contextEdges = (data.edges || []).map(e => ({
        id: e.id || `${e.from_id}-${e.to_id}-${e.type}`,
        from_id: e.from_id,
        to_id: e.to_id,
        type: e.type || "related_to",
        weak: e.weak ? 1 : 0,
      }));
      peerContexts.forEach(function (contextIds, personId) {
        state.contextEdges.push({
          id: "shared-context-" + nodeId + "-" + personId,
          from_id: nodeId,
          to_id: personId,
          type: "shared_context",
          weak: 0,
          derived: true,
          context_ids: contextIds,
        });
      });

      applyGraphFilters();
      var contextCount = Array.from(memberships.entries()).filter(function (entry) {
        return entry[1].has(nodeId);
      }).length;
      var status = peerContexts.size + " people across " + contextCount
        + " shared employer/project context" + (contextCount === 1 ? "" : "s")
        + ". Dashed links are derived context, not verified relationships.";
      if (data.truncated) status += " The view is capped at 500 neighboring nodes.";
      dom.contextStatus.textContent = status;
      dom.contextStatus.hidden = false;
      state.graph.zoomToFit(300);
    } catch (err) {
      showError(err.message || String(err));
    } finally {
      dom.showContext.disabled = false;
      hideSpinner();
    }
  }

  // ---------------------------------------------------------------------------
  // Node detail panel
  // ---------------------------------------------------------------------------

  async function loadNodeDetail(nodeId) {
    dom.nodeEmpty.hidden = true;
    dom.nodeLoading.hidden = false;
    dom.nodeDetail.hidden = true;

    try {
      var data = await apiFetch("/api/node/" + encodeURIComponent(nodeId));
      renderNodeDetail(data.node, data.relations);
    } catch (err) {
      dom.nodeLoading.hidden = true;
      dom.nodeEmpty.hidden = false;
      dom.nodeEmpty.textContent = "Error loading node: " + err.message;
    }
  }

  function renderNodeDetail(node, relations) {
    dom.nodeLoading.hidden = true;
    dom.nodeDetail.hidden = false;

    dom.nodeName.textContent = node.name || node.id;
    dom.nodeId.textContent = node.id;
    dom.nodeType.textContent = node.type || "—";
    dom.nodeReview.textContent = node.review_status || "—";
    dom.nodeAliases.textContent = (node.aliases && node.aliases.length)
      ? node.aliases.join(", ") : "—";
    dom.nodeTags.textContent = (node.tags && node.tags.length)
      ? node.tags.join(", ") : "—";
    dom.showContext.hidden = node.type !== "person";
    dom.contextStatus.hidden = state.contextFocusId !== node.id;

    // Properties: render as key/value pairs
    var props = node.properties || {};
    var propKeys = Object.keys(props);
    if (propKeys.length) {
      dom.nodePropertiesBlock.hidden = false;
      dom.nodeProperties.innerHTML = "";
      var pfrag = document.createDocumentFragment();
      propKeys.forEach(function (key) {
        var dt = document.createElement("dt");
        dt.textContent = key;
        var dd = document.createElement("dd");
        var val = props[key];
        dd.textContent = (val === null || val === undefined) ? "—"
          : (typeof val === "object" ? JSON.stringify(val) : String(val));
        pfrag.appendChild(dt);
        pfrag.appendChild(dd);
      });
      dom.nodeProperties.appendChild(pfrag);
    } else {
      dom.nodePropertiesBlock.hidden = true;
    }

    // Provenance
    var prov = node.provenance;
    if (prov) {
      dom.nodeProvenanceBlock.hidden = false;
      if (typeof prov === "string") {
        dom.nodeProvenance.textContent = prov;
      } else if (typeof prov === "object") {
        dom.nodeProvenance.textContent = prov.source_file
          ? "Source: " + prov.source_file
          : JSON.stringify(prov);
      }
    } else {
      dom.nodeProvenanceBlock.hidden = true;
    }

    // Relations grouped by type
    dom.nodeRelations.innerHTML = "";

    if (!relations || !relations.length) {
      dom.nodeRelations.innerHTML = "<div class='relation-item muted'>No relations.</div>";
    } else {
      var byType = {};
      relations.forEach(function (rel) {
        if (!byType[rel.type]) byType[rel.type] = [];
        byType[rel.type].push(rel);
      });

      var rfrag = document.createDocumentFragment();
      Object.keys(byType).sort().forEach(function (rtype) {
        var heading = document.createElement("div");
        heading.className = "rel-group-head";
        heading.textContent = rtype + " (" + byType[rtype].length + ")";
        rfrag.appendChild(heading);

        byType[rtype].forEach(function (rel) {
          var item = document.createElement("div");
          item.className = "relation-item";
          if (rel.weak) item.classList.add("weak-rel");

          var line = document.createElement("div");
          line.className = "rel-line";

          var dirSpan = document.createElement("span");
          dirSpan.className = "rel-dir";
          dirSpan.textContent = rel.dir === "out" ? "→" : "←";
          line.appendChild(dirSpan);

          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "relation-link";
          btn.textContent = (rel.other && rel.other.name) ? rel.other.name : rel.other.id;
          btn.title = rel.other.id;
          btn.addEventListener("click", function () {
            focusNode(rel.other.id);
          });
          line.appendChild(btn);
          item.appendChild(line);

          var edgeProps = rel.properties || {};
          var epKeys = Object.keys(edgeProps);
          if (epKeys.length || rel.weak) {
            var meta = document.createElement("div");
            meta.className = "relation-meta";
            var parts = [];
            if (rel.weak) parts.push("weak");
            epKeys.forEach(function (k) {
              var v = edgeProps[k];
              if (v !== null && v !== undefined && v !== "") {
                parts.push(k + ": " + (typeof v === "object" ? JSON.stringify(v) : String(v)));
              }
            });
            meta.textContent = parts.join(" · ");
            item.appendChild(meta);
          }

          rfrag.appendChild(item);
        });
      });
      dom.nodeRelations.appendChild(rfrag);
    }
  }

  function deselectNode() {
    clearContext();
    state.selectedNodeId = null;
    applyGraphFilters();

    dom.nodeEmpty.hidden = false;
    dom.nodeEmpty.textContent = "Select a node to inspect its relations and metadata.";
    dom.nodeDetail.hidden = true;
    dom.nodeLoading.hidden = true;
  }

  // ---------------------------------------------------------------------------
  // Node click handler
  // ---------------------------------------------------------------------------

  function handleNodeClick(nodeId) {
    var now = Date.now();
    if (state.lastClickNodeId === nodeId && now - state.lastClickAt < 350) {
      state.lastClickAt = 0;
      expandNode(nodeId);
      return;
    }
    state.lastClickNodeId = nodeId;
    state.lastClickAt = now;
    onNodeClick(nodeId);
  }

  function onNodeClick(nodeId) {
    if (state.contextFocusId && state.contextFocusId !== nodeId) {
      var isContextOnly = !state.rawNodes.some(n => n.id === nodeId);
      clearContext();
      if (isContextOnly) {
        focusNode(nodeId);
        return;
      }
    }
    state.selectedNodeId = nodeId;
    applyGraphFilters();
    loadNodeDetail(nodeId);
  }

  // ---------------------------------------------------------------------------
  // Search
  // ---------------------------------------------------------------------------

  var searchDebounce = null;

  function onSearchInput() {
    var q = dom.searchInput.value.trim();
    clearTimeout(searchDebounce);

    if (!q) {
      dom.searchResults.hidden = true;
      dom.searchResults.innerHTML = "";
      dom.searchStatus.textContent = "";
      return;
    }

    searchDebounce = setTimeout(async function () {
      try {
        var data = await apiFetch("/api/search?q=" + encodeURIComponent(q) + "&limit=20");
        var results = data.results || [];
        dom.searchStatus.textContent = results.length + " result" + (results.length === 1 ? "" : "s");
        renderSearchResults(results);
      } catch (err) {
        dom.searchStatus.textContent = "Search error";
      }
    }, 280);
  }

  function renderSearchResults(results) {
    dom.searchResults.innerHTML = "";
    if (!results.length) {
      dom.searchResults.hidden = true;
      return;
    }

    var frag = document.createDocumentFragment();
    results.forEach(function (r) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "search-result-item";
      var nameEl = document.createElement("span");
      nameEl.className = "sr-name";
      nameEl.textContent = r.name;
      var metaEl = document.createElement("span");
      metaEl.className = "sr-meta";
      metaEl.textContent = r.type + (r.current_company ? " · " + r.current_company : "");
      btn.appendChild(nameEl);
      btn.appendChild(metaEl);
      btn.addEventListener("click", function () {
        dom.searchInput.value = "";
        dom.searchResults.hidden = true;
        dom.searchStatus.textContent = "";
        focusNode(r.id);
      });
      frag.appendChild(btn);
    });
    dom.searchResults.appendChild(frag);
    dom.searchResults.hidden = false;
  }

  // ---------------------------------------------------------------------------
  // Path finder
  // ---------------------------------------------------------------------------

  async function findPath() {
    var sourceVal = dom.pathSource.value.trim();
    var targetVal = dom.pathTarget.value.trim();

    if (!sourceVal || !targetVal) {
      dom.pathStatus.textContent = "Enter both source and target.";
      return;
    }

    var sourceId = resolveEntityRef(sourceVal);
    var targetId = resolveEntityRef(targetVal);

    if (!sourceId) {
      dom.pathStatus.textContent = "Source not found: " + sourceVal;
      return;
    }
    if (!targetId) {
      dom.pathStatus.textContent = "Target not found: " + targetVal;
      return;
    }

    dom.pathStatus.textContent = "Searching…";
    dom.pathList.innerHTML = "";
    clearPathHighlight();

    try {
      var url = "/api/path?source=" + encodeURIComponent(sourceId)
        + "&target=" + encodeURIComponent(targetId)
        + "&undirected=true"
        + "&include_weak=" + (state.includeWeak ? "true" : "false");

      var data = await apiFetch(url);

      if (!data.found) {
        dom.pathStatus.textContent = "No path found.";
        return;
      }

      dom.pathStatus.textContent = data.hops + " hop" + (data.hops === 1 ? "" : "s");

      // Merge path nodes & links
      mergeIntoState(data.nodes, data.edges);
      
      // Auto enable checkbox for types if path contains them
      data.nodes.forEach(n => state.activeEntityTypes.add(n.type));
      data.edges.forEach(e => state.activeRelTypes.add(e.type));

      applyGraphFilters();

      // Highlight path elements
      state.pathHighlightedNodes = new Set((data.nodes || []).map(n => n.id));
      state.pathHighlightedEdges = new Set((data.edges || []).map(e => e.id));

      applyPathHighlight();
      renderPathList(data);
    } catch (err) {
      dom.pathStatus.textContent = "Error: " + err.message;
    }
  }

  function resolveEntityRef(val) {
    if (!val) return null;
    var lower = val.toLowerCase().trim();
    var byId = state.launcherEntities.find(function (e) {
      return e.id.toLowerCase() === lower;
    });
    if (byId) return byId.id;
    var byName = state.launcherEntities.find(function (e) {
      return e.name.toLowerCase() === lower;
    });
    if (byName) return byName.id;
    var byPrefix = state.launcherEntities.find(function (e) {
      return e.name.toLowerCase().startsWith(lower);
    });
    if (byPrefix) return byPrefix.id;
    return null;
  }

  function renderPathList(data) {
    dom.pathList.innerHTML = "";
    var nodes = data.nodes || [];
    var edges = data.edges || [];
    var frag = document.createDocumentFragment();
    nodes.forEach(function (n, i) {
      var li = document.createElement("li");
      li.className = "path-step";
      var nameBtn = document.createElement("button");
      nameBtn.type = "button";
      nameBtn.className = "relation-link";
      nameBtn.textContent = n.name || n.id;
      nameBtn.addEventListener("click", function () {
        focusNode(n.id);
      });
      li.appendChild(nameBtn);
      if (i < edges.length) {
        var via = document.createTextNode(" → via " + edges[i].type);
        li.appendChild(via);
      }
      frag.appendChild(li);
    });
    dom.pathList.appendChild(frag);
  }

  function clearPathHighlight() {
    state.pathHighlightedNodes.clear();
    state.pathHighlightedEdges.clear();
    updateHighlights();
    redrawGraph();
  }

  function applyPathHighlight() {
    updateHighlights();
    redrawGraph();
    state.graph.zoomToFit(300);
  }

  // ---------------------------------------------------------------------------
  // Filters Panel Rendering
  // ---------------------------------------------------------------------------

  function renderRelationFilters() {
    dom.relationFilters.innerHTML = "";
    var frag = document.createDocumentFragment();

    state.metaRelationTypes.forEach(function (rt) {
      var item = document.createElement("div");
      item.className = "filter-item";

      var label = document.createElement("label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = rt.type;
      checkbox.checked = state.activeRelTypes.has(rt.type);
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) {
          state.activeRelTypes.add(rt.type);
        } else {
          state.activeRelTypes.delete(rt.type);
        }
        applyGraphFilters();
      });

      var dot = document.createElement("span");
      dot.className = "filter-dot";
      dot.style.background = edgeColor(rt.type);

      var text = document.createElement("span");
      text.className = "filter-label";
      text.textContent = rt.type;
      if (rt.weak) {
        var weakTag = document.createElement("span");
        weakTag.className = "weak-tag";
        weakTag.textContent = "weak";
        text.appendChild(weakTag);
      }

      var count = document.createElement("span");
      count.className = "filter-count";
      count.textContent = rt.count;

      label.appendChild(checkbox);
      label.appendChild(dot);
      label.appendChild(text);
      item.appendChild(label);
      item.appendChild(count);
      frag.appendChild(item);
    });

    dom.relationFilters.appendChild(frag);
  }

  function renderEntityFilters() {
    dom.entityFilters.innerHTML = "";
    var frag = document.createDocumentFragment();

    // Compute dynamic count based on loaded node objects in memory
    const counts = {};
    state.rawNodes.forEach(n => {
      counts[n.type] = (counts[n.type] || 0) + 1;
    });

    state.metaEntityTypes.forEach(function (type) {
      var item = document.createElement("div");
      item.className = "filter-item";

      var label = document.createElement("label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = type;
      checkbox.checked = state.activeEntityTypes.has(type);
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) {
          state.activeEntityTypes.add(type);
        } else {
          state.activeEntityTypes.delete(type);
        }
        applyGraphFilters();
      });

      var dot = document.createElement("span");
      dot.className = "filter-dot";
      dot.style.background = nodeColor(type);

      var text = document.createElement("span");
      text.className = "filter-label";
      text.textContent = type;

      var count = document.createElement("span");
      count.className = "filter-count";
      count.textContent = counts[type] || 0;

      label.appendChild(checkbox);
      label.appendChild(dot);
      label.appendChild(text);
      item.appendChild(label);
      item.appendChild(count);
      frag.appendChild(item);
    });

    dom.entityFilters.appendChild(frag);
  }

  // ---------------------------------------------------------------------------
  // Autocomplete data-list
  // ---------------------------------------------------------------------------

  function populateSuggestionsDatalist() {
    dom.nodeSuggestions.innerHTML = "";
    var frag = document.createDocumentFragment();
    state.launcherEntities.forEach(function (e) {
      var opt = document.createElement("option");
      opt.value = e.name;
      opt.setAttribute("data-id", e.id);
      frag.appendChild(opt);
    });
    dom.nodeSuggestions.appendChild(frag);
  }

  // ---------------------------------------------------------------------------
  // Bind Event Listeners
  // ---------------------------------------------------------------------------

  function bindEvents() {
    dom.searchInput.addEventListener("input", onSearchInput);

    dom.includeWeak.addEventListener("change", function () {
      state.includeWeak = dom.includeWeak.checked;
      applyGraphFilters();
    });

    dom.relationsReset.addEventListener("click", function () {
      state.activeRelTypes.clear();
      state.metaRelationTypes.forEach(function (rt) {
        state.activeRelTypes.add(rt.type);
      });
      applyGraphFilters();
    });

    dom.entitiesReset.addEventListener("click", function () {
      state.activeEntityTypes.clear();
      state.metaEntityTypes.forEach(function (type) {
        state.activeEntityTypes.add(type);
      });
      applyGraphFilters();
    });

    dom.findPath.addEventListener("click", findPath);

    dom.clearPath.addEventListener("click", function () {
      dom.pathSource.value = "";
      dom.pathTarget.value = "";
      dom.pathStatus.textContent = "";
      dom.pathList.innerHTML = "";
      clearPathHighlight();
    });

    dom.fitView.addEventListener("click", function () {
      if (state.graph) {
        state.graph.zoomToFit(400);
      }
    });

    dom.resetView.addEventListener("click", loadOwnerEgo);

    dom.expandNeighbors.addEventListener("click", function () {
      if (state.selectedNodeId) {
        expandNode(state.selectedNodeId);
      }
    });

    dom.showContext.addEventListener("click", function () {
      if (state.selectedNodeId) {
        showWorkContext(state.selectedNodeId);
      }
    });

    // Physics pause/play listener
    dom.togglePhysics.addEventListener("click", function () {
      if (state.physicsEnabled) {
        state.graph.pauseAnimation();
        state.physicsEnabled = false;
        dom.togglePhysics.classList.add("paused");
        dom.togglePhysics.textContent = "Resume Physics";
      } else {
        state.graph.resumeAnimation();
        state.graph.d3ReheatSimulation();
        state.physicsEnabled = true;
        dom.togglePhysics.classList.remove("paused");
        dom.togglePhysics.textContent = "Pause Physics";
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Main Entry Point
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var v2Probe = window.__synapseV2Probe || Promise.resolve(false);
    v2Probe.then(function (isV2) {
      if (isV2) return;
      cacheDom();
      initForceGraph();
      bindEvents();
      loadOwnerEgo();
    });
  });

})();
