import { useState } from 'react'
import { LoginGate } from '../components/LoginGate'
import { EditableTable, ColumnDef } from '../components/EditableTable'
import {
  useScanPresets,
  useDetectorPresets,
  useScanPresetMutations,
  useDetectorPresetMutations,
} from '../api/presets'

const SCAN_COLUMNS: ColumnDef[] = [
  { key: 'edge_index', label: 'Edge Index', type: 'text' },
  { key: 'start', label: 'Start', type: 'number' },
  { key: 'stop', label: 'Stop', type: 'number' },
  { key: 'velocity', label: 'Velocity', type: 'number' },
  { key: 'deadband', label: 'Deadband', type: 'number' },
  { key: 'epu1offset', label: 'EPU1 Offset', type: 'number' },
  { key: 'epu_table', label: 'EPU Table', type: 'number' },
  { key: 'scan_count', label: 'Scan Count', type: 'number' },
  { key: 'intervals', label: 'Intervals', type: 'number' },
  { key: 'au_mesh', label: 'Au Mesh', type: 'number' },
  { key: 'e_align', label: 'E Align', type: 'number' },
  { key: 'm1b1_sp', label: 'M1B1 SP', type: 'number' },
]

const DETECTOR_COLUMNS: ColumnDef[] = [
  { key: 'edge_index', label: 'Edge Index', type: 'text' },
  { key: 'samplegain', label: 'Sample Gain', type: 'text' },
  { key: 'sampledecade', label: 'Sample Decade', type: 'text' },
  { key: 'aumeshgain', label: 'Au Mesh Gain', type: 'text' },
  { key: 'aumeshdecade', label: 'Au Mesh Decade', type: 'text' },
  { key: 'pd_gain', label: 'PD Gain', type: 'text' },
  { key: 'pd_decade', label: 'PD Decade', type: 'text' },
  { key: 'vortex_low', label: 'Vortex Low', type: 'number' },
  { key: 'vortex_high', label: 'Vortex High', type: 'number' },
  { key: 'ipfy_low', label: 'IPFY Low', type: 'number' },
  { key: 'ipfy_high', label: 'IPFY High', type: 'number' },
  { key: 'vortex_pos', label: 'Vortex Pos', type: 'number' },
  { key: 'vortex_time', label: 'Vortex Time', type: 'number' },
  { key: 'sclr_time', label: 'Sclr Time', type: 'number' },
]

function PresetsTables() {
  const scan = useScanPresets()
  const detector = useDetectorPresets()

  const scanMut = useScanPresetMutations()
  const detectorMut = useDetectorPresetMutations()

  return (
    <div className="flex flex-col gap-6 w-full min-w-0 bg-white">
      <EditableTable
        title="Scan Presets"
        columns={SCAN_COLUMNS}
        rows={scan.data}
        isLoading={scan.isLoading}
        loadError={scan.error as Error | null}
        onCreate={(entry) => scanMut.create.mutateAsync(entry)}
        onUpdate={(edgeIndex, patch) =>
          scanMut.update.mutateAsync({ edgeIndex, patch })
        }
        onDelete={(edgeIndex) => scanMut.remove.mutateAsync(edgeIndex)}
      />

      <EditableTable
        title="Detector Presets"
        columns={DETECTOR_COLUMNS}
        rows={detector.data}
        isLoading={detector.isLoading}
        loadError={detector.error as Error | null}
        onCreate={(entry) => detectorMut.create.mutateAsync(entry)}
        onUpdate={(edgeIndex, patch) =>
          detectorMut.update.mutateAsync({ edgeIndex, patch })
        }
        onDelete={(edgeIndex) => detectorMut.remove.mutateAsync(edgeIndex)}
      />
    </div>
  )
}

export default function PresetsAdmin() {
  const [authed, setAuthed] = useState(false)

  if (!authed) {
    return <LoginGate onAuth={() => setAuthed(true)} />
  }

  return (
    <div className="w-full min-h-screen bg-white">
      <div className="p-6 w-full max-w-app mx-auto box-border">
        <div className="flex items-center justify-between mb-5">
        <h1 className="m-0 text-brand-teal text-[1.4rem] font-bold">
          Presets Admin
        </h1>
        <button
          type="button"
          className="px-[0.9rem] py-[0.45rem] bg-white text-brand-teal border border-[#9fc8d8] rounded-lg text-[0.85rem] font-semibold cursor-pointer transition-all hover:bg-brand-teal hover:text-white"
          onClick={() => setAuthed(false)}
        >
          Sign Out
        </button>
        </div>
        <PresetsTables />
      </div>
    </div>
  )
}
