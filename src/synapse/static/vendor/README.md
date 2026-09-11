# Vendored frontend assets

These files are committed into the repo so the graph UI works **fully offline** —
the server never loads anything from a CDN at runtime.

## force-graph.min.js

- **Library:** force-graph (2D HTML5 Canvas graph rendering)
- **Version:** 1.51.4
- **License:** MIT (see force-graph-LICENSE.txt; version header retained in the bundle)
- **Source:** https://unpkg.com/force-graph@1.51.4/dist/force-graph.min.js
- **SHA-256:** `1008539bb9e171a0dc343453366451a1b3a6ded06028ef4f978608b658ba2d0a`

### Refreshing / upgrading force-graph

```bash
curl -fsSL -o src/synapse/static/vendor/force-graph.min.js \
  https://unpkg.com/force-graph@<version>/dist/force-graph.min.js
sha256sum src/synapse/static/vendor/force-graph.min.js   # update the hash above
```

