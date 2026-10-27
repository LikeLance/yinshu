"""多进程压测：验证审计链在 `--prod` 多 worker 下不会分叉。

用法：
    python -m tests.stress_audit_multiproc          # 默认 3 进程 × 40 条
    python -m tests.stress_audit_multiproc 4 100    # 4 进程 × 100 条

为什么不放进 pytest：它要 spawn 新解释器，CI 上跑得慢且容易受资源限制；
这是一把一次性的"验枪"工具，改动审计模块后手动跑一次即可。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile


def _work(args: tuple[int, int]) -> int:
    idx, n = args
    from backend.security import audit          # 子进程里按各自线程连接写

    for j in range(n):
        audit.append(f"mp-{idx}-{j}", "multiproc_probe", {"i": idx, "j": j}, "ok")
    return idx


def main() -> int:
    procs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    data_dir = tempfile.mkdtemp(prefix="yinshu_mp_")
    os.environ["YINSHU_DATA_DIR"] = data_dir
    print(f"[mp] data_dir={data_dir}  processes={procs}  each={per}")

    ctx = mp.get_context("spawn")
    with ctx.Pool(procs) as pool:
        pool.map(_work, [(i, per) for i in range(procs)])

    from backend.core.config import APP_DB
    from backend.security import audit
    import sqlite3

    audit.close()
    v = audit.verify(full=True)
    c = sqlite3.connect(str(APP_DB))
    dup = c.execute(
        "SELECT COUNT(*) FROM (SELECT prev_hash FROM audit_logs GROUP BY prev_hash"
        " HAVING COUNT(*) > 1)").fetchone()[0]
    c.close()

    print(f"[mp] verify={v}")
    print(f"[mp] 分叉的 prev_hash 组数={dup}（必须为 0）")
    ok = v["ok"] and dup == 0 and v["total"] == procs * per
    print("[mp]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
