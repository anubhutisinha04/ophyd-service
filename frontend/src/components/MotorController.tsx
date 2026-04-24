import { TableDeviceController, useOphydDeviceSocket } from '@blueskyproject/finch';

const MOTOR_DEVICES = ['motor1', 'motor2', 'motor3'];

interface MotorControllerProps {
  deviceNames?: string[];
}

function MotorController({ deviceNames = MOTOR_DEVICES }: MotorControllerProps) {
  const { devices, handleSetValueRequest, toggleDeviceLock, toggleExpand } =
    useOphydDeviceSocket(deviceNames);

  return (
    <div>
      <h2>Motor Controller</h2>
      <TableDeviceController
        devices={devices}
        handleSetValueRequest={handleSetValueRequest}
        toggleDeviceLock={toggleDeviceLock}
        toggleExpand={toggleExpand}
      />
    </div>
  );
}

export default MotorController;
