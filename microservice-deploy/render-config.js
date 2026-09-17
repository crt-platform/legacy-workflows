#!/usr/bin/env node
/*
 * render-config.js — runs ON THE BOX, never on the runner.
 *
 * Reads heimdall/<service>/prod with the instance role, substitutes
 * {{PLACEHOLDER}}s into app*.settings.js, and moves every config file into
 * /deployment/<service>/.
 *
 * Deliberately identical to Heimdall's behaviour
 * (deployment-execute-deploy.service.ts:505-545), with ONE deliberate
 * divergence, marked below:
 *   - unresolved placeholders SURVIVE (heimdall/ecom.catalog/prod has 6 keys
 *     for 8 placeholders and the live box genuinely contains '{{sqlServer...}}')
 *   - substitution is textual, before any parsing
 *   - files are staged then moved, and nothing is ever deleted
 *
 * DIVERGENCE FROM HEIMDALL: `appsettings*.json` is templated too.
 * Heimdall templates only /^app.*\.settings\.js$/, so `appsettings.production.json`
 * — which does NOT match — would be copied byte for byte and any {{placeholder}}
 * in it would land literally, breaking the service. That file is the 4th config
 * layer (`appsettings.${MODE}.json` beats app.settings.js), it carries real
 * credentials, and it is NOT in the service repo or the build artifact: on
 * ms-jobs it was hand-made and is gitignored. Templating it is what lets
 * ms-deploy own it the way it owns jobs.json.
 *
 * Secret VALUES are never printed — only key names and counts.
 *
 * usage: node render-config.js <secret-id> <region> <tmp-dir> <dest-dir>
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const [secretId, region, tmpDir, destDir] = process.argv.slice(2);
if (!secretId || !region || !tmpDir || !destDir) {
  console.error('usage: render-config.js <secret-id> <region> <tmp-dir> <dest-dir>');
  process.exit(2);
}

const SETTINGS = /^app.*\.settings\.js$/;           // Heimdall's own rule
const APPSETTINGS = /^appsettings.*\.json$/;        // the divergence (see header)
const isTemplated = (name) => SETTINGS.test(name) || APPSETTINGS.test(name);

let secret = {};
try {
  const raw = execFileSync('aws', [
    'secretsmanager', 'get-secret-value',
    '--secret-id', secretId,
    '--region', region,
    '--query', 'SecretString',
    '--output', 'text',
  ], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  secret = JSON.parse(raw);
} catch (err) {
  // Never echo err.stdout: on some failures the CLI includes the secret.
  console.error(`FATAL: could not read ${secretId} from Secrets Manager (${err.status ?? 'error'})`);
  console.error('The instance role needs secretsmanager:GetSecretValue on heimdall/*.');
  process.exit(1);
}

const files = fs.readdirSync(tmpDir).filter((f) => !f.startsWith('.'));
if (!files.some((f) => SETTINGS.test(f))) {
  console.error(`FATAL: no app*.settings.js in ${tmpDir} — refusing to deploy a service with no config`);
  process.exit(1);
}

const unresolved = new Set();
let templated = 0;
let copied = 0;

for (const name of files) {
  const src = path.join(tmpDir, name);
  if (!fs.statSync(src).isFile()) continue;

  if (isTemplated(name)) {
    let text = fs.readFileSync(src, 'utf8');
    for (const [key, value] of Object.entries(secret)) {
      text = text.split(`{{${key}}}`).join(String(value));
    }
    for (const m of text.matchAll(/\{\{([^}]+)\}\}/g)) unresolved.add(m[1]);
    // An appsettings*.json left holding a placeholder is not a survivable state
    // the way a settings.js one is: it is parsed as JSON at boot and the value
    // would be used as a literal credential. Fail here instead of on the box.
    if (APPSETTINGS.test(name) && /\{\{[^}]+\}\}/.test(text)) {
      console.error(`FATAL: ${name} still holds unresolved placeholders after substitution.`);
      console.error('Add the missing keys to the service secret before deploying.');
      process.exit(1);
    }
    fs.writeFileSync(src, text, { mode: 0o600 });
    templated += 1;
  } else {
    copied += 1;
  }
  fs.renameSync(src, path.join(destDir, name));
}

const bits = [`templated ${templated}`, `copied ${copied}`, `secret keys ${Object.keys(secret).length}`];
if (unresolved.size) bits.push(`unresolved placeholders: ${[...unresolved].sort().join(',')}`);
console.log(bits.join(', '));
