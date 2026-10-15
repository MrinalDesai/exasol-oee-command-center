"""OEE Command Center command line.

    python -m oee.cli generate      # synthetic data -> data/generated/*.csv
    python -m oee.cli setup         # schema + load + OEE/MTBF/MTTR + feature views in Exasol
    python -m oee.cli train         # train on OEE.V_TRAINING (features computed in Exasol)
    python -m oee.cli deploy        # compile model -> PYTHON3 UDF, score all readings in-database
    python -m oee.cli status        # plant OEE, riskiest machines, spare-parts exposure
    python -m oee.cli all           # everything above, in order
    python -m oee.cli serve         # web console at http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "sql"
DATA = ROOT / "data" / "generated"
MODELS = ROOT / "models"
BUILD = ROOT / "build"


def cmd_generate(args):
    from oee.generate import generate

    DATA.mkdir(parents=True, exist_ok=True)
    for name, df in generate(days=args.days, seed=args.seed).items():
        df.to_csv(DATA / f"{name.lower()}.csv", index=False)
        print(f"  {name:18s} {len(df):>8,d} rows")


def cmd_setup(args):
    from oee.db import connect
    from oee.load import load_all
    from oee.sqlrun import run_file

    with connect() as conn:
        run_file(conn, SQL / "01_schema.sql")
        load_all(conn, DATA)
        run_file(conn, SQL / "02_oee_views.sql")
        run_file(conn, SQL / "03_features.sql")


def cmd_train(args):
    from oee.train import load_training_csv, load_training_exasol, train

    df = load_training_csv(DATA) if args.offline else load_training_exasol()
    model, metrics = train(df)
    MODELS.mkdir(exist_ok=True)
    model.get_booster().save_model(MODELS / "failure_risk.json")
    (MODELS / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({k: v for k, v in metrics.items() if k != "features"}, indent=2))


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
            "INSERT INTO OEE.MODEL_REGISTRY VALUES ({name}, CURRENT_TIMESTAMP, {n}, {auc}, {pak}, {feats})",
            {
                "name": "FAILURE_RISK",
                "n": metrics["n_trees"],
                "auc": metrics["test_auc"],
                "pak": metrics["test_precision_at_top5pct"],
                "feats": ",".join(FEATURES),
            },
        )
        run_file(conn, SQL / "04_scoring.sql", params={"FEATURE_COLUMNS": ", ".join(FEATURES)})


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
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("all")
    a.add_argument("--days", type=int, default=60)
    a.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.cmd == "all":
        args.offline = False
        for step in (cmd_generate, cmd_setup, cmd_train, cmd_deploy, cmd_status):
            print(f"\n### {step.__name__.removeprefix('cmd_')}")
            step(args)
    else:
        {"generate": cmd_generate, "setup": cmd_setup, "train": cmd_train,
         "deploy": cmd_deploy, "status": cmd_status, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    main()
