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

const renderAuthScript = (authData) => {
  // Escape < to prevent XSS if authData contains malicious content like </script>
  const safeJson = JSON.stringify(authData).replace(/</g, '\\u003c');
  return `<script>window.__AUTH_DATA__=${safeJson};</script>`;
};

// Safely extract a header value as a string (handles string | string[] | undefined)
const getHeader = (req, name) => {
  const value = req.headers[name];
  if (Array.isArray(value)) return value[0] || '';
  return value || '';
};

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
    // Preserve leading slash for SSR route matching
    const url = basePath === '/'
      ? req.originalUrl
      : (req.originalUrl.startsWith(basePath) ? '/' + req.originalUrl.slice(basePath.length) : req.originalUrl);

    /** @type {string} */
    let template;
    /** @type {import('./src/entry-server.tsx').render | undefined} */
    let render;

    // Extract Entra ID auth headers from HAProxy
    const rolesHeader = getHeader(req, 'access-token-roles');
    let authData = {
      upn: getHeader(req, 'access-token-upn'),
      name: getHeader(req, 'access-token-name'),
      roles: rolesHeader ? rolesHeader.split(',').map(r => r.trim()) : [],
      givenName: getHeader(req, 'access-token-given-name'),
      familyName: getHeader(req, 'access-token-family-name'),
    };

    if (allowDevAuth && authData.roles.length === 0) {
      authData = defaultDevAuthData;
    }

    // Log auth for debugging (don't log in production for security)
    if (!isProduction) {
      console.log(`[${req.correlationId()}] Auth: ${authData.upn || 'none'} - Roles: ${authData.roles.join(', ') || 'none'}`);
    }

    if (isProduction) {
      // Production: Use pre-built SSR bundle for fast server-side rendering
      template = templateHtml;
      render = (await import('./dist/server/entry-server.js')).render;
      const rendered = await render(url, authData);
      
      const html = template
        .replace(`<!--app-head-->`, rendered.head ?? '')
        .replace(`<!--app-html-->`, rendered.html ?? '');
      
      res.status(authData.upn && authData.roles.length > 0 ? 200 : 401).set({ 'Content-Type': 'text/html' }).send(html);
    } else {
      // Development: Use Vite's SSR module loading with HMR
      template = await fs.readFile('./index.html', 'utf-8');
      template = await vite.transformIndexHtml(url, template);
      render = (await vite.ssrLoadModule('/src/entry-server.tsx')).render;
      
      const rendered = await render(url, authData);
      
      const html = template
        .replace(`<!--app-head-->`, rendered.head ?? '')
        .replace(`<!--app-html-->`, rendered.html ?? '');
      
      res.status(authData.upn && authData.roles.length > 0 ? 200 : 401).set({ 'Content-Type': 'text/html' }).send(html);
    }
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
