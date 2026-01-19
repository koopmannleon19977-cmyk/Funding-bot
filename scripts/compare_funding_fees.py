import csv
from datetime import datetime
from pathlib import Path


def normalize_market(m: str) -> str:
    # Convert e.g. "WLFI-USD" -> "WLFI"
    return m.replace("-USD", "").strip().upper()


def parse_iso_timestamp(ts: str) -> datetime:
    # Our CSV uses ISO with Z and optional milliseconds
    # Example: 2025-12-14T15:00:00.692Z
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1]
    # Drop milliseconds if present
    if "." in ts:
        ts = ts.split(".")[0]
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")


def parse_exchange_timestamp(ts: str) -> datetime:
    # Exchange CSV example: 2025-12-14 15:00:00
    return datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S")


def read_our_csv(path: Path) -> dict[tuple[str, datetime], dict]:
    result: dict[tuple[str, datetime], dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                market = normalize_market(row["market"])  # e.g., WLFI-USD -> WLFI
                side = row.get("side", "").strip().lower()
                pos_size = float(row["position_size"]) if row.get("position_size") else None
                value = float(row["value"]) if row.get("value") else None
                mark_price = float(row["mark_price"]) if row.get("mark_price") else None
                funding_payment = float(row["funding_payment"]) if row.get("funding_payment") else None
                rate = float(row["rate"]) if row.get("rate") else None
                t = parse_iso_timestamp(row["time"]) if row.get("time") else None
            except Exception:
                continue
            key = (market, t)
            result[key] = {
                "market": market,
                "side": side,
                "position_size": pos_size,
                "value": value,
                "mark_price": mark_price,
                "funding_payment": funding_payment,
                "rate": rate,
                "time": t,
            }
    return result


def read_exchange_csv(path: Path) -> dict[tuple[str, datetime], dict]:
    result: dict[tuple[str, datetime], dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                market = row["Market"].strip().upper()
                side = row.get("Side", "").strip().lower()
                pos_size = float(row["Position Size"]) if row.get("Position Size") else None
                payment = float(row["Payment"]) if row.get("Payment") else None
                rate_str = row.get("Rate", "").strip()
                # Rate appears with a '%' suffix, but observed payment matches when treating it as a raw fraction (not divided by 100).
                # We'll store the raw numeric part for reference and rely on "Payment" directly for comparison.
                rate = None
                if rate_str:
                    if rate_str.endswith("%"):
                        rate_str = rate_str[:-1]
                    try:
                        rate = float(rate_str)
                    except Exception:
                        rate = None
                t = parse_exchange_timestamp(row["Date"]) if row.get("Date") else None
            except Exception:
                continue
            key = (market, t)
            result[key] = {
                "market": market,
                "side": side,
                "position_size": pos_size,
                "payment": payment,
                "rate": rate,
                "time": t,
            }
    return result


def compare(ours: dict[tuple[str, datetime], dict], ex: dict[tuple[str, datetime], dict]) -> list[dict]:
    comparisons: list[dict] = []
    keys = set(ours.keys()) & set(ex.keys())
    for key in sorted(keys, key=lambda k: (k[1], k[0])):
        o = ours[key]
        e = ex[key]
        our_pay = o.get("funding_payment")
        ex_pay = e.get("payment")
        side_mismatch = (o.get("side") or "") != (e.get("side") or "")
        # Compute theoretical payment from our value and rate when available
        theo = None
        if o.get("value") is not None and o.get("rate") is not None:
            theo = o["value"] * o["rate"]
        diff = None
        ratio = None
        if our_pay is not None and ex_pay is not None:
            diff = our_pay - (-ex_pay)  # Our sign as LONG receive vs exchange SHORT pay convention
            # ratio of magnitudes
            denom = abs(ex_pay) if abs(ex_pay) > 0 else None
            ratio = (abs(our_pay) / denom) if denom else None
        comparisons.append(
            {
                "market": key[0],
                "time": key[1].isoformat(sep=" "),
                "our_side": o.get("side"),
                "ex_side": e.get("side"),
                "side_mismatch": side_mismatch,
                "our_payment": our_pay,
                "ex_payment": ex_pay,
                "our_rate": o.get("rate"),
                "ex_rate": e.get("rate"),
                "our_value": o.get("value"),
                "theoretical_our_payment": theo,
                "abs_diff_vs_neg_ex": diff,
                "abs_ratio_vs_ex": ratio,
            }
        )
    return comparisons


def main():
    root = Path(__file__).resolve().parents[1]
    our_path = root / "funding_fees.csv"
    ex_path = root / "lighter-funding-export-2025-12-14T15_07_24.842Z-UTC.csv"
    if not our_path.exists() or not ex_path.exists():
        print("Missing CSV files. Expected funding_fees.csv and lighter-funding-export-...csv in project root.")
        return
    ours = read_our_csv(our_path)
    ex = read_exchange_csv(ex_path)
    comps = compare(ours, ex)
    if not comps:
        print("No overlapping records found to compare.")
        return
    # Print a compact report
    print(
        "Market,Time,OurSide,ExSide,SideMismatch,OurPay,ExPay,OurRate,ExRate,OurValue,TheoOurPay,Diff(our-(-ex)),Ratio(|our|/|ex|)"
    )
    for c in comps:
        print(
            f"{c['market']},{c['time']},{c['our_side']},{c['ex_side']},{c['side_mismatch']}"
            f",{c['our_payment']},{c['ex_payment']},{c['our_rate']},{c['ex_rate']},{c['our_value']},{c['theoretical_our_payment']},{c['abs_diff_vs_neg_ex']},{c['abs_ratio_vs_ex']}"
        )
    # Highlight large mismatches
    bad = [c for c in comps if c.get("abs_ratio_vs_ex") and (c["abs_ratio_vs_ex"] < 0.5 or c["abs_ratio_vs_ex"] > 1.5)]
    if bad:
        print("\nPotential mismatches (ratio outside [0.5, 1.5]):")
        for c in bad:
            print(f" - {c['market']} {c['time']}: ratio={c['abs_ratio_vs_ex']}, diff={c['abs_diff_vs_neg_ex']}")
    else:
        print("\nAll compared payments are within expected tolerance.")


if __name__ == "__main__":
    main()
