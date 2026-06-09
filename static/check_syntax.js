const fs = require('fs');
const html = fs.readFileSync('/Users/yangxu/MyWork/OpenClaw_Multi_Agent/static/dashboard.html', 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);
if (!scriptMatch) { console.log('No script found'); process.exit(1); }
try {
  new Function(scriptMatch[1]);
  console.log('JS syntax OK');
} catch(e) {
  console.log('JS syntax ERROR:', e.message);
}
