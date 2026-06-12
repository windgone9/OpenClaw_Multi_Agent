# Routing Decision Memory

## Routing Patterns Learned
- 单步问答/简单聊天 [你好,hello,hi,谢谢,再见,thanks,bye,天气,怎么样] → direct_local (本地Ollama/vLLM, avg~6s)
- 数学/计算 [计算,等于,加,减,乘,除,数学] → direct_local (本地Ollama/vLLM, avg~6s)
- 翻译请求 [翻译,translate,translation] → direct_local (本地Ollama/vLLM, avg~6s)
- 多步批处理/Volcano任务 [计划,规划,多步,批处理,批量,方案,plan,自主,执行,设计,架构,调研,Volcano,volcano,工作流,pipeline] → gateway (OfficialGW→Agent→Volcano, avg~60s)
- 代码执行/编程 [代码,code,执行,脚本,程序,python,javascript,编程] → gateway (OfficialGW→Agent→Volcano, avg~60s)
- 多模态/图片/音频 [图片, 图像, 照片, 截图, OCR, 识别图片, 视觉, 音频, 语音, 录音, 视频, image, photo, vision, audio, video, multimodal, 识别] → multimodal (多模态专用模型)
- 隐私敏感数据 [隐私,敏感,个人信息,医疗,本地文件,private,sensitive,身份证,银行卡,脱敏] → local_inference (本地Ollama/vLLM, 隐私保护)
- 一般问答/信息查询 [什么是,解释,比较,区别,how,what,为什么] → direct_local (本地Ollama/vLLM, avg~6s)
- 数据库查询/SQL [sql,数据库,查询,query,select,insert,table] → gateway (OfficialGW→Agent→Volcano)
- 部署/DevOps [部署,deploy,发布,release,上线,微服务,分布式] → gateway (OfficialGW→Agent→Volcano)
- 测试/QA [测试,test,qa,验证,单元测试] → gateway (OfficialGW→Agent→Volcano)
- 监控/可观测性 [监控,monitor,日志,log,指标] → direct_local (本地Ollama/vLLM)
- 文档 [文档,doc,文档生成] → direct_local (本地Ollama/vLLM)

## Key Rules
- require_local=true → always local_inference (本地Ollama/vLLM, 隐私保护)
- type=code/code_execution/tool_call → always gateway (OfficialGW→Agent→Volcano)
- has_tools=true → always gateway (OfficialGW→Agent→Volcano)
- 多模态关键词(图片/音频/视频/OCR/视觉) → multimodal (多模态专用模型)
- 多步批处理关键词(多步骤/批处理/Volcano/工作流/部署) → gateway (OfficialGW→Agent→Volcano)
- 简单单步问答/聊天/翻译/计算 → direct_local (本地Ollama/vLLM, 低延迟)
- direct_local → 本地Ollama/vLLM直连 (bypass Bridge, avg~6s)
- gateway → Bridge → OfficialGW → Agent → Volcano (avg~60-90s)
- multimodal → Bridge → 多模态专用模型 (图片/音频/视频处理)
- local_inference → 本地Ollama/vLLM直连 (隐私约束, bypass Bridge)
- gateway延迟avg~84s, 简单请求优先用direct_local(avg~7s)
- gateway延迟avg~70s, 简单请求优先用direct_local(avg~6s)

## Latency Stats (auto-updated)
- direct_local: avg=41314ms, p95=390320ms, samples=204 (Hermes路由实测)
- gateway: avg=91855ms, p95=237519ms, samples=198 (Hermes路由实测)
- local_inference: avg=44853ms, p95=290799ms, samples=58 (Hermes路由实测)
- multimodal: avg=52481ms, p95=298884ms, samples=86 (Hermes路由实测)

## Feedback History
- multimodal ✓ 26779ms '识别图片中的物体并分类'
- multimodal ✓ 12134ms '将这段音频转换为文字'
- multimodal ✓ 20520ms '分析视频中的关键帧'
- local_inference ✓ 21320ms '分析这份内部财务数据'
- local_inference ✓ 24746ms '处理用户个人信息并脱敏'
- local_inference ✓ 29863ms '分析医疗影像诊断报告'
