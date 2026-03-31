#!/usr/bin/env python3
"""end-to-end tests for opencode-evolve core modules."""

import json, os, shutil, subprocess, sys, tempfile, stat
from pathlib import Path

PASS = FAIL = 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def check(desc, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {desc}")
        if detail:
            print(f"  {detail}")

def run_node(script, env_override=None):
    """run a node script that imports our modules and returns JSON on stdout."""
    env = None
    if env_override:
        env = {**os.environ, **env_override}
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script, capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), env=env,
    )
    if proc.returncode != 0:
        return None, proc.stderr
    lines = [l for l in proc.stdout.strip().split('\n') if l.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line), None
        except json.JSONDecodeError:
            continue
    return None, f"invalid json: {proc.stdout}"

# --- config: stripJsoncComments ---

result, err = run_node("""
import { stripJsoncComments } from './dist/config.js';
const cases = [
  { input: '{"a": 1} // comment', expected: '{"a": 1} ' },
  { input: '{"a": 1}', expected: '{"a": 1}' },
  { input: '// full line\\n{"a": 1}', expected: '\\n{"a": 1}' },
  { input: '{"a": "b" /* block */}', expected: '{"a": "b" }' },
];
console.log(JSON.stringify(cases.map(c => ({
  ok: stripJsoncComments(c.input) === c.expected,
  input: c.input,
  got: stripJsoncComments(c.input),
  expected: c.expected,
}))));
""")
if err:
    check("stripJsoncComments: run", False, err)
else:
    for r in result:
        check(f"stripJsoncComments: {r['input'][:40]}", r['ok'], f"got={r['got']!r} expected={r['expected']!r}")

# --- config: coerceEnv ---

result, err = run_node("""
import { coerceEnv } from './dist/config.js';
console.log(JSON.stringify([
  { desc: 'string val for null', got: coerceEnv('hello', null), expected: 'hello' },
  { desc: 'null string for null', got: coerceEnv('null', null), expected: null },
  { desc: 'string val for string', got: coerceEnv('world', 'existing'), expected: 'world' },
  { desc: 'null string for string', got: coerceEnv('null', 'existing'), expected: null },
  { desc: 'number coercion', got: coerceEnv('42', 100), expected: 42 },
  { desc: 'null string for number', got: coerceEnv('null', 100), expected: null },
  { desc: 'fallback for other types', got: coerceEnv('val', true), expected: 'val' },
]));
""")
if err:
    check("coerceEnv: run", False, err)
else:
    for r in result:
        check(f"coerceEnv: {r['desc']}", r['got'] == r['expected'], f"got={r['got']!r} expected={r['expected']!r}")

# --- config: normalizeModel ---

result, err = run_node("""
import { normalizeModel } from './dist/config.js';
const c1 = { model: 'anthropic/claude-sonnet-4-20250514' };
normalizeModel(c1);
const c2 = { model: { providerID: 'x', modelID: 'y' } };
normalizeModel(c2);
const c3 = { model: null };
normalizeModel(c3);
const c4 = { model: 'provider/nested/model/id' };
normalizeModel(c4);
console.log(JSON.stringify([c1, c2, c3, c4]));
""")
if err:
    check("normalizeModel: run", False, err)
else:
    check("normalizeModel: string with slash", result[0]['model'] == {'providerID': 'anthropic', 'modelID': 'claude-sonnet-4-20250514'}, f"got={result[0]['model']}")
    check("normalizeModel: already object", result[1]['model'] == {'providerID': 'x', 'modelID': 'y'}, f"got={result[1]['model']}")
    check("normalizeModel: null unchanged", result[2]['model'] is None, f"got={result[2]['model']}")
    check("normalizeModel: nested slashes", result[3]['model'] == {'providerID': 'provider', 'modelID': 'nested/model/id'}, f"got={result[3]['model']}")

# --- config: loadConfig with defaults ---

tmp = tempfile.mkdtemp()
result, err = run_node(f"""
import {{ loadConfig }} from './dist/config.js';
const config = loadConfig('{tmp}');
console.log(JSON.stringify(config));
""")
shutil.rmtree(tmp)
if err:
    check("loadConfig: defaults", False, err)
else:
    check("loadConfig: default heartbeat_ms", result['heartbeat_ms'] == 120 * 60 * 1000, f"got={result['heartbeat_ms']}")
    check("loadConfig: default hook_timeout", result['hook_timeout'] == 30000, f"got={result['hook_timeout']}")
    check("loadConfig: default model", result['model'] is None, f"got={result['model']}")
    check("loadConfig: default heartbeat_cleanup", result['heartbeat_cleanup'] == 'none', f"got={result['heartbeat_cleanup']}")

# --- config: loadConfig from file ---

tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "config"))
with open(os.path.join(tmp, "config", "evolve.jsonc"), "w") as f:
    f.write('{\n  // custom config\n  "heartbeat_ms": 5000,\n  "heartbeat_title": "custom"\n}\n')
result, err = run_node(f"""
import {{ loadConfig }} from './dist/config.js';
const config = loadConfig('{tmp}');
console.log(JSON.stringify(config));
""")
shutil.rmtree(tmp)
if err:
    check("loadConfig: from file", False, err)
else:
    check("loadConfig: file heartbeat_ms", result['heartbeat_ms'] == 5000, f"got={result['heartbeat_ms']}")
    check("loadConfig: file heartbeat_title", result['heartbeat_title'] == 'custom', f"got={result['heartbeat_title']}")
    check("loadConfig: file keeps defaults", result['hook_timeout'] == 30000, f"got={result['hook_timeout']}")

# --- config: loadConfig with env overrides ---

tmp = tempfile.mkdtemp()
result, err = run_node(f"""
import {{ loadConfig }} from './dist/config.js';
const config = loadConfig('{tmp}');
console.log(JSON.stringify(config));
""", env_override={"EVOLVE_HEARTBEAT_MS": "9999", "EVOLVE_HEARTBEAT_TITLE": "envtitle"})
shutil.rmtree(tmp)
if err:
    check("loadConfig: env overrides", False, err)
else:
    check("loadConfig: env heartbeat_ms", result['heartbeat_ms'] == 9999, f"got={result['heartbeat_ms']}")
    check("loadConfig: env heartbeat_title", result['heartbeat_title'] == 'envtitle', f"got={result['heartbeat_title']}")

# --- edit: editContent ---

result, err = run_node("""
import { editContent } from './dist/edit.js';
console.log(JSON.stringify([
  { desc: 'simple replace', got: editContent('hello world', 'world', 'earth') },
  { desc: 'not found', got: editContent('hello world', 'mars', 'earth') },
  { desc: 'multiple matches no replaceAll', got: editContent('aaa', 'a', 'b') },
  { desc: 'replaceAll', got: editContent('aaa', 'a', 'b', true) },
  { desc: 'empty oldString match', got: editContent('hello', 'hello', '') },
  { desc: 'multiline', got: editContent('line1\\nline2\\nline3', 'line2', 'replaced') },
]));
""")
if err:
    check("editContent: run", False, err)
else:
    check("editContent: simple replace", result[0]['got'] == 'hello earth', f"got={result[0]['got']}")
    check("editContent: not found", result[1]['got'] == {'error': 'oldString not found'}, f"got={result[1]['got']}")
    check("editContent: multiple matches error", 'error' in result[2]['got'], f"got={result[2]['got']}")
    check("editContent: replaceAll", result[3]['got'] == 'bbb', f"got={result[3]['got']}")
    check("editContent: replace to empty", result[4]['got'] == '', f"got={result[4]['got']}")
    check("editContent: multiline", result[5]['got'] == 'line1\nreplaced\nline3', f"got={result[5]['got']}")

# --- hook: parseHookOutput ---

result, err = run_node("""
import { parseHookOutput } from './dist/hook.js';
const logs = [];
const logFn = (msg) => logs.push(msg);

const r1 = parseHookOutput('{"key": "val"}\\n', logFn);
const r2 = parseHookOutput('{"a": 1}\\n{"b": 2}\\n', logFn);
const r3 = parseHookOutput('{"log": "debug msg"}\\n{"result": "ok"}\\n', logFn);
const r4 = parseHookOutput('\\n  \\n', logFn);

console.log(JSON.stringify({ r1, r2, r3, r4, logs }));
""")
if err:
    check("parseHookOutput: run", False, err)
else:
    check("parseHookOutput: single line", result['r1'] == {'key': 'val'}, f"got={result['r1']}")
    check("parseHookOutput: multiple lines merge", result['r2'] == {'a': 1, 'b': 2}, f"got={result['r2']}")
    check("parseHookOutput: result after log", result['r3'] == {'result': 'ok'}, f"got={result['r3']}")
    check("parseHookOutput: log captured", result['logs'] == ['debug msg'], f"got={result['logs']}")
    check("parseHookOutput: empty input", result['r4'] == {}, f"got={result['r4']}")

# --- hook: mergeResults ---

result, err = run_node("""
import { mergeResults } from './dist/hook.js';
console.log(JSON.stringify([
  { desc: 'disjoint keys', got: mergeResults({a: 1}, {b: 2}) },
  { desc: 'array concat', got: mergeResults({a: [1, 2]}, {a: [3]}) },
  { desc: 'string concat', got: mergeResults({a: 'hello'}, {a: 'world'}) },
  { desc: 'scalar override', got: mergeResults({a: 1}, {a: 2}) },
  { desc: 'mixed types override', got: mergeResults({a: [1]}, {a: 'str'}) },
  { desc: 'empty base', got: mergeResults({}, {a: 1}) },
  { desc: 'empty incoming', got: mergeResults({a: 1}, {}) },
]));
""")
if err:
    check("mergeResults: run", False, err)
else:
    check("mergeResults: disjoint keys", result[0]['got'] == {'a': 1, 'b': 2}, f"got={result[0]['got']}")
    check("mergeResults: array concat", result[1]['got'] == {'a': [1, 2, 3]}, f"got={result[1]['got']}")
    check("mergeResults: string concat", result[2]['got'] == {'a': 'hello\nworld'}, f"got={result[2]['got']}")
    check("mergeResults: scalar override", result[3]['got'] == {'a': 2}, f"got={result[3]['got']}")
    check("mergeResults: mixed types override", result[4]['got'] == {'a': 'str'}, f"got={result[4]['got']}")
    check("mergeResults: empty base", result[5]['got'] == {'a': 1}, f"got={result[5]['got']}")
    check("mergeResults: empty incoming", result[6]['got'] == {'a': 1}, f"got={result[6]['got']}")

# --- datetime: formatDatetime ---

result, err = run_node("""
import { formatDatetime } from './dist/datetime.js';
const d = new Date('2025-06-15T12:30:45.123Z');
console.log(JSON.stringify({
  utc: formatDatetime(d, 'UTC'),
  ny: formatDatetime(d, 'America/New_York'),
  default_tz: formatDatetime(d),
}));
""")
if err:
    check("formatDatetime: run", False, err)
else:
    check("formatDatetime: utc format", result['utc'] == '2025-06-15T12:30:45.123+00:00', f"got={result['utc']}")
    check("formatDatetime: default is utc", result['default_tz'] == '2025-06-15T12:30:45.123+00:00', f"got={result['default_tz']}")
    check("formatDatetime: ny has offset", '-04:00' in result['ny'] or '-05:00' in result['ny'], f"got={result['ny']}")

# --- path: safePath ---

result, err = run_node("""
import { safePath } from './dist/path.js';
const results = [];
results.push({ desc: 'normal path', got: safePath('/workspace', 'hooks', 'evolve.py'), ok: true });
try {
  safePath('/workspace', 'hooks', '../../../etc/passwd');
  results.push({ desc: 'traversal rejected', ok: false, got: 'no error thrown' });
} catch (e) {
  results.push({ desc: 'traversal rejected', ok: true, got: e.message });
}
try {
  safePath('/workspace', 'hooks', '/absolute/path');
  results.push({ desc: 'absolute path rejected', ok: false, got: 'no error thrown' });
} catch (e) {
  results.push({ desc: 'absolute path rejected', ok: true, got: e.message });
}
console.log(JSON.stringify(results));
""")
if err:
    check("safePath: run", False, err)
else:
    check("safePath: normal path resolves", result[0]['ok'] and result[0]['got'].endswith('/hooks/evolve.py'), f"got={result[0]['got']}")
    check("safePath: traversal rejected", result[1]['ok'], f"got={result[1]['got']}")
    check("safePath: absolute path rejected", result[2]['ok'], f"got={result[2]['got']}")

# --- path: existingPath ---

tmp = tempfile.mkdtemp()
hooks_dir = os.path.join(tmp, "hooks")
os.makedirs(hooks_dir)
with open(os.path.join(hooks_dir, "test.py"), "w") as f:
    f.write("# test")
result, err = run_node(f"""
import {{ existingPath }} from './dist/path.js';
const results = [];
results.push({{ desc: 'existing file', got: existingPath('{tmp}', 'hooks', 'test.py'), ok: true }});
try {{
  existingPath('{tmp}', 'hooks', 'missing.py');
  results.push({{ desc: 'missing file throws', ok: false }});
}} catch (e) {{
  results.push({{ desc: 'missing file throws', ok: true, got: e.message }});
}}
console.log(JSON.stringify(results));
""")
shutil.rmtree(tmp)
if err:
    check("existingPath: run", False, err)
else:
    check("existingPath: finds existing file", result[0]['ok'], f"got={result[0]}")
    check("existingPath: throws for missing", result[1]['ok'], f"got={result[1]}")

# --- path: discoverHookPaths ---

tmp = tempfile.mkdtemp()
hooks_dir = os.path.join(tmp, "hooks")
os.makedirs(hooks_dir)
# create executable file
exec_path = os.path.join(hooks_dir, "alpha.py")
with open(exec_path, "w") as f:
    f.write("#!/usr/bin/env python3\n")
os.chmod(exec_path, 0o755)
# create non-executable file
with open(os.path.join(hooks_dir, "readme.txt"), "w") as f:
    f.write("not executable")
# create hidden file
with open(os.path.join(hooks_dir, ".hidden"), "w") as f:
    f.write("hidden")
os.chmod(os.path.join(hooks_dir, ".hidden"), 0o755)
# create __pycache__ dir
os.makedirs(os.path.join(hooks_dir, "__pycache__"))
# create another executable
beta_path = os.path.join(hooks_dir, "beta.sh")
with open(beta_path, "w") as f:
    f.write("#!/bin/sh\n")
os.chmod(beta_path, 0o755)
# create a subdirectory (should be skipped)
os.makedirs(os.path.join(hooks_dir, "subdir"))

result, err = run_node(f"""
import {{ discoverHookPaths }} from './dist/path.js';
import path from 'node:path';
const paths = discoverHookPaths('{tmp}');
console.log(JSON.stringify(paths.map(p => path.basename(p))));
""")
shutil.rmtree(tmp)
if err:
    check("discoverHookPaths: run", False, err)
else:
    check("discoverHookPaths: finds executables only", result == ['alpha.py', 'beta.sh'], f"got={result}")

# --- path: discoverHookPaths with no hooks dir ---

tmp = tempfile.mkdtemp()
result, err = run_node(f"""
import {{ discoverHookPaths }} from './dist/path.js';
console.log(JSON.stringify(discoverHookPaths('{tmp}')));
""")
shutil.rmtree(tmp)
if err:
    check("discoverHookPaths: no hooks dir", False, err)
else:
    check("discoverHookPaths: returns empty for missing dir", result == [], f"got={result}")

# --- permission: permissionPatterns ---

result, err = run_node("""
import { permissionPatterns } from './dist/permission.js';
console.log(JSON.stringify([
  { desc: 'no permission field', got: permissionPatterns({}, {}) },
  { desc: 'permission with arg string', got: permissionPatterns({ permission: { arg: 'trait' } }, { trait: 'SOUL.md' }) },
  { desc: 'permission with arg missing from args', got: permissionPatterns({ permission: { arg: 'trait' } }, {}) },
  { desc: 'permission with arg array', got: permissionPatterns({ permission: { arg: ['old_trait', 'new_trait'] } }, { old_trait: 'a.md', new_trait: 'b.md' }) },
  { desc: 'permission with arg array partial', got: permissionPatterns({ permission: { arg: ['old_trait', 'new_trait'] } }, { old_trait: 'a.md' }) },
]));
""")
if err:
    check("permissionPatterns: run", False, err)
else:
    check("permissionPatterns: no permission field", result[0]['got'] == ['*'], f"got={result[0]['got']}")
    check("permissionPatterns: arg string", result[1]['got'] == ['SOUL.md'], f"got={result[1]['got']}")
    check("permissionPatterns: arg missing", result[2]['got'] == ['*'], f"got={result[2]['got']}")
    check("permissionPatterns: arg array", result[3]['got'] == ['a.md', 'b.md'], f"got={result[3]['got']}")
    check("permissionPatterns: arg array partial", result[4]['got'] == ['a.md'], f"got={result[4]['got']}")

# --- summary ---

print(f"\n{PASS + FAIL} tests, {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
