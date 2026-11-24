// Runs the unmodified web module in an independent DOM runtime. No account
// credentials are passed to this process. Python handles the authenticated I/O.
import fs from 'node:fs';
import readline from 'node:readline';
import { TextEncoder, TextDecoder } from 'node:util';
import { createRequire } from 'node:module';
import path from 'node:path';
const runtime = path.resolve(process.argv[2]);
const jsdom = createRequire(path.join(runtime, 'package.json'))('jsdom');
const { JSDOM, VirtualConsole, requestInterceptor } = jsdom;

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })[Symbol.asyncIterator]();
function emit(value) { process.stdout.write(JSON.stringify(value) + '\n'); }
async function receive() {
  const item = await lines.next();
  if (item.done) throw new Error('InputClosed');
  return JSON.parse(item.value);
}

const deadline = setTimeout(() => {
  emit({ type: 'error', code: 'driver_timeout' });
  process.exit(2);
}, 65000);

let dom;
try {
  const options = await receive();
  const consoleErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => consoleErrors.push(error.type || error.name));
  dom = new JSDOM('<!doctype html><html><head><script src="https://chatgpt.com/cdn/assets/646436.8f73ec6517.js"></script></head><body></body></html>', {
    url: 'https://chatgpt.com/', runScripts: 'outside-only', pretendToBeVisual: true,
    resources: { userAgent: options.user_agent,
      interceptors: [requestInterceptor(() => new Response(null, { status: 204 }))] }, virtualConsole,
  });
  const window = dom.window;
  // jsdom does not install these standard encoding classes on its window.
  window.TextEncoder = TextEncoder;
  window.TextDecoder = TextDecoder;
  const exports = window.Object.create(null);
  const load = name => {
    if (name !== 'DD2M') throw new Error('UnexpectedModuleDependency');
    return { a: window.crypto.randomUUID.bind(window.crypto) };
  };
  load.d = (target, definitions) => {
    for (const [key, getter] of Object.entries(definitions)) {
      Object.defineProperty(target, key, { enumerable: true, get: getter });
    }
  };
  const factory = window.eval(fs.readFileSync(path.join(runtime, 'client-module.js'), 'utf8'));
  factory({}, exports, load);
  emit({ type: 'ready', standalone_dom: true, user_agent: window.navigator.userAgent });
  const result = await exports.f(async p => {
    emit({ type: 'prepare', p });
    const message = await receive();
    if (!message.requirements) throw new Error('NoRequirements');
    return message.requirements;
  });
  emit({ type: 'integrity', headers: result.headers,
    proof_present: !!result.proofToken, environment_present: !!result.turnstileToken,
    dom_error_types: [...new Set(consoleErrors)] });
} catch (error) {
  // Never print server challenge strings or derived credentials in diagnostics.
  emit({ type: 'error', code: String(error?.name || 'RuntimeError') });
  process.exitCode = 2;
} finally {
  clearTimeout(deadline);
  dom?.window.close();
}
