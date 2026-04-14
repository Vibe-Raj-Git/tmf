/**
 * routes.js — network path routes
 * Serves Riyadh→Dubai path data for topology visualization.
 * Mounted at /api/routes
 */
const express = require('express');
const router  = express.Router();
const driver  = require('../db');

// GET /api/routes — all network paths
router.get('/', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (p:NetworkPath)
      RETURN p ORDER BY p.timestamp DESC LIMIT 50
    `);
    const paths = result.records.map(r => r.get('p').properties);
    res.json({ paths });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// GET /api/routes/:intentId — path for a specific intent
router.get('/:intentId', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})-[:ROUTED_VIA]->(p:NetworkPath)
      RETURN p ORDER BY p.timestamp DESC LIMIT 10
    `, { intentId: req.params.intentId });
    const paths = result.records.map(r => r.get('p').properties);
    res.json({ intent_id: req.params.intentId, paths });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

module.exports = router;