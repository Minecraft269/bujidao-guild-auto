# vulture 白名单 —— 这些"未使用"是由外部 API 强制的，不是死代码。
# 用法见 .github/workflows/vulture.yml

# signal.signal(handler) 要求 handler 形参为 (signum, frame)，
# 即使函数体不用它们也必须保留，否则 TypeError。
sig = frame = None
