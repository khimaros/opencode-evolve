// extract permission patterns from a tool definition and its runtime args
export function permissionPatterns(def: any, args: Record<string, any>): string[] {
  const spec = def.permission
  if (!spec?.arg) return ['*']
  const argNames = Array.isArray(spec.arg) ? spec.arg : [spec.arg]
  const patterns = argNames.map((name: string) => args[name]).filter((v: any) => typeof v === 'string' && v)
  return patterns.length ? patterns : ['*']
}
