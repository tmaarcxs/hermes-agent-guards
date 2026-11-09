// Paths come from the reviewer: the installed Jot plugin and its plugin-data directory.
const { JotStore } = await import(process.env.JOT_PLUGIN_DIR + '/src/store.ts');
const { validateRichDoc } = await import(process.env.JOT_PLUGIN_DIR + '/src/model.ts');
import { isDeepStrictEqual } from 'node:util';

// Remove one three-cell row from a day table of the weekly Jot board, matched by text.
// Usage: tsx remove-row.mts <note-id> "<Day heading>" "<unique text in the row's Tarea cell>"
// Same guards as append-row.mts: fresh note, unique day heading, unique row match,
// validateRichDoc, isolation check, write with the fresh revision.
const [id, day, match] = process.argv.slice(2);
if (!id || !day || !match) throw new Error('usage: remove-row.mts <note-id> <day> <match>');

const store = new JotStore({ directory: process.env.JOT_DATA_DIR });
const note: any = await store.getNote(id, 'agent');

const candidate: any = structuredClone(note.content);
const nodes = candidate.content;
const indexes = nodes.flatMap((n: any, i: number) =>
  n.type === 'heading' && JSON.stringify(n).includes(day) ? [i] : []);
if (indexes.length !== 1) throw new Error(`abort: day heading "${day}" not found or not unique`);

const table = nodes[indexes[0] + 1];
if (table?.type !== 'table') throw new Error('abort: no day table below the heading');

const rows = table.content.flatMap((r: any, i: number) =>
  r.type === 'tableRow' && r.content?.length === 3 &&
  JSON.stringify(r.content[1]).includes(match) ? [i] : []);
if (rows.length !== 1) throw new Error(`abort: row matching "${match}" not found or not unique`);
const [rowIndex] = rows;
const removed = table.content[rowIndex];
if (!removed.content.every((c: any) => c.type === 'tableCell'))
  throw new Error('abort: matched row is not a three-cell row');

table.content.splice(rowIndex, 1);
validateRichDoc(candidate); // rejects a bad candidate BEFORE the write, with a named error

// Isolation check: re-inserting the removed row on a clone reproduces the live document exactly.
const check = structuredClone(candidate);
check.content[indexes[0] + 1].content.splice(rowIndex, 0, removed);
if (!isDeepStrictEqual(check, note.content)) throw new Error('abort: candidate changes more than the removed row');

const updated = await store.updateNote(id, note.revision, { content: candidate }, 'agent');
console.log(JSON.stringify({ id, revision: updated.revision, removed: match }));
