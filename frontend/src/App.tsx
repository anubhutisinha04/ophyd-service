import './App.css'
import DeviceMotorController from './components/DeviceMotorController'
import DeviceBrowser from './components/DeviceBrowser'
import { SignalMonitorPlotPV } from '@blueskyproject/finch'

function App() {

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem', padding: '1rem' }}>      <DeviceBrowser />
      {/* <MotorController /> */}
      <DeviceMotorController />
      <SignalMonitorPlotPV pv="mini:current" />
    </div>
  )
}

export default App
