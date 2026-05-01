import { useEffect, useState } from 'react';
import { DeviceControllerBox, useOphydPVSocket, useOptionalFinchConfig } from '@blueskyproject/finch';

interface DeviceMotorControllerProps {
  deviceName?: string;
}

function DeviceMotorController({ deviceName = 'motor_ph' }: DeviceMotorControllerProps) {
  const [pvName, setPvName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const config = useOptionalFinchConfig();
  const apiUrl = config?.ophydApiUrl ?? 'http://localhost:8003/api/v1';

  // Fetch the PV name from the config service via the direct control proxy.
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setPvName(null);

    const url = `${apiUrl}/devices/${encodeURIComponent(deviceName)}`;
    fetch(url, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to fetch device: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        const pvs: Record<string, string> = data.pvs || {};
        const firstPv = Object.values(pvs)[0];
        if (!firstPv) throw new Error(`No PVs found for device ${deviceName}`);
        setPvName(firstPv);
      })
      .catch((err) => {
        if (err.name !== 'AbortError') setError(err.message);
      });

    return () => controller.abort();
  }, [deviceName, apiUrl]);

  const { devices, handleSetValueRequest, toggleDeviceLock } =
    useOphydPVSocket(pvName ? [pvName] : []);

  const device = pvName ? devices[pvName] : undefined;

  if (error) {
    return <div>Error: {error}</div>;
  }
  if (!pvName || !device) {
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
