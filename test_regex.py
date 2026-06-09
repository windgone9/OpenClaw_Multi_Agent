#!/usr/bin/env python3
import re
content = open('/Users/yangxu/.hermes/memories/MEMORY.md').read()

patternMatch = re.search(r'## Routing Patterns Learned\n([\s\S]*?)(?=\n## )', content)
keyRulesMatch = re.search(r'## Key Rules\n([\s\S]*?)(?=\n## )', content)
latencyMatch = re.search(r'## Latency Stats[\s\S]*?\n([\s\S]*?)(?=\n## |\n*$)', content)

print('Pattern match:', 'OK' if patternMatch else 'FAIL')
if patternMatch:
    lines = [l for l in patternMatch[1].strip().split('\n') if l.startswith('-')]
    print('  Lines:', len(lines))
    for l in lines[:3]:
        m = re.match(r'^- (.+?)\s*\[(.+?)\]\s*→\s*(\w+)', l)
        print('  ', 'MATCH' if m else 'NO_MATCH', l[:80])

print('KeyRules match:', 'OK' if keyRulesMatch else 'FAIL')
if keyRulesMatch:
    lines = [l for l in keyRulesMatch[1].strip().split('\n') if l.startswith('-')]
    print('  Lines:', len(lines))

print('Latency match:', 'OK' if latencyMatch else 'FAIL')
if latencyMatch:
    lines = [l for l in latencyMatch[1].strip().split('\n') if l.startswith('-')]
    print('  Lines:', len(lines))
    for l in lines:
        m = re.match(r'^- (\w+): avg=(\d+)ms, p95=(\d+)ms, samples=(\d+)', l)
        print('  ', 'MATCH' if m else 'NO_MATCH', l[:80])
