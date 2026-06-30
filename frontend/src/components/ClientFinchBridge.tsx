import { useState, useEffect, ReactNode } from 'react';
import { Routes, Route } from 'react-router';
import type { RouteItem } from '@blueskyproject/finch';

interface FinchBridgeProps {
  routes: RouteItem[];
  headerTitle: string;
  fallback?: ReactNode;
}

/**
 * Client-only bridge for @blueskyproject/finch
 * 
 * Finch touches `window` at module load time, which crashes Node.js SSR.
 * This component:
 * 1. Server-renders a basic route structure with auth context intact
 * 2. Client-side dynamically imports finch after hydration
 * 3. Seamlessly swaps in HubAppLayout once loaded
 */
export function ClientFinchBridge({ routes, headerTitle, fallback }: FinchBridgeProps) {
  const [FinchModule, setFinchModule] = useState<any>(null);

  useEffect(() => {
    // Dynamic import only executes in the browser
    import('@blueskyproject/finch').then((finch) => {
      setFinchModule(finch);
    });
  }, []);

  // Server-side and initial client render: use basic Routes fallback
  if (!FinchModule) {
    return (
      <>
        {fallback}
        <Routes>
          {routes.map((route) => (
            <Route key={route.path} path={route.path} element={route.element} />
          ))}
        </Routes>
      </>
    );
  }

  // Once finch loads, render HubAppLayout
  const { HubAppLayout, FinchConfigProvider } = FinchModule;

  return (
    <FinchConfigProvider config={{ ophydApiUrl: '/api/v1' }}>
      <HubAppLayout routes={routes} headerTitle={headerTitle} />
    </FinchConfigProvider>
  );
}
