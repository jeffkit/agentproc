'use strict';
/**
 * Minimal YAML parser for profile.yaml files.
 *
 * Extracted from cli.js so hub.js can parse bundled profile.yaml without
 * pulling in cli.js (which would create a circular require: cli.js requires
 * hub.js at the top, and is itself only fully exported after main() runs).
 *
 * Zero dependencies. Handles the subset of YAML our profiles use: nested
 * maps, sequences, block scalars (|, |-, >), quoted strings, flow sequences,
 * booleans/null/numbers.
 */

function stripScalar(v) {
  // Quoted string — return inner content verbatim.
  if ((v.startsWith('"') && v.endsWith('"')) ||
      (v.startsWith("'") && v.endsWith("'"))) {
    return v.slice(1, -1);
  }
  // Flow sequence: [a, b, c]
  if (v.startsWith('[') && v.endsWith(']')) {
    const inner = v.slice(1, -1).trim();
    if (inner === '') return [];
    return inner.split(',').map(s => stripScalar(s.trim()));
  }
  // Booleans / null
  const lv = v.toLowerCase();
  if (lv === 'true') return true;
  if (lv === 'false') return false;
  if (lv === 'null' || lv === '~') return null;
  // Numbers (int / float, optional sign)
  if (/^[+-]?\d+$/.test(v)) return parseInt(v, 10);
  if (/^[+-]?\d+\.\d+$/.test(v)) return parseFloat(v);
  return v;
}

function container_set(obj, key, value) {
  obj[key] = value;
}

function parseYaml(text) {
  try { return JSON.parse(text); } catch {}

  // Use a line-based state machine that handles sequences better.
  const lines = text.split(/\r?\n/);
  const root = {};
  /** @type {Array<{indent:number, obj:Object|Array, parentKey:string|null, parent:Object|null}>} */
  const stack = [{ indent: -1, obj: root, parentKey: null, parent: null }];

  function top() { return stack[stack.length - 1]; }
  function popUntil(minIndent) {
    while (stack.length > 1 && top().indent >= minIndent) stack.pop();
    return top();
  }

  function getIndent(s) {
    return s.match(/^[ \t]*/)[0].replace(/\t/g, '  ').length;
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (raw.trim() === '' || raw.trim().startsWith('#')) continue;
    const indent = getIndent(raw);
    const content = raw.slice(indent).replace(/\r$/, '');
    const cont = popUntil(indent);

    // Sequence item: "- value" or "-"
    if (content.startsWith('- ') || content === '-') {
      if (Array.isArray(cont.obj)) {
        const rest = content === '-' ? '' : content.slice(2);
        if (rest.trim() === '') {
          // Map under sequence — rare in our profiles, skip gracefully.
          continue;
        }
        cont.obj.push(stripScalar(rest));
      }
      continue;
    }

    // key: value
    const m = content.match(/^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$/);
    if (!m) continue;
    const [, key, val] = m;

    if (val === '') {
      // Look ahead: is the next non-empty, more-indented line a sequence?
      let j = i + 1;
      while (j < lines.length && (lines[j].trim() === '' || lines[j].trim().startsWith('#'))) j++;
      const nextRaw = lines[j] || '';
      const nextIndent = getIndent(nextRaw);
      const nextContent = nextRaw.slice(nextIndent);
      if (nextIndent > indent && (nextContent.startsWith('- ') || nextContent === '-')) {
        const arr = [];
        cont.obj[key] = arr;
        stack.push({ indent, obj: arr, parentKey: key, parent: cont.obj });
      } else if (nextIndent > indent) {
        const child = {};
        cont.obj[key] = child;
        stack.push({ indent, obj: child, parentKey: key, parent: cont.obj });
      } else {
        cont.obj[key] = '';
      }
    } else if (val === '|' || val === '|-' || val === '>') {
      const blockLines = [];
      let j = i + 1;
      for (; j < lines.length; j++) {
        const nr = lines[j];
        const ni = getIndent(nr);
        if (nr.trim() === '') { blockLines.push(''); continue; }
        if (ni <= indent) break;
        blockLines.push(nr.slice(Math.min(indent + 2, nr.length)));
      }
      const joined = blockLines.join('\n');
      container_set(cont.obj, key, val === '|'
        ? joined.replace(/\n*$/, '\n')
        : joined.replace(/\n*$/, ''));
      i = j - 1;
    } else {
      container_set(cont.obj, key, stripScalar(val));
    }
  }

  return root;
}

module.exports = { parseYaml, parseYamlSimple: parseYaml };
