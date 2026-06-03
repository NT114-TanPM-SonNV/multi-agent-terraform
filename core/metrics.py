"""Định nghĩa & tính toán metric đánh giá — tách bạch, có thống kê.

Khắc phục các điểm yếu phương pháp luận:
  1. Phân tách 2 nhóm metric:
       • Deterministic (tái lập được, không tốn tiền): plan_valid, semantic_correct, security_score
       • Environment-dependent (phụ thuộc account AWS): deploy_success
  2. pass@k CHUẨN (ước lượng không chệch của Codex/HumanEval) trên n run độc lập —
     KHÔNG nhầm với "resolved within k iterations" (số vòng retry trong 1 lần chạy).
  3. Mean ± std + CI95 khi có nhiều run.
  4. Breakdown theo Difficulty.

Thuật ngữ (đã đổi tên để không lạm dụng ký hiệu pass@k):
  resolved@≤k  — tỉ lệ prompt mà pipeline tự giải quyết trong ≤ k vòng iteration
                 (đặc trưng của hệ multi-agent có retry; metric NỘI BỘ 1 lần chạy).
  pass@k       — ước lượng xác suất ≥1 trong k mẫu độc lập đạt, từ n run (CHUẨN).
"""
import math
import random
from statistics import mean, stdev

# Giá trị tới hạn t (two-sided 95%) theo bậc tự do df=n-1. Cho n NHỎ (eval thường
# 3-5 run), normal-approx z=1.96 cho CI HẸP GIẢ TẠO → dùng t. df>=30 ≈ 1.96.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 25: 2.060}


def _t_crit(df: int) -> float:
    if df <= 0:
        return 0.0
    if df in _T95:
        return _T95[df]
    if df >= 30:
        return 1.96
    # nội suy thô giữa các mốc gần nhất
    keys = sorted(_T95)
    lo = max(k for k in keys if k <= df)
    return _T95[lo]


def pass_at_k(n: int, c: int, k: int) -> float:
    """Ước lượng không chệch pass@k (Chen et al., 2021 / HumanEval).

    n: tổng số mẫu độc lập; c: số mẫu đạt; k: ngân sách mẫu.
    pass@k = 1 - C(n-c, k) / C(n, k)
    """
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def aggregate(values: list[float]) -> dict:
    """Mean ± sample-std + CI95 (t-distribution) cho tỉ lệ trên nhiều run.

    Dùng t thay z=1.96 vì số run nhỏ (n=3-5); pstdev→stdev (sample, unbiased).
    """
    if not values:
        return {"mean": 0.0, "std": 0.0, "n": 0, "ci95": 0.0}
    n = len(values)
    m = mean(values)
    sd = stdev(values) if n > 1 else 0.0
    ci95 = _t_crit(n - 1) * sd / math.sqrt(n) if n > 1 else 0.0
    return {"mean": round(m, 4), "std": round(sd, 4), "n": n,
            "ci95": round(ci95, 4)}


def bootstrap_ci(success: list[bool], resamples: int = 1000,
                 seed: int = 42) -> dict:
    """Bootstrap CI95 cho tỉ lệ success ở mức TASK (giống MACOG: resample tasks).

    Robust hơn t-CI khi phân phối lệch / n task vừa phải. success = list bool
    per-task (đã gộp qua các run, vd success nếu pass ở BẤT KỲ run nào, hoặc
    trung bình). Trả mean + CI95 [lo, hi].
    """
    if not success:
        return {"mean": 0.0, "ci95_lo": 0.0, "ci95_hi": 0.0, "n": 0}
    rng = random.Random(seed)
    n = len(success)
    base = sum(success) / n
    means = []
    for _ in range(resamples):
        s = sum(success[rng.randrange(n)] for _ in range(n)) / n
        means.append(s)
    means.sort()
    lo = means[int(0.025 * resamples)]
    hi = means[int(0.975 * resamples)]
    return {"mean": round(base, 4), "ci95_lo": round(lo, 4),
            "ci95_hi": round(hi, 4), "n": n}


def rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


# ── Metric definitions (dùng để in legend trong report) ───────────────────────
METRIC_DEFS = {
    "plan_valid":       "Deterministic: terraform validate + plan đều pass (config hợp lệ, deployable).",
    "semantic_correct": "Deterministic: gold Rego policy thỏa (cấu hình ĐÚNG intent), điều kiện plan_valid.",
    "security_score":   "DESCRIPTIVE posture: tỉ lệ pass khi quét TOÀN BỘ Checkov rules. KHÔNG phải target tối ưu — hệ thiết kế PROPORTIONAL (cố ý không enforce hết), nên 'cao hơn' KHÔNG đồng nghĩa 'tốt hơn'. Dùng để chứng minh KHÔNG regress vs baseline, không phải để maximize.",
    "security_enforced": "Proportional follow-through: trong số check A2 CHỌN, tỉ lệ checkov THẬT SỰ pass (skip/fail trừ điểm). Đo đúng mục tiêu pipeline (A2 chọn hợp lý + A3 thỏa + checkov chạy thật) mà KHÔNG thưởng maximal-hardening.",
    "phantom_rate":     "Tỉ lệ check A2 chọn bị checkov SKIP (không enforce thật = phantom). Mục tiêu ≈0.",
    "deploy_success":   "Environment-dependent: terraform apply lên AWS thật thành công (phụ thuộc quota/naming).",
    "resolved_at_k":    "Internal: tỉ lệ prompt pipeline tự giải quyết trong ≤ k vòng iteration (retry nội bộ).",
    "pass_at_k":        "Standard: ước lượng pass@k trên các run độc lập (chỉ tính khi có ≥2 run).",
}
