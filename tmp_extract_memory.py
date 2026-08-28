import json
from pathlib import Path

path = Path(r"C:\Users\QYC\.cursor\projects\c-Users-QYC-Documents-AMR-Agent\agent-transcripts\0a849874-325b-45a2-9ac9-e0ebbe37372e\0a849874-325b-45a2-9ac9-e0ebbe37372e.jsonl")
out = Path(r"C:\Users\QYC\Documents\AMR_Agent\tmp_memory_qa.txt")
chunks: list[str] = []
needles = (
    "价值阈值",
    "冲突策略",
    "作用域",
    "长期记忆",
    "Episodic",
    "RAG 就是 Agent",
    "跨任务",
    "a14",
    "memory",
    "Memory",
)

with path.open("r", encoding="utf-8") as handle:
    for i, line in enumerate(handle, 1):
        obj = json.loads(line)
        text = json.dumps(obj, ensure_ascii=False)
        hits = [n for n in needles if n in text]
        if not hits:
            continue
        for n in hits:
            idx = text.find(n)
            if idx < 0:
                continue
            start = max(0, idx - 600)
            end = min(len(text), idx + 2500)
            chunks.append(f"===== LINE {i} needle={n} =====\n{text[start:end]}")

out.write_text("\n\n".join(chunks), encoding="utf-8")
print("chunks", len(chunks), "bytes", out.stat().st_size)
