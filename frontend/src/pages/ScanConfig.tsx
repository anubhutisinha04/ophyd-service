import { useState } from 'react'
import type { ElementData } from '../components/ElementPicker'
import { useFullPreset, type EdgeFullPreset, type ScanPresetEntry } from '../api/presets'
import { getEdgesForElement } from '../api/edgeMapping'
import { ScanParameters } from '../components/ScanParameters'
import { DetectorSettings } from '../components/DetectorSettings'
import { ControlsPanel } from '../components/ControlsPanel'

interface ScanConfigProps {
  element: ElementData
  onBack: () => void
}

export default function ScanConfig({ element, onBack }: ScanConfigProps) {
  const edges = getEdgesForElement(element.symbol)
  const [selectedEdge, setSelectedEdge] = useState(edges[0] ?? '')
  const { data, isLoading, isError, error } = useFullPreset(selectedEdge)

  return (
    <div className="w-full min-h-screen bg-white">
      <div className="flex flex-col gap-5 p-6 lg:p-5 w-full max-w-app mx-auto box-border">
        <header className="flex flex-col gap-2">
        <button className="self-start bg-transparent border-none text-brand-teal text-[0.875rem] cursor-pointer p-0 hover:text-brand-cyan" onClick={onBack}>
          ← Back to periodic table
        </button>
        <h1 className="flex items-center gap-[0.625rem] text-[1.5rem] font-bold text-gray-800 m-0">
          <span
            className="inline-flex items-center justify-center w-10 h-10 rounded-md text-xl font-extrabold text-[#1a1a2e]"
            style={{ backgroundColor: `var(--ep-${element.category})` }}
          >
            {element.symbol}
          </span>
          {element.name} — Scan Configuration
        </h1>
      </header>

      {edges.length === 0 && (
        <div className="text-[0.95rem] text-gray-600 bg-gray-100 border border-dashed border-gray-300 rounded-lg p-8 text-center">
          No presets configured for {element.name}. Contact beamline staff to add edge presets.
        </div>
      )}

      {edges.length > 1 && (
        <div className="flex gap-2">
          {edges.map((edge) => (
            <button
              key={edge}
              className={`px-4 py-[0.375rem] border rounded-md text-[0.875rem] font-semibold cursor-pointer transition-all ${edge === selectedEdge ? 'bg-brand-teal border-brand-teal text-white' : 'bg-white border-gray-300 text-gray-700 hover:bg-gray-50'}`}
              onClick={() => setSelectedEdge(edge)}
            >
              {edge}
            </button>
          ))}
        </div>
      )}

      {edges.length === 1 && (
        <div className="text-[0.875rem] text-gray-700 font-semibold">Edge: {selectedEdge}</div>
      )}

      {isLoading && <div className="text-[0.875rem] text-gray-600">Loading presets…</div>}
      {isError && (
        <div className="text-[0.875rem] text-red-600">
          Failed to load presets: {(error as Error).message}
        </div>
      )}

        {data && <PresetPanels data={data} />}
      </div>
    </div>
  )
}

function PresetPanels({ data }: { data: EdgeFullPreset }) {
  const [scanData, setScanData] = useState<Omit<ScanPresetEntry, 'edge_index'> | null>(data.scan)

  return (
    <div className="flex flex-col gap-6 lg:gap-[1.1rem] w-full bg-white">
      <div className="flex gap-6 items-start lg:gap-4 max-xl:flex-col max-xl:w-full">
        {/* Scan Presets — interactive component */}
        {scanData ? (
          <ScanParameters
            data={scanData}
            onChange={(patch) => setScanData((prev) => prev ? { ...prev, ...patch } : prev)}
          />
        ) : (
          <section className="flex-[1_1_0] min-w-0 max-xl:w-full bg-gray-100 border border-gray-300 rounded-lg p-4 px-5">
            <h2 className="text-base font-bold text-gray-800 m-0 mb-3 pb-2 border-b border-gray-300">Scan Parameters</h2>
            <p className="text-gray-600 text-[0.85rem] italic m-0">Not configured</p>
          </section>
        )}

        {/* Detector Presets — interactive component */}
        <DetectorSettings />
      </div>

      <div className="flex gap-[0.35rem] items-start max-lg:flex-col max-lg:gap-4 max-xl:gap-4">
        <section className="flex-1 min-w-0 min-h-[20.125rem] max-xl:min-h-0 max-xl:w-full bg-white border border-panel-border rounded-xl overflow-hidden shadow-[0_1px_3px_rgba(16,92,120,0.08)]" aria-label="Live Spectrum">
          <div className="bg-brand-teal text-white text-center px-4 py-[0.66rem] text-base font-bold tracking-[0.02em]">Live Spectrum (Vortex MCA)</div>
          <div className="px-4 pt-3 pb-4">
            <div className="h-[16.25rem] max-xl:h-48 rounded-md border border-[#d9e0e5] bg-[linear-gradient(0deg,#f6f8fa_0,#f6f8fa_1px,transparent_1px,transparent_2rem),linear-gradient(90deg,#f6f8fa_0,#f6f8fa_1px,transparent_1px,transparent_2rem),linear-gradient(180deg,#ffffff,#fbfdff)]" />
          </div>
        </section>
        <ControlsPanel />
      </div>
    </div>
  )
}
