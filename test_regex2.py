#!/usr/bin/env python3
"""Test Dashboard regex patterns in JavaScript-compatible way"""
import re

test_lines = [
    '- Simple chat/greetings [你好,hello,hi,谢谢,再见,thanks,bye,天气,怎么样] → direct_local (本地Ollama/vLLM, avg~6s)',
    '- Math/calculation [计算,等于,加,减,乘,除,数学] → direct_local (本地Ollama/vLLM, avg~6s)',
    '- Code execution/programming [代码,code,执行,脚本,程序,python,javascript,编程] → gateway (OfficialGW→openclaw/main, avg~60s)',
    '- Privacy-sensitive data [隐私,敏感,个人信息,医疗,本地文件,private,sensitive,身份证,银行卡,脱敏] → local_inference (本地Ollama/vLLM, 隐私保护)',
]

# This is the regex from Dashboard JS
pattern = r'^- (.+?)\s*\[(.+?)\]\s*→\s*(\w+)'

for line in test_lines:
    m = re.match(pattern, line)
    if m:
        print(f'MATCH: cat="{m.group(1)}", kws="{m.group(2)[:30]}", route="{m.group(3)}"')
    else:
        print(f'NO_MATCH: {line[:80]}')
