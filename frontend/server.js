// Shared server configuration for both development and production environments

import fs from 'node:fs/promises';
import express from 'express';
import correlator from 'express-correlation-id';
import { createServer as createViteServer } from 'vite';

// Service configuration    
const isProduction = process.env.NODE_ENV === 'production';
const basePath = process.env.BASE || '/';
const port = process.env.PORT ? Number.parseInt(process.env.PORT, 10) : 5173;

// Cached production assets
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
    /** @type {import('./src/entry-server.tsx').render} */
    let render;

    if (isProduction) {
      // Use pre-built template and server entry in production
      template = templateHtml;
      render = (await import('./dist/server/entry-server.js')).render;
    } else {
      // Always read fresh template in development
      template = await fs.readFile('./index.html', 'utf-8');
      template = await vite.transformIndexHtml(url, template);
      render = (await vite.ssrLoadModule('/src/entry-server.tsx')).render;
    }

    // Extract Entra ID auth headers from HAProxy
    const authData = {
      upn: req.headers['access-token-upn'] || '',
      name: req.headers['access-token-name'] || '',
      roles: req.headers['access-token-roles']
        ? req.headers['access-token-roles'].split(',').map(r => r.trim())
        : [],
      givenName: req.headers['access-token-given-name'],
      familyName: req.headers['access-token-family-name'],
    };

    // Log auth for debugging (don't log in production for security)
    if (!isProduction) {
      console.log(`[${req.correlationId()}] Auth: ${authData.upn || 'none'} - Roles: ${authData.roles.join(', ') || 'none'}`);
    }

    const rendered = await render(url, authData);

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
