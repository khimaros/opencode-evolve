import { readFileSync } from 'node:fs'
import path from 'node:path'

// --- types ---

export interface EvolveConfig {
  heartbeat_ms: number
  hook_timeout: number
  heartbeat_title: string
  heartbeat_agent: string
  model: { providerID: string; modelID: string } | null
  heartbeat_cleanup: 'none' | 'new' | 'archive' | 'compact'
  heartbeat_cleanup_count: number | null
  heartbeat_cleanup_tokens: number | null
  heartbeat_skip_active: boolean
}

export const DEFAULTS: EvolveConfig = {
  heartbeat_ms: 120 * 60 * 1000,
  hook_timeout: 30_000,
  heartbeat_title: 'heartbeat',
  heartbeat_agent: 'evolve',
  model: null,
  heartbeat_cleanup: 'none',
  heartbeat_cleanup_count: null,
  heartbeat_cleanup_tokens: null,
  heartbeat_skip_active: true,
}

// --- per-hook registration data returned by discover ---

export interface HookRegistration {
  path: string
  name: string
  test: string | null
}

// --- pure helpers ---

// strip jsonc comments (// and /* */) while preserving strings
export function stripJsoncComments(raw: string): string {
  return raw.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '')
}

// coerce env string to match the type of the existing config value
export function coerceEnv(val: string, existing: any): any {
  if (existing === null || typeof existing === 'string') return val === 'null' ? null : val
  if (typeof existing === 'number') return val === 'null' ? null : Number(val)
  if (typeof existing === 'boolean') return val !== 'false' && val !== '0' && val !== ''
  return val
}

// apply EVOLVE_<FIELD> env vars to config (e.g. EVOLVE_HEARTBEAT_MS -> heartbeat_ms)
export function applyEnvOverrides(config: any) {
  for (const field of Object.keys(DEFAULTS)) {
    const val = process.env[`EVOLVE_${field.toUpperCase()}`]
    if (val !== undefined) config[field] = coerceEnv(val, DEFAULTS[field as keyof EvolveConfig])
  }
}

export function normalizeModel(config: any) {
  if (typeof config.model === 'string' && config.model.includes('/')) {
    const [providerID, ...rest] = config.model.split('/')
    config.model = { providerID, modelID: rest.join('/') }
  }
}

export function loadConfig(workspace: string): EvolveConfig {
  const configPath = path.join(workspace, 'config', 'evolve.jsonc')
  let parsed: any
  try {
    const raw = readFileSync(configPath, 'utf-8')
    const json = stripJsoncComments(raw)
    parsed = { ...DEFAULTS, ...JSON.parse(json) }
  } catch {
    parsed = { ...DEFAULTS }
  }
  applyEnvOverrides(parsed)
  normalizeModel(parsed)
  return parsed
}
