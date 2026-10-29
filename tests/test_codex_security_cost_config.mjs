import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { parse } from 'yaml';
import { ProjectConfigInputSchema } from '../node_modules/@openai/codex-security/dist/project-config-schema.js';

test('cs project config uses the cost-safe deep-scan defaults', () => {
  const source = fs.readFileSync('.codex/codex-security.yaml', 'utf8');
  const parsed = ProjectConfigInputSchema.safeParse(parse(source));
  assert.equal(parsed.success, true, parsed.success ? '' : JSON.stringify(parsed.error.issues));
  assert.deepEqual(parsed.data.scan?.deep, {
    workers: 2,
    subagents_per_worker: 1,
    stop_after_no_new: 3,
    stop_after_consecutive_errors: 2,
    max_discovery_runs: 10,
    max_time_hours: 1.5,
  });
});
