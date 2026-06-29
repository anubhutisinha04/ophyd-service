import React from 'react';
import ReactDOMServer from 'react-dom/server';
import { StaticRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FinchConfigProvider } from '@blueskyproject/finch';
import { AuthProvider } from './contexts/AuthContext';
import type { AuthData } from './types/auth';
import App from './App';

export async function render(url: string, authData: AuthData) {
  const queryClient = new QueryClient();

  const html = ReactDOMServer.renderToString(
    <React.StrictMode>
      <StaticRouter location={url}>
        <QueryClientProvider client={queryClient}>
          <FinchConfigProvider config={{
            ophydApiUrl: '/api/v1',
          }}>
            <AuthProvider authData={authData}>
              <App />
            </AuthProvider>
          </FinchConfigProvider>
        </QueryClientProvider>
      </StaticRouter>
    </React.StrictMode>
  );

  const head = `<script>window.__AUTH_DATA__=${JSON.stringify(authData)};</script>`;

  return { html, head };
}
