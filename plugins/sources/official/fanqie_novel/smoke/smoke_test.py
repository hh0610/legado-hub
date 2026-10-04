"""番茄小说插件 smoke 测试 runner。

读 smoke.yaml 配置 + fixtures/dz1_pairs_*.jsonl 样本，验证 chapter() 解密链路：
  _gunzip_content(gzip_b64) -> _html_to_text(html) -> (title, text)

样本覆盖 s1/s2 两个会话密钥（共 126 条）。

用法（在 smoke/ 目录下）：
  D:\\github\\legado-hub\\.venv\\Scripts\\python.exe smoke_test.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# 让脚本可以从 smoke/ 目录直接跑：把插件根目录加入 sys.path
SMOKE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SMOKE_DIR.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import yaml  # noqa: E402
from source import Source  # noqa: E402


class CtxStub:
    """最小 ctx stub：trace 收集到 self.traces 便于失败时打印。"""

    def __init__(self) -> None:
        self.traces: list[tuple[str, dict]] = []

    def trace(self, tag: str, **kw) -> None:
        self.traces.append((tag, kw))


def load_config() -> dict:
    cfg_path = SMOKE_DIR / "smoke.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_samples(files: list[str]) -> list[dict]:
    samples: list[dict] = []
    for rel in files:
        path = SMOKE_DIR / rel
        if not path.exists():
            print(f"[WARN] sample file missing: {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    obj["__source"] = path.name
                    obj["__line"] = line_no
                    samples.append(obj)
                except json.JSONDecodeError as exc:
                    print(f"[WARN] {path.name}:{line_no} json parse: {exc}")
    return samples


def run_one(src: Source, ctx: CtxStub, sample: dict) -> tuple[bool, str, dict]:
    ctx.traces.clear()
    metrics: dict = {}

    gzip_b64 = sample.get("gzip_b64")
    if not gzip_b64:
        return False, "empty gzip_b64 field", metrics

    metrics["gzip_b64_len"] = len(gzip_b64)

    html = src._gunzip_content(ctx, gzip_b64)
    if not html:
        return False, "gunzip returned empty (see ctx traces)", metrics
    metrics["html_len"] = len(html)

    title, text = src._html_to_text(ctx, html)
    metrics["title"] = title
    metrics["text_len"] = len(text)
    if not title:
        return False, "empty title after _html_to_text", metrics
    if not text:
        return False, "empty text after _html_to_text", metrics

    para_count = text.count("\n\n") + 1
    metrics["para_count"] = para_count

    return True, "", metrics


def main() -> int:
    cfg = load_config()
    print(f"[INFO] loaded smoke.yaml: keyword={cfg.get('keyword')} "
          f"requires_oracle={cfg.get('requires_oracle')}")

    ch_fixture = (cfg.get("fixtures") or {}).get("chapter_decrypt") or {}
    files = ch_fixture.get("files") or []
    expect = (cfg.get("expect") or {}).get("chapter_decrypt") or {}

    if not files:
        print("[FAIL] no chapter_decrypt fixtures listed in smoke.yaml")
        return 2

    samples = load_samples(files)
    if not samples:
        print("[FAIL] no samples loaded")
        return 2

    print(f"[INFO] loaded {len(samples)} samples from {len(files)} files")
    print("=" * 70)

    src = Source()
    ctx = CtxStub()

    ok_count = 0
    fail_count = 0
    failures: list[dict] = []
    html_lens: list[int] = []
    text_lens: list[int] = []
    para_counts: list[int] = []
    titles_sampled: list[str] = []

    for idx, sample in enumerate(samples):
        ok, err, metrics = run_one(src, ctx, sample)
        if ok:
            ok_count += 1
            html_lens.append(metrics.get("html_len", 0))
            text_lens.append(metrics.get("text_len", 0))
            para_counts.append(metrics.get("para_count", 0))
            if len(titles_sampled) < 5:
                titles_sampled.append(metrics.get("title", ""))
        else:
            fail_count += 1
            failures.append({
                "idx": idx, "source": sample.get("__source"),
                "line": sample.get("__line"), "err": err, "metrics": metrics,
                "traces": list(ctx.traces),
                "gzip_b64_prefix": (sample.get("gzip_b64") or "")[:60],
            })

    total = ok_count + fail_count
    success_rate = (ok_count / total * 100) if total else 0

    print(f"TOTAL: {total}")
    print(f"OK:    {ok_count}  ({success_rate:.1f}%)")
    print(f"FAIL:  {fail_count}")
    print()

    if html_lens:
        print(f"html_len  min={min(html_lens)} max={max(html_lens)} "
              f"avg={sum(html_lens) // len(html_lens)}")
    if text_lens:
        print(f"text_len  min={min(text_lens)} max={max(text_lens)} "
              f"avg={sum(text_lens) // len(text_lens)}")
    if para_counts:
        print(f"para_cnt  min={min(para_counts)} max={max(para_counts)} "
              f"avg={sum(para_counts) // len(para_counts)}")
    print()
    print("Sampled titles (first 5):")
    for t in titles_sampled:
        print(f"  - {t}")

    if failures:
        print()
        print("=" * 70)
        print(f"FAILURES ({len(failures)}):")
        for f in failures[:10]:
            print(f"  [#{f['idx']} {f['source']}:{f['line']}] {f['err']}")
            print(f"    metrics={f['metrics']}")
            if f["traces"]:
                for tag, kw in f["traces"][:3]:
                    print(f"    trace:{tag} {kw}")
            print(f"    gzip_b64[:60]={f['gzip_b64_prefix']}...")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more")

    # 期望断言
    print()
    print("=" * 70)
    print("ASSERTIONS:")
    min_pass_rate = expect.get("minPassRate", 1.0)
    min_text_len = expect.get("minTextLength", 0)
    min_para = expect.get("minParagraphCount", 0)
    title_required = expect.get("titleRequired", False)

    actual_pass_rate = ok_count / total if total else 0
    pass_rate_ok = actual_pass_rate >= min_pass_rate
    print(f"  passRate {actual_pass_rate:.3f} >= {min_pass_rate}: {pass_rate_ok}")

    text_len_ok = all(t >= min_text_len for t in text_lens) if text_lens else False
    print(f"  textLen all >= {min_text_len}: {text_len_ok}")

    para_ok = all(p >= min_para for p in para_counts) if para_counts else False
    print(f"  paraCount all >= {min_para}: {para_ok}")

    title_ok = all(bool(t) for t in titles_sampled) if title_required and titles_sampled else (not title_required)
    if title_required:
        # 检查所有样本的 title，不只前 5 个 — 重新扫一遍
        src2 = Source()
        ctx2 = CtxStub()
        title_ok = True
        for s in samples:
            ok, _, m = run_one(src2, ctx2, s)
            if ok and not m.get("title"):
                title_ok = False
                break
    print(f"  titleRequired={title_required}: {title_ok}")

    all_ok = pass_rate_ok and text_len_ok and para_ok and title_ok
    print()
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok and fail_count == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
