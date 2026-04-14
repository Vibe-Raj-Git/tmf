/**
 * audit.js — MoDaaS audit trail routes
 * Receives write events from mock-vendors and serves read data to Angular.
 * Mounted at /api/audit
 *
 * Graph model:
 *   (:Intent)-[:HAS_PLAN]->(:Plan)
 *   (:Intent)-[:VALIDATED_BY]->(:AuditEntry)
 *   (:Intent)-[:ROUTED_VIA]->(:NetworkPath)
 *   (:Intent)-[:PATH_UPDATED]->(:PathUpdate)
 *
 * Security: NO credentials stored in Neo4j — audit events only.
 * sovereigntyToken stored as 12-char hint only — never full token.
 */
const express = require('express');
const router  = express.Router();
const driver  = require('../db');


// ── WRITE endpoints (called by mock-vendors) ──────────────────────────────

// POST /api/audit/intent — BSS acknowledged, Intent node created
router.post('/intent', async (req, res) => {
  const { intent_id, intent_ref, status, org_name, customer_id,
          service_type, timestamp } = req.body;
  const session = driver.session();
  try {
    await session.run(`
      MERGE (i:Intent {intent_id: $intent_id})
      SET i.intent_ref   = $intent_ref,
          i.status       = $status,
          i.org_name     = $org_name,
          i.customer_id  = $customer_id,
          i.service_type = $service_type,
          i.created_at   = $timestamp
    `, { intent_id, intent_ref, status, org_name, customer_id,
         service_type, timestamp });
    console.log(`[Neo4j] Intent node created: ${intent_id}`);
    res.json({ ok: true, intent_id });
  } catch (err) {
    console.error('[Neo4j] intent write error:', err.message);
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// POST /api/audit/status — status transition (PLANNING / ACTIVE / REJECTED)
router.post('/status', async (req, res) => {
  const { intent_id, status, plan_id, activated_at, timestamp } = req.body;
  const session = driver.session();
  try {
    await session.run(`
      MERGE (i:Intent {intent_id: $intent_id})
      SET i.status = $status,
          i.last_updated = $timestamp
    `, { intent_id, status, timestamp });

    // Create Plan node on PLANNING transition
    if (status === 'PLANNING' && plan_id) {
      await session.run(`
        MERGE (i:Intent {intent_id: $intent_id})
        MERGE (p:Plan {plan_id: $plan_id})
        SET p.intent_id  = $intent_id,
            p.created_at = $timestamp
        MERGE (i)-[:HAS_PLAN]->(p)
      `, { intent_id, plan_id, timestamp });
    }

    // Set activated_at on ACTIVE transition
    if (status === 'ACTIVE' && activated_at) {
      await session.run(`
        MATCH (i:Intent {intent_id: $intent_id})
        SET i.activated_at = $activated_at
      `, { intent_id, activated_at });
    }

    console.log(`[Neo4j] Intent ${intent_id} → ${status}`);
    res.json({ ok: true, intent_id, status });
  } catch (err) {
    console.error('[Neo4j] status write error:', err.message);
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// POST /api/audit/nai — NAI governance validation result
// NOTE: sovereigntyToken stored as hint only (first 12 chars) — never full token
router.post('/nai', async (req, res) => {
  const { intent_id, rules_passed, compliance_key,
          validated, source, timestamp } = req.body;
  const session = driver.session();
  try {
    // Store only a redacted hint — full token stays in NAI secure store + LLM Router
    const key_hint = compliance_key
      ? compliance_key.substring(0, 12) + '...'
      : 'N/A';

    await session.run(`
      MERGE (i:Intent {intent_id: $intent_id})
      CREATE (a:AuditEntry {
        intent_id:              $intent_id,
        rules_passed:           $rules_passed,
        sovereignty_token_hint: $key_hint,
        validated:              $validated,
        nai_source:             $source,
        timestamp:              $timestamp
      })
      MERGE (i)-[:VALIDATED_BY]->(a)
    `, {
      intent_id,
      rules_passed: JSON.stringify(rules_passed),
      key_hint,
      validated,
      source,
      timestamp
    });

    console.log(`[Neo4j] NAI audit entry for ${intent_id} — validated: ${validated}`);
    res.json({ ok: true, intent_id, validated });
  } catch (err) {
    console.error('[Neo4j] nai write error:', err.message);
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// POST /api/audit/path — TC path feasibility confirmed
// Also sets current_path directly on Intent node so Orders page can display it
router.post('/path', async (req, res) => {
  const { intent_id, path, status, timestamp } = req.body;
  const session = driver.session();
  try {
    // Create NetworkPath node linked to Intent
    await session.run(`
      MERGE (i:Intent {intent_id: $intent_id})
      CREATE (p:NetworkPath {
        intent_id: $intent_id,
        path:      $path,
        status:    $status,
        timestamp: $timestamp
      })
      MERGE (i)-[:ROUTED_VIA]->(p)
    `, { intent_id, path, status, timestamp });

    // ── FIX: also write current_path directly on Intent node ─────────────
    // The status poll (GET /orders/:id/status) reads from Intent node only.
    // Without this, current_path is null and Orders page shows wrong fallback.
    await session.run(`
      MATCH (i:Intent {intent_id: $intent_id})
      SET i.current_path = $path
    `, { intent_id, path });

    console.log(`[Neo4j] Path recorded for ${intent_id}: ${path}`);
    res.json({ ok: true, intent_id, path });
  } catch (err) {
    console.error('[Neo4j] path write error:', err.message);
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// POST /api/audit/pathupdate — TC async path change (Step 6)
router.post('/pathupdate', async (req, res) => {
  const { intent_id, new_path, new_router_ip, reason, timestamp } = req.body;
  const session = driver.session();
  try {
    // Update current_path on Intent node — Orders page reads this
    await session.run(`
      MATCH (i:Intent {intent_id: $intent_id})
      SET i.current_path     = $new_path,
          i.last_path_update = $timestamp
    `, { intent_id, new_path, timestamp });

    // Create PathUpdate node — audit record of the reroute
    await session.run(`
      MERGE (i:Intent {intent_id: $intent_id})
      CREATE (u:PathUpdate {
        intent_id: $intent_id,
        new_path:  $new_path,
        reason:    $reason,
        timestamp: $timestamp
      })
      MERGE (i)-[:PATH_UPDATED]->(u)
    `, { intent_id, new_path, reason, timestamp });

    // new_router_ip not stored in Neo4j — updated in LLM Router store only
    console.log(`[Neo4j] PathUpdate for ${intent_id}: ${new_path}`);
    res.json({ ok: true, intent_id, new_path });
  } catch (err) {
    console.error('[Neo4j] pathupdate write error:', err.message);
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});


// ── READ endpoints (called by Angular Orders page) ────────────────────────

// GET /api/audit/orders — all intents for Orders page list
router.get('/orders', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (i:Intent)
      RETURN i ORDER BY i.created_at DESC LIMIT 50
    `);
    const orders = result.records.map(r => r.get('i').properties);
    res.json({ orders });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// GET /api/audit/orders/:intentId — full audit trail for one intent
router.get('/orders/:intentId', async (req, res) => {
  const session = driver.session();
  try {
    const intentResult = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})
      RETURN i
    `, { intentId: req.params.intentId });

    if (intentResult.records.length === 0)
      return res.status(404).json({ error: 'Intent not found' });

    const intent = intentResult.records[0].get('i').properties;

    const planResult = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})-[:HAS_PLAN]->(p:Plan)
      RETURN p
    `, { intentId: req.params.intentId });
    const plans = planResult.records.map(r => r.get('p').properties);

    const naiResult = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})-[:VALIDATED_BY]->(a:AuditEntry)
      RETURN a ORDER BY a.timestamp DESC
    `, { intentId: req.params.intentId });
    const auditEntries = naiResult.records.map(r => r.get('a').properties);

    const pathResult = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})-[:ROUTED_VIA]->(p:NetworkPath)
      RETURN p ORDER BY p.timestamp DESC
    `, { intentId: req.params.intentId });
    const paths = pathResult.records.map(r => r.get('p').properties);

    const updateResult = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})-[:PATH_UPDATED]->(u:PathUpdate)
      RETURN u ORDER BY u.timestamp DESC
    `, { intentId: req.params.intentId });
    const pathUpdates = updateResult.records.map(r => r.get('u').properties);

    res.json({ intent, plans, auditEntries, paths, pathUpdates });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

// GET /api/audit/orders/:intentId/status — lightweight status poll
// Called by Angular Orders page every 5 seconds
router.get('/orders/:intentId/status', async (req, res) => {
  const session = driver.session();
  try {
    const result = await session.run(`
      MATCH (i:Intent {intent_id: $intentId})
      RETURN i.status       AS status,
             i.activated_at AS activated_at,
             i.current_path AS current_path
    `, { intentId: req.params.intentId });

    if (result.records.length === 0)
      return res.status(404).json({ error: 'Intent not found' });

    const rec = result.records[0];
    res.json({
      intent_id:    req.params.intentId,
      status:       rec.get('status'),
      activated_at: rec.get('activated_at'),
      current_path: rec.get('current_path')
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  } finally {
    await session.close();
  }
});

module.exports = router;