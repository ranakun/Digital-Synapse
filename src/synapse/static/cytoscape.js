(function (global) {
  "use strict";

  function normalizeElements(elements) {
    if (!Array.isArray(elements)) {
      return [];
    }

    return elements
      .map(function (element) {
        if (!element || typeof element !== "object") {
          return null;
        }
        if (element.data && typeof element.data === "object") {
          return {
            group: element.group || (element.data.source || element.data.target ? "edges" : "nodes"),
            data: Object.assign({}, element.data),
            classes: element.classes || "",
          };
        }
        return {
          group: element.group || (element.source || element.target ? "edges" : "nodes"),
          data: Object.assign({}, element),
          classes: element.classes || "",
        };
      })
      .filter(Boolean);
  }

  function cytoscape(options) {
    var state = {
      options: options || {},
      elements: normalizeElements((options && options.elements) || []),
      destroyed: false,
    };

    function api() {
      return api;
    }

    api.version = "stub-0.1.0";
    api.json = function (next) {
      if (!next) {
        return {
          elements: state.elements.slice(),
        };
      }
      if (next.elements) {
        state.elements = normalizeElements(next.elements);
      }
      return api;
    };
    api.add = function (elements) {
      state.elements = state.elements.concat(normalizeElements(elements));
      return api;
    };
    api.remove = function (selector) {
      if (selector === "node" || selector === "nodes") {
        state.elements = state.elements.filter(function (element) {
          return element.group !== "nodes";
        });
      } else if (selector === "edge" || selector === "edges") {
        state.elements = state.elements.filter(function (element) {
          return element.group !== "edges";
        });
      }
      return api;
    };
    api.on = function () {
      return api;
    };
    api.off = function () {
      return api;
    };
    api.destroy = function () {
      state.destroyed = true;
      var container = state.options && state.options.container;
      if (container && container.innerHTML !== undefined) {
        container.innerHTML = "";
      }
    };
    api.fit = function () {
      return api;
    };
    api.center = function () {
      return api;
    };
    api.layout = function () {
      return {
        run: function () {
          return api;
        },
      };
    };
    api.nodes = function () {
      return normalizeElements(state.elements.filter(function (element) {
        return element.group === "nodes";
      }));
    };
    api.edges = function () {
      return normalizeElements(state.elements.filter(function (element) {
        return element.group === "edges";
      }));
    };
    api.getElementById = function (id) {
      var found = state.elements.find(function (element) {
        return element.data && element.data.id === id;
      });
      return found ? normalizeElements([found])[0] : null;
    };
    api.collection = function (elements) {
      return normalizeElements(elements);
    };

    return api;
  }

  global.cytoscape = cytoscape;
})(typeof window !== "undefined" ? window : globalThis);
