import { pathToFileURL } from 'node:url';
import path from 'node:path';
const root = process.env.JOT_PLUGIN_DIR;
if (!root) throw new Error('JOT_PLUGIN_DIR missing');
const store = await import(pathToFileURL(path.join(root, 'src/store.ts')).href);
const model = await import(pathToFileURL(path.join(root, 'src/model.ts')).href);
if (typeof store.JotStore !== 'function' || typeof model.validateRichDoc !== 'function') throw new Error('Jot bridge exports unavailable');
process.stdout.write('runtime-ready\n');
