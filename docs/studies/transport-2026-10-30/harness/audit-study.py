"""Independent consistency checks for the completed Starship study."""
import collections, hashlib, json, math, pathlib
BASE = pathlib.Path(__file__).parent
RAW = BASE / "resumed-starship"
report = json.loads((BASE / "starship-analysis.json").read_text())
checks = []
issues = []
for trial in report["trials"]:
    name = trial["name"]
    if not trial["ok"]:
        issues.append({"name": name, "error": "failed trial"})
        continue
    raw = json.loads((RAW / (name + ".result.json")).read_text())
    images = json.loads((RAW / (name + ".image-metrics.json")).read_text())
    rows = [json.loads(s) for s in (RAW / (name + ".transport-metrics.jsonl")).read_text().splitlines() if s.strip()]
    assert raw["visibility"] == {"state": "visible", "hidden": False, "focus": True}, name
    assert raw["window"]["is_focused"] and raw["window"]["layout"]["window_size"] == [1200, 850], name
    assert raw["geometry"]["viewport"] == [1200, 760] and raw["geometry"]["dpr"] == 1, name
    assert trial["output"]["mode"] == {"width": 5120, "height": 1440, "refresh_rate": 144000, "is_preferred": False}, name
    flow = None
    last_received = -1
    for row in rows:
        assert len(row["transport_sockets"]) == 1, name
        sock = row["transport_sockets"][0]
        identity = (sock["local"], sock["peer"])
        if flow is None:
            flow = identity
        assert identity == flow and sock["bytes_received"] >= last_received, name
        last_received = sock["bytes_received"]
    assert all(b["monotonic"] > a["monotonic"] for a, b in zip(rows, rows[1:])), name
    assert trial["timing"]["transport_seconds"] >= 48 and trial["timing"]["image_seconds"] >= 60, name
    assert all(v > 0 for v in trial["matched_cpu_processes"].values()), name
    assert len(images["keys"]) == len({k["seq"] for k in images["keys"]}), name
    for key in images["keys"]:
        assert key["wtype_code"] == 0, name
        if key["status"] == "observed":
            lo, hi = key["latency_lower_ms"], key["latency_upper_ms"]
            assert math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi, name
    rejected_fraction = len(images["rejected_captures"]) / max(1, len(images["samples"]) + len(images["rejected_captures"]))
    assert images["quality_passed"] == (rejected_fraction < .05), name
    assert trial["input_marker_quality_passed"] == images["quality_passed"], name
    assert trial["inputs"]["all_source_events_trusted"], name
    assert trial["metrics"]["chrome_gpu_process_crashes"] == 0, name
    if trial["mode"] == "h264":
        assert trial["backend"]["source"]["encoder_hw"] > 0 and trial["backend"]["viewer"]["decoder_hw"] > 0, name
        assert trial["backend"]["source"].get("encoder_sw", 0) == 0 and trial["backend"]["viewer"].get("decoder_sw", 0) == 0, name
    checks.append({"name": name, "ok": True, "flow_stable": True, "focus_geometry_verified": True,
                   "input_attempts": len(images["keys"]), "source_f8_events": trial["inputs"]["source_f8_events"],
                   "source_event_gap": len(images["keys"]) - trial["inputs"]["source_f8_events"],
                   "invalid_frame_captures": len(images.get("invalid_frame_captures", [])),
                   "rejected_input_captures": len(images["rejected_captures"]), "qualified_latency": images["quality_passed"]})
expected = collections.Counter((c, m) for c in ["native", "proxy", "delay", "jitter", "loss", "cap"] for m in ["h264", "lossless"] for _ in range(3))
actual = collections.Counter((t["condition"], t["mode"]) for t in report["trials"] if t["ok"])
complete = expected == actual and not issues and len(checks) == 36
out = {"schema": 1, "matrix_complete": complete, "checked_trials": len(checks), "issues": issues,
       "checks": checks, "note": "This audit checks evidence consistency. It does not turn sampled latency into physical input-to-photon timing or establish subjective acceptance."}
(BASE / "starship-audit.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps({k: out[k] for k in ["matrix_complete", "checked_trials", "issues"]}))
