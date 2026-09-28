#!/usr/bin/env node
// Assemble .codex/models.json from the per-model entries in .codex/catalog/.
// An entry may name a `base_instructions_file` (relative to the catalog dir); its
// contents are inlined as `base_instructions`, which Codex promotes to
// model_messages.instructions_template. Codex refuses a catalog entry that has neither.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const catalogDir = path.join(root, '.codex', 'catalog');
const output = path.join(root, '.codex', 'models.json');
const ORDER = ['deepseek-flash'];

const models = fs.readdirSync(catalogDir)
  .filter((name) => name.endsWith('.json'))
  .map((name) => {
    const entry = JSON.parse(fs.readFileSync(path.join(catalogDir, name), 'utf8'));
    if (entry.base_instructions_file) {
      entry.base_instructions = fs.readFileSync(path.join(catalogDir, entry.base_instructions_file), 'utf8');
      delete entry.base_instructions_file;
    }
    const hasTemplate = typeof entry.model_messages?.instructions_template === 'string'
      && entry.model_messages.instructions_template.length > 0;
    if (!hasTemplate && !entry.base_instructions) {
      throw new Error(`${name}: needs base_instructions_file or model_messages.instructions_template`);
    }
    return entry;
  })
  .sort((a, b) => ORDER.indexOf(a.slug) - ORDER.indexOf(b.slug));

fs.writeFileSync(output, `${JSON.stringify({ models }, null, 2)}\n`);
console.log(`wrote ${output}: ${models.map((m) => m.slug).join(', ')}`);
