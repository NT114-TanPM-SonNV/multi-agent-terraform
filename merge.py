"""merge.py — gộp nhiều file kết quả (eval shard) thành 1, sort theo row.

Robust hơn one-liner: dedup row trùng (giữ bản sau cùng), cảnh báo nếu thiếu row,
ghi UTF-8 (không crash cp1252 trên Windows).

Chạy:
    python merge.py reviews/eval_c.json reviews/eval_d.json --out reviews/run2.json
    python merge.py reviews/eval_a.json reviews/eval_b.json --out reviews/run1.json
"""
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="Gộp nhiều file result JSON thành 1 (sort theo row)")
    ap.add_argument("inputs", nargs="+", help="Các file JSON cần gộp")
    ap.add_argument("--out", required=True, help="File output")
    args = ap.parse_args()

    by_row: dict[int, dict] = {}
    dups: list[int] = []
    total = 0
    for p in args.inputs:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit(f"{p}: không phải list JSON")
        for r in data:
            if not isinstance(r, dict) or not isinstance(r.get("row"), int):
                continue
            row = r["row"]
            if row in by_row:
                dups.append(row)
            by_row[row] = r          # row trùng → giữ bản sau cùng
            total += 1

    merged = [by_row[k] for k in sorted(by_row)]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    n_pass = sum(1 for r in merged if (r.get("deployment") or {}).get("ok"))
    print(f"merged {len(args.inputs)} file | {total} record -> {len(merged)} row "
          f"| {n_pass} deploy-pass -> {out}")
    if dups:
        print(f"[warn] row trùng (giữ bản sau cùng): {sorted(set(dups))}")
    if merged:
        rows = {r["row"] for r in merged}
        missing = [i for i in range(max(rows) + 1) if i not in rows]
        if missing:
            print(f"[warn] thiếu row: {missing}")


if __name__ == "__main__":
    main()
