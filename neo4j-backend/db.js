
/**
 * db.js — shared Neo4j driver
 * Single connection reused across all route files.
 * Credentials loaded from .env
 */
const neo4j = require('neo4j-driver');

const driver = neo4j.driver(
  process.env.NEO4J_URI,
  neo4j.auth.basic(process.env.NEO4J_USERNAME, process.env.NEO4J_PASSWORD)
);

// Verify connection on startup
driver.verifyConnectivity()
  .then(() => console.log('Neo4j connected:', process.env.NEO4J_URI))
  .catch(err => console.error('Neo4j connection failed:', err.message));

module.exports = driver;