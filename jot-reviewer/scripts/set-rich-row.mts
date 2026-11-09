// Paths come from the reviewer: the installed Jot plugin and its plugin-data directory.
const { JotStore } = await import(process.env.JOT_PLUGIN_DIR + '/src/store.ts');
const { validateRichDoc } = await import(process.env.JOT_PLUGIN_DIR + '/src/model.ts');
import { isDeepStrictEqual } from 'node:util';

const [id, day, task, oldState, oldNext, state, next, mode] = process.argv.slice(2);
if (!id || !day || !task || !oldState || !oldNext || !state || !next || !['--dry-run', '--write'].includes(mode))
  throw new Error('usage: set-rich-row.mts <note-id> <day> <task> <old-state> <old-next> <state> <next> <--dry-run|--write>');
if (!['🔄', '🙋', '⏳', '✅', '⚪'].includes(state) || (state === '✅' && next !== 'Nada') || (state !== '✅' && next === 'Nada'))
  throw new Error('abort: invalid state or next action');
const store = new JotStore({ directory: process.env.JOT_DATA_DIR });
const note: any = await store.getNote(id, 'agent');
const candidate: any = structuredClone(note.content);
const nodes = candidate.content;
const headings = nodes.flatMap((n: any, i: number) => n.type === 'heading' && JSON.stringify(n).includes(day) ? [i] : []);
if (headings.length !== 1) throw new Error('abort: day heading not unique');
const table = nodes[headings[0] + 1];
if (table?.type !== 'table') throw new Error('abort: day table not found');
const textNode = (cell: any) => {
  const paragraph = cell?.content;
  if (cell?.type !== 'tableCell' || paragraph?.length !== 1 || paragraph[0].type !== 'paragraph' || paragraph[0].content?.length !== 1 || paragraph[0].content[0].type !== 'text')
    throw new Error('abort: cell is not a single text node');
  return paragraph[0].content[0];
};
const rows = table.content.filter((r: any) => r.type === 'tableRow' && r.content?.length === 3 && JSON.stringify(r.content[1]).includes(task));
if (rows.length !== 1) throw new Error('abort: task row not unique');
const row = rows[0];
const stateNode = textNode(row.content[0]);
const nextNode = textNode(row.content[2]);
if (stateNode.text !== oldState || nextNode.text !== oldNext) throw new Error('abort: row changed since it was read');
stateNode.text = state;
nextNode.text = next;
validateRichDoc(candidate);
const check = structuredClone(candidate);
const checkRow = check.content[headings[0] + 1].content.find((r: any) => r.type === 'tableRow' && r.content?.length === 3 && JSON.stringify(r.content[1]).includes(task));
textNode(checkRow.content[0]).text = oldState;
textNode(checkRow.content[2]).text = oldNext;
if (!isDeepStrictEqual(check, note.content)) throw new Error('abort: changes outside the two target cells');
if (mode === '--write') {
  const updated = await store.updateNote(id, note.revision, { content: candidate }, 'agent');
  console.log(JSON.stringify({ id, revision: updated.revision, task, state, next }));
} else console.log(JSON.stringify({ dryRun: true, id, revision: note.revision, task, state, next }));
