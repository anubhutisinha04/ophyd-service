import './App.css'
import MotorController from './components/MotorController'
import DeviceMotorController from './components/DeviceMotorController'
import DeviceBrowser from './components/DeviceBrowser'

function App() {
 
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '2rem', padding: '1rem' }}>      <DeviceBrowser />
      {/* <MotorController /> */}
      {/* <DeviceMotorController /> */}
    </div>
  )
}

export default App
