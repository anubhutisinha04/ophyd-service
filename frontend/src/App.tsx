import { HubAppLayout, RouteItem } from '@blueskyproject/finch'
import { Atom, SlidersHorizontal, Table } from '@phosphor-icons/react'
import IosScan from './pages/IosScan'
import ScanSettings from './pages/ScanSettings'
import PresetsAdmin from './pages/PresetsAdmin'

function App() {
  const routes: RouteItem[] = [
    {
      path: '/',
      label: 'IOS Scan',
      element: <IosScan />,
      icon: <Atom size={28} />,
      isBackgroundTransparent: false,
      classNameContainer: 'w-full max-w-none min-h-screen bg-white p-0',
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
      classNameContainer: 'w-full max-w-none min-h-screen bg-white p-0',
    },
  ]

  return (
    <HubAppLayout
      routes={routes}
      headerTitle="IOS Scan"
    />
  )
}

export default App
