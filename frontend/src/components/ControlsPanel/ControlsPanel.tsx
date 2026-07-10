interface ControlsPanelProps {
  onPdScan?: () => void
  onSingleScan?: () => void
  onAddToQueue?: () => void
  onStop?: () => void
}

export function ControlsPanel({
  onPdScan,
  onSingleScan,
  onAddToQueue,
  onStop,
}: ControlsPanelProps) {
  return (
    <section 
      className="flex-[0_1_18rem] min-w-[14rem] max-lg:flex-none max-lg:w-full max-lg:min-w-0 min-h-[333px] bg-white border border-panel-border rounded-xl overflow-hidden shadow-[0_1px_3px_rgba(16,92,120,0.08)]"
      aria-label="Controls"
    >
      <div className="bg-brand-teal text-white text-center px-4 py-[0.66rem] text-base font-bold tracking-[0.02em]">
        Controls
      </div>
      <div className="flex flex-col flex-1 items-center justify-around gap-4 p-[0.9rem_0.8rem] max-md:p-[0.8rem_0.7rem] max-md:gap-2">
        <button
          className="w-full px-4 py-[0.65rem] border-none rounded-md text-white text-base font-semibold leading-tight cursor-pointer transition-all active:scale-[0.98] bg-brand-cyan hover:bg-[#009dc8]"
          type="button"
          onClick={onPdScan}
        >
          PD Scan
        </button>
        <button
          className="w-full px-4 py-[0.65rem] border-none rounded-md text-white text-base font-semibold leading-tight cursor-pointer transition-all active:scale-[0.98] bg-brand-cyan hover:bg-[#009dc8]"
          type="button"
          onClick={onSingleScan}
        >
          Single Scan
        </button>
        <button
          className="w-full px-4 py-[0.65rem] border-none rounded-md text-white text-base font-semibold leading-tight cursor-pointer transition-all active:scale-[0.98] mt-2 mb-2 bg-brand-teal hover:bg-[#0e5068]"
          type="button"
          onClick={onAddToQueue}
        >
          Add to Queue
        </button>
        <button
          className="w-full px-4 py-[0.65rem] border-none rounded-md text-white text-base font-semibold leading-tight cursor-pointer transition-all active:scale-[0.98] bg-brand-red hover:bg-[#cc0000]"
          type="button"
          onClick={onStop}
        >
          Stop
        </button>
      </div>
    </section>
  )
}