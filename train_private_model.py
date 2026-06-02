import os
import json
import sqlite3
import argparse
import joblib

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

DB_PATH = os.getenv("ITTE_DB_PATH", "itte.db")

def load_training_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = []

    cur.execute("""
    SELECT text, label FROM memory_items
    WHERE label IN ('allow', 'review', 'block')
    """)
    for r in cur.fetchall():
        rows.append((r["text"], r["label"]))

    cur.execute("""
    SELECT
        changes.environment,
        changes.change_type,
        changes.title,
        changes.diff,
        changes.metadata_json,
        senior_judgments.label
    FROM senior_judgments
    JOIN changes ON changes.id = senior_judgments.change_id
    WHERE senior_judgments.label IN ('allow', 'review', 'block')
    """)
    for r in cur.fetchall():
        text = f"{r['environment']} {r['change_type']} {r['title']}\n{r['diff']}\n{r['metadata_json']}"
        rows.append((text, r["label"]))

    cur.execute("""
    SELECT
        changes.environment,
        changes.change_type,
        changes.title,
        changes.diff,
        changes.metadata_json,
        outcomes.incident,
        outcomes.severity
    FROM outcomes
    JOIN changes ON changes.id = outcomes.change_id
    """)
    for r in cur.fetchall():
        text = f"{r['environment']} {r['change_type']} {r['title']}\n{r['diff']}\n{r['metadata_json']}"
        if r["incident"] and r["severity"] in ["high", "critical"]:
            label = "block"
        elif r["incident"]:
            label = "review"
        else:
            label = "allow"
        rows.append((text, label))

    conn.close()
    return rows

def main():
    parser = argparse.ArgumentParser(description="Train ITTE private distilled risk model")
    parser.add_argument("--out", default="models/risk_model.joblib")
    args = parser.parse_args()

    rows = load_training_data()

    if len(rows) < 8:
        raise SystemExit("Need at least 8 labeled memory/judgment/outcome rows.")

    X = [x for x, y in rows]
    y = [y for x, y in rows]

    if len(set(y)) < 2:
        raise SystemExit("Need at least 2 classes among allow/review/block.")

    vectorizer = TfidfVectorizer(
        max_features=20000,
        ngram_range=(1, 2),
        min_df=1
    )

    Xv = vectorizer.fit_transform(X)

    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    clf.fit(Xv, y)

    if len(rows) >= 20 and len(set(y)) >= 2:
        X_train, X_test, y_train, y_test = train_test_split(
            Xv, y, test_size=0.25, random_state=42, stratify=y
        )
        clf_eval = LogisticRegression(max_iter=1000, class_weight="balanced")
        clf_eval.fit(X_train, y_train)
        pred = clf_eval.predict(X_test)
        print(classification_report(y_test, pred))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    joblib.dump({
        "vectorizer": vectorizer,
        "classifier": clf
    }, args.out)

    print(f"Saved private model to {args.out}")
    print(f"Training rows: {len(rows)}")
    print(f"Classes: {sorted(set(y))}")

if __name__ == "__main__":
    main()
