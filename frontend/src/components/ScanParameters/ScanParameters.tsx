import { useState } from 'react'
import type { ScanPresetEntry } from '../../api/presets'
import { NumberInput } from '../NumberInput'

export interface ScanParametersProps {
  data: Omit<ScanPresetEntry, 'edge_index'>
  onChange: (updated: Partial<Omit<ScanPresetEntry, 'edge_index'>>) => void
}

export function ScanParameters({ data, onChange }: ScanParametersProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const field = (key: keyof Omit<ScanPresetEntry, 'edge_index'>, label: string) => (
    <NumberInput
      key={key}
      label={label}
      value={data[key] as number}
      onChange={(v) => onChange({ [key]: v })}
    />
  )

  return (
    <section className="scan-parameters flex-[1_1_0] min-w-0 max-xl:w-full min-h-[31rem] flex flex-col bg-white border border-panel-border rounded-xl overflow-hidden shadow-[0_1px_3px_rgba(16,92,120,0.08)]">
      <div className="bg-brand-teal text-white text-center px-4 py-[0.7rem] text-base font-bold tracking-[0.02em]">Scan Parameters</div>
      <div className="flex flex-col flex-1 px-4 pt-3 pb-4">
        <div className="flex flex-col max-h-[14.5rem] overflow-y-auto pr-[0.4rem] [&>div:last-child]:border-b-0 [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:bg-[#c2ccd2] [&::-webkit-scrollbar-thumb]:rounded [&::-webkit-scrollbar-thumb:hover]:bg-brand-cyan">
          {field('start', 'start')}
          {field('stop', 'stop')}
          {field('velocity', 'velocity')}
          {field('deadband', 'deadband')}
          {field('epu1offset', 'epu1offset')}
          {field('scan_count', 'scan count')}
          {field('e_align', 'e align')}
          {field('m1b1_sp', 'm1b1 sp')}
        </div>

        <button
          className="flex items-center justify-between w-full mt-[0.85rem] px-4 py-[0.6rem] bg-brand-teal text-white rounded-md text-[0.9rem] font-semibold cursor-pointer transition-colors hover:bg-brand-cyan"
          onClick={() => setAdvancedOpen(!advancedOpen)}
        >
          Advanced Settings
          <span className="text-[0.7rem] ml-2">{advancedOpen ? '▲' : '▼'}</span>
        </button>

        {advancedOpen && (
          <div className="flex flex-col pt-2">
            {field('epu_table', 'epu table')}
            {field('intervals', 'intervals')}
            {field('au_mesh', 'au mesh')}
          </div>
        )}
      </div>
    </section>
  )
}
