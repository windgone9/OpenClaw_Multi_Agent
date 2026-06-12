import httpx
import time
import threading

H = "http://localhost:8082"
BATCH = [
    {"type": "chat", "prompt": "你好，今天天气怎么样？"},
    {"type": "chat", "prompt": "1+1等于几？"},
    {"type": "chat", "prompt": "翻译：hello world"},
    {"type": "chat", "prompt": "什么是AI？"},
    {"type": "chat", "prompt": "请解释量子计算的基本原理"},
    {"type": "code", "prompt": "执行Python脚本计算斐波那契数列"},
    {"type": "code", "prompt": "写一个JavaScript函数实现快速排序"},
    {"type": "code", "prompt": "调试这段SQL查询语句的性能问题"},
    {"type": "chat", "prompt": "请规划一个CI/CD部署流程"},
    {"type": "chat", "prompt": "使用Volcano调度分布式训练任务"},
    {"type": "chat", "prompt": "请分析这张图片中的文字内容"},
    {"type": "chat", "prompt": "描述这张照片中的场景"},
    {"type": "chat", "prompt": "识别图片中的物体并分类"},
    {"type": "chat", "prompt": "将这段音频转换为文字"},
    {"type": "chat", "prompt": "分析视频中的关键帧"},
    {"type": "chat", "prompt": "分析这份内部财务数据"},
    {"type": "chat", "prompt": "处理用户个人信息并脱敏"},
    {"type": "chat", "prompt": "分析医疗影像诊断报告"},
]

results = []
lock = threading.Lock()
health_checks = []

def check_health_periodically():
    start = time.time()
    while time.time() - start < 300:
        try:
            r = httpx.get(f"{H}/proxy/health", timeout=5)
            data = r.json()
            agent = data.get("hermesAgent", {})
            status = "OK" if agent.get("healthy") else "FAIL"
            agent_data = agent.get("data", {})
            detail = agent_data.get("status", "unknown")
            pid = agent_data.get("pid", "-")
            with lock:
                health_checks.append(f"[{time.strftime('%H:%M:%S')}] Agent health: {status} (status={detail}, pid={pid})")
        except Exception as e:
            with lock:
                health_checks.append(f"[{time.strftime('%H:%M:%S')}] Health check error: {e}")
        time.sleep(10)

def submit_req(idx, req):
    start = time.time()
    try:
        r = httpx.post(f"{H}/queue/submit-sync", json={
            "appid": "batch-test",
            "type": req["type"],
            "prompt": req["prompt"],
            "priority": 3,
        }, timeout=500, params={"timeout": 400})
        elapsed = int((time.time() - start) * 1000)
        data = r.json()
        status = data.get("status", "unknown")
        route = data.get("routing", {}).get("route_path", "-")
        with lock:
            results.append((idx, status, route, elapsed))
            print(f"[q-{idx+1}] {status.upper():3s} path={route:15s} latency={elapsed}ms", flush=True)
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        with lock:
            results.append((idx, "error", "-", elapsed))
            print(f"[q-{idx+1}] ERR path=- latency={elapsed}ms ({e})", flush=True)

ht = threading.Thread(target=check_health_periodically, daemon=True)
ht.start()

threads = []
for i, req in enumerate(BATCH):
    t = threading.Thread(target=submit_req, args=(i, req))
    threads.append(t)
    t.start()
    time.sleep(2)

for t in threads:
    t.join(timeout=420)

print("\n=== Health Check Log During Test ===")
for h in health_checks:
    print(h)

print(f"\n=== Summary: {len(results)}/{len(BATCH)} completed ===")
ok = sum(1 for r in results if r[1] == "success")
err = sum(1 for r in results if r[1] != "success")
print(f"Success: {ok}, Failed: {err}")
