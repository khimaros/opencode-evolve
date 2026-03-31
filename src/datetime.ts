export function formatDatetime(date: Date, timezone = 'UTC'): string {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    fractionalSecondDigits: 3, hour12: false, timeZoneName: 'longOffset',
  })
  const p = Object.fromEntries(fmt.formatToParts(date).map(v => [v.type, v.value]))
  const offset = p.timeZoneName === 'GMT' ? '+00:00' : p.timeZoneName.replace('GMT', '')
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}.${p.fractionalSecond}${offset}`
}
