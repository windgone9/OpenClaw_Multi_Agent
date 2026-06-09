import re

with open("/Users/yangxu/MyWork/OpenClaw_Multi_Agent/static/dashboard.html", "r") as f:
    html = f.read()

m = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
if not m:
    print("No script found!")
else:
    js = m.group(1)
    # Check for common issues
    print(f"JS length: {len(js)} chars")

    # Check if startService function exists
    if "async function startService" in js:
        print("startService: FOUND")
    else:
        print("startService: MISSING!")

    # Check if stopService function exists
    if "async function stopService" in js:
        print("stopService: FOUND")
    else:
        print("stopService: MISSING!")

    # Check Hermes API endpoint
    if "H+'/services/start'" in js:
        print("Hermes start API: FOUND")
    else:
        print("Hermes start API: MISSING!")

    # Check for the SVC_DEFS with bridge startSvc
    if "startSvc:'bridge'" in js:
        print("Bridge startSvc: FOUND")
    else:
        print("Bridge startSvc: MISSING!")

    # Check for managedStatus fetch
    if "H+'/services/status'" in js:
        print("Hermes status API: FOUND")
    else:
        print("Hermes status API: MISSING!")

    # Check for AbortSignal.timeout
    timeout_count = js.count("AbortSignal.timeout")
    print(f"AbortSignal.timeout count: {timeout_count}")

    # Check for balanced braces
    open_braces = js.count('{')
    close_braces = js.count('}')
    print(f"Braces: {{ = {open_braces}, }} = {close_braces}, balanced = {open_braces == close_braces}")

    # Check for balanced parens
    open_parens = js.count('(')
    close_parens = js.count(')')
    print(f"Parens: ( = {open_parens}, ) = {close_parens}, balanced = {open_parens == close_parens}")
