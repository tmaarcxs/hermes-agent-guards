// Paths come from the reviewer: the installed Jot plugin and its plugin-data directory.
const { JotStore } = await import(process.env.JOT_PLUGIN_DIR + '/src/store.ts');
const { validateRichDoc } = await import(process.env.JOT_PLUGIN_DIR + '/src/model.ts');
import { isDeepStrictEqual } from 'node:util';

// Append one three-cell row to a daily table of the weekly Jot board.
// Usage: tsx append-row.mts <note-id> "Martes 3" "<estado>" "<tarea>" "<siguiente paso>"
// Reads the note fresh (actor 'agent') on every run; never edits a stored snapshot.
const [id, day, state, task, next] = process.argv.slice(2);
if (!id || !day || !state || !task || !next) throw new Error('usage: append-row.mts <note-id> <day> <state> <task> <next>');

const store = new JotStore({ directory: process.env.JOT_DATA_DIR });
const note: any = await store.getNote(id, 'agent');

// Guard the OUTPUT: a re-run against a stale snapshot would create a duplicate row.
if (note.text.includes(task)) throw new Error(`abort: task text already present in live note`);

const candidate: any = structuredClone(note.content);
const nodes = candidate.content;
const indexes = nodes.flatMap((n: any, i: number) =>
  n.type === 'heading' && JSON.stringify(n).includes(day) ? [i] : []);
if (indexes.length !== 1) throw new Error(`abort: day heading "${day}" not found or not unique`);

const table = nodes[indexes[0] + 1];
if (table?.type !== 'table' || table.content.length < 2) throw new Error('abort: no day table below the heading');

// Reuse an existing three-cell row as the template so attributes stay verbatim.
const template = table.content.find((r: any) =>
  r.type === 'tableRow' && r.content?.length === 3 &&
  r.content.every((c: any) => c.type === 'tableCell'));
if (!template) throw new Error('abort: no template row with three cells');

const row = structuredClone(template);
for (const [i, text] of [state, task, next].entries())
  row.content[i].content = [{ type: 'paragraph', content: [{ type: 'text', text }] }];
table.content.push(row);

validateRichDoc(candidate); // rejects a bad candidate BEFORE the write, with a named error

// Isolation check: the ONLY change vs the live note is the new row.
const check = structuredClone(candidate);
check.content[indexes[0] + 1].content.pop();
if (!isDeepStrictEqual(check, note.content)) throw new Error('abort: candidate changes more than the appended row');

const updated = await store.updateNote(id, note.revision, { content: candidate }, 'agent');
console.log(JSON.stringify({ id, revision: updated.revision, task }));
