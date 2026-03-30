import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import { createOpencodeClient } from "@opencode-ai/sdk"
import { execFile, spawn } from 'node:child_process'
import { promisify } from 'node:util'
import { homedir } from 'os'
import path from 'node:path'
import { readFileSync, readdirSync, writeFileSync, mkdirSync, cpSync, chmodSync, mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'

const execFileAsync = promisify(execFile)

const DEFAULT_READ_LIMIT = 2000

// --- config ---

interface EvolveConfig {
  hook: string
  heartbeat_ms: number
  hook_timeout: number
  heartbeat_title: string
  heartbeat_agent: string
  test_script: string | null
  model: { providerID: string; modelID: string } | null
  heartbeat_cleanup: 'none' | 'new' | 'archive' | 'compact'
  heartbeat_cleanup_count: number | null
  heartbeat_cleanup_tokens: number | null
}

const DEFAULTS: EvolveConfig = {
  hook: 'hooks/evolve.py',
  heartbeat_ms: 120 * 60 * 1000,
  hook_timeout: 30_000,
  heartbeat_title: 'heartbeat',
  heartbeat_agent: 'evolve',
  test_script: null,
  model: null,
  heartbeat_cleanup: 'none',
  heartbeat_cleanup_count: null,
  heartbeat_cleanup_tokens: null,
}

// coerce env string to match the type of the existing config value
function coerceEnv(val: string, existing: any): any {
  if (existing === null || typeof existing === 'string') return val === 'null' ? null : val
  if (typeof existing === 'number') return val === 'null' ? null : Number(val)
  return val
}

// apply EVOLVE_<FIELD> env vars to config (e.g. EVOLVE_HEARTBEAT_MS -> heartbeat_ms)
function applyEnvOverrides(config: any) {
  for (const field of Object.keys(DEFAULTS)) {
    const val = process.env[`EVOLVE_${field.toUpperCase()}`]
    if (val !== undefined) config[field] = coerceEnv(val, DEFAULTS[field as keyof EvolveConfig])
  }
}

function normalizeModel(config: any) {
  if (typeof config.model === 'string' && config.model.includes('/')) {
    const [providerID, ...rest] = config.model.split('/')
    config.model = { providerID, modelID: rest.join('/') }
  }
}

function loadConfig(workspace: string): EvolveConfig {
  const configPath = path.join(workspace, 'config', 'evolve.jsonc')
  let parsed: any
  try {
    const raw = readFileSync(configPath, 'utf-8')
    // strip jsonc comments (// and /* */)
    const json = raw.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '')
    parsed = { ...DEFAULTS, ...JSON.parse(json) }
  } catch {
    parsed = { ...DEFAULTS }
  }
  applyEnvOverrides(parsed)
  normalizeModel(parsed)
  return parsed
}

// resolve a filename within a workspace subdirectory, rejecting traversal
function safePath(dir: string, filename: string): string {
  const base = path.join(WORKSPACE, dir)
  const resolved = path.resolve(base, filename)
  if (!resolved.startsWith(base + path.sep) && resolved !== base) {
    throw new Error(`invalid ${dir} path: ${filename}`)
  }
  return resolved
}

// require that a file already exists (no implicit creation)
function existingPath(dir: string, filename: string): string {
  const resolved = safePath(dir, filename)
  if (!existsSync(resolved)) throw new Error(`not found: ${dir}/${filename}`)
  return resolved
}

// --- state ---

const WORKSPACE = process.env.OPENCODE_EVOLVE_WORKSPACE || process.env.OPENCODE_SIDECAR_WORKSPACE || path.join(homedir(), 'workspace')
const CONFIG = loadConfig(WORKSPACE)
const HOOK_PATH = path.join(WORKSPACE, CONFIG.hook)
const STATE_PATH = path.join(WORKSPACE, 'state', 'evolve.json')
const TOOL_PREFIX = path.parse(CONFIG.hook).name
const LOG_PREFIX = '[evolve]'
// observational hooks — failure should not trigger recover cascade
const NO_RECOVER_HOOKS = new Set(['tool_before', 'tool_after', 'observe_message', 'format_notification'])

// --- datetime ---

function formatDatetime(date: Date, timezone = 'UTC'): string {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    fractionalSecondDigits: 3, hour12: false, timeZoneName: 'longOffset',
  })
  const p = Object.fromEntries(fmt.formatToParts(date).map(v => [v.type, v.value]))
  const offset = p.timeZoneName === 'GMT' ? '+00:00' : p.timeZoneName.replace('GMT', '')
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}.${p.fractionalSecond}${offset}`
}

// --- logging ---

function debug(msg: string) {
  console.log(`${LOG_PREFIX}: ${msg}`)
}

// --- runtime persistence ---

function loadRuntime(): any {
  try {
    return JSON.parse(readFileSync(STATE_PATH, 'utf-8'))
  } catch {
    return {}
  }
}

function persistRuntime(patch: Record<string, any>) {
  try {
    const current = loadRuntime()
    const updated = { ...current, ...patch }
    mkdirSync(path.dirname(STATE_PATH), { recursive: true })
    writeFileSync(STATE_PATH, JSON.stringify(updated, null, 2) + '\n')
  } catch (e: any) {
    debug(`persist runtime failed: ${e.message}`)
  }
}

function loadModel(): any {
  return loadRuntime().model
}

function persistModel(model: { providerID: string, modelID: string }) {
  const current = loadModel()
  if (current?.providerID === model.providerID && current?.modelID === model.modelID) return
  persistRuntime({ model })
  debug(`persisted model: ${model.providerID}/${model.modelID}`)
}

// --- git ---

let gitReady = false

async function gitExec(...args: string[]): Promise<string> {
  const { stdout } = await execFileAsync('git', ['-C', WORKSPACE, ...args])
  return stdout
}

async function ensureGitRepo() {
  if (gitReady) return
  await gitExec('init')
  try { await gitExec('config', 'user.email') }
  catch { await gitExec('config', 'user.email', TOOL_PREFIX) }
  try { await gitExec('config', 'user.name') }
  catch { await gitExec('config', 'user.name', TOOL_PREFIX) }
  gitReady = true
}

async function commitWorkspace(message: string) {
  try {
    await ensureGitRepo()
    await gitExec('add', '-A')
    await gitExec('diff', '--cached', '--quiet').catch(async () => {
      await gitExec('commit', '-m', message)
      const diff = await gitExec('show', '--color=always', '--pretty=format:', 'HEAD')
      diff.trim().split('\n').forEach(l => debug(l))
    })
  } catch (e: any) {
    debug(`git error: ${e.message}`)
  }
}

// --- hook validation ---

// run test suite against a candidate hook in an isolated temp workspace
async function validateHook(hookContent: string): Promise<{ ok: boolean, output: string }> {
  if (!CONFIG.test_script) return { ok: true, output: 'no test_script configured' }
  const testScript = path.join(WORKSPACE, CONFIG.test_script)
  const tmp = mkdtempSync(path.join(tmpdir(), 'evolve-validate-'))
  try {
    cpSync(WORKSPACE, tmp, { recursive: true })
    const hookPath = path.join(tmp, CONFIG.hook)
    writeFileSync(hookPath, hookContent)
    chmodSync(hookPath, 0o755)
    const { ok, output } = await new Promise<{ ok: boolean, output: string }>((resolve) => {
      const proc = spawn('python3', [testScript], {
        env: { ...process.env, OPENCODE_EVOLVE_WORKSPACE: tmp },
        stdio: ['pipe', 'pipe', 'pipe'],
      })
      let stdout = '', stderr = '', done = false
      const timer = setTimeout(() => {
        if (!done) { proc.kill(); resolve({ ok: false, output: stdout + stderr + 'timeout' }) }
      }, CONFIG.hook_timeout)
      proc.stdout.on('data', (d: Buffer) => stdout += d)
      proc.stderr.on('data', (d: Buffer) => stderr += d)
      proc.on('error', (e) => { done = true; clearTimeout(timer); resolve({ ok: false, output: stdout + stderr + e.message }) })
      proc.on('close', (code) => { done = true; clearTimeout(timer); resolve({ ok: code === 0, output: stdout + stderr }) })
    })
    return { ok, output }
  } catch (e: any) {
    return { ok: false, output: e.message || 'unknown error' }
  } finally {
    rmSync(tmp, { recursive: true, force: true })
  }
}

// find-and-replace with optional replaceAll support
function editContent(content: string, oldString: string, newString: string, replaceAll = false): string | { error: string } {
  const n = content.split(oldString).length - 1
  if (n === 0) return { error: 'oldString not found' }
  if (n > 1 && !replaceAll) return { error: `${n} matches for oldString, expected 1 (use replaceAll to replace all)` }
  return replaceAll ? content.replaceAll(oldString, newString) : content.replace(oldString, newString)
}

// --- hook IPC ---

// spawn hook subprocess with explicit stdin pipe and manual timeout
// (Bun's execFile doesn't pipe input; spawn ignores the timeout option)
function spawnHook(name: string, input: string): Promise<{ stdout: string }> {
  return new Promise((resolve, reject) => {
    const proc = spawn(HOOK_PATH, [name], { cwd: WORKSPACE, stdio: ['pipe', 'pipe', 'inherit'] })
    let stdout = '', done = false
    const timer = setTimeout(() => {
      if (!done) { proc.kill(); reject(new Error('timeout')) }
    }, CONFIG.hook_timeout)
    proc.stdout.on('data', (d: Buffer) => stdout += d)
    proc.on('error', (e) => { done = true; clearTimeout(timer); reject(e) })
    proc.on('close', (code, signal) => {
      done = true; clearTimeout(timer)
      if (code === 0) resolve({ stdout })
      else reject(Object.assign(new Error(`exit ${code ?? signal}`), { code, signal }))
    })
    proc.stdin.end(input)
  })
}

// parse JSONL stdout: merge all lines into one result, log any {log} lines
function parseHookOutput(name: string, stdout: string): any {
  const result: any = {}
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue
    const obj = JSON.parse(line)
    if (obj.log) { debug(obj.log); continue }
    Object.assign(result, obj)
  }
  return result
}

async function callHook(name: string, context: object, sessionId?: string): Promise<any> {
  if (!existsSync(HOOK_PATH)) {
    debug(`hook ${name} skipped: hook not found`)
    return {}
  }
  const start = Date.now()
  try {
    const history = sessionId ? sessionHistory.get(sessionId) || [] : undefined
    const input = JSON.stringify({ hook: name, ...context, ...(history ? { history } : {}) })
    debug(`hook ${name} start`)
    const { stdout } = await spawnHook(name, input)
    const ms = Date.now() - start
    if (!stdout.trim()) { debug(`hook ${name} empty (${ms}ms)`); return {} }
    const result = parseHookOutput(name, stdout)
    const keys = Object.keys(result).join(', ')
    if (result.error) debug(`hook ${name} error: ${result.error} (${ms}ms)`)
    else debug(`hook ${name} ok [${keys}] (${ms}ms)`)
    return result
  } catch (e: any) {
    const ms = Date.now() - start
    debug(`hook ${name} failed (${ms}ms): ${e.message}`)
    if (e.code != null) debug(`hook ${name} exit code: ${e.code}`)
    if (e.signal) debug(`hook ${name} signal: ${e.signal}`)
    if (name !== 'recover' && !NO_RECOVER_HOOKS.has(name)) {
      return callHook('recover', { error: e.message, failed_hook: name })
    }
    return {}
  }
}

// --- actions ---

async function executeActions(client: any, actions: any[]) {
  for (const action of actions) {
    try {
      if (action.type === 'send') {
        const parts = [{ type: 'text' as const, text: action.message, synthetic: action.synthetic ?? true }]
        const resp = await client.session.prompt({
          path: { id: action.session_id },
          body: { agent: CONFIG.heartbeat_agent, parts },
        })
        if (resp.error) throw new Error(`send failed: ${JSON.stringify(resp.error)}`)
        debug(`sent message to session ${action.session_id}`)
      } else if (action.type === 'create_session') {
        const created = await client.session.create({ body: { title: action.title } })
        if (created.error) throw new Error(`create_session failed: ${JSON.stringify(created.error)}`)
        debug(`created session ${created.data?.id}`)
      }
    } catch (e: any) {
      debug(`action ${action.type} failed: ${e.message}`)
    }
  }
}

// --- session helpers ---

async function createSession(client: any, title: string): Promise<string> {
  const created = await client.session.create({ body: { title } })
  if (created.error) throw new Error(`create session failed: ${JSON.stringify(created.error)}`)
  return created.data!.id
}

// --- session history ---

// messages.transform fires before system.transform; FIFO queue correlates them
const sessionHistory = new Map<string, any[]>()
const pendingMessagesQueue: any[][] = []

// --- immutable system prompt ---

// frozen system prompt per-session (byte-identical on every LLM call)
const sessionBasePrompt = new Map<string, string[]>()

// send persistent notifications to all sessions except the source.
// uses session.prompt({noReply}) so notifications survive restarts.
async function sendNotifications(client: any, notifications: any[], sourceSessionId?: string) {
  const formatted = await callHook('format_notification', { notifications })
  if (!formatted.message) return
  const wrapped = `<system-reminder>\n<assistant-notification>${formatted.message}</assistant-notification>\n</system-reminder>`
  for (const [sessionId] of sessionBasePrompt) {
    if (sessionId === sourceSessionId) continue
    try {
      await client.session.prompt({
        path: { id: sessionId },
        body: {
          noReply: true,
          agent: CONFIG.heartbeat_agent,
          parts: [{ type: 'text', text: wrapped, synthetic: true }],
        },
      })
      debug(`notification sent to session ${sessionId}`)
    } catch (e: any) {
      debug(`notification send failed for ${sessionId}: ${e.message}`)
    }
  }
}

// --- heartbeat cleanup ---

async function shouldCleanup(client: any, sessionId: string): Promise<boolean> {
  if (CONFIG.heartbeat_cleanup === 'none') return false

  // check triggers independently and simultaneously
  if (CONFIG.heartbeat_cleanup_count !== null) {
    const count = loadRuntime().heartbeat_count || 0
    if (count >= CONFIG.heartbeat_cleanup_count) {
      debug(`heartbeat cleanup: count limit reached (${count} >= ${CONFIG.heartbeat_cleanup_count})`)
      return true
    }
  }
  if (CONFIG.heartbeat_cleanup_tokens !== null) {
    const msgs = await client.session.messages({ path: { id: sessionId } })
    if (msgs.error) {
      debug(`heartbeat cleanup check failed (list messages): ${JSON.stringify(msgs.error)}`)
      return false
    }
    let total = 0
    for (const m of (msgs.data || [])) {
      const t = m.info?.tokens
      if (t) total += (t.input || 0) + (t.output || 0)
    }
    if (total >= CONFIG.heartbeat_cleanup_tokens) {
      debug(`heartbeat cleanup: token limit reached (${total} >= ${CONFIG.heartbeat_cleanup_tokens})`)
      return true
    }
  }
  return false
}

async function performCleanup(client: any, sessionId: string, model: any): Promise<string | null> {
  if (CONFIG.heartbeat_cleanup === 'compact') {
    debug(`heartbeat cleanup: attempting to compact session ${sessionId}`)
    const summarize = await client.session.summarize({
      path: { id: sessionId },
      body: { providerID: model.providerID, modelID: model.modelID },
    })
    if (summarize.error) {
      debug(`heartbeat cleanup compact failed: ${JSON.stringify(summarize.error)}`)
      return null
    }
    debug('heartbeat cleanup: compaction triggered')
    return null // stay in current session
  }

  if (CONFIG.heartbeat_cleanup !== 'new' && CONFIG.heartbeat_cleanup !== 'archive') return null

  if (CONFIG.heartbeat_cleanup === 'archive') {
    debug(`heartbeat cleanup: attempting to archive session ${sessionId}`)
    // set archived timestamp to hide it from the active list in webui
    const update = await client.session.update({
      path: { id: sessionId },
      body: {
        time: { archived: Date.now() }
      } as any
    })
    if (update.error) {
      debug(`heartbeat cleanup archive failed: ${JSON.stringify(update.error)}`)
      return null
    }
  }

  // rotate: create new session for 'new' or 'archive' actions
  const newTitle = `${CONFIG.heartbeat_title} (${formatDatetime(new Date())})`
  const newId = await createSession(client, newTitle)
  debug(`heartbeat cleanup successful: action=${CONFIG.heartbeat_cleanup} old=${sessionId} new=${newId}`)
  return newId
}

// --- plugin ---

// build opencode tool registrations from hook-defined tool definitions
// calls spawnHook directly to avoid recover cascade during init
async function discoverTools(client: any): Promise<Record<string, ReturnType<typeof tool>>> {
  if (!existsSync(HOOK_PATH)) {
    debug('hook discover skipped: hook not found')
    return {}
  }
  let discovered: any = {}
  const start = Date.now()
  try {
    const input = JSON.stringify({ hook: 'discover' })
    debug('hook discover start')
    const { stdout } = await spawnHook('discover', input)
    const ms = Date.now() - start
    if (stdout.trim()) discovered = parseHookOutput('discover', stdout)
    debug(`hook discover ok (${ms}ms)`)
  } catch (e: any) {
    const ms = Date.now() - start
    debug(`hook discover failed (${ms}ms): ${e.message}`)
    return {}
  }
  const tools: Record<string, ReturnType<typeof tool>> = {}
  for (const def of discovered.tools || []) {
    const args: Record<string, any> = {}
    for (const [param, spec] of Object.entries(def.parameters || {})) {
      // backwards compat: bare string = string type with that description
      if (typeof spec === 'string') {
        args[param] = tool.schema.string().describe(spec)
        continue
      }
      const { type, description, optional } = spec as { type?: string, description?: string, optional?: boolean }
      const desc = description || param
      const SCHEMA_TYPES: Record<string, () => any> = {
        string: () => tool.schema.string().describe(desc),
        number: () => tool.schema.number().describe(desc),
        boolean: () => tool.schema.boolean().describe(desc),
        object: () => tool.schema.record(tool.schema.string(), tool.schema.any()).describe(desc),
        array: () => tool.schema.array(tool.schema.any()).describe(desc),
        any: () => tool.schema.any().describe(desc),
      }
      let schema = (SCHEMA_TYPES[type || 'string'] || SCHEMA_TYPES.string)()
      if (optional) schema = schema.optional()
      args[param] = schema
    }
    tools[`${TOOL_PREFIX}_${def.name}`] = tool({
      description: def.description,
      args,
      async execute(toolArgs, context) {
        try {
          debug(`tool ${TOOL_PREFIX}_${def.name} execute session=${context.sessionID}`)
          const result = await callHook('execute_tool', { tool: def.name, args: toolArgs, session: { id: context.sessionID } }, context.sessionID)
          if (result.modified?.length) trackModified(result.modified)
          if (result.notify?.length) await sendNotifications(client, result.notify, context.sessionID)
          await commitWorkspace(`update ${def.name}`)
          return result.result || 'done'
        } catch (e: any) {
          return `tool error: ${e.message}`
        }
      },
    })
  }
  // --- builtin tools (escape hatch — work even if hook is bricked) ---

  tools['evolve_datetime'] = tool({
    description: `get the current date and time`,
    args: {
      timezone: tool.schema.string().describe('IANA timezone (e.g. America/New_York, Europe/London)').optional(),
    },
    async execute({ timezone }) {
      return formatDatetime(new Date(), timezone || 'UTC')
    },
  })

  tools['evolve_heartbeat_time'] = tool({
    description: `get the last heartbeat runtime in UTC`,
    args: {},
    async execute() {
      const runtime = loadRuntime()
      return runtime.heartbeat_time ?? 'no heartbeat has run yet'
    },
  })

  tools['evolve_prompt_list'] = tool({
    description: `list prompt files in prompts/ (bare filenames like "chat.md")`,
    args: {},
    async execute() {
      debug('prompt_list')
      try {
        const files = readdirSync(path.join(WORKSPACE, 'prompts')).filter(f => f.endsWith('.md')).sort()
        return `available prompts: ${files.join(', ')}`
      } catch (e: any) {
        debug(`prompt_list error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_prompt_read'] = tool({
    description: `read an existing prompt file from prompts/ (must already exist)`,
    args: {
      prompt: tool.schema.string().describe('prompt filename in prompts/ (e.g. "chat.md")'),
      offset: tool.schema.number().optional().describe('the line number to start reading from (1-indexed)'),
      limit: tool.schema.number().optional().describe('the maximum number of lines to read (defaults to 2000)'),
    },
    async execute({ prompt, offset, limit }) {
      debug(`prompt_read: ${prompt}`)
      try {
        const content = readFileSync(existingPath('prompts', prompt), 'utf-8')
        const lines = content.split('\n')
        const start = offset ? offset - 1 : 0
        const end = start + (limit ?? DEFAULT_READ_LIMIT)
        const sliced = lines.slice(start, end)
        return sliced.join('\n')
      } catch (e: any) {
        debug(`prompt_read error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_prompt_write'] = tool({
    description: `overwrite an existing prompt file in prompts/ (cannot create new files)`,
    args: {
      prompt: tool.schema.string().describe('prompt filename in prompts/ (e.g. "chat.md")'),
      content: tool.schema.string().describe('full content for the prompt'),
    },
    async execute({ prompt, content }) {
      debug(`prompt_write: ${prompt}`)
      try {
        existingPath('prompts', prompt)
        writeFileSync(safePath('prompts', prompt), content)
        trackModified([`prompts/${prompt}`])
        await commitWorkspace(`write prompt ${prompt}`)
        debug(`prompt_write ok: ${prompt}`)
        return `successfully wrote ${prompt}`
      } catch (e: any) {
        debug(`prompt_write error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_prompt_edit'] = tool({
    description: `edit an existing prompt file in prompts/ (find-and-replace, cannot create new files)`,
    args: {
      prompt: tool.schema.string().describe('prompt filename in prompts/ (e.g. "chat.md")'),
      oldString: tool.schema.string().describe('the text to replace'),
      newString: tool.schema.string().describe('the text to replace it with (must be different from oldString)'),
      replaceAll: tool.schema.boolean().optional().describe('replace all occurrences (default false)'),
    },
    async execute({ prompt, oldString, newString, replaceAll }) {
      debug(`prompt_edit: ${prompt}`)
      try {
        const filePath = existingPath('prompts', prompt)
        const content = readFileSync(filePath, 'utf-8')
        const result = editContent(content, oldString, newString, replaceAll)
        if (typeof result !== 'string') { debug(`prompt_edit failed: ${result.error}`); return `failed: ${result.error}` }
        writeFileSync(filePath, result)
        trackModified([`prompts/${prompt}`])
        await commitWorkspace(`edit prompt ${prompt}`)
        debug(`prompt_edit ok: ${prompt}`)
        return `successfully edited ${prompt}`
      } catch (e: any) {
        debug(`prompt_edit error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_hook_list'] = tool({
    description: `list hook files in hooks/ (bare filenames like "persona.py")`,
    args: {},
    async execute() {
      debug('hook_list')
      try {
        const files = readdirSync(path.join(WORKSPACE, 'hooks')).sort()
        return `available hooks: ${files.join(', ')}`
      } catch (e: any) {
        debug(`hook_list error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_hook_read'] = tool({
    description: `read an existing hook file from hooks/ (must already exist)`,
    args: {
      hook: tool.schema.string().describe('hook filename in hooks/ (e.g. "persona.py")'),
      offset: tool.schema.number().optional().describe('the line number to start reading from (1-indexed)'),
      limit: tool.schema.number().optional().describe('the maximum number of lines to read (defaults to 2000)'),
    },
    async execute({ hook, offset, limit }) {
      debug(`hook_read: ${hook}`)
      try {
        const content = readFileSync(existingPath('hooks', hook), 'utf-8')
        const lines = content.split('\n')
        const start = offset ? offset - 1 : 0
        const end = start + (limit ?? DEFAULT_READ_LIMIT)
        const sliced = lines.slice(start, end)
        return sliced.join('\n')
      } catch (e: any) {
        debug(`hook_read error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_hook_write'] = tool({
    description: `overwrite an existing hook file in hooks/ (validated before install if it is the configured hook, cannot create new files)`,
    args: {
      hook: tool.schema.string().describe('hook filename in hooks/ (e.g. "persona.py")'),
      content: tool.schema.string().describe('full content for the hook'),
    },
    async execute({ hook, content }) {
      debug(`hook_write: ${hook}`)
      try {
        const filePath = existingPath('hooks', hook)
        const isConfiguredHook = path.resolve(filePath) === path.resolve(HOOK_PATH)
        if (isConfiguredHook) {
          const validation = await validateHook(content)
          if (!validation.ok) { debug('hook_write: validation failed'); return `validation failed:\n${validation.output}` }
        }
        writeFileSync(filePath, content)
        chmodSync(filePath, 0o755)
        await commitWorkspace(`write hook ${hook}`)
        debug(`hook_write ok: ${hook}`)
        return `successfully wrote ${hook}`
      } catch (e: any) {
        debug(`hook_write error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_hook_edit'] = tool({
    description: `edit an existing hook file in hooks/ (find-and-replace, validated before install if it is the configured hook, cannot create new files)`,
    args: {
      hook: tool.schema.string().describe('hook filename in hooks/ (e.g. "persona.py")'),
      oldString: tool.schema.string().describe('the text to replace'),
      newString: tool.schema.string().describe('the text to replace it with (must be different from oldString)'),
      replaceAll: tool.schema.boolean().optional().describe('replace all occurrences (default false)'),
    },
    async execute({ hook, oldString, newString, replaceAll }) {
      debug(`hook_edit: ${hook}`)
      try {
        const filePath = existingPath('hooks', hook)
        const content = readFileSync(filePath, 'utf-8')
        const result = editContent(content, oldString, newString, replaceAll)
        if (typeof result !== 'string') { debug(`hook_edit failed: ${result.error}`); return `failed: ${result.error}` }
        const isConfiguredHook = path.resolve(filePath) === path.resolve(HOOK_PATH)
        if (isConfiguredHook) {
          const validation = await validateHook(result)
          if (!validation.ok) { debug('hook_edit: validation failed'); return `validation failed:\n${validation.output}` }
        }
        writeFileSync(filePath, result)
        chmodSync(filePath, 0o755)
        await commitWorkspace(`edit hook ${hook}`)
        debug(`hook_edit ok: ${hook}`)
        return `successfully edited ${hook}`
      } catch (e: any) {
        debug(`hook_edit error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  tools['evolve_hook_validate'] = tool({
    description: `validate hook content against the test suite without installing it`,
    args: {
      content: tool.schema.string().describe('full content for the hook to validate'),
    },
    async execute({ content }) {
      debug('hook_validate')
      try {
        const validation = await validateHook(content)
        if (validation.ok) { debug('hook_validate: passed'); return `validation passed` }
        debug('hook_validate: failed')
        return `validation failed:\n${validation.output}`
      } catch (e: any) {
        debug(`hook_validate error: ${e.message}`)
        return `error: ${e.message}`
      }
    },
  })

  return tools
}

// track modified files for git commits only
function trackModified(_files: string[]) {
  // git commit handled by caller — no prompt cache invalidation needed
}

export const EvolvePlugin: Plugin = async ({ client: projectClient, directory, serverUrl }) => {
  debug(`evolve initialized in ${directory}`)
  debug(`workspace: ${WORKSPACE}`)
  debug(`hook: ${CONFIG.hook} (prefix: ${TOOL_PREFIX})`)
  if (CONFIG.heartbeat_ms < 0) debug('heartbeat: disabled (heartbeat_ms < 0)')

  // ensure git repo exists before creating client so the server resolves
  // the workspace as a real project instead of falling back to global "/"
  await commitWorkspace('initial')

  // workspace-scoped client for session operations (heartbeat, actions, etc.)
  // WARNING: this only works if --port= is specified explicitly to `opencode serve`
  const client = createOpencodeClient({ baseUrl: serverUrl.toString(), directory: WORKSPACE })

  const registeredTools = await discoverTools(client)
  debug(`registered tools: ${Object.keys(registeredTools).join(', ')}`)

  let heartbeatSessionId: string | null = loadRuntime().heartbeat_session || null
  let lastModel: any = CONFIG.model || loadModel()

  // skip heartbeat when any other session has an active LLM call
  async function hasActiveSessions(includeHeartbeat = false): Promise<boolean> {
    const resp = await client.session.status({})
    if (!resp.data) return false
    for (const [id, status] of Object.entries(resp.data as Record<string, { type: string }>)) {
      if (!includeHeartbeat && id === heartbeatSessionId) continue
      if (status.type !== 'idle') return true
    }
    return false
  }

  // use setTimeout chaining to guarantee only one heartbeat runs at a time
  async function heartbeatTick() {
    const heartbeatModel = CONFIG.model || loadModel() || lastModel
    debug(`heartbeat: tick start (${heartbeatModel?.providerID}/${heartbeatModel?.modelID})`)
    try {
      if (await hasActiveSessions()) {
        debug('heartbeat: tick skipped (other sessions active)')
        return
      }
      if (!heartbeatSessionId) {
        const title = `${CONFIG.heartbeat_title} (${formatDatetime(new Date())})`
        heartbeatSessionId = await createSession(client, title)
        persistRuntime({ heartbeat_session: heartbeatSessionId })
        debug(`heartbeat: session created id=${heartbeatSessionId}`)
      }
      if (!heartbeatModel) {
        debug('heartbeat: tick skipped (no model captured)')
        return
      }
      if (await shouldCleanup(client, heartbeatSessionId)) {
        // if cleanup is needed, ensure the heartbeat session itself is idle
        if (await hasActiveSessions(true)) {
          debug('heartbeat: cleanup skipped (heartbeat session busy)')
          return
        }
        const newId = await performCleanup(client, heartbeatSessionId, heartbeatModel)
        if (newId) heartbeatSessionId = newId
        persistRuntime({ heartbeat_count: 0, heartbeat_session: heartbeatSessionId })
      }
      const result = await callHook('heartbeat', { sessions: [] }, heartbeatSessionId)
      if (!result.user) debug('heartbeat: hook returned no user message')
      if (result.user) {
        const parts = [{ type: 'text' as const, text: `[heartbeat] ${result.user}`, synthetic: true }]
        const resp = await client.session.prompt({
          path: { id: heartbeatSessionId },
          body: { agent: CONFIG.heartbeat_agent, model: heartbeatModel, parts },
        })
        if (resp.error) {
          debug(`heartbeat: prompt failed: ${JSON.stringify(resp.error)}`)
          heartbeatSessionId = null
          persistRuntime({ heartbeat_session: null })
          return
        }
        debug('heartbeat: prompt sent')
        const count = (loadRuntime().heartbeat_count || 0) + 1
        persistRuntime({ heartbeat_count: count, heartbeat_time: formatDatetime(new Date()) })
      }
      if (result.modified?.length) trackModified(result.modified)
      if (result.notify?.length) await sendNotifications(client, result.notify, heartbeatSessionId!)
      if (result.actions?.length) await executeActions(client, result.actions)
    } catch (e: any) {
      debug(`heartbeat: tick failed: ${e.message}`)
    } finally {
      debug('heartbeat: tick finish')
      if (CONFIG.heartbeat_ms >= 0) {
        debug(`heartbeat: next tick scheduled at ${formatDatetime(new Date(Date.now() + CONFIG.heartbeat_ms))}`)
        setTimeout(heartbeatTick, CONFIG.heartbeat_ms)
      }
    }
  }
  if (CONFIG.heartbeat_ms >= 0) {
    debug(`heartbeat: initial tick scheduled at ${formatDatetime(new Date(Date.now() + CONFIG.heartbeat_ms))}`)
    setTimeout(heartbeatTick, CONFIG.heartbeat_ms)
  }

  return {
    tool: registeredTools,

    "chat.message": async (input, output) => {
      try {
        if (input.model) {
          lastModel = input.model
          persistModel(input.model)
        }
        const parts = output?.parts || []
        const answer = parts.filter((p: any) => p.type === 'text').map((p: any) => p.text).join('\n')
        const result = await callHook('observe_message', {
          session: { id: input.sessionID, agent: input.agent },
          thinking: parts.filter((p: any) => p.type === 'reasoning').map((p: any) => p.text).join('\n'),
          calls: parts.filter((p: any) => p.type === 'tool'),
          answer,
        }, input.sessionID)
        if (result.modified?.length) trackModified(result.modified)
        if (result.notify?.length) await sendNotifications(client, result.notify, input.sessionID)
        if (result.actions?.length) await executeActions(client, result.actions)
        // idle continuation: when LLM gives a final response (no tool calls),
        // ask the hook if the session should be forced to continue
        const hasToolCalls = parts.some((p: any) => p.type === 'tool')
        if (!hasToolCalls) {
          const idle = await callHook('idle', {
            session: { id: input.sessionID, agent: input.agent },
            answer,
          }, input.sessionID)
          if (idle.continue) {
            await client.session.promptAsync({
              path: { id: input.sessionID },
              body: {
                agent: input.agent || CONFIG.heartbeat_agent,
                model: lastModel,
                parts: [{ type: 'text' as const, text: idle.continue, synthetic: true }],
              },
            })
            debug(`idle: continued session ${input.sessionID}`)
          }
        }
      } catch (e: any) {
        debug(`response hook failed: ${e.message}`)
      }
    },

    "tool.execute.before": async (input, output) => {
      debug(`tool call: ${input.tool} session=${input.sessionID} call=${input.callID} args=${JSON.stringify(output.args)}`)
      await callHook('tool_before', {
        session: { id: input.sessionID },
        tool: input.tool, callID: input.callID, args: output.args,
      }, input.sessionID)
    },

    "tool.execute.after": async (input, output) => {
      const preview = output.output?.length > 200 ? output.output.slice(0, 200) + '...' : output.output
      debug(`tool done: ${input.tool} session=${input.sessionID} call=${input.callID} output=${preview}`)
      await callHook('tool_after', {
        session: { id: input.sessionID },
        tool: input.tool, callID: input.callID,
        title: output.title, output: output.output,
      }, input.sessionID)
    },

    "experimental.chat.messages.transform": async (input, output) => {
      // capture history for hook context (FIFO correlation with system.transform)
      pendingMessagesQueue.push((output.messages || []).map((m: any) => ({
        role: m.info?.role, agent: m.info?.agent, parts: m.parts,
      })))
    },

    "experimental.chat.system.transform": async (input, output) => {
      try {
        // consume pending messages from messages.transform (FIFO correlation)
        const msgs = pendingMessagesQueue.shift()
        if (msgs && input.sessionID) sessionHistory.set(input.sessionID, msgs)
        // freeze system prompt once per session (byte-identical on every call)
        const cached = sessionBasePrompt.get(input.sessionID)
        if (cached) {
          output.system.splice(0, output.system.length, ...cached)
        } else {
          const result = await callHook('mutate_request', {
            session: { id: input.sessionID },
            system: output.system,
          }, input.sessionID)
          if (result.system?.length) {
            output.system.splice(0, output.system.length, ...result.system)
            sessionBasePrompt.set(input.sessionID, result.system)
          }
        }
      } catch (e: any) {
        debug(`request hook failed: ${e.message}`)
      }
    },

    "experimental.session.compacting": async (input, output) => {
      try {
        const result = await callHook('compacting', {
          session: { id: input.sessionID },
        }, input.sessionID)
        if (result.prompt) {
          output.prompt = result.prompt
        }
      } catch (e: any) {
        debug(`compacting hook failed: ${e.message}`)
      }
    },
  }
}

export default EvolvePlugin
