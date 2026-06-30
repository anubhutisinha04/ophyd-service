import type { RouteItem } from '@blueskyproject/finch'
import { Atom, SlidersHorizontal, Table } from '@phosphor-icons/react'
import { useAuth } from './contexts/AuthContext'
import { ClientFinchBridge } from './components/ClientFinchBridge'
import IosScan from './pages/IosScan'
import ScanSettings from './pages/ScanSettings'
import PresetsAdmin from './pages/PresetsAdmin'

function App() {
  const auth = useAuth()

  const allRoutes: RouteItem[] = [
    {
      path: '/',
      label: 'IOS Scan',
      element: <IosScan />,
      icon: <Atom size={28} />,
      isBackgroundTransparent: false,
    },
    {
      path: '/settings',
      label: 'Component Testing',
      element: <ScanSettings />,
      icon: <SlidersHorizontal size={28} />,
      isBackgroundTransparent: false,
    },
    {
      path: '/presets-admin',
      label: 'Presets Admin',
      element: <PresetsAdmin />,
      icon: <Table size={28} />,
      isBackgroundTransparent: false,
    },
  ]

  // Filter routes based on user permissions
  const routes = allRoutes.filter((route) => {
    // Presets admin is only for admins
    if (route.path === '/presets-admin') {
      return auth.canAccessPresetsAdmin()
    }
    // All other routes are accessible to operators
    return auth.hasRole('ios.operator') || auth.isAdmin()
  })

  return (
    <ClientFinchBridge
      routes={routes}
      headerTitle="IOS Scan"
      fallback={<div className="p-4 text-gray-500">Loading interface...</div>}
    />
  )
}

export default App
