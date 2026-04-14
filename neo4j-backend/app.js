const express = require('express');
const cors    = require('cors');
require('dotenv').config();

const graphRoutes = require('./routes/graph');
const routePaths  = require('./routes/routes');
const auditRoutes = require('./routes/audit');

const app = express();
app.use(cors());
app.use(express.json());

app.use('/api/graph',  graphRoutes);
app.use('/api/routes', routePaths);
app.use('/api/audit',  auditRoutes);

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', service: 'MoDaaS Neo4j Backend', port: PORT });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Neo4j backend running on port ${PORT}`));