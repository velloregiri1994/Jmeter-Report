#!/usr/bin/env python3
"""
JMeter HTML Performance Dashboard (Tabs, Filters, Advanced Analysis) - v1.3
--------------------------------------------------------------------

Features:
- Dashboard tab (summary, transaction SLA view, transaction stats, insights)
- Graphs tab (time-series charts, time window filter, insights)
- Error tab (error summary + transaction-level error details + insights with probable causes)
- Comparison tab (baseline vs current + regression-focused insights)
- Overall Insights & Recommendations at the top (combined view)
- Time Window Filter (presets + custom date/time range) – applies to graphs
- SLA-based transaction view using configurable SLA JSON (override from CLI)
- Transaction Statistics with avg, percentiles, throughput, hits/sec, error%
- NEW (v1.3): Error tab includes Transaction + Error Message breakdown

Usage:
    python jmeter_report_v1_3.py \
        --input results.csv \
        --output report.html \
        --test-name "My Load Test" \
        --environment "UAT" \
        --apdex-threshold 1.5 \
        --baseline baseline_results.csv \
        --sla-config-file sla.json

or

    python jmeter_report_v1_3.py \
        --input results.csv \
        --output report.html \
        --sla-config-json '{"*":{"p95_ms":2500,"error_rate":0.01}}'
"""

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional

# =========================
# DEFAULT SLA CONFIGURATION
# =========================

DEFAULT_SLA_CONFIG = {
    "*": {
        "p95_ms": 3000.0,  # 95% of requests under 3 seconds
        "error_rate": 0.02  # <= 2% errors
        # Optionally add "avg_ms": 2000.0, "tps_min": 5.0 per label
    },
}


# =========================
# DATA MODELS
# =========================

@dataclass
class Sample:
    timestamp_ms: int
    elapsed_ms: float
    label: str
    response_code: str
    success: bool
    all_threads: Optional[int] = None
    response_message: str = ""  # NEW: capture error message/responseMessage


@dataclass
class LabelStats:
    label: str
    count: int = 0
    success_count: int = 0
    error_count: int = 0
    elapsed_values_ms: List[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.count if self.count else 0.0

    def to_summary(self) -> Dict[str, Any]:
        if not self.elapsed_values_ms:
            return {
                "label": self.label,
                "count": self.count,
                "min": None,
                "max": None,
                "avg": None,
                "p50": None,
                "p90": None,
                "p95": None,
                "p99": None,
                "error_rate": self.error_rate,
            }
        values = sorted(self.elapsed_values_ms)

        def pct(p: float) -> float:
            return percentile(values, p)

        return {
            "label": self.label,
            "count": self.count,
            "min": min(values),
            "max": max(values),
            "avg": statistics.mean(values),
            "p50": pct(50),
            "p90": pct(90),
            "p95": pct(95),
            "p99": pct(99),
            "error_rate": self.error_rate,
        }


# =========================
# UTILS
# =========================

def percentile(sorted_values: List[float], p: float) -> float:
    """Compute percentile p (0-100) for a sorted list using linear interpolation."""
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def epoch_ms_to_sec_bucket(ts_ms: int) -> int:
    return int(ts_ms // 1000)


def format_ms(ms: Optional[float]) -> str:
    if ms is None or ms != ms:  # NaN check
        return "-"
    return f"{ms:.1f}"


def format_pct(p: Optional[float]) -> str:
    if p is None or p != p:
        return "-"
    return f"{p * 100:.2f}%"


def compute_apdex(values_ms: List[float], threshold_s: float) -> float:
    """
    APDEX: (Satisfied + Tolerating/2) / Total
    Satisfied: t <= T
    Tolerating: T < t <= 4T
    """
    if not values_ms:
        return float("nan")
    T_ms = threshold_s * 1000.0
    satisfied = sum(1 for v in values_ms if v <= T_ms)
    tolerating = sum(1 for v in values_ms if T_ms < v <= 4 * T_ms)
    total = len(values_ms)
    return (satisfied + tolerating / 2.0) / total if total else float("nan")


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}h {m}m {s}s"


# =========================
# PARSING
# =========================

def parse_jmeter_csv(path: str) -> List[Sample]:
    samples: List[Sample] = []
    with open(path, "r", newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row.get("timeStamp") or row.get("timestamp") or 0)
            except ValueError:
                continue
            try:
                elapsed = float(row.get("elapsed") or 0)
            except ValueError:
                elapsed = 0.0

            label = row.get("label", "UNKNOWN")
            rc = str(row.get("responseCode", "")).strip()

            success_raw = str(row.get("success", "true")).strip().lower()
            success = success_raw in ("true", "1", "yes", "y")

            all_threads = None
            at = row.get("allThreads") or row.get("all_threads")
            if at is not None and at != "":
                try:
                    all_threads = int(at)
                except ValueError:
                    all_threads = None

            # NEW: best-effort capture of error/response message
            response_message = (
                    row.get("responseMessage")
                    or row.get("failureMessage")
                    or ""
            )

            samples.append(Sample(
                timestamp_ms=ts,
                elapsed_ms=elapsed,
                label=label,
                response_code=rc,
                success=success,
                all_threads=all_threads,
                response_message=response_message,
            ))
    return samples


# =========================
# METRIC COMPUTATION
# =========================


def extract_transaction_names(samples):
    """
    Return Transaction Controller names found in samples.
    We consider a sample to be a transaction controller parent sample if its
    response_message contains the text "Number of samples" (case-insensitive).
    This function always returns a list (may be empty).
    """
    txn_set = set()
    for s in samples:
        try:
            msg = getattr(s, "response_message", None)
            if msg and isinstance(msg, str) and "number of samples" in msg.lower():
                txn_set.add(s.label)
        except Exception:
            continue
    return sorted(txn_set)


def compute_metrics(samples: List[Sample], apdex_threshold_s: float) -> Dict[str, Any]:
    if not samples:
        return {}

    timestamps = [s.timestamp_ms for s in samples]
    min_ts = min(timestamps)
    max_ts = max(timestamps)
    duration_sec = max(1.0, (max_ts - min_ts) / 1000.0)

    elapsed_all = [s.elapsed_ms for s in samples]
    sorted_elapsed = sorted(elapsed_all)

    total = len(samples)
    errors = sum(1 for s in samples if not s.success)
    error_rate = errors / total if total else 0.0

    def pct(p: float) -> float:
        return percentile(sorted_elapsed, p)

    global_stats = {
        "total_requests": total,
        "duration_sec": duration_sec,
        "avg_ms": statistics.mean(sorted_elapsed),
        "min_ms": min(sorted_elapsed),
        "max_ms": max(sorted_elapsed),
        "p50_ms": pct(50),
        "p90_ms": pct(90),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "error_rate": error_rate,
        "errors": errors,
        "successes": total - errors,
        "apdex": compute_apdex(sorted_elapsed, apdex_threshold_s),
    }

    # Per-label stats
    per_label: Dict[str, LabelStats] = {}
    for s in samples:
        if s.label not in per_label:
            per_label[s.label] = LabelStats(label=s.label)
        ls = per_label[s.label]
        ls.count += 1
        if s.success:
            ls.success_count += 1
        else:
            ls.error_count += 1
        ls.elapsed_values_ms.append(s.elapsed_ms)

    per_label_summaries = [ls.to_summary() for ls in per_label.values()]
    per_label_summaries.sort(key=lambda x: x["label"])

    # Time series (bucketed per second)
    buckets: Dict[int, List[Sample]] = defaultdict(list)
    for s in samples:
        buckets[epoch_ms_to_sec_bucket(s.timestamp_ms)].append(s)

    timeseries = []
    for sec_bucket, bucket_samples in sorted(buckets.items()):
        rps = len(bucket_samples)
        avg_rt = statistics.mean([b.elapsed_ms for b in bucket_samples])
        active_threads = bucket_samples[0].all_threads if bucket_samples[0].all_threads is not None else None
        errors_sec = sum(1 for b in bucket_samples if not b.success)  # errors per second

        timeseries.append({
            "sec": sec_bucket,
            "rps": rps,
            "avg_ms": avg_rt,
            "active_threads": active_threads,
            "errors": errors_sec,
        })

    peak_rps = max(ts["rps"] for ts in timeseries) if timeseries else 0

    # Error summary (per response code)
    error_counter = Counter()
    # NEW: detailed error counter per (label, response_code, response_message)
    error_details_counter = Counter()
    for s in samples:
        if not s.success:
            code = s.response_code or "UNKNOWN"
            error_counter[code] += 1
            key = (s.label, code, s.response_message or "")
            error_details_counter[key] += 1

    error_summary = []
    for code, count in error_counter.most_common():
        error_summary.append({
            "response_code": code,
            "count": count,
            "pct": count / errors if errors else 0.0,
        })

    # NEW: error details list
    error_details = []
    for (label, code, msg), count in error_details_counter.most_common():
        error_details.append({
            "label": label,
            "response_code": code,
            "response_message": msg,
            "count": count,
            "pct": count / errors if errors else 0.0,
        })

    metrics = {
        "global": global_stats,
        "per_label": per_label_summaries,
        "timeseries": timeseries,
        "peak_rps": peak_rps,
        "error_summary": error_summary,
        "error_details": error_details,  # NEW
        "start_time": min_ts,
        "end_time": max_ts,
    }
    return metrics


# =========================
# SLA EVALUATION
# =========================

def get_label_sla(label: str, sla_config: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    if label in sla_config:
        base = dict(sla_config.get("*", {}))
        base.update(sla_config[label])
        return base
    return sla_config.get("*", {})


def evaluate_sla(metrics: Dict[str, Any],
                 sla_config: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    per_label = metrics.get("per_label", [])
    duration_sec = metrics.get("global", {}).get("duration_sec", 0.0)
    rows = []

    for ls in per_label:
        label = ls["label"]
        sla = get_label_sla(label, sla_config)
        if not sla:
            continue

        row = {"label": label, "checks": []}

        if "p95_ms" in sla and ls["p95"] is not None:
            target = sla["p95_ms"]
            actual = ls["p95"]
            status = actual <= target
            row["checks"].append({
                "metric": "P95",
                "target": target,
                "actual": actual,
                "status": status,
            })

        if "avg_ms" in sla and ls["avg"] is not None:
            target = sla["avg_ms"]
            actual = ls["avg"]
            status = actual <= target
            row["checks"].append({
                "metric": "Avg",
                "target": target,
                "actual": actual,
                "status": status,
            })

        if "error_rate" in sla:
            target = sla["error_rate"]
            actual = ls["error_rate"]
            status = actual <= target
            row["checks"].append({
                "metric": "Error Rate",
                "target": target,
                "actual": actual,
                "status": status,
            })

        # Optional TPS SLA
        if "tps_min" in sla and duration_sec > 0:
            target = sla["tps_min"]
            actual = ls["count"] / duration_sec
            status = actual >= target
            row["checks"].append({
                "metric": "TPS",
                "target": target,
                "actual": actual,
                "status": status,
            })

        if row["checks"]:
            rows.append(row)

    total_checks = sum(len(r["checks"]) for r in rows)
    passed_checks = sum(1 for r in rows for c in r["checks"] if c["status"])
    overall_status = (passed_checks == total_checks) if total_checks else True
    overall_pct = (passed_checks / total_checks) if total_checks else 1.0

    return {
        "rows": rows,
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "overall_status": overall_status,
        "overall_pct": overall_pct,
    }


# =========================
# BASELINE COMPARISON
# =========================

def compare_baseline(current: Dict[str, Any],
                     baseline: Dict[str, Any]) -> Dict[str, Any]:
    cur_map = {x["label"]: x for x in current.get("per_label", [])}
    base_map = {x["label"]: x for x in baseline.get("per_label", [])}
    labels = sorted(set(cur_map.keys()) | set(base_map.keys()))
    rows = []
    for label in labels:
        cur = cur_map.get(label)
        base = base_map.get(label)
        rows.append({
            "label": label,
            "baseline_p95": base["p95"] if base else None,
            "current_p95": cur["p95"] if cur else None,
            "delta_p95": (cur["p95"] - base["p95"])
            if (cur and base and base["p95"] is not None) else None,
            "baseline_error": base["error_rate"] if base else None,
            "current_error": cur["error_rate"] if cur else None,
            "delta_error": (cur["error_rate"] - base["error_rate"])
            if (cur and base) else None,
        })
    return {"rows": rows}


# =========================
# ADVANCED ANALYSIS HELPERS
# =========================
# (unchanged from v1.2 – dashboard, graphs, error, comparison and overall insights)

def build_dashboard_insights(global_stats: Dict[str, Any],
                             sla_result: Dict[str, Any],
                             metrics: Dict[str, Any]) -> str:
    total = global_stats["total_requests"]
    error_rate = global_stats["error_rate"]
    p95 = global_stats["p95_ms"]
    apdex = global_stats["apdex"]
    peak_rps = metrics["peak_rps"]
    sla_pass = sla_result["overall_status"]
    sla_pct = sla_result["overall_pct"] * 100.0
    per_label = metrics.get("per_label", [])
    sla_rows = sla_result.get("rows", [])

    items: List[str] = []

    if total == 0:
        items.append("No samples were recorded in this run; verify that the JMeter test produced results.")
        return "<ul>" + "".join(f"<li>{line}</li>" for line in items) + "</ul>"

    if sla_pass:
        items.append(
            f"Overall SLA status is <strong>PASS</strong>: {sla_result['passed_checks']} of "
            f"{sla_result['total_checks']} checks passed ({sla_pct:.1f}%)."
        )
    else:
        items.append(
            f"Overall SLA status is <strong>FAIL</strong>: only {sla_result['passed_checks']} of "
            f"{sla_result['total_checks']} checks passed ({sla_pct:.1f}%)."
        )

    items.append(
        f"Global error rate is <strong>{error_rate * 100:.2f}%</strong> over "
        f"<strong>{total}</strong> samples, with P95 latency at <strong>{p95:.0f} ms</strong> "
        f"and APDEX score <strong>{apdex:.2f}</strong>."
    )
    items.append(
        f"Peak throughput during the run reached <strong>{peak_rps} requests/second</strong>, "
        f"over a total test duration of <strong>{format_duration(global_stats['duration_sec'])}</strong>."
    )

    failing_checks = []
    for row in sla_rows:
        label = row["label"]
        for check in row["checks"]:
            if check["status"]:
                continue
            metric = check["metric"]
            target = check["target"]
            actual = check["actual"]
            if metric == "TPS":
                severity = (target - actual) / target if target > 0 else 0.0
            else:
                severity = (actual - target) / target if target > 0 else 0.0
            failing_checks.append({
                "label": label,
                "metric": metric,
                "target": target,
                "actual": actual,
                "severity": severity,
            })

    if failing_checks:
        severe = [c for c in failing_checks if c["metric"] != "TPS" and c["severity"] > 0.5]
        moderate = [c for c in failing_checks if 0.2 < c["severity"] <= 0.5 and c["metric"] != "TPS"]

        if severe:
            items.append(
                f"{len(severe)} SLA checks exceed their thresholds by more than 50%. "
                "These represent critical performance or stability issues."
            )
        if moderate:
            items.append(
                f"{len(moderate)} SLA checks exceed their thresholds by 20–50%, "
                "indicating notable risk under production-like load."
            )

    duration_sec = global_stats.get("duration_sec", 0.0)
    per_label_map = {x["label"]: x for x in per_label}
    label_severity: Dict[str, float] = {}
    for fc in failing_checks:
        label = fc["label"]
        label_severity[label] = max(label_severity.get(label, 0.0), fc["severity"])

    if label_severity:
        worst_labels = sorted(label_severity.items(), key=lambda x: x[1], reverse=True)[:5]
        items.append("Top 5 worst transactions based on SLA violations and latency/error behaviour:")
        sub_items = []
        for label, sev in worst_labels:
            ls = per_label_map.get(label)
            if not ls:
                continue
            count = ls["count"]
            avg = ls["avg"]
            p90 = ls["p90"]
            p95_lbl = ls["p95"]
            err_pct = ls["error_rate"] * 100.0
            tps = (count / duration_sec) if duration_sec > 0 else 0.0
            sub_items.append(
                f"<strong>{label}</strong> — avg {avg:.0f} ms, P90 {p90:.0f} ms, "
                f"P95 {p95_lbl:.0f} ms, TPS {tps:.2f}, error {err_pct:.2f}% "
                f"(worst SLA breach ≈ {sev * 100:.0f}% beyond target)."
            )
        if sub_items:
            items.append("<ul>" + "".join(f"<li>{line}</li>" for line in sub_items) + "</ul>")

    lis = []
    for line in items:
        if line.strip().startswith("<ul>"):
            lis.append(f"<li>{line}</li>")
        else:
            lis.append(f"<li>{line}</li>")
    return "<ul>" + "\n".join(lis) + "</ul>"


def build_graphs_insights(metrics: Dict[str, Any]) -> str:
    timeseries = metrics.get("timeseries", [])
    if not timeseries:
        return "<p class=\"small\">No time-series data available for advanced analysis.</p>"

    n = len(timeseries)
    if n < 3:
        avg_rt = statistics.mean(d["avg_ms"] for d in timeseries)
        avg_rps = statistics.mean(d["rps"] for d in timeseries)
        return (
            "<ul>"
            f"<li>Average response time over the run is <strong>{avg_rt:.0f} ms</strong>, "
            f"with throughput around <strong>{avg_rps:.1f} requests/second</strong>.</li>"
            "</ul>"
        )

    early_end = max(1, n // 4)
    late_start = max(early_end + 1, n - n // 4)

    early = timeseries[:early_end]
    middle = timeseries[early_end:late_start] if late_start > early_end else []
    late = timeseries[late_start:] if late_start < n else []

    def avg_field(data, key):
        vals = [d[key] for d in data if d[key] is not None]
        return statistics.mean(vals) if vals else float("nan")

    early_rt = avg_field(early, "avg_ms")
    mid_rt = avg_field(middle, "avg_ms")
    late_rt = avg_field(late, "avg_ms")
    early_rps = avg_field(early, "rps")
    mid_rps = avg_field(middle, "rps")
    late_rps = avg_field(late, "rps")

    peak_latency = max(timeseries, key=lambda d: d["avg_ms"])
    peak_errors = max(timeseries, key=lambda d: d["errors"])
    peak_time_rt = datetime.fromtimestamp(peak_latency["sec"]).strftime("%Y-%m-%d %H:%M:%S")
    peak_time_err = datetime.fromtimestamp(peak_errors["sec"]).strftime("%Y-%m-%d %H:%M:%S")

    items: List[str] = []

    if not math.isnan(early_rt) and not math.isnan(mid_rt) and not math.isnan(late_rt):
        items.append(
            "Response time trend across the run: "
            f"early ≈ <strong>{early_rt:.0f} ms</strong>, "
            f"middle ≈ <strong>{mid_rt:.0f} ms</strong>, "
            f"late ≈ <strong>{late_rt:.0f} ms</strong>."
        )
        if late_rt > early_rt * 1.5:
            items.append(
                "Latency increases significantly towards the end of the test, suggesting resource "
                "saturation or contention under sustained load."
            )
        elif late_rt < early_rt * 0.8:
            items.append(
                "Latency improves over time, which is typical of warm-up effects (caching, JIT, etc.)."
            )
        else:
            items.append("Latency remains broadly stable from start to end of the run.")

    if not math.isnan(early_rps) and not math.isnan(mid_rps) and not math.isnan(late_rps):
        items.append(
            "Throughput trend (requests/second): "
            f"early ≈ <strong>{early_rps:.1f}</strong>, "
            f"middle ≈ <strong>{mid_rps:.1f}</strong>, "
            f"late ≈ <strong>{late_rps:.1f}</strong>."
        )
        if late_rps < early_rps * 0.7:
            items.append(
                "Throughput drops noticeably in the later segment; check for bottlenecks such as "
                "CPU saturation, connection pool exhaustion, or external dependency slowdowns."
            )
        elif late_rps > early_rps * 1.3:
            items.append(
                "Throughput ramps up in the later part of the run; validate that this matches the "
                "intended ramp-up profile and that the system can handle this sustained rate."
            )

    items.append(
        "The highest average response time occurs around "
        f"<strong>{peak_time_rt}</strong> (~{peak_latency['avg_ms']:.0f} ms), "
        "and the highest error burst occurs around "
        f"<strong>{peak_time_err}</strong> ({peak_errors['errors']} errors/sec). "
        "These time ranges are good candidates for deep-dive investigation."
    )

    lis = "\n".join(f"<li>{line}</li>" for line in items)
    return f"<ul>{lis}</ul>"


def build_errors_insights(error_summary: List[Dict[str, Any]],
                          global_stats: Dict[str, Any]) -> str:
    total_errors = global_stats["errors"]
    if total_errors == 0:
        return "<p class=\"small\">No errors recorded in this run. Focus optimisation efforts on latency and capacity.</p>"

    top_n = error_summary[:3]
    items: List[str] = []

    if top_n:
        details = []
        for er in top_n:
            details.append(
                f"{er['response_code']} ({er['count']} occurrences, {er['pct'] * 100:.2f}% of errors)"
            )
        items.append(
            "Top error patterns: " + "; ".join(details) + "."
        )

    codes_present = {er["response_code"] for er in top_n}
    if "503" in codes_present:
        items.append(
            "HTTP 503 errors typically indicate that an upstream service (e.g. application server) "
            "is overloaded or temporarily unavailable. Consider increasing backend capacity, "
            "tuning keep-alive/timeout settings, or reviewing load balancer configuration."
        )
    if "500" in codes_present:
        items.append(
            "HTTP 500 errors usually represent unhandled exceptions in application code. "
            "Inspect application logs and APM traces around peak load periods to identify failing code paths."
        )
    if "408" in codes_present:
        items.append(
            "HTTP 408 errors suggest request timeouts, which can be caused by long-running database "
            "queries, locks, or slow external dependencies. Review DB execution plans, lock waits, and "
            "downstream service latencies."
        )

    if global_stats["error_rate"] > 0.05:
        items.append(
            "Given the high overall error rate, stabilising the system should be the top priority "
            "before attempting higher load or longer duration tests."
        )
    else:
        items.append(
            "Overall error rate is moderate; after addressing the dominant error codes, you can "
            "shift focus to performance tuning of slow transactions."
        )

    lis = "\n".join(f"<li>{line}</li>" for line in items)
    return f"<ul>{lis}</ul>"


def build_comparison_insights(baseline_comparison: Optional[Dict[str, Any]]) -> str:
    if not baseline_comparison or not baseline_comparison.get("rows"):
        return "<p class=\"small\">No baseline data provided. Add <code>--baseline</code> to track regressions and improvements over time.</p>"

    rows = baseline_comparison["rows"]
    regressions = []
    improvements = []

    for r in rows:
        dp = r.get("delta_p95")
        de = r.get("delta_error")
        if dp is None and de is None:
            continue
        if (dp is not None and dp > 0) or (de is not None and de > 0):
            regressions.append(r)
        elif (dp is not None and dp < 0) or (de is not None and de < 0):
            improvements.append(r)

    items: List[str] = []

    if regressions:
        items.append(
            f"{len(regressions)} transaction(s) show performance regressions versus baseline "
            "(higher P95 and/or error rate)."
        )
        reg_details = []
        for r in sorted(regressions, key=lambda x: (x.get("delta_p95") or 0.0, x.get("delta_error") or 0.0),
                        reverse=True)[:5]:
            label = r["label"]
            dp = r.get("delta_p95")
            de = r.get("delta_error")
            parts = []
            if dp is not None and dp > 0:
                parts.append(f"P95 +{dp:.1f} ms")
            if de is not None and de > 0:
                parts.append(f"error +{de * 100:.2f}%")
            if not parts:
                continue
            reg_details.append(f"<strong>{label}</strong> ({', '.join(parts)})")
        if reg_details:
            items.append(
                "Regressed transactions: " + "; ".join(reg_details) + "."
            )
        items.append(
            "Prioritise investigation of regressed transactions, correlating these metrics with code, "
            "infrastructure, and configuration changes since the baseline run."
        )

    if improvements:
        items.append(
            f"{len(improvements)} transaction(s) improved compared to baseline in P95 and/or error rate. "
            "Capture and codify the changes that led to these gains."
        )

    if not regressions and not improvements:
        items.append(
            "No significant differences detected between the current run and the baseline in terms of "
            "P95 latency or error rate."
        )

    lis = "\n".join(f"<li>{line}</li>" for line in items)
    return f"<ul>{lis}</ul>"


def build_overall_insights(global_stats: Dict[str, Any],
                           sla_result: Dict[str, Any],
                           metrics: Dict[str, Any],
                           baseline_comparison: Optional[Dict[str, Any]]) -> str:
    items: List[str] = []

    total = global_stats["total_requests"]
    if total == 0:
        return "<p class=\"small\">No data available for this run. Check that your JMeter test produced samples.</p>"

    sla_pct = sla_result["overall_pct"] * 100.0
    if sla_result["overall_status"]:
        items.append(
            f"Overall SLA status is <strong>PASS</strong> with "
            f"<strong>{sla_result['passed_checks']}/{sla_result['total_checks']}</strong> checks "
            f"passing ({sla_pct:.1f}%)."
        )
    else:
        items.append(
            f"Overall SLA status is <strong>FAIL</strong>: only "
            f"<strong>{sla_result['passed_checks']}/{sla_result['total_checks']}</strong> checks "
            f"passed ({sla_pct:.1f}%). Focus on the worst offending transactions first."
        )

    items.append(
        f"The system handled <strong>{total}</strong> requests over "
        f"<strong>{format_duration(global_stats['duration_sec'])}</strong> with a global P95 of "
        f"<strong>{global_stats['p95_ms']:.0f} ms</strong>, average response time "
        f"<strong>{global_stats['avg_ms']:.0f} ms</strong>, and error rate "
        f"<strong>{global_stats['error_rate'] * 100:.2f}%</strong>."
    )
    items.append(
        f"Peak throughput reached <strong>{metrics['peak_rps']} requests/second</strong>, "
        f"with an APDEX of <strong>{global_stats['apdex']:.2f}</strong> "
        f"(T = {global_stats['p95_ms'] / 1000.0:.1f}s approx reference)."
    )

    timeseries = metrics.get("timeseries", [])
    if timeseries:
        n = len(timeseries)
        early_end = max(1, n // 4)
        late_start = max(early_end + 1, n - n // 4)

        early = timeseries[:early_end]
        late = timeseries[late_start:] if late_start < n else timeseries[-early_end:]

        def avg_field(data, key):
            vals = [d[key] for d in data if d[key] is not None]
            return statistics.mean(vals) if vals else float("nan")

        early_rt = avg_field(early, "avg_ms")
        late_rt = avg_field(late, "avg_ms")
        if not math.isnan(early_rt) and not math.isnan(late_rt):
            if late_rt > early_rt * 1.5:
                items.append(
                    f"Performance degrades over time: late-test P95 is roughly "
                    f"<strong>{late_rt:.0f} ms</strong> vs <strong>{early_rt:.0f} ms</strong> "
                    "in the early phase, indicating saturation or resource contention under sustained load."
                )
            elif late_rt < early_rt * 0.8:
                items.append(
                    f"Performance improves over time: late-test latency drops to "
                    f"<strong>{late_rt:.0f} ms</strong> from <strong>{early_rt:.0f} ms</strong>, "
                    "likely due to cache warm-up or JIT optimisation."
                )
            else:
                items.append(
                    f"Latency remains broadly stable from start (~<strong>{early_rt:.0f} ms</strong>) "
                    f"to end (~<strong>{late_rt:.0f} ms</strong>) of the test."
                )

    error_summary = metrics.get("error_summary", [])
    if global_stats["errors"] == 0:
        items.append(
            "No HTTP-level errors were observed; focus tuning efforts on latency and capacity rather than stability.")
    else:
        top = error_summary[0] if error_summary else None
        if top:
            items.append(
                f"Errors are dominated by HTTP <strong>{top['response_code']}</strong> "
                f"({top['count']} occurrences, {top['pct'] * 100:.2f}% of errors)."
            )
            if top["response_code"] == "503":
                items.append(
                    "503s suggest overloaded or unavailable upstream services — consider increasing application "
                    "server capacity or tuning load balancer / timeout settings."
                )
            elif top["response_code"] == "500":
                items.append(
                    "500s indicate server-side exceptions — inspect application logs and APM traces at peak load."
                )
            elif top["response_code"] == "408":
                items.append(
                    "408s (request timeout) point to slow backend operations such as long-running DB queries or "
                    "slow external dependencies."
                )

    if baseline_comparison and baseline_comparison.get("rows"):
        rows = baseline_comparison["rows"]
        regressions = []
        for r in rows:
            dp = r.get("delta_p95")
            de = r.get("delta_error")
            if (dp is not None and dp > 0) or (de is not None and de > 0):
                regressions.append(r)

        if regressions:
            items.append(
                f"<strong>{len(regressions)}</strong> transaction(s) regressed vs baseline in P95 and/or error rate. "
                "These should be prioritised for investigation before promoting this build."
            )
        else:
            items.append(
                "No significant latency or error-rate regressions were detected compared to the baseline; "
                "overall behaviour is consistent or improved."
            )
    else:
        items.append(
            "No baseline comparison was provided, so regressions vs previous runs cannot be automatically identified."
        )

    lis = "\n".join(f"<li>{line}</li>" for line in items)
    return f"<ul>{lis}</ul>"


# =========================
# HTML GENERATION
# =========================

def build_html(metrics: Dict[str, Any],
               sla_result: Dict[str, Any],
               sla_config: Dict[str, Dict[str, float]],
               test_name: str,
               environment: str,
               apdex_threshold_s: float,
               baseline_comparison: Optional[Dict[str, Any]] = None) -> str:
    global_stats = metrics["global"]
    per_label = metrics["per_label"]

    # --- Filter per_label to only include detected transaction names (strict transaction-only) ---
    txn_names = metrics.get("transaction_names", []) or []
    # Use only per_label entries whose label is explicitly detected as a transaction controller
    txn_per_label = [x for x in per_label if x.get("label") in txn_names]
    # For later code that expects 'per_label', keep original variable but also provide txn_per_label where needed.
    timeseries = metrics["timeseries"]
    error_summary = metrics["error_summary"]
    error_details = metrics.get("error_details", [])

    start_dt = datetime.fromtimestamp(metrics["start_time"] / 1000.0)
    end_dt = datetime.fromtimestamp(metrics["end_time"] / 1000.0)
    generated_at = datetime.now().isoformat(timespec="seconds")

    duration_sec_val = global_stats["duration_sec"]
    duration_str = format_duration(duration_sec_val)

    ts_epochs = [ts["sec"] for ts in timeseries]
    ts_labels = [datetime.fromtimestamp(ts["sec"]).strftime("%H:%M:%S") for ts in timeseries]
    ts_rps = [ts["rps"] for ts in timeseries]
    ts_avg_ms = [ts["avg_ms"] for ts in timeseries]
    ts_active = [ts["active_threads"] if ts["active_threads"] is not None else None for ts in timeseries]
    ts_errors = [ts.get("errors", 0) for ts in timeseries]

    chart_data = {
        "ts_epoch": ts_epochs,
        "ts_labels": ts_labels,
        "ts_rps": ts_rps,
        "ts_avg_ms": ts_avg_ms,
        "ts_active": ts_active,
        "ts_errors": ts_errors,
    }

    chart_data_json = json.dumps(chart_data)
    per_label_json = json.dumps(per_label)
    baseline_json = json.dumps(baseline_comparison) if baseline_comparison else "null"

    dashboard_insights_html = build_dashboard_insights(global_stats, sla_result, metrics)
    graphs_insights_html = build_graphs_insights(metrics)
    errors_insights_html = build_errors_insights(error_summary, global_stats)
    comparison_insights_html = build_comparison_insights(baseline_comparison)
    overall_insights_html = build_overall_insights(global_stats, sla_result, metrics, baseline_comparison)

    # Transaction SLA View rows based on SLA config

    transaction_sla_rows_html = []
    # Iterate only over detected transaction per-label summaries
    for ls in txn_per_label:
        sla = get_label_sla(ls["label"], sla_config)
        if not sla:
            continue

        avg = ls.get("avg")
        p90 = ls.get("p90")
        p95 = ls.get("p95")
        err_rate = ls.get("error_rate")
        count = ls.get("count", 0)
        tps = (count / duration_sec_val) if duration_sec_val > 0 else 0.0
        err_pct = (err_rate * 100.0) if err_rate is not None else 0.0

        failed = False
        if "p95_ms" in sla and p95 is not None and p95 > sla["p95_ms"]:
            failed = True
        if "avg_ms" in sla and avg is not None and avg > sla["avg_ms"]:
            failed = True
        if "error_rate" in sla and err_rate is not None and err_rate > sla["error_rate"]:
            failed = True
        if "tps_min" in sla and tps < sla["tps_min"]:
            failed = True

        status_text = "FAIL" if failed else "PASS"
        status_class = "status-fail" if failed else "status-ok"

        transaction_sla_rows_html.append(f"""
        <tr>
          <td>{ls["label"]}</td>
          <td>{format_ms(avg)}</td>
          <td>{format_ms(p90)}</td>
          <td>{format_ms(p95)}</td>
          <td>{tps:.2f}</td>
          <td>{err_pct:.2f}%</td>
          <td><span class="status-badge {status_class}">{status_text}</span></td>
        </tr>
        """)
    if transaction_sla_rows_html:
        transaction_sla_rows_html_str = "\n".join(transaction_sla_rows_html)
    else:
        transaction_sla_rows_html_str = (
            '<tr><td colspan="7" class="small">'
            'No transactions matched the SLA configuration. Update your SLA JSON to enable this view.'
            '</td></tr>'
        )

    html_template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{test_name} - JMeter Performance Report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{
    --bg:#f5f7fb;
    --card:#ffffff;
    --accent:#2563eb;
    --muted:#6b7280;
    --danger:#ef4444;
    --ok:#16a34a;
    --border:#e5e7eb;
}}
* {{
    box-sizing:border-box;
}}
body {{
    background:var(--bg);
    color:#0f172a;
    font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    margin:0;
    padding:18px;
}}
.container {{
    max-width:1200px;
    margin:0 auto;
}}
.header {{
    background:var(--card);
    padding:18px;
    border-radius:10px;
    box-shadow:0 4px 18px rgba(2,6,23,0.06);
    margin-bottom:12px;
}}
.header h1 {{
    margin:0;
    color:var(--accent);
    font-size:22px;
}}
.header .meta {{
    color:var(--muted);
    font-size:13px;
    margin-top:6px;
}}
.header .meta strong {{
    color:#111827;
}}
.small {{
    font-size:13px;
    color:var(--muted);
}}
.mono {{
    font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,monospace;
    background:#f3f4f6;
    padding:2px 5px;
    border-radius:4px;
}}
.tabs {{
    display:flex;
    gap:0;
    border-radius:10px;
    overflow:hidden;
    margin:8px 0 0 0;
    border:1px solid #e5e7eb;
}}
.tab-button {{
    flex:1;
    padding:10px 14px;
    background:#f8fafc;
    border:none;
    cursor:pointer;
    text-align:center;
    font-weight:600;
    color:#334155;
    font-size:13px;
    border-right:1px solid #e5e7eb;
}}
.tab-button:last-child {{
    border-right:none;
}}
.tab-button.active {{
    background:var(--accent);
    color:#ffffff;
}}
.tab-content {{
    display:none;
    background:var(--card);
    padding:16px;
    border-radius:0 0 10px 10px;
    box-shadow:0 2px 10px rgba(2,6,23,0.04);
    margin-bottom:12px;
}}
.tab-content.active {{
    display:block;
}}
.section-title {{
    font-size:16px;
    font-weight:600;
    margin:4px 0 4px 0;
    color:#0f172a;
}}
.section-sub {{
    font-size:12px;
    color:var(--muted);
    margin:0 0 8px 0;
}}
.pill {{
    display:inline-block;
    padding:2px 8px;
    font-size:11px;
    border-radius:999px;
    background:#eef2ff;
    color:#3730a3;
    margin-left:6px;
}}
.summary-grid {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:16px;
    margin-top:6px;
}}
.summary-card {{
    background:#f8fafc;
    padding:14px;
    border-radius:10px;
    border-left:4px solid var(--accent);
}}
.metrics-grid {{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:10px;
    margin-top:10px;
}}
.metric-card {{
    background:linear-gradient(135deg,#667eea,#764ba2);
    color:#fff;
    padding:12px;
    border-radius:10px;
    text-align:center;
}}
.metric-value {{
    font-size:20px;
    font-weight:700;
}}
.metric-label {{
    font-size:12px;
    opacity:0.9;
    margin-top:4px;
}}
.card-plain {{
    background:var(--card);
    border-radius:10px;
    padding:10px;
    border:1px solid var(--border);
    margin-top:10px;
}}
.insights-card {{
    background:#fefce8;
    border-color:#facc15;
}}
.insights-card ul {{
    padding-left:20px;
    margin:6px 0 0 0;
    font-size:12px;
    color:#4b5563;
}}
.insights-card li {{
    margin-bottom:4px;
}}
table {{
    width:100%;
    border-collapse:collapse;
    background:var(--card);
    font-size:12px;
}}
th,td {{
    padding:8px 10px;
    border-bottom:1px solid #eef2f7;
    text-align:left;
}}
th {{
    background:#f8fafc;
    font-weight:700;
    color:#475569;
}}
tr:nth-child(even) td {{
    background:#f9fafb;
}}
.status-badge {{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    min-width:48px;
    padding:2px 8px;
    border-radius:999px;
    font-size:10px;
    font-weight:600;
}}
.status-ok {{
    background-color:rgba(22,163,74,0.07);
    color:#15803d;
    border:1px solid rgba(22,163,74,0.6);
}}
.status-fail {{
    background-color:rgba(248,113,113,0.1);
    color:#b91c1c;
    border:1px solid rgba(248,113,113,0.7);
}}
.sla-overall-ok {{
    color:#15803d;
}}
.sla-overall-fail {{
    color:#b91c1c;
}}

/* CHART LAYOUT – 2-2-1 layout, bigger charts */
.chart-row {{
    display:grid;
    grid-template-columns:repeat(2, minmax(0,1fr));
    gap:18px;
    margin-top:12px;
}}
.chart-container {{
    background:var(--card);
    padding:14px;
    border-radius:10px;
    box-shadow:0 2px 8px rgba(2,6,23,0.04);
    height:420px;
    border:1px solid var(--border);
    position:relative;
}}
.chart-container canvas {{
    width:100% !important;
    height:100% !important;
    display:block;
}}
.chart-container.full-width {{
    grid-column:1 / -1;
}}
.error-badge {{
    background:var(--danger);
    color:#fff;
    padding:6px 10px;
    border-radius:18px;
    font-size:12px;
}}
.footer {{
    margin-top:8px;
    font-size:11px;
    color:var(--muted);
    text-align:center;
}}
.filter-row {{
    display:flex;
    flex-wrap:wrap;
    gap:10px;
    align-items:flex-end;
    margin-top:6px;
}}
.filter-group {{
    display:flex;
    flex-direction:column;
    gap:4px;
    font-size:12px;
    color:var(--muted);
}}
.filter-group input, .filter-group select {{
    padding:4px 8px;
    border-radius:6px;
    border:1px solid var(--border);
    font-size:12px;
}}
.filter-actions button {{
    padding:6px 10px;
    border-radius:6px;
    border:none;
    font-size:12px;
    cursor:pointer;
}}
.btn-primary {{
    background:var(--accent);
    color:#fff;
}}
.btn-secondary {{
    background:#e5e7eb;
    color:#374151;
}}
@media (max-width:900px) {{
    .summary-grid {{
        grid-template-columns:1fr;
    }}
    .chart-row {{
        grid-template-columns:1fr;
    }}
    .chart-container {{
        height:340px;
    }}
}}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>JMeter Performance Report</h1>
    <div class="meta">
      Generated: <span class="mono">{generated_at}</span> ·
      Samples: <strong>{total_requests}</strong> ·
      Duration: <strong>{duration_str}</strong>
    </div>
    <div class="meta">
      Test: <strong>{test_name}</strong> · Environment: <strong>{environment}</strong> ·
      Start: <span class="mono">{start_time}</span> · End: <span class="mono">{end_time}</span>
    </div>
  </div>

  <!-- GLOBAL TIME WINDOW FILTER -->
  <div class="card-plain" style="margin-top:8px;margin-bottom:8px;">
    <div class="small" style="margin-bottom:4px;">Time Window Filter (applies to charts)</div>
    <div class="filter-row">
      <div class="filter-group">
        <label for="timeWindowSelectTop">Window</label>
        <select id="timeWindowSelectTop">
          <option value="full">Full Run</option>
          <option value="first10">First 10 Minutes</option>
          <option value="last10">Last 10 Minutes</option>
          <option value="firstHalf">First Half</option>
          <option value="secondHalf">Second Half</option>
          <option value="custom">Custom Range</option>
        </select>
      </div>
      <div class="filter-group">
        <label for="timeWindowCustomStart">Custom Start</label>
        <input type="datetime-local" id="timeWindowCustomStart">
      </div>
      <div class="filter-group">
        <label for="timeWindowCustomEnd">Custom End</label>
        <input type="datetime-local" id="timeWindowCustomEnd">
      </div>
      <div class="filter-actions" style="display:flex;gap:8px;">
        <button id="applyCustomWindow" class="btn-primary">Apply Custom Range</button>
        <button id="clearTimeWindow" class="btn-secondary">Clear</button>
      </div>
    </div>
  </div>

  <!-- OVERALL INSIGHTS -->
  <div class="card-plain insights-card" style="margin-top:8px;margin-bottom:8px;">
    <div class="section-title" style="margin-top:0;">Overall Insights &amp; Recommendations</div>
    <p class="section-sub">
      High-level summary combining performance, stability, and regression analysis so you can
      understand the test outcome without reading every tab.
    </p>
    {overall_insights_html}
  </div>

  <!-- Tabs -->
  <div class="tabs" role="tablist">
    <button class="tab-button active" data-tab="dashboard">Dashboard</button>
    <button class="tab-button" data-tab="graphs">Graphs</button>
    <button class="tab-button" data-tab="errors">Error Report</button>
    <button class="tab-button" data-tab="comparison">Comparison</button>
  </div>

  <!-- DASHBOARD TAB -->
  <div id="tab-dashboard" class="tab-content active">
    <div class="section-title">Dashboard <span class="pill">Overview</span></div>
    <p class="section-sub">High-level view of the test run and transaction-level metrics.</p>

    <div class="summary-grid">
      <div class="summary-card">
        <h3>Test Summary</h3>
        <div style="margin-top:8px">
          <div class="small">Total Samples: <strong>{total_requests}</strong></div>
          <div class="small">Success: <strong>{successes}</strong> · Failures: <strong>{errors}</strong></div>
          <div class="small">Error Rate: <strong>{error_rate_pct:.2f}%</strong></div>
          <div class="small">Avg Response: <strong>{avg_ms:.1f} ms</strong></div>
          <div class="small">P95 Response: <strong>{p95_ms:.1f} ms</strong></div>
        </div>
      </div>
      <div class="summary-card">
        <h3>SLA Summary</h3>
        <div style="margin-top:8px">
          <div class="{sla_overall_class}">Overall SLA: <strong>{sla_overall_text}</strong></div>
          <div class="small" style="margin-top:6px">
            Checks passed: <strong>{sla_passed}</strong> / {sla_total}
            ({sla_pct:.1f}%)
          </div>
          <div class="small" style="margin-top:6px">
            APDEX (T = {apdex_threshold:.1f}s): <strong>{apdex_value:.2f}</strong>
          </div>
        </div>
      </div>
    </div>

    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-value">{peak_rps}</div>
        <div class="metric-label">Peak Throughput (req/s)</div>
      </div>
      <div class="metric-card">
        <div class="metric-value">{avg_ms:.1f}</div>
        <div class="metric-label">Average Response (ms)</div>
      </div>
      <div class="metric-card">
        <div class="metric-value">{p95_ms:.1f}</div>
        <div class="metric-label">95th Percentile (ms)</div>
      </div>
      <div class="metric-card">
        <div class="metric-value">{error_rate_pct:.2f}%</div>
        <div class="metric-label">Error Rate</div>
      </div>
    </div>

    <!-- DASHBOARD INSIGHTS -->
    <div class="card-plain insights-card" style="margin-top:14px;">
      <div class="section-title" style="margin-top:0;">Insights &amp; Recommendations</div>
      <p class="section-sub">Automatically generated, data-driven summary based on global KPIs and SLA results.</p>
      {dashboard_insights_html}
    </div>

    <!-- TRANSACTION SLA VIEW -->
    <div class="card-plain" style="margin-top:14px;">
      <div class="section-title" style="margin-top:0;">Transaction SLA View</div>
      <p class="section-sub">
        Transactions evaluated against configured SLA values. Status is PASS/FAIL based on P95, Avg, error rate and optional TPS thresholds.
      </p>
      <div style="max-height:260px;overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>Transaction</th>
              <th>Avg (ms)</th>
              <th>P90 (ms)</th>
              <th>P95 (ms)</th>
              <th>Throughput (req/s)</th>
              <th>Error %</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {transaction_sla_rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- TRANSACTION STATISTICS -->
    <div class="card-plain" style="margin-top:14px;">
      <div class="section-title" style="margin-top:0;">Transaction Statistics</div>
      <p class="section-sub">
        Detailed statistics per transaction, with filters by average response time, throughput and error percentage.
      </p>

      <div class="filter-row">
        <div class="filter-group">
          <label for="filterAvgMax">Max Avg Response (ms)</label>
          <input id="filterAvgMax" type="number" placeholder="e.g. 2000">
        </div>
        <div class="filter-group">
          <label for="filterTpsMin">Min TPS (Hits/sec)</label>
          <input id="filterTpsMin" type="number" step="0.01" placeholder="e.g. 5">
        </div>
        <div class="filter-group">
          <label for="filterErrMax">Max Error %</label>
          <input id="filterErrMax" type="number" step="0.01" placeholder="e.g. 2">
        </div>
        <div class="filter-actions" style="display:flex;gap:8px;">
          <button id="applySlaFilter" class="btn-primary">Apply</button>
          <button id="resetSlaFilter" class="btn-secondary">Clear</button>
        </div>
      </div>

      <div style="max-height:320px;overflow:auto;margin-top:10px;">
        <table id="perTxnTable">
          <thead>
            <tr>
              <th>Label</th>
              <th>Count</th>
              <th>Min (ms)</th>
              <th>Avg (ms)</th>
              <th>P50 (ms)</th>
              <th>P90 (ms)</th>
              <th>P95 (ms)</th>
              <th>P99 (ms)</th>
              <th>Max (ms)</th>
              <th>Throughput (req/min)</th>
              <th>Hits/sec</th>
              <th>Error Rate</th>
            </tr>
          </thead>
          <tbody>
            {per_label_rows_html}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- GRAPHS TAB -->
  <div id="tab-graphs" class="tab-content">
    <div class="section-title">Time-series &amp; Trends <span class="pill">Graphs</span></div>
    <p class="section-sub">
      Throughput, response time, errors, and active threads over time.
      Use the time window filter to focus on specific portions of the test.
    </p>

    <!-- GRAPHS INSIGHTS -->
    <div class="card-plain insights-card" style="margin-top:6px;">
      <div class="section-title" style="margin-top:0;">Insights &amp; Recommendations</div>
      <p class="section-sub">Trend segmentation based on early, middle, and late phases of the test.</p>
      {graphs_insights_html}
    </div>

    <div class="card-plain" style="margin-top:6px;margin-bottom:8px;">
      <div class="small" style="margin-bottom:4px;">Time Window Filter (applies to charts below)</div>
      <div class="filter-row">
        <div class="filter-group">
          <label for="timeWindowSelect">Window</label>
          <select id="timeWindowSelect">
            <option value="full">Full Run</option>
            <option value="first10">First 10 Minutes</option>
            <option value="last10">Last 10 Minutes</option>
            <option value="firstHalf">First Half</option>
            <option value="secondHalf">Second Half</option>
            <option value="custom">Custom Range</option>
          </select>
        </div>
      </div>
    </div>

    <!-- ROW 1 -->
    <div class="chart-row">
      <div class="chart-container">
        <div class="small" style="margin-bottom:6px;">Requests Per Second</div>
        <canvas id="rpsChart"></canvas>
      </div>
      <div class="chart-container">
        <div class="small" style="margin-bottom:6px;">Average Response Time (ms)</div>
        <canvas id="rtChart"></canvas>
      </div>
    </div>

    <!-- ROW 2 -->
    <div class="chart-row">
      <div class="chart-container">
        <div class="small" style="margin-bottom:6px;">Errors Per Second</div>
        <canvas id="errChart"></canvas>
      </div>
      <div class="chart-container">
        <div class="small" style="margin-bottom:6px;">Response Time vs Errors Per Second</div>
        <canvas id="rtErrChart"></canvas>
      </div>
    </div>

    <!-- ROW 3 -->
    <div class="chart-row">
      <div class="chart-container full-width">
        <div class="small" style="margin-bottom:6px;">Throughput vs Threads</div>
        <canvas id="rpsThreadChart"></canvas>
      </div>
    </div>
  </div>

  <!-- ERRORS TAB -->
  <div id="tab-errors" class="tab-content">
    <div class="section-title">Error Report <span class="pill">Stability</span></div>
    <p class="section-sub">Grouped by response code for this test run, plus transaction-level error details.</p>

    <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px;">
      <div class="error-badge">{errors} Errors</div>
      <div class="small">Non-success responses by HTTP code and transaction.</div>
    </div>

    <!-- ERRORS INSIGHTS -->
    <div class="card-plain insights-card" style="margin-top:0;margin-bottom:10px;">
      <div class="section-title" style="margin-top:0;">Insights &amp; Recommendations</div>
      <p class="section-sub">Most frequent error classes with probable root causes.</p>
      {errors_insights_html}
    </div>

    <!-- SUMMARY BY RESPONSE CODE -->
    <div style="max-height:260px;overflow:auto;">
      <table>
        <thead>
          <tr>
            <th>Response Code</th>
            <th>Count</th>
            <th>Percentage of Errors</th>
          </tr>
        </thead>
        <tbody>
          {error_rows_html}
        </tbody>
      </table>
    </div>

    <!-- NEW: DETAILED BREAKDOWN BY TRANSACTION & MESSAGE -->
    <div style="max-height:260px;overflow:auto;margin-top:10px;">
      <table>
        <thead>
          <tr>
            <th>Transaction</th>
            <th>Response Code</th>
            <th>Error Message</th>
            <th>Count</th>
            <th>Percentage of Errors</th>
          </tr>
        </thead>
        <tbody>
          {error_detail_rows_html}
        </tbody>
      </table>
    </div>
  </div>

  <!-- COMPARISON TAB -->
  <div id="tab-comparison" class="tab-content">
    <div class="section-title">Baseline Comparison <span class="pill">Trends</span></div>
    <p class="section-sub">Compare this run against a baseline CSV/JTL (if provided).</p>

    <!-- COMPARISON INSIGHTS -->
    <div class="card-plain insights-card" style="margin-top:6px;">
      <div class="section-title" style="margin-top:0;">Insights &amp; Recommendations</div>
      <p class="section-sub">Regression detection: which transactions got slower or less stable.</p>
      {comparison_insights_html}
    </div>

    <div class="card-plain" style="margin-top:10px;max-height:260px;overflow:auto;">
      <div id="baselineSection"></div>
      <p class="small" style="margin-top:6px;">
        Provide <span class="mono">--baseline baseline.csv</span> to enable this comparison.
      </p>
    </div>
  </div>

  <div class="footer">
    Generated by custom JMeter HTML Performance Dashboard · Python · Chart.js
  </div>

</div>

<script>
document.addEventListener('DOMContentLoaded', function() {{
  const chartData = {chart_data_json};
  const perLabelData = {per_label_json};
  const baselineComparison = {baseline_json};

  // --- Charts ---

  const ctxRps = document.getElementById('rpsChart').getContext('2d');
  const rpsChart = new Chart(ctxRps, {{
      type: 'line',
      data: {{
          labels: chartData.ts_labels,
          datasets: [{{
              label: 'RPS',
              data: chartData.ts_rps,
              borderColor: 'rgba(37,99,235,1)',
              backgroundColor: 'rgba(37,99,235,0.15)',
              borderWidth: 1.6,
              pointRadius: 0,
              tension: 0.25,
              fill: true
          }}]
      }},
      options: {{
          responsive: true,
          plugins: {{
              legend: {{ display: false }},
              tooltip: {{ mode: 'index', intersect: false }}
          }},
          interaction: {{ mode: 'index', intersect: false }},
          maintainAspectRatio: false,
          scales: {{
              x: {{
                  ticks: {{ maxTicksLimit: 10 }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y: {{
                  title: {{ display: true, text: 'Requests / Second' }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }}
          }}
      }}
  }});

  const ctxRt = document.getElementById('rtChart').getContext('2d');
  const rtChart = new Chart(ctxRt, {{
      type: 'line',
      data: {{
          labels: chartData.ts_labels,
          datasets: [{{
              label: 'Avg Response Time (ms)',
              data: chartData.ts_avg_ms,
              borderColor: 'rgba(79,70,229,1)',
              backgroundColor: 'rgba(79,70,229,0.16)',
              borderWidth: 1.6,
              pointRadius: 0,
              tension: 0.25,
              fill: true
          }}]
      }},
      options: {{
          responsive: true,
          plugins: {{
              legend: {{ display: false }},
              tooltip: {{ mode: 'index', intersect: false }}
          }},
          interaction: {{ mode: 'index', intersect: false }},
          maintainAspectRatio: false,
          scales: {{
              x: {{
                  ticks: {{ maxTicksLimit: 10 }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y: {{
                  title: {{ display: true, text: 'Avg Response Time (ms)' }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }}
          }}
      }}
  }});

  const ctxErr = document.getElementById('errChart').getContext('2d');
  const errChart = new Chart(ctxErr, {{
      type: 'line',
      data: {{
          labels: chartData.ts_labels,
          datasets: [{{
              label: 'Errors / Second',
              data: chartData.ts_errors,
              borderColor: 'rgba(239,68,68,1)',
              backgroundColor: 'rgba(239,68,68,0.18)',
              borderWidth: 1.6,
              pointRadius: 0,
              tension: 0.25,
              fill: true
          }}]
      }},
      options: {{
          responsive: true,
          plugins: {{
              legend: {{ display: false }},
              tooltip: {{ mode: 'index', intersect: false }}
          }},
          interaction: {{ mode: 'index', intersect: false }},
          maintainAspectRatio: false,
          scales: {{
              x: {{
                  ticks: {{ maxTicksLimit: 10 }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y: {{
                  title: {{ display: true, text: 'Errors / Second' }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }}
          }}
      }}
  }});

  const ctxRtErr = document.getElementById('rtErrChart').getContext('2d');
  const rtErrChart = new Chart(ctxRtErr, {{
      type: 'line',
      data: {{
          labels: chartData.ts_labels,
          datasets: [
              {{
                  label: 'Avg Response Time (ms)',
                  data: chartData.ts_avg_ms,
                  borderColor: 'rgba(79,70,229,1)',
                  backgroundColor: 'rgba(79,70,229,0.0)',
                  borderWidth: 1.6,
                  pointRadius: 0,
                  tension: 0.25,
                  fill: false,
                  yAxisID: 'y'
              }},
              {{
                  label: 'Errors / Second',
                  data: chartData.ts_errors,
                  borderColor: 'rgba(239,68,68,1)',
                  backgroundColor: 'rgba(239,68,68,0.0)',
                  borderWidth: 1.6,
                  pointRadius: 0,
                  tension: 0.25,
                  fill: false,
                  yAxisID: 'y1'
              }}
          ]
      }},
      options: {{
          responsive: true,
          plugins: {{
              legend: {{ display: true }},
              tooltip: {{ mode: 'index', intersect: false }}
          }},
          interaction: {{ mode: 'index', intersect: false }},
          maintainAspectRatio: false,
          scales: {{
              x: {{
                  ticks: {{ maxTicksLimit: 10 }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y: {{
                  title: {{ display: true, text: 'Response Time (ms)' }},
                  position: 'left',
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y1: {{
                  title: {{ display: true, text: 'Errors / Second' }},
                  position: 'right',
                  grid: {{ drawOnChartArea: false }}
              }}
          }}
      }}
  }});

  const ctxRpsThread = document.getElementById('rpsThreadChart').getContext('2d');
  const rpsThreadChart = new Chart(ctxRpsThread, {{
      type: 'line',
      data: {{
          labels: chartData.ts_labels,
          datasets: [
              {{
                  label: 'RPS',
                  data: chartData.ts_rps,
                  borderColor: 'rgba(37,99,235,1)',
                  backgroundColor: 'rgba(37,99,235,0.0)',
                  borderWidth: 1.6,
                  pointRadius: 0,
                  tension: 0.25,
                  fill: false,
                  yAxisID: 'y'
              }},
              {{
                  label: 'Active Threads',
                  data: chartData.ts_active,
                  borderColor: 'rgba(34,197,94,1)',
                  backgroundColor: 'rgba(34,197,94,0.0)',
                  borderWidth: 1.6,
                  pointRadius: 0,
                  tension: 0.25,
                  fill: false,
                  yAxisID: 'y1'
              }}
          ]
      }},
      options: {{
          responsive: true,
          plugins: {{
              legend: {{ display: true }},
              tooltip: {{ mode: 'index', intersect: false }}
          }},
          interaction: {{ mode: 'index', intersect: false }},
          maintainAspectRatio: false,
          scales: {{
              x: {{
                  ticks: {{ maxTicksLimit: 10 }},
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y: {{
                  title: {{ display: true, text: 'Requests / Second' }},
                  position: 'left',
                  grid: {{ color: 'rgba(148,163,184,0.4)' }}
              }},
              y1: {{
                  title: {{ display: true, text: 'Active Threads' }},
                  position: 'right',
                  grid: {{ drawOnChartArea: false }}
              }}
          }}
      }}
  }});

  // --- Baseline comparison ---
  if (baselineComparison && baselineComparison.rows && baselineComparison.rows.length > 0) {{
      const container = document.getElementById('baselineSection');
      let html = '<table><thead><tr>' +
                 '<th>Transaction</th>' +
                 '<th>Baseline P95 (ms)</th>' +
                 '<th>Current P95 (ms)</th>' +
                 '<th>Δ P95 (ms)</th>' +
                 '<th>Baseline Error</th>' +
                 '<th>Current Error</th>' +
                 '<th>Δ Error</th>' +
                 '</tr></thead><tbody>';

      baselineComparison.rows.forEach(row => {{
          const fmtMs = x => (x === null || x === undefined) ? '-' : x.toFixed(1);
          const fmtPct = x => (x === null || x === undefined) ? '-' : (x * 100).toFixed(2) + '%';

          let deltaClass = '';
          if (row.delta_p95 !== null && row.delta_p95 !== undefined) {{
              if (row.delta_p95 > 0) deltaClass = 'status-badge status-fail';
              else if (row.delta_p95 < 0) deltaClass = 'status-badge status-ok';
          }}

          html += '<tr>' +
              '<td>' + row.label + '</td>' +
              '<td>' + fmtMs(row.baseline_p95) + '</td>' +
              '<td>' + fmtMs(row.current_p95) + '</td>' +
              '<td><span class=\"' + deltaClass + '\">' +
                  ((row.delta_p95 === null || row.delta_p95 === undefined)
                      ? '-' : row.delta_p95.toFixed(1)) +
              '</span></td>' +
              '<td>' + fmtPct(row.baseline_error) + '</td>' +
              '<td>' + fmtPct(row.current_error) + '</td>' +
              '<td>' + fmtPct(row.delta_error) + '</td>' +
              '</tr>';
      }});
      html += '</tbody></table>';
      container.innerHTML = html;
  }} else {{
      const container = document.getElementById('baselineSection');
      if (container) {{
          container.innerHTML =
              '<p class="small">No baseline data provided.</p>';
      }}
  }}

  // --- Tabs ---
  const tabButtons = document.querySelectorAll('.tab-button');
  const tabContents = document.querySelectorAll('.tab-content');
  tabButtons.forEach(btn => {{
      btn.addEventListener('click', () => {{
          const tab = btn.dataset.tab;
          tabButtons.forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          tabContents.forEach(c => {{
              c.classList.toggle('active', c.id === 'tab-' + tab);
          }});
      }});
  }});

  // --- Transaction Statistics filter ---
  const filterAvgMax = document.getElementById('filterAvgMax');
  const filterTpsMin = document.getElementById('filterTpsMin');
  const filterErrMax = document.getElementById('filterErrMax');
  const perTxnTableBody = document.querySelector('#perTxnTable tbody');

  function applySlaFilter() {{
      if (!perTxnTableBody) return;
      const avgMax = parseFloat(filterAvgMax.value);
      const tpsMin = parseFloat(filterTpsMin.value);
      const errMax = parseFloat(filterErrMax.value);

      const rows = perTxnTableBody.querySelectorAll('tr');
      rows.forEach(row => {{
          const avg = parseFloat(row.dataset.avgMs || 'NaN');
          const tps = parseFloat(row.dataset.tps || 'NaN');
          const err = parseFloat(row.dataset.errorPct || 'NaN');

          let visible = true;

          if (!isNaN(avgMax) && !isNaN(avg) && avg > avgMax) {{
              visible = false;
          }}
          if (!isNaN(tpsMin) && (isNaN(tps) || tps < tpsMin)) {{
              visible = false;
          }}
          if (!isNaN(errMax) && !isNaN(err) && err > errMax) {{
              visible = false;
          }}

          row.style.display = visible ? '' : 'none';
      }});
  }}

  function resetSlaFilter() {{
      if (!perTxnTableBody) return;
      filterAvgMax.value = '';
      filterTpsMin.value = '';
      filterErrMax.value = '';
      perTxnTableBody.querySelectorAll('tr').forEach(row => row.style.display = '');
  }}

  const applySlaBtn = document.getElementById('applySlaFilter');
  const resetSlaBtn = document.getElementById('resetSlaFilter');
  if (applySlaBtn) {{
      applySlaBtn.addEventListener('click', (e) => {{
          e.preventDefault();
          applySlaFilter();
      }});
  }}
  if (resetSlaBtn) {{
      resetSlaBtn.addEventListener('click', (e) => {{
          e.preventDefault();
          resetSlaFilter();
      }});
  }}

  // --- Time Window Filter (presets + custom) ---
  const timeWindowSelect = document.getElementById('timeWindowSelect');
  const timeWindowSelectTop = document.getElementById('timeWindowSelectTop');
  const customStartInput = document.getElementById('timeWindowCustomStart');
  const customEndInput = document.getElementById('timeWindowCustomEnd');
  const applyCustomBtn = document.getElementById('applyCustomWindow');
  const clearTimeWindowBtn = document.getElementById('clearTimeWindow');

  function applyTimeWindow(mode, customLow = null, customHigh = null) {{
      const epochs = chartData.ts_epoch;
      if (!epochs || epochs.length === 0) return;

      const labelsFull = chartData.ts_labels;
      const rpsFull = chartData.ts_rps;
      const avgFull = chartData.ts_avg_ms;
      const errFull = chartData.ts_errors;
      const activeFull = chartData.ts_active;

      const minSec = epochs[0];
      const maxSec = epochs[epochs.length - 1];
      const span = maxSec - minSec;

      let low = minSec;
      let high = maxSec;

      if (mode === 'custom' && customLow !== null && customHigh !== null) {{
          low = Math.max(minSec, Math.min(customLow, customHigh));
          high = Math.min(maxSec, Math.max(customLow, customHigh));
      }} else {{
          if (mode === 'first10') {{
              high = Math.min(maxSec, minSec + 600);
          }} else if (mode === 'last10') {{
              low = Math.max(minSec, maxSec - 600);
          }} else if (mode === 'firstHalf') {{
              high = minSec + span / 2;
          }} else if (mode === 'secondHalf') {{
              low = minSec + span / 2;
          }} else {{
              low = minSec;
              high = maxSec;
          }}
      }}

      const newLabels = [];
      const newRps = [];
      const newAvg = [];
      const newErr = [];
      const newActive = [];

      for (let i = 0; i < epochs.length; i++) {{
          const t = epochs[i];
          if (t >= low && t <= high) {{
              newLabels.push(labelsFull[i]);
              newRps.push(rpsFull[i]);
              newAvg.push(avgFull[i]);
              newErr.push(errFull[i]);
              newActive.push(activeFull[i]);
          }}
      }}

      const L = newLabels.length > 0 ? newLabels : labelsFull;
      const R = newLabels.length > 0 ? newRps : rpsFull;
      const A = newLabels.length > 0 ? newAvg : avgFull;
      const E = newLabels.length > 0 ? newErr : errFull;
      const T = newLabels.length > 0 ? newActive : activeFull;

      rpsChart.data.labels = L;
      rpsChart.data.datasets[0].data = R;
      rpsChart.update();

      rtChart.data.labels = L;
      rtChart.data.datasets[0].data = A;
      rtChart.update();

      errChart.data.labels = L;
      errChart.data.datasets[0].data = E;
      errChart.update();

      rtErrChart.data.labels = L;
      rtErrChart.data.datasets[0].data = A;
      rtErrChart.data.datasets[1].data = E;
      rtErrChart.update();

      rpsThreadChart.data.labels = L;
      rpsThreadChart.data.datasets[0].data = R;
      rpsThreadChart.data.datasets[1].data = T;
      rpsThreadChart.update();
  }}

  function setTimeWindow(mode) {{
      if (timeWindowSelect) timeWindowSelect.value = mode;
      if (timeWindowSelectTop) timeWindowSelectTop.value = mode;
      if (mode !== 'custom') {{
          applyTimeWindow(mode);
      }}
  }}

  function applyCustomWindow() {{
      if (!customStartInput || !customEndInput) return;
      const startVal = customStartInput.value;
      const endVal = customEndInput.value;
      if (!startVal || !endVal) return;
      const startEpoch = Math.floor(new Date(startVal).getTime() / 1000);
      const endEpoch = Math.floor(new Date(endVal).getTime() / 1000);
      if (isNaN(startEpoch) || isNaN(endEpoch)) return;

      if (timeWindowSelect) timeWindowSelect.value = 'custom';
      if (timeWindowSelectTop) timeWindowSelectTop.value = 'custom';
      applyTimeWindow('custom', startEpoch, endEpoch);
  }}

  function clearTimeWindow() {{
      if (customStartInput) customStartInput.value = '';
      if (customEndInput) customEndInput.value = '';
      setTimeWindow('full');
  }}

  if (timeWindowSelect) {{
      timeWindowSelect.addEventListener('change', () => {{
          const mode = timeWindowSelect.value;
          setTimeWindow(mode);
      }});
  }}
  if (timeWindowSelectTop) {{
      timeWindowSelectTop.addEventListener('change', () => {{
          const mode = timeWindowSelectTop.value;
          setTimeWindow(mode);
      }});
  }}
  if (applyCustomBtn) {{
      applyCustomBtn.addEventListener('click', (e) => {{
          e.preventDefault();
          applyCustomWindow();
      }});
  }}
  if (clearTimeWindowBtn) {{
      clearTimeWindowBtn.addEventListener('click', (e) => {{
          e.preventDefault();
          clearTimeWindow();
      }});
  }}

}}); // DOMContentLoaded
</script>

</body>
</html>
"""

    # Per-transaction rows (Transaction Statistics table)
    per_label = metrics["per_label"]
    duration_sec_val = metrics["global"]["duration_sec"]

    per_label_rows_html = []
    # Use only detected transactions' per-label summaries
    for ls in txn_per_label:
        avg = ls.get("avg")
        err_rate = ls.get("error_rate")
        count = ls.get("count", 0)
        tps = (count / duration_sec_val) if duration_sec_val > 0 else 0.0
        throughput_per_min = tps * 60.0
        err_pct = (err_rate * 100.0) if err_rate is not None else 0.0

        per_label_rows_html.append(f"""
        <tr data-avg-ms="{avg if avg is not None else ''}"
            data-error-pct="{err_pct if err_rate is not None else ''}"
            data-tps="{tps}">
          <td>{ls["label"]}</td>
          <td>{ls["count"]}</td>
          <td>{format_ms(ls["min"])}</td>
          <td>{format_ms(ls["avg"])}</td>
          <td>{format_ms(ls["p50"])}</td>
          <td>{format_ms(ls["p90"])}</td>
          <td>{format_ms(ls["p95"])}</td>
          <td>{format_ms(ls["p99"])}</td>
          <td>{format_ms(ls["max"])}</td>
          <td>{throughput_per_min:.2f}</td>
          <td>{tps:.3f}</td>
          <td>{format_pct(ls["error_rate"])}</td>
        </tr>
        """)
    per_label_rows_html_str = "\n".join(per_label_rows_html)

    # Error rows (summary by response code)
    error_rows_html = []
    if error_summary:
        for er in error_summary:
            error_rows_html.append(f"""
            <tr>
              <td>{er["response_code"]}</td>
              <td>{er["count"]}</td>
              <td>{format_pct(er["pct"])}</td>
            </tr>
            """)
    else:
        error_rows_html.append(
            '<tr><td colspan="3" class="small">No errors recorded.</td></tr>'
        )
    error_rows_html_str = "\n".join(error_rows_html)

    # NEW: error details rows (transaction + message)
    error_detail_rows_html = []
    if error_details:
        for ed in error_details:
            msg = ed["response_message"] or ""
            msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            error_detail_rows_html.append(f"""
            <tr>
              <td>{ed["label"]}</td>
              <td>{ed["response_code"]}</td>
              <td>{msg}</td>
              <td>{ed["count"]}</td>
              <td>{format_pct(ed["pct"])}</td>
            </tr>
            """)
    else:
        error_detail_rows_html.append(
            '<tr><td colspan="5" class="small">No detailed error records available.</td></tr>'
        )
    error_detail_rows_html_str = "\n".join(error_detail_rows_html)

    sla_total = sla_result["total_checks"]
    sla_passed = sla_result["passed_checks"]
    sla_pct = sla_result["overall_pct"] * 100.0
    sla_overall_class = "sla-overall-ok" if sla_result["overall_status"] else "sla-overall-fail"
    sla_overall_text = "PASS" if sla_result["overall_status"] else "FAIL"

    html = html_template.format(
        test_name=test_name,
        environment=environment,
        generated_at=generated_at,
        start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        duration_str=duration_str,
        total_requests=global_stats["total_requests"],
        duration_sec=global_stats["duration_sec"],
        peak_rps=metrics["peak_rps"],
        avg_ms=global_stats["avg_ms"],
        p95_ms=global_stats["p95_ms"],
        error_rate_pct=global_stats["error_rate"] * 100.0,
        apdex_value=global_stats["apdex"],
        apdex_threshold=apdex_threshold_s,
        successes=global_stats["successes"],
        errors=global_stats["errors"],
        error_rows_html=error_rows_html_str,
        error_detail_rows_html=error_detail_rows_html_str,
        per_label_rows_html=per_label_rows_html_str,
        transaction_sla_rows_html=transaction_sla_rows_html_str,
        chart_data_json=chart_data_json,
        per_label_json=per_label_json,
        baseline_json=baseline_json,
        sla_total=sla_total,
        sla_passed=sla_passed,
        sla_pct=sla_pct,
        sla_overall_class=sla_overall_class,
        sla_overall_text=sla_overall_text,
        dashboard_insights_html=dashboard_insights_html,
        graphs_insights_html=graphs_insights_html,
        errors_insights_html=errors_insights_html,
        comparison_insights_html=comparison_insights_html,
        overall_insights_html=overall_insights_html,
    )
    return html


# =========================
# CLI
# =========================

def main():
    parser = argparse.ArgumentParser(
        description="Generate a modern HTML Performance Dashboard from JMeter CSV/JTL."
    )
    parser.add_argument("--input", required=True, help="Path to JMeter CSV/JTL results file")
    parser.add_argument("--output", required=True, help="Output HTML report path")
    parser.add_argument("--test-name", default="JMeter Load Test",
                        help="Name of the test to display in the report")
    parser.add_argument("--environment", default="Unknown",
                        help="Environment (e.g. QA, UAT, Prod-like)")
    parser.add_argument("--apdex-threshold", type=float, default=1.5,
                        help="APDEX threshold T in seconds (default 1.5)")
    parser.add_argument("--baseline",
                        help="Optional baseline JMeter CSV/JTL for comparison")
    parser.add_argument("--sla-config-file",
                        help="Path to SLA config JSON file (overrides built-in DEFAULT_SLA_CONFIG)")
    parser.add_argument("--sla-config-json",
                        help="SLA config as JSON string (used if no file provided)")

    args = parser.parse_args()

    sla_config = DEFAULT_SLA_CONFIG
    if args.sla_config_file:
        try:
            with open(args.sla_config_file, "r", encoding="utf-8") as f:
                sla_config = json.load(f)
        except Exception as e:
            raise SystemExit(f"Failed to load SLA config from file: {e}")
    elif args.sla_config_json:
        try:
            sla_config = json.loads(args.sla_config_json)
        except Exception as e:
            raise SystemExit(f"Failed to parse SLA config JSON: {e}")

    samples = parse_jmeter_csv(args.input)
    if not samples:
        raise SystemExit("No samples parsed from input file. Check CSV/JTL format.")

    metrics = compute_metrics(samples, args.apdex_threshold)

    # --- Always-on transaction-only behavior (Option A) ---
    # Extract transaction controller names from the original samples and store in metrics.
    try:
        metrics["transaction_names"] = extract_transaction_names(samples)
    except Exception:
        metrics["transaction_names"] = []
    # --- end transaction-only insertion ---

    sla_result = evaluate_sla(metrics, sla_config)

    baseline_comparison = None  # Note: baseline is optional
    if args.baseline:
        baseline_samples = parse_jmeter_csv(args.baseline)
        if baseline_samples:
            baseline_metrics = compute_metrics(baseline_samples, args.apdex_threshold)
            baseline_comparison = compare_baseline(metrics, baseline_metrics)

    html = build_html(
        metrics=metrics,
        sla_result=sla_result,
        sla_config=sla_config,
        test_name=args.test_name,
        environment=args.environment,
        apdex_threshold_s=args.apdex_threshold,
        baseline_comparison=baseline_comparison,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report generated: {args.output}")


if __name__ == "__main__":
    main()
