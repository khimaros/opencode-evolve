// parse JSONL stdout: merge all lines into one result, collect {log} lines
export function parseHookOutput(stdout: string, logFn?: (msg: string) => void): any {
  const result: any = {}
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue
    const obj = JSON.parse(line)
    if (obj.log) { logFn?.(obj.log); continue }
    Object.assign(result, obj)
  }
  return result
}

// merge two hook results: arrays concatenate, scalars concatenate with newline
export function mergeResults(base: any, incoming: any): any {
  const merged = { ...base }
  for (const [key, val] of Object.entries(incoming)) {
    if (!(key in merged)) { merged[key] = val; continue }
    const prev = merged[key]
    if (Array.isArray(prev) && Array.isArray(val)) {
      merged[key] = [...prev, ...val]
    } else if (typeof prev === 'string' && typeof val === 'string') {
      merged[key] = prev + '\n' + val
    } else {
      merged[key] = val
    }
  }
  return merged
}
