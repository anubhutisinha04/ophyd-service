import { useState } from 'react'
import { NumberInput } from '../NumberInput'
import { SelectInput } from '../SelectInput'

// Dummy dropdown options — replace with values from presets_service later.
const GAIN_OPTIONS = ['Low', 'Med', 'High']
const DECADE_OPTIONS = ['1e3', '1e4', '1e5', '1e6', '1e7']

interface ScalarState {
  dwellTime: number
  pd: number
  pdGain: string
  pdDecade: string
  aumesh: number
  aumeshGain: string
  aumeshDecade: string
  sample: number
  sampleGain: string
  sampleDecade: string
}

interface VortexState {
  vortexTime: number
  pfyStart: number
  pfySize: number
  pfyCounts: number
  ipfyStart: number
  ipfySize: number
  ipfyCounts: number
}

// Dummy initial values — replace with presets_service data later.
const initialScalar: ScalarState = {
  dwellTime: 1051,
  pd: 1051,
  pdGain: '',
  pdDecade: '',
  aumesh: 1051,
  aumeshGain: '',
  aumeshDecade: '',
  sample: 1051,
  sampleGain: '',
  sampleDecade: '',
}

const initialVortex: VortexState = {
  vortexTime: 1051,
  pfyStart: 620,
  pfySize: 230,
  pfyCounts: 1051,
  ipfyStart: 450,
  ipfySize: 150,
  ipfyCounts: 0,
}

export function DetectorSettings() {
  const [scalar, setScalar] = useState<ScalarState>(initialScalar)
  const [vortex, setVortex] = useState<VortexState>(initialVortex)

  const patchScalar = (patch: Partial<ScalarState>) =>
    setScalar((prev) => ({ ...prev, ...patch }))
  const patchVortex = (patch: Partial<VortexState>) =>
    setVortex((prev) => ({ ...prev, ...patch }))

  const gainRow = 'flex items-center justify-between gap-2 py-[0.55rem] px-1 border-b border-[#e3e8ec]'
  const gainLabel = 'text-[0.9rem] text-brand-slate whitespace-nowrap'
  const gainControls = 'flex items-center gap-[0.35rem]'
  const gainX = 'text-[#6b7280] text-[0.85rem]'
  const rangeRow = 'flex items-center justify-between gap-2 py-[0.55rem] px-1 border-b border-[#e3e8ec]'
  const rangeLabel = 'text-[0.9rem] font-semibold text-brand-slate whitespace-nowrap'
  const rangeControls = 'flex items-center gap-[0.3rem] [&>div]:p-0 [&>div]:gap-1 [&>div]:border-b-0 [&_input]:w-[58px]'
  const rangeSub = 'text-[0.8rem] text-[#6b7280]'
  const card = 'bg-white border border-panel-border rounded-lg overflow-hidden'
  const cardHeader = 'bg-brand-teal text-white px-3 py-[0.45rem] text-[0.85rem] font-bold'
  const cardBody = 'flex flex-col px-3 pt-2 pb-3'

  return (
    <section className="detector-settings flex-[1.5_1_0] min-w-0 max-xl:w-full min-h-[32rem] flex flex-col bg-white border border-panel-border rounded-xl overflow-hidden shadow-[0_1px_3px_rgba(16,92,120,0.08)]">
      <div className="bg-brand-teal text-white text-center px-4 py-[0.7rem] text-base font-bold tracking-[0.02em]">Detector Settings</div>
      <div className="grid grid-cols-[2fr_1fr] gap-3 flex-1 px-4 pt-3 pb-4 max-lg:grid-cols-1">
        {/* ── Scalar Settings ─────────────────────────────────── */}
        <div className={card}>
          <div className={cardHeader}>Scalar Settings</div>
          <div className={cardBody}>
            <NumberInput
              label="Dwell Time"
              value={scalar.dwellTime}
              onChange={(v) => patchScalar({ dwellTime: v })}
            />
            <NumberInput
              label="pd"
              value={scalar.pd}
              onChange={(v) => patchScalar({ pd: v })}
            />
            <div className={gainRow}>
              <span className={gainLabel}>pd gain</span>
              <div className={gainControls}>
                <SelectInput
                  value={scalar.pdGain}
                  options={GAIN_OPTIONS}
                  onChange={(v) => patchScalar({ pdGain: v })}
                />
                <span className={gainX}>×</span>
                <SelectInput
                  value={scalar.pdDecade}
                  options={DECADE_OPTIONS}
                  onChange={(v) => patchScalar({ pdDecade: v })}
                />
              </div>
            </div>
            <NumberInput
              label="aumesh"
              value={scalar.aumesh}
              onChange={(v) => patchScalar({ aumesh: v })}
            />
            <div className={gainRow}>
              <span className={gainLabel}>aumesh gain</span>
              <div className={gainControls}>
                <SelectInput
                  value={scalar.aumeshGain}
                  options={GAIN_OPTIONS}
                  onChange={(v) => patchScalar({ aumeshGain: v })}
                />
                <span className={gainX}>×</span>
                <SelectInput
                  value={scalar.aumeshDecade}
                  options={DECADE_OPTIONS}
                  onChange={(v) => patchScalar({ aumeshDecade: v })}
                />
              </div>
            </div>
            <NumberInput
              label="sample"
              value={scalar.sample}
              onChange={(v) => patchScalar({ sample: v })}
            />
            <div className={gainRow}>
              <span className={gainLabel}>sample gain</span>
              <div className={gainControls}>
                <SelectInput
                  value={scalar.sampleGain}
                  options={GAIN_OPTIONS}
                  onChange={(v) => patchScalar({ sampleGain: v })}
                />
                <span className={gainX}>×</span>
                <SelectInput
                  value={scalar.sampleDecade}
                  options={DECADE_OPTIONS}
                  onChange={(v) => patchScalar({ sampleDecade: v })}
                />
              </div>
            </div>
          </div>
        </div>

        {/* ── Vortex Settings ─────────────────────────────────── */}
        <div className={card}>
          <div className={cardHeader}>Vortex Settings</div>
          <div className={cardBody}>
            <NumberInput
              label="vortex time"
              value={vortex.vortexTime}
              onChange={(v) => patchVortex({ vortexTime: v })}
            />
            <div className={rangeRow}>
              <span className={rangeLabel}>PFY</span>
              <div className={rangeControls}>
                <span className={rangeSub}>start</span>
                <NumberInput
                  label=""
                  value={vortex.pfyStart}
                  onChange={(v) => patchVortex({ pfyStart: v })}
                />
                <span className={rangeSub}>size</span>
                <NumberInput
                  label=""
                  value={vortex.pfySize}
                  onChange={(v) => patchVortex({ pfySize: v })}
                />
              </div>
            </div>
            <NumberInput
              label="PFY counts"
              value={vortex.pfyCounts}
              onChange={(v) => patchVortex({ pfyCounts: v })}
            />
            <div className={rangeRow}>
              <span className={rangeLabel}>IPFY</span>
              <div className={rangeControls}>
                <span className={rangeSub}>start</span>
                <NumberInput
                  label=""
                  value={vortex.ipfyStart}
                  onChange={(v) => patchVortex({ ipfyStart: v })}
                />
                <span className={rangeSub}>size</span>
                <NumberInput
                  label=""
                  value={vortex.ipfySize}
                  onChange={(v) => patchVortex({ ipfySize: v })}
                />
              </div>
            </div>
            <NumberInput
              label="IPFY counts"
              value={vortex.ipfyCounts}
              onChange={(v) => patchVortex({ ipfyCounts: v })}
            />
            <button
              className="mt-3 px-4 py-[0.55rem] bg-brand-cyan text-white rounded-md text-[0.9rem] font-semibold cursor-pointer transition-colors hover:bg-brand-teal"
              type="button"
            >
              Counter
            </button>
          </div>
        </div>
      </div>
    </section>
  )
}
