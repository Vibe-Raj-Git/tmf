/**
 * graph.js — topology routes
 * Mounted at /api/graph
 */
const express = require('express');
const router  = express.Router();
const driver  = require('../db');

// GET /api/graph — D3-compatible format for topology.html
// Returns { nodes: [...], links: [...] }
// Node IDs use Neo4j internal identity (unique per node type).
// display field = "Label\nshortId" for human-readable labels in D3.
router.get('/', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (n)-[r]->(m)
      RETURN n, r, m LIMIT 200
    `);
    const nodes = [];
    const links = [];
    const seen  = new Set();

    result.records.forEach(rec => {
      const n   = rec.get('n');
      const m   = rec.get('m');
      const rel = rec.get('r');

      [n, m].forEach(node => {
        const uid = node.identity.toString();
        if (!seen.has(uid)) {
          seen.add(uid);
          const props   = node.properties;
          const label   = node.labels[0] || 'Node';
          const shortId = props.intent_id
            ? props.intent_id.substring(0, 8)
            : props.name || props.org_name || props.path || uid;
          nodes.push({
            id:         uid,                        // unique Neo4j internal ID — used by D3 force links
            display:    `${label}\n${shortId}`,     // human-readable label shown on graph
            label:      label,
            shortId:    shortId,
            labels:     node.labels,
            properties: props
          });
        }
      });

      links.push({
        source:     n.identity.toString(),          // matches node.id above
        target:     m.identity.toString(),          // matches node.id above
        type:       rel.type,
        properties: rel.properties
      });
    });

    res.json({ nodes, links });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// GET /api/graph/topology — full network topology (original, kept for compatibility)
router.get('/topology', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (n)-[r]->(m)
      WHERE n:NetworkNode OR n:Intent OR n:Plan
      RETURN n, r, m LIMIT 100
    `);
    const nodes = [], edges = [];
    const seen  = new Set();
    result.records.forEach(rec => {
      [rec.get('n'), rec.get('m')].forEach(node => {
        if (!seen.has(node.identity.toString())) {
          seen.add(node.identity.toString());
          nodes.push({ id: node.identity.toString(), labels: node.labels, properties: node.properties });
        }
      });
      const rel = rec.get('r');
      edges.push({
        from:       rel.start.toString(),
        to:         rel.end.toString(),
        type:       rel.type,
        properties: rel.properties
      });
    });
    res.json({ nodes, edges });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// GET /api/graph/nodes — all nodes summary
router.get('/nodes', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run('MATCH (n) RETURN n LIMIT 200');
    const nodes  = result.records.map(r => ({
      id:         r.get('n').identity.toString(),
      labels:     r.get('n').labels,
      properties: r.get('n').properties
    }));
    res.json({ nodes });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

module.exports = router;