import { DeviceControllerBox, useOphydDeviceSocket } from '@blueskyproject/finch';

interface DeviceMotorControllerProps {
  deviceName?: string;
}

function DeviceMotorController({ deviceName = 'motor1' }: DeviceMotorControllerProps) {
  const { devices, handleSetValueRequest, toggleDeviceLock } =
    useOphydDeviceSocket([deviceName]);

  const device = devices[deviceName];

  if (!device) {
    return <div>Connecting to {deviceName}...</div>;
  }

  return (
    <DeviceControllerBox
      device={device}
      handleSetValueRequest={handleSetValueRequest}
      handleLockClick={toggleDeviceLock}
      title={deviceName}
    />
  );
}

export default DeviceMotorController;
