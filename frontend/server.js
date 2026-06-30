// Shared server configuration for both development and production environments

import fs from 'node:fs/promises';
import express from 'express';
import correlator from 'express-correlation-id';
import { createServer as createViteServer } from 'vite';

// Service configuration    
const isProduction = process.env.NODE_ENV === 'production';
const basePath = process.env.BASE || '/';
const port = process.env.PORT ? Number.parseInt(process.env.PORT, 10) : 5173;
// Inject a fake admin user when no HAProxy auth headers are present. Always on
// in dev; opt-in for local production preview via DEV_AUTH=true. Never enable
// in real deployments — HAProxy supplies the headers there.
const allowDevAuth = !isProduction || process.env.DEV_AUTH === 'true';

const defaultDevAuthData = {
  upn: 'local.dev@bnl.gov',
  name: 'Local Dev User',
  roles: ['ios.admin'],
  givenName: 'Local',
  familyName: 'Dev',
};

const renderAuthScript = (authData) => `<script>window.__AUTH_DATA__=${JSON.stringify(authData)};</script>`;

// Cached production assets (client-side only - finch doesn't support SSR)
const templateHtml = isProduction
  ? await fs.readFile('./dist/client/index.html', 'utf-8')
  : '';

// Create http server
const app = express();

// Middleware: Depends on environment
/** @type {import('vite').ViteDevServer | undefined} */
let vite;

// Middleware: Correlation ID
app.use(correlator({ header: 'X-Request-ID' }));

// API proxy — forward backend requests in both dev and production.
// Mirrors the proxy config in vite.config.ts (which only applies to
// the standalone Vite dev server, not middleware mode).
const { createProxyMiddleware } = await import('http-proxy-middleware');

const PRESETS_TARGET = process.env.VITE_PRESETS_TARGET || 'http://localhost:8005';
const CONFIG_TARGET  = process.env.VITE_CONFIG_TARGET  || 'http://localhost:8004';
const CONTROL_TARGET = process.env.VITE_CONTROL_TARGET || 'http://localhost:8003';

app.use('/api/presets', createProxyMiddleware({
  target: PRESETS_TARGET, changeOrigin: true,
  pathRewrite: (path) => '/api/v1' + path,
}));
app.use('/api/config', createProxyMiddleware({
  target: CONFIG_TARGET, changeOrigin: true,
  pathRewrite: (path) => '/api/v1' + path,
}));
app.use('/api/control', createProxyMiddleware({
  target: CONTROL_TARGET, changeOrigin: true,
  pathRewrite: (path) => '/api/v1' + path,
}));

if (isProduction) {
  // Production middleware layers
  const compression = (await import('compression')).default;
  const sirv = (await import('sirv')).default;

  app.use(compression());
  app.use(basePath, sirv('./dist/client', { extensions: [] }));
} else {
  // Vite server as middleware
  vite = await createViteServer({
    server: { middlewareMode: true },
    appType: 'custom',
    base: basePath,
  });

  app.use(vite.middlewares);
}

// Serve HTML - catch-all route for SSR
app.use(async (req, res) => {
  try {
    const url = req.originalUrl.replace(basePath, '');

    /** @type {string} */
    let template;
    /** @type {import('./src/entry-server.tsx').render | undefined} */
    let render;

    if (isProduction) {
      // Use pre-built template and server entry in production
      template = templateHtml;
      render = (await import('./dist/server/entry-server.js')).render;
    } else {
      // Always read fresh template in development
      template = await fs.readFile('./index.html', 'utf-8');
      template = await vite.transformIndexHtml(url, template);
    }

    // Extract Entra ID auth headers from HAProxy
    let authData = {
      upn: req.headers['access-token-upn'] || '',
      name: req.headers['access-token-name'] || '',
      roles: req.headers['access-token-roles']
        ? req.headers['access-token-roles'].split(',').map(r => r.trim())
        : [],
      givenName: req.headers['access-token-given-name'],
      familyName: req.headers['access-token-family-name'],
    };

    if (allowDevAuth && authData.roles.length === 0) {
      authData = defaultDevAuthData;
    }

    // Log auth for debugging (don't log in production for security)
    if (!isProduction) {
      console.log(`[${req.correlationId()}] Auth: ${authData.upn || 'none'} - Roles: ${authData.roles.join(', ') || 'none'}`);
    }

    const rendered = render
      ? await render(url, authData)
      : { html: '', head: renderAuthScript(authData) };

    const html = template
      .replace(`<!--app-head-->`, rendered.head ?? '')
      .replace(`<!--app-html-->`, rendered.html ?? '');

    res.status(200).set({ 'Content-Type': 'text/html' }).send(html);
  } catch (e) {
    vite?.ssrFixStacktrace(e);
    console.error(e.stack);
    res.status(500).end('Internal Server Error');
  }
});

// Start http server
app.listen(port, () => {
  console.log(`Server started at http://localhost:${port}`);
});
