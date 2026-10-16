"""OEE Command Center command line.

    python -m oee.cli generate      # synthetic data -> data/generated/*.csv
    python -m oee.cli setup         # schema + load + OEE/MTBF/MTTR + feature views in Exasol
    python -m oee.cli train         # train on OEE.V_TRAINING (features computed in Exasol)
    python -m oee.cli deploy        # compile model -> PYTHON3 UDF, score all readings in-database
    python -m oee.cli status        # plant OEE, riskiest machines, spare-parts exposure
    python -m oee.cli all           # everything above, in order
    python -m oee.cli serve         # web console at http://127.0.0.1:8000
    python -m oee.cli bench         # sensor-fusion benchmark: ~14M one-minute readings (~1 GB)
"""
from __future__ import annotations

import argparse
import json
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "sql"
DATA = ROOT / "data" / "generated"
MODELS = ROOT / "models"
BUILD = ROOT / "build"


def cmd_generate(args):
    from oee.generate import generate_plant, write_tables

    tables = generate_plant(days=args.days, seed=args.seed)
    write_tables(tables, DATA)
    for name, df in tables.items():
        if name != "TELEMETRY_TRUTH":
            print(f"  {name:18s} {len(df):>10,d} rows")


def cmd_setup(args):
    from oee.db import connect
    from oee.load import load_all
    from oee.sqlrun import run_file

    with connect() as conn:
        run_file(conn, SQL / "01_schema.sql")
        load_all(conn, DATA)
        t0 = time.perf_counter()
        run_file(conn, SQL / "02_fusion.sql")
        print(f"  combined and completed the five sensor sources in {time.perf_counter() - t0:.1f} s")
        run_file(conn, SQL / "03_oee_views.sql")
        run_file(conn, SQL / "03_stores.sql")
        print("  read stock from four store systems (SAP x2, Tally, WMS) into SPARE_PARTS")
        run_file(conn, SQL / "04_features.sql")
        run_file(conn, SQL / "04_quality_features.sql")


def cmd_train(args):
    from oee.train import load_training_csv, load_training_exasol, train

    df = load_training_csv(DATA) if args.offline else load_training_exasol()
    model, metrics = train(df)
    MODELS.mkdir(exist_ok=True)
    model.get_booster().save_model(MODELS / "failure_risk.json")
    (MODELS / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({k: v for k, v in metrics.items() if k != "features"}, indent=2))

    # Step 4: next-shift scrap model
    from oee.quality import train_quality

    if args.offline:
        print("  (quality model needs Exasol's V_QUALITY_TRAINING; skipped offline)")
        return
    from oee.db import connect

    with connect() as conn:
        qdf = conn.export_to_pandas("SELECT * FROM OEE.V_QUALITY_TRAINING")
    qmodel, qmetrics = train_quality(qdf)
    qmodel.get_booster().save_model(MODELS / "quality_risk.json")
    (MODELS / "quality_metrics.json").write_text(json.dumps(qmetrics, indent=2))
    print("  quality model:", json.dumps({k: qmetrics[k] for k in ("test_auc", "test_positive_rate", "split_cutoff")}))


def cmd_deploy(args):
    import xgboost as xgb

    from oee.compile_udf import booster_to_spec, render_udf
    from oee.db import connect
    from oee.features import FEATURES
    from oee.sqlrun import run_file, split_statements

    booster = xgb.Booster()
    booster.load_model(MODELS / "failure_risk.json")
    sql = render_udf(booster_to_spec(booster))
    BUILD.mkdir(exist_ok=True)
    (BUILD / "udf_failure_risk.sql").write_text(sql)
    metrics = json.loads((MODELS / "metrics.json").read_text())

    with connect() as conn:
        print("  creating UDF OEE.FAILURE_RISK")
        conn.execute(split_statements(sql)[0])
        try:
            conn.execute("SELECT OEE.FAILURE_RISK(" + ", ".join(["0"] * len(FEATURES)) + ")")
        except Exception as exc:  # most common cause on a fresh local install
            sys.exit(
                f"UDF smoke test failed: {exc}\n"
                "On a local Exasol Personal deployment, install the Python container first:\n"
                "    exasol slc install python3"
            )
        conn.execute(
            "INSERT INTO OEE.MODEL_REGISTRY VALUES ({name}, CURRENT_TIMESTAMP, {n}, {auc}, {pak}, {feats}, {cut})",
            {
                "name": "FAILURE_RISK",
                "n": metrics["n_trees"],
                "auc": metrics["test_auc"],
                "pak": metrics["test_precision_at_top5pct"],
                "feats": ",".join(FEATURES),
                "cut": str(metrics["split_cutoff"])[:19],
            },
        )
        # Step 4: the next-shift scrap model, compiled the same way
        from oee.quality import QUALITY_FEATURES, QUALITY_UDF

        qbooster = xgb.Booster()
        qbooster.load_model(MODELS / "quality_risk.json")
        qsql = render_udf(booster_to_spec(qbooster, features=QUALITY_FEATURES), name=QUALITY_UDF)
        (BUILD / "udf_quality_risk.sql").write_text(qsql)
        qmetrics = json.loads((MODELS / "quality_metrics.json").read_text())
        print("  creating UDF OEE.QUALITY_RISK")
        conn.execute(split_statements(qsql)[0])
        conn.execute(
            "INSERT INTO OEE.MODEL_REGISTRY VALUES ({name}, CURRENT_TIMESTAMP, {n}, {auc}, {pak}, {feats}, {cut})",
            {
                "name": "QUALITY_RISK",
                "n": qmetrics["n_trees"],
                "auc": qmetrics["test_auc"],
                "pak": qmetrics["test_precision_at_top5pct"],
                "feats": ",".join(QUALITY_FEATURES),
                "cut": str(qmetrics["split_cutoff"])[:19],
            },
        )
        from oee.pipeline import deploy_steps

        for name, text in deploy_steps():
            t0 = time.perf_counter()
            for s in split_statements(text):
                conn.execute(s)
            print(f"  {name:32s} {time.perf_counter() - t0:6.1f} s")


def cmd_status(args):
    from oee.db import connect

    queries = {
        "Plant OEE": "SELECT PLANT_ID, ROUND(AVAILABILITY,3) A, ROUND(PERFORMANCE,3) P, "
                     "ROUND(QUALITY,3) Q, ROUND(OEE,3) OEE FROM OEE.V_OEE_PLANT ORDER BY OEE",
        "Highest 24h failure risk": "SELECT MACHINE_ID, MACHINE_TYPE, ROUND(RISK_24H,3) RISK, "
                     "ROUND(MTBF_HOURS,1) MTBF_H, ROUND(MTTR_HOURS,1) MTTR_H "
                     "FROM OEE.V_RISK_LATEST ORDER BY RISK_24H DESC LIMIT 8",
        "Spare parts at risk": "SELECT PLANT_ID, PART_NO, ON_HAND, ROUND(EXPECTED_DEMAND_24H,2) DEMAND, "
                     "LEAD_TIME_DAYS, STATUS FROM OEE.V_PARTS_EXPOSURE WHERE STATUS <> 'OK' "
                     "ORDER BY EXPECTED_DEMAND_24H DESC",
    }
    with connect() as conn:
        for title, q in queries.items():
            print(f"\n== {title}")
            print(conn.export_to_pandas(q).to_string(index=False))


def cmd_bench(args):
    from oee.db import connect
    from oee.scale import run_bench

    model = MODELS / "failure_risk.json"
    if not model.exists():
        sys.exit("No trained model yet. Run `python -m oee.cli all` first.")
    with connect() as conn:
        run_bench(conn, ROOT / "data" / "scale", machines=args.machines, days=args.days,
                  cadence_min=args.cadence, model_path=model, regenerate=not args.reuse)


def cmd_preflight(args):
    from oee.db import connect
    from oee.preflight import run_preflight

    with connect() as conn:
        if not run_preflight(conn):
            sys.exit(1)


def cmd_pillars(args):
    from oee.db import connect
    from oee.pillars import run_pillars

    sessions = tuple(int(x) for x in args.sessions.split(","))
    with connect() as conn:
        run_pillars(conn, connect, sessions=sessions, seconds=args.seconds)


def cmd_serve(args):
    from oee.app import serve

    print(f"OEE Command Center on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    serve(host=args.host, port=args.port)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--days", type=int, default=60)
    g.add_argument("--seed", type=int, default=42)
    sub.add_parser("setup")
    t = sub.add_parser("train")
    t.add_argument("--offline", action="store_true", help="train from CSVs instead of Exasol")
    sub.add_parser("deploy")
    sub.add_parser("status")
    b = sub.add_parser("bench", help="sensor-fusion benchmark at fleet scale")
    b.add_argument("--machines", type=int, default=100)
    b.add_argument("--days", type=int, default=100)
    b.add_argument("--cadence", type=int, default=1, help="minutes between readings")
    b.add_argument("--reuse", action="store_true", help="reuse the CSVs from the last run")
    sub.add_parser("preflight", help="check this Exasol supports every SQL feature used")
    pl = sub.add_parser("pillars", help="measure compression, self-tuning and concurrency")
    pl.add_argument("--sessions", default="1,4,8,16")
    pl.add_argument("--seconds", type=float, default=8.0)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("all")
    a.add_argument("--days", type=int, default=60)
    a.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.cmd == "all":
        args.offline = False
        for step in (cmd_preflight, cmd_generate, cmd_setup, cmd_train, cmd_deploy, cmd_status):
            print(f"\n### {step.__name__.removeprefix('cmd_')}")
            step(args)
    else:
        {"generate": cmd_generate, "setup": cmd_setup, "train": cmd_train,
         "deploy": cmd_deploy, "status": cmd_status, "serve": cmd_serve, "bench": cmd_bench,
         "preflight": cmd_preflight, "pillars": cmd_pillars}[args.cmd](args)


if __name__ == "__main__":
    main()
