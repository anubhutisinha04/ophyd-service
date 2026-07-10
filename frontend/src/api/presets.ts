/**
 * API hooks for the Presets Service.
 *
 * All requests go through the Vite proxy:
 *   /api/presets/* → http://localhost:8005/api/v1/*
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const BASE = '/api/presets'

// ── Types ─────────────────────────────────────────────────────────


export interface ScanPresetEntry {
  edge_index: string
  start: number
  stop: number
  velocity: number
  deadband: number
  epu1offset: number
  epu_table: number
  scan_count: number
  intervals: number
  au_mesh: number
  e_align: number
  m1b1_sp: number
}

export interface DetectorPresetEntry {
  edge_index: string
  samplegain: string
  sampledecade: string
  aumeshgain: string
  aumeshdecade: string
  pd_gain: string
  pd_decade: string
  vortex_low: number
  vortex_high: number
  ipfy_low: number
  ipfy_high: number
  vortex_pos: number
  vortex_time: number
  sclr_time: number
}

export interface EdgeFullPreset {
  edge_index: string
  scan: Omit<ScanPresetEntry, 'edge_index'> | null
  detector: Omit<DetectorPresetEntry, 'edge_index'> | null
}

// ── Fetchers ──────────────────────────────────────────────────────

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

async function writeJson<T>(
  path: string,
  method: 'POST' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (data?.detail) detail = `${res.status}: ${data.detail}`
    } catch {
      /* response had no JSON body */
    }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ── Query hooks ───────────────────────────────────────────────────


export function useScanPresets() {
  return useQuery({
    queryKey: ['presets', 'scan-presets'],
    queryFn: () => fetchJson<ScanPresetEntry[]>('/scan-presets'),
  })
}

export function useDetectorPresets() {
  return useQuery({
    queryKey: ['presets', 'detector-presets'],
    queryFn: () => fetchJson<DetectorPresetEntry[]>('/detector-presets'),
  })
}

export function useScanPreset(edgeIndex: string) {
  return useQuery({
    queryKey: ['presets', 'scan-presets', edgeIndex],
    queryFn: () => fetchJson<ScanPresetEntry>(`/scan-presets/${edgeIndex}`),
    enabled: !!edgeIndex,
  })
}

export function useDetectorPreset(edgeIndex: string) {
  return useQuery({
    queryKey: ['presets', 'detector-presets', edgeIndex],
    queryFn: () => fetchJson<DetectorPresetEntry>(`/detector-presets/${edgeIndex}`),
    enabled: !!edgeIndex,
  })
}

export function useFullPreset(edgeIndex: string) {
  return useQuery({
    queryKey: ['presets', 'edges', edgeIndex, 'full'],
    queryFn: () => fetchJson<EdgeFullPreset>(`/edges/${edgeIndex}/full`),
    enabled: !!edgeIndex,
  })
}

// ── Mutation hooks ────────────────────────────────────────────────

type TableSlug = 'scan-presets' | 'detector-presets'

function useTableMutations<T extends { edge_index: string }>(table: TableSlug) {
  const queryClient = useQueryClient()
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['presets'] })

  const create = useMutation({
    mutationFn: (entry: T) => writeJson<T>(`/${table}`, 'POST', entry),
    onSuccess: invalidate,
  })

  const update = useMutation({
    mutationFn: ({ edgeIndex, patch }: { edgeIndex: string; patch: Partial<Omit<T, 'edge_index'>> }) =>
      writeJson<T>(`/${table}/${edgeIndex}`, 'PUT', patch),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: (edgeIndex: string) => writeJson<void>(`/${table}/${edgeIndex}`, 'DELETE'),
    onSuccess: invalidate,
  })

  return { create, update, remove }
}

export function useScanPresetMutations() {
  return useTableMutations<ScanPresetEntry>('scan-presets')
}

export function useDetectorPresetMutations() {
  return useTableMutations<DetectorPresetEntry>('detector-presets')
}
