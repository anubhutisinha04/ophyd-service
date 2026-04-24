import { useState, useEffect } from 'react';

const API_BASE = 'http://localhost:8004/api/v1';

const DEVICE_LABELS = ['motor', 'detector', 'signal', 'flyer', 'readable', 'device'];

const btnStyle: React.CSSProperties = {
  backgroundColor: '#105C78',
  color: '#fff',
  border: 'none',
  padding: '8px 16px',
  borderRadius: '4px',
  cursor: 'pointer',
  fontSize: '0.9rem',
  fontWeight: 600,
};

function DeviceBrowser() {
  const [selectedLabel, setSelectedLabel] = useState<string>('');
  const [devices, setDevices] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [pvs, setPvs] = useState<string[]>([]);
  const [pvCount, setPvCount] = useState<number>(0);
  const [pvLoading, setPvLoading] = useState(false);
  const [showPvs, setShowPvs] = useState(false);

  useEffect(() => {
    if (!selectedLabel) {
      setDevices([]);
      return;
    }
    setLoading(true);
    fetch(`${API_BASE}/devices?device_label=${encodeURIComponent(selectedLabel)}`, {
      headers: { accept: 'application/json' },
    })
      .then((res) => res.json())
      .then((data: string[]) => setDevices(data))
      .catch((err) => console.error('Failed to fetch devices:', err))
      .finally(() => setLoading(false));
  }, [selectedLabel]);

  const handleShowPvs = () => {
    if (showPvs) {
      setShowPvs(false);
      return;
    }
    setPvLoading(true);
    fetch(`${API_BASE}/pvs`, { headers: { accept: 'application/json' } })
      .then((res) => res.json())
      .then((data: { success: boolean; pvs: string[]; count: number }) => {
        setPvs(data.pvs.filter((pv) => pv !== ''));
        setPvCount(data.count);
        setShowPvs(true);
      })
      .catch((err) => console.error('Failed to fetch PVs:', err))
      .finally(() => setPvLoading(false));
  };

  return (
    <div>
      <h2>Device Browser</h2>

      <div style={{ display: 'flex', justifyContent: 'center', margin: '1rem 0' }}>
        <button
          onClick={handleShowPvs}
          disabled={pvLoading}
          style={{
            ...btnStyle,
            backgroundColor: pvLoading ? '#858889' : showPvs ? '#B72467' : '#105C78',
          }}
        >
          {pvLoading ? 'Loading...' : showPvs ? 'Hide PVs' : 'Show PVs'}
        </button>
      </div>

      <div style={{ display: 'flex', gap: '2rem' }}>
        {/* Left section — Device dropdown & list */}
        <div style={{ flex: 1, borderRight: '2px solid #00ADDC', paddingRight: '2rem' }}>
          <label htmlFor="device-label-select" style={{ color: '#105C78', fontWeight: 600 }}>Device: </label>
          <select
            id="device-label-select"
            value={selectedLabel}
            onChange={(e) => setSelectedLabel(e.target.value)}
            style={{
              backgroundColor: '#105C78',
              color: '#fff',
              border: 'none',
              padding: '8px 16px',
              borderRadius: '4px',
              cursor: 'pointer',
              fontSize: '0.9rem',
              fontWeight: 600,
            }}
          >
            <option value="">-- Select a device --</option>
            {DEVICE_LABELS.map((label) => (
              <option key={label} value={label}>
                {label}
              </option>
            ))}
          </select>

          {loading && <p>Loading...</p>}

          {!loading && devices.length > 0 && (
            <ul style={{ marginTop: '1rem' }}>
              {devices.map((device) => (
                <li key={device}>{device}</li>
              ))}
            </ul>
          )}

          {!loading && selectedLabel && devices.length === 0 && (
            <p style={{ marginTop: '1rem' }}>No devices found for "{selectedLabel}".</p>
          )}
        </div>

        {/* Right section — PV table */}
        <div style={{ flex: 1, paddingLeft: '2rem' }}>
          {showPvs ? (
            <>
              <h3 style={{ color: '#105C78' }}>PVs ({pvCount})</h3>
              <div style={{ maxHeight: '400px', overflowY: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={{ textAlign: 'left', padding: '8px', borderBottom: '2px solid #00ADDC', position: 'sticky', top: 0, background: '#105C78', color: '#fff' }}>#</th>
                      <th style={{ textAlign: 'left', padding: '8px', borderBottom: '2px solid #00ADDC', position: 'sticky', top: 0, background: '#105C78', color: '#fff' }}>PV Name</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pvs.map((pv, index) => (
                      <tr key={pv} style={{ borderBottom: '1px solid #eee' }}>
                        <td style={{ padding: '6px 8px' }}>{index + 1}</td>
                        <td style={{ padding: '6px 8px', fontFamily: 'monospace', fontSize: '0.9rem' }}>{pv}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p style={{ color: '#858889' }}>Click "Show PVs" to view all process variables.</p>
          )}
        </div>
      </div>
    </div>
  );
}

export default DeviceBrowser;
