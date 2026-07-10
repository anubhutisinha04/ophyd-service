import { elements, type ElementData } from './elements'

// CSS variables for element category colors (preserved for use with Tailwind arbitrary values)
const elementCategoryVars = `
  [--ep-alkali-metal:#a5d6a7]
  [--ep-alkaline-earth:#c5e1a5]
  [--ep-transition-metal:#fff59d]
  [--ep-post-transition:#80cbc4]
  [--ep-metalloid:#80deea]
  [--ep-nonmetal:#90caf9]
  [--ep-halogen:#81d4fa]
  [--ep-noble-gas:#f48fb1]
  [--ep-lanthanide:#ce93d8]
  [--ep-actinide:#ef9a9a]
  [--ep-unknown:#e0e0e0]
`

export interface ElementPickerProps {
  onSelect?: (element: ElementData) => void
  selectedSymbol?: string
  /** Predicate returning true for symbols that should be visually highlighted (e.g. have presets) */
  highlightSymbols?: (symbol: string) => boolean
  /** Additional CSS classes applied to the outer container */
  className?: string
}

export function ElementPicker({ onSelect, selectedSymbol, highlightSymbols, className }: ElementPickerProps) {
  return (
    <div className={`w-full min-h-full bg-[#F2FAFD] flex flex-col items-center justify-start py-[clamp(12px,2vw,28px)] px-[clamp(16px,3vw,40px)] box-border ${elementCategoryVars} ${className || ''}`}>
      <div className="w-full max-w-[min(1600px,96vw)] mx-auto flex flex-col items-center">
        <div className="flex items-center justify-center gap-[clamp(6px,0.8vw,12px)] mb-[clamp(12px,2vw,24px)]">
          <h1 className="m-0 text-[clamp(1.4rem,2.6vw,2.1rem)] font-bold text-[#0b3a4d] text-center tracking-[0.01em]">Pick an Element</h1>
          <span className="relative inline-flex items-center">
            <button
              type="button"
              className="inline-flex items-center justify-center w-[clamp(18px,1.6vw,24px)] h-[clamp(18px,1.6vw,24px)] p-0 border-none rounded-full bg-[#0b3a4d] text-white font-[Georgia,'Times_New_Roman',serif] italic font-bold text-[clamp(0.7rem,1vw,0.9rem)] leading-none cursor-help transition-colors hover:bg-[#1976d2] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#1976d2] focus-visible:outline-offset-2"
              aria-label="How to use this page"
              aria-describedby="element-picker-help-tip"
            >
              i
            </button>
            <span
              id="element-picker-help-tip"
              role="tooltip"
              className="absolute top-[calc(100%+10px)] left-1/2 -translate-x-1/2 z-10 w-max max-w-[min(280px,80vw)] px-3 py-2 rounded-lg bg-[#0b3a4d] text-[#f2fafd] text-[clamp(0.72rem,0.9vw,0.9rem)] font-medium leading-[1.35] text-left shadow-[0_4px_14px_rgba(0,0,0,0.22)] opacity-0 invisible transition-all peer-hover:opacity-100 peer-hover:visible peer-focus-visible:opacity-100 peer-focus-visible:visible before:content-[''] before:absolute before:bottom-full before:left-1/2 before:-translate-x-1/2 before:border-[6px] before:border-transparent before:border-b-[#0b3a4d]"
            >
              Choose an element to set up a scan. Colored elements have ready-made
              presets; greyed-out elements aren&rsquo;t available yet.
            </span>
          </span>
        </div>
        <div className="grid grid-cols-[repeat(18,minmax(0,1fr))] grid-rows-[repeat(7,auto)_clamp(8px,1vw,14px)_repeat(2,auto)] gap-[clamp(2px,0.35vw,5px)] w-full">
          {elements.map((el) => {
            const isSelected = selectedSymbol === el.symbol
            const hasData = highlightSymbols?.(el.symbol) ?? true
            const isDisabled = highlightSymbols !== undefined && !hasData
            
            const categoryColors: Record<string, string> = {
              'alkali-metal': 'bg-[var(--ep-alkali-metal)]',
              'alkaline-earth': 'bg-[var(--ep-alkaline-earth)]',
              'transition-metal': 'bg-[var(--ep-transition-metal)]',
              'post-transition': 'bg-[var(--ep-post-transition)]',
              'metalloid': 'bg-[var(--ep-metalloid)]',
              'nonmetal': 'bg-[var(--ep-nonmetal)]',
              'halogen': 'bg-[var(--ep-halogen)]',
              'noble-gas': 'bg-[var(--ep-noble-gas)]',
              'lanthanide': 'bg-[var(--ep-lanthanide)]',
              'actinide': 'bg-[var(--ep-actinide)]',
              'unknown': 'bg-[var(--ep-unknown)]',
            }

            return (
              <button
                key={el.number}
                className={`relative flex flex-col items-center justify-center aspect-square min-h-0 p-0 border-none rounded-[clamp(3px,0.4vw,6px)] cursor-pointer overflow-hidden transition-all box-border ${categoryColors[el.category] || 'bg-gray-200'} ${isSelected ? 'shadow-[0_0_0_2.5px_#1976d2] brightness-[0.88]' : ''} ${isDisabled ? 'opacity-35 grayscale-[60%] cursor-not-allowed pointer-events-none' : 'hover:brightness-[0.92] hover:shadow-[0_0_0_2px_rgba(0,0,0,0.25)] hover:-translate-y-[1px]'} focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#1976d2] focus-visible:outline-offset-1 disabled:pointer-events-none max-[900px]:[&>span:nth-child(3)]:hidden max-[900px]:[&>span:nth-child(4)]:hidden`}
                style={{ gridRow: el.row, gridColumn: el.col }}
                onClick={() => onSelect?.(el)}
                disabled={isDisabled}
                aria-label={`${el.name} (${el.symbol})`}
              >
                <span className="absolute top-[4%] left-[8%] text-[clamp(0.4rem,0.7vw,0.95rem)] font-medium leading-none text-black/70">{el.number}</span>
                <span className="text-[clamp(0.8rem,1.6vw,2rem)] font-bold leading-[1.1] text-[#111]">{el.symbol}</span>
                <span className="text-[clamp(0.4rem,0.7vw,0.85rem)] font-medium leading-[1.2] text-[#222] max-w-full overflow-hidden text-ellipsis whitespace-nowrap">{el.name}</span>
                <span className="text-[clamp(0.38rem,0.6vw,0.8rem)] leading-[1.2] text-black/65">{el.mass}</span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
