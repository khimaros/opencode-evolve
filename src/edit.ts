// find-and-replace with optional replaceAll support
export function editContent(content: string, oldString: string, newString: string, replaceAll = false): string | { error: string } {
  const n = content.split(oldString).length - 1
  if (n === 0) return { error: 'oldString not found' }
  if (n > 1 && !replaceAll) return { error: `${n} matches for oldString, expected 1 (use replaceAll to replace all)` }
  return replaceAll ? content.replaceAll(oldString, newString) : content.replace(oldString, newString)
}
