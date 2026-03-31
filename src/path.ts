import path from 'node:path'
import { existsSync, readdirSync, statSync } from 'node:fs'

// resolve a filename within a workspace subdirectory, rejecting traversal
export function safePath(workspace: string, dir: string, filename: string): string {
  const base = path.join(workspace, dir)
  const resolved = path.resolve(base, filename)
  if (!resolved.startsWith(base + path.sep) && resolved !== base) {
    throw new Error(`invalid ${dir} path: ${filename}`)
  }
  return resolved
}

// require that a file already exists (no implicit creation)
export function existingPath(workspace: string, dir: string, filename: string): string {
  const resolved = safePath(workspace, dir, filename)
  if (!existsSync(resolved)) throw new Error(`not found: ${dir}/${filename}`)
  return resolved
}

// autodiscover all executable files in hooks/
export function discoverHookPaths(workspace: string): string[] {
  const hooksDir = path.join(workspace, 'hooks')
  try {
    return readdirSync(hooksDir)
      .filter(f => !f.startsWith('.') && !f.startsWith('__'))
      .map(f => path.join(hooksDir, f))
      .filter(p => { const s = statSync(p); return s.isFile() && (s.mode & 0o111) !== 0 })
      .sort()
  } catch {
    return []
  }
}
