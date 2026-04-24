import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FinchConfigProvider } from '@blueskyproject/finch'
import '@blueskyproject/finch/style.css'
import App from './App.tsx'
import './index.css'

const queryClient = new QueryClient()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <FinchConfigProvider config={{
        ophydApiUrl: 'http://localhost:8001/api/v1',
      }}>
        <App />
      </FinchConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
)
