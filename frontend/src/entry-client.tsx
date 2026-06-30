import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FinchConfigProvider } from '@blueskyproject/finch'
import { AuthProvider } from './contexts/AuthContext'
import '@blueskyproject/finch/style.css'
import App from './App.tsx'
import './index.css'

const queryClient = new QueryClient()

// Read auth data injected during SSR
const authData = window.__AUTH_DATA__ || null

const app = (
  <React.StrictMode>
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <FinchConfigProvider config={{
          ophydApiUrl: import.meta.env.VITE_API_URL || 'http://localhost:8003/api/v1',
        }}>
          <AuthProvider authData={authData}>
            <App />
          </AuthProvider>
        </FinchConfigProvider>
      </QueryClientProvider>
    </BrowserRouter>
  </React.StrictMode>
)

const rootElement = document.getElementById('root')!

if (rootElement.hasChildNodes()) {
  ReactDOM.hydrateRoot(rootElement, app)
} else {
  ReactDOM.createRoot(rootElement).render(app)
}
